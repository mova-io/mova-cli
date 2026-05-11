"""Linear single-agent executor.

Pipeline (v0.1):

    validate input
        → render prompt
        → invoke provider (with retries and fallback chain)
        → validate output
        → record metrics + persist

Workflow orchestration is Phase 3 (`movate.core.workflow`).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from jsonschema import ValidationError as JsonSchemaError

from movate.core.config import ModelPolicy
from movate.core.failures import (
    DEFAULT_RETRY,
    BudgetExceededError,
    MovateError,
    PolicyViolationError,
    SchemaError,
    TenantBudgetExceededError,
)
from movate.core.loader import AgentBundle
from movate.core.models import (
    ErrorInfo,
    FailureRecord,
    JobStatus,
    Metrics,
    ModelConfig,
    ModelFallback,
    RunRecord,
    RunRequest,
    RunResponse,
    TokenUsage,
)
from movate.core.retry import RetryExhaustedError, run_with_retries
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from movate.providers.pricing import PricingTable
from movate.storage.base import StorageProvider
from movate.tracing.base import SpanCtx, Tracer

log = logging.getLogger(__name__)

_COST_DRIFT_THRESHOLD = 0.05  # 5%


class Executor:
    def __init__(
        self,
        *,
        provider: BaseLLMProvider,
        pricing: PricingTable,
        storage: StorageProvider,
        tracer: Tracer,
        tenant_id: str = "local",
        policy: ModelPolicy | None = None,
    ) -> None:
        self._provider = provider
        self._pricing = pricing
        self._storage = storage
        self._tracer = tracer
        self._tenant_id = tenant_id
        # Permissive default — an executor built without a policy enforces
        # nothing, preserving v0.1-style behavior for callers that haven't
        # opted in yet (tests, downstream embedders).
        self._policy = policy or ModelPolicy()

    async def execute(
        self,
        bundle: AgentBundle,
        request: RunRequest,
        *,
        job_id: str | None = None,
        model_override: ModelConfig | None = None,
        workflow_run_id: str | None = None,
        node_id: str | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> RunResponse:
        """Execute one agent against one input.

        ``model_override`` swaps provider/params for a single run (used by
        ``movate bench`` in v0.2). Override disables the configured fallback
        chain so each comparison row tests exactly one model.

        ``workflow_run_id`` + ``node_id`` are stamped onto the persisted
        :class:`RunRecord` when the executor is invoked from a
        :class:`movate.core.workflow.WorkflowRunner` — keeps the runner from
        having to re-save the same run with a workflow link patched on.

        ``on_token`` opts into streaming. When set, the executor calls
        ``provider.stream()`` and invokes the callback with each text
        delta as it arrives — useful for ``movate run --stream`` to
        render tokens live in the terminal. The accumulated text is
        still schema-validated, persisted, and returned the same way
        as a non-streaming run; ``on_token`` only adds an observation
        callback. Streaming inherits retries + fallback identically
        to one-shot calls (a stream that exhausts retries falls
        through to the next provider in the chain)."""
        job_id = job_id or str(uuid4())
        run_id = str(uuid4())
        spec = bundle.spec
        effective_model = model_override or spec.model

        span = self._tracer.start_span(
            "agent.execute",
            {
                "agent": spec.name,
                "agent_version": spec.version,
                "provider": effective_model.provider,
                "tenant_id": self._tenant_id,
                "job_id": job_id,
                "run_id": run_id,
                "model_override": model_override is not None,
            },
        )

        started = time.monotonic()
        try:
            # Tenant-budget check FIRST — if the tenant's monthly cap
            # is breached, no run should fire (not even a doomed one).
            # Cheap PK lookup + a single SUM aggregate; the index on
            # (tenant_id, created_at) is the perf path.
            await self._check_tenant_budget()

            # Policy check happens BEFORE schema validation and prompt
            # rendering — a denied model shouldn't get to bill latency
            # or trigger any side effects. ``check_model`` is also
            # cheaper than schema validation so a misconfigured agent
            # fails fast.
            #
            # We check the effective model + every fallback the executor
            # might try. ``bench`` uses ``model_override`` which disables
            # the fallback chain, so we only check the override in that
            # case (mirrors the chain construction below).
            if not self._policy.is_permissive():
                self._enforce_policy(spec, effective_model, model_override is not None)

            try:
                bundle.input_validator.validate(request.input)
            except JsonSchemaError as exc:
                raise SchemaError(f"input failed schema: {exc.message}") from exc

            rendered = bundle.render_prompt(request.input)
            self._tracer.log_event(span, {"prompt_hash": bundle.prompt_hash})

            chain: list[tuple[str, dict[str, Any]]] = [
                (effective_model.provider, dict(effective_model.params))
            ]
            if model_override is None:
                for fb in spec.model.fallback:
                    merged = dict(spec.model.params)
                    merged.update(fb.params)
                    chain.append((fb.provider, merged))

            completion: CompletionResponse | None = None
            chosen_provider = ""
            last_error: MovateError | None = None

            for provider_str, params in chain:
                req = CompletionRequest(
                    provider=provider_str,
                    messages=[Message(role="user", content=rendered)],
                    params=params,
                )

                async def _invoke(req: CompletionRequest = req) -> CompletionResponse:
                    if on_token is None:
                        return await self._provider.complete(req)
                    # Stream path. Accumulate chunks into a single
                    # CompletionResponse so everything below this
                    # (schema validation, cost calc, persistence) sees
                    # the same shape regardless of streaming.
                    return await self._invoke_streaming(req, on_token)

                try:
                    completion = await run_with_retries(_invoke)
                    chosen_provider = provider_str
                    break
                except RetryExhaustedError as exc:
                    last_error = exc.last_error
                    rule = _retry_rule_for(exc.last_error)
                    if rule and rule.fallback_on_exhaust:
                        self._tracer.log_event(
                            span,
                            {
                                "fallback_triggered": True,
                                "from": provider_str,
                                "reason": exc.last_error.failure_type.value,
                            },
                        )
                        continue
                    raise

            if completion is None:
                assert last_error is not None
                raise last_error

            cost = self._pricing.cost_for(provider=chosen_provider, tokens=completion.tokens)
            self._check_cost_drift(span, completion, cost)

            # The effective ceiling is the MIN of the agent's declared
            # budget and the project policy's ceiling. Project policy
            # never relaxes — it can only tighten. If a project sets no
            # ceiling, the agent's own budget wins.
            effective_ceiling = self._policy.effective_max_cost(spec.budget.max_cost_usd_per_run)
            if cost > effective_ceiling:
                raise BudgetExceededError(
                    f"run cost ${cost:.4f} exceeds ceiling ${effective_ceiling:.4f} "
                    f"(agent budget ${spec.budget.max_cost_usd_per_run:.4f}, "
                    f"policy {self._policy.max_cost_per_run_usd})"
                )

            output = _parse_json_output(completion.text)
            try:
                bundle.output_validator.validate(output)
            except JsonSchemaError as exc:
                raise SchemaError(f"model output failed schema: {exc.message}") from exc

            metrics = Metrics(
                latency_ms=int((time.monotonic() - started) * 1000),
                tokens=completion.tokens,
                cost_usd=cost,
                provider=chosen_provider,
                pricing_version=self._pricing.version,
            )

            response = RunResponse(
                status="success",
                run_id=run_id,
                data=output,
                human_readable=_extract_human_readable(output),
                trace_id=span.trace_id,
                metrics=metrics,
            )

            await self._record_run(
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                request=request,
                response=response,
                chosen_provider=chosen_provider,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            )
            self._tracer.end_span(span, status="ok")
            return response

        except MovateError as exc:
            return await self._handle_failure(
                span=span,
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                started=started,
                err=exc,
            )
        except RetryExhaustedError as exc:
            return await self._handle_failure(
                span=span,
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                started=started,
                err=exc.last_error,
            )

    async def _invoke_streaming(
        self,
        req: CompletionRequest,
        on_token: Callable[[str], None],
    ) -> CompletionResponse:
        """Drive ``provider.stream()`` and accumulate into a single
        :class:`CompletionResponse`.

        Token totals come from the LAST chunk in the stream (providers
        return them via ``stream_options={'include_usage': True}``).
        If a stream ends without ever delivering usage stats — older
        providers, mis-configured proxies — we fall through with
        zeros. Cost accounting then reads zero, which is wrong but
        survivable; the cost-drift check downstream will flag it."""
        text_parts: list[str] = []
        final_tokens: TokenUsage | None = None
        raw: dict[str, Any] = {}
        async for chunk in self._provider.stream(req):
            if chunk.text:
                text_parts.append(chunk.text)
                on_token(chunk.text)
            if chunk.tokens is not None:
                final_tokens = chunk.tokens
            if chunk.raw:
                # Last write wins — adapters that forward provider
                # metadata typically only stamp it on the final chunk.
                raw.update(chunk.raw)
        return CompletionResponse(
            text="".join(text_parts),
            tokens=final_tokens or TokenUsage(),
            raw=raw,
        )

    async def _check_tenant_budget(self) -> None:
        """Abort the run if the tenant has hit its monthly cap.

        Reads :meth:`StorageProvider.get_tenant_budget` (PK lookup —
        cheap). If no row exists for this tenant or
        ``monthly_usd_limit`` is ``None``, returns immediately (the
        default-unlimited case, backwards compatible with every
        pre-budget deployment).

        Race window: under high concurrency two requests can both
        observe "under budget" simultaneously and both succeed,
        pushing combined cost over the cap. The overrun is bounded
        by the in-flight call count — not catastrophic, but
        operators should set the cap slightly below the hard cost
        ceiling they actually want to enforce.
        """
        budget = await self._storage.get_tenant_budget(self._tenant_id)
        if budget is None or budget.monthly_usd_limit is None:
            return
        current = await self._storage.sum_tenant_cost_current_month(self._tenant_id)
        if current >= budget.monthly_usd_limit:
            raise TenantBudgetExceededError(
                f"tenant {self._tenant_id!r} has spent ${current:.2f} of "
                f"${budget.monthly_usd_limit:.2f} this month; runs are paused. "
                f"Operator can raise the budget with "
                f"`movate tenants set-budget {self._tenant_id} --monthly-usd <new>` "
                f"or wait for next-month rollover."
            )

    def _enforce_policy(
        self,
        spec: Any,
        effective_model: ModelConfig,
        is_override: bool,
    ) -> None:
        """Raise ``PolicyViolationError`` if the run would violate policy.

        Called at the top of ``execute()`` before any provider hits.
        Checks the model the executor is about to invoke plus every
        fallback it might try; for ``bench`` (model_override=True) the
        fallback chain is disabled, so we only check the override.
        """
        violations: list[str] = []
        if err := self._policy.check_model(effective_model.provider):
            violations.append(f"primary model: {err}")
        if not is_override:
            for fb in spec.model.fallback:
                if err := self._policy.check_model(fb.provider):
                    violations.append(f"fallback {fb.provider!r}: {err}")
        # Budget ceiling is enforced separately at the cost-check step
        # (we don't know cost yet at executor entry). But if the agent
        # declared a static budget larger than the policy ceiling, the
        # operator should know NOW, not after spending money — so we
        # flag it here too.
        if (
            self._policy.max_cost_per_run_usd is not None
            and spec.budget.max_cost_usd_per_run > self._policy.max_cost_per_run_usd
        ):
            violations.append(
                f"budget.max_cost_usd_per_run={spec.budget.max_cost_usd_per_run} "
                f"exceeds policy ceiling {self._policy.max_cost_per_run_usd}"
            )
        if violations:
            joined = "; ".join(violations)
            raise PolicyViolationError(
                f"agent {spec.name!r} violates model policy: {joined}. See movate.yaml: policy."
            )

    def _check_cost_drift(
        self, span: SpanCtx, completion: CompletionResponse, our_cost: float
    ) -> None:
        litellm_cost = completion.raw.get("litellm_cost_usd")
        if not isinstance(litellm_cost, (int, float)):
            return
        if our_cost <= 0 and litellm_cost <= 0:
            return
        denom = max(abs(our_cost), abs(float(litellm_cost)))
        if denom == 0:
            return
        drift = abs(our_cost - float(litellm_cost)) / denom
        if drift > _COST_DRIFT_THRESHOLD:
            log.warning(
                "cost drift > %.0f%%: pricing-table=$%.6f litellm=$%.6f",
                _COST_DRIFT_THRESHOLD * 100,
                our_cost,
                litellm_cost,
            )
            self._tracer.log_event(
                span,
                {
                    "cost_drift": drift,
                    "cost_pricing_table_usd": our_cost,
                    "cost_litellm_usd": float(litellm_cost),
                },
            )

    async def _record_run(
        self,
        *,
        bundle: AgentBundle,
        run_id: str,
        job_id: str,
        request: RunRequest,
        response: RunResponse,
        chosen_provider: str,
        workflow_run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        record = RunRecord(
            run_id=run_id,
            job_id=job_id,
            tenant_id=self._tenant_id,
            agent=bundle.spec.name,
            agent_version=bundle.spec.version,
            prompt_hash=bundle.prompt_hash,
            provider=chosen_provider,
            provider_version=self._provider.version,
            pricing_version=self._pricing.version,
            status=JobStatus.SUCCESS,
            input=request.input,
            output=response.data,
            metrics=response.metrics,
            workflow_run_id=workflow_run_id,
            node_id=node_id,
        )
        await self._storage.save_run(record)

    async def _handle_failure(
        self,
        *,
        span: SpanCtx,
        bundle: AgentBundle,
        run_id: str,
        job_id: str,
        started: float,
        err: MovateError,
    ) -> RunResponse:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        status = "safety_blocked" if err.failure_type.value == "content_filter" else "error"
        info = ErrorInfo(type=err.failure_type.value, message=str(err), retryable=err.retryable)
        self._tracer.log_event(span, {"error": info.model_dump()})
        self._tracer.end_span(span, status="error")

        await self._storage.save_failure(
            FailureRecord(
                failure_id=str(uuid4()),
                run_id=run_id,
                tenant_id=self._tenant_id,
                agent=bundle.spec.name,
                failure_type=err.failure_type.value,
                message=str(err),
                retryable=err.retryable,
            )
        )
        # job_id reserved for the workflow + server phases.
        _ = job_id

        return RunResponse(
            status=status,  # type: ignore[arg-type]
            run_id=run_id,
            data={},
            human_readable=f"**Error**: {err}",
            trace_id=span.trace_id,
            metrics=Metrics(latency_ms=elapsed_ms, tokens=TokenUsage()),
            error=info,
        )


def _retry_rule_for(err: MovateError) -> Any:
    return DEFAULT_RETRY.get(err.failure_type)


def _parse_json_output(text: str) -> dict[str, Any]:
    """Extract a JSON object from model output, tolerating markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise SchemaError(f"model output must be a JSON object, got {type(result).__name__}")
    return result


def _extract_human_readable(output: dict[str, Any]) -> str:
    for key in ("human_readable", "message", "summary"):
        val = output.get(key)
        if isinstance(val, str):
            return val
    return ""


# Forward-ref bookkeeping
_ = ModelFallback  # keep import for typing in agent specs that reference it
