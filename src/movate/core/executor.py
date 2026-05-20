"""Single-agent executor.

Pipeline:

    validate input
        → render prompt
        → invoke provider (with retries and fallback chain)
        → validate output
        → record metrics + persist

Workflow orchestration lives in ``movate.core.workflow``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from jsonschema import ValidationError as JsonSchemaError

from movate.core.config import GuardrailsConfig, ModelPolicy, RuntimePolicy, SkillPolicy
from movate.core.failures import (
    DEFAULT_RETRY,
    BudgetExceededError,
    ContentFilterError,
    GuardrailViolationError,
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
from movate.core.reflection import build_revision_prompt, call_judge
from movate.core.retry import RetryExhaustedError, run_with_retries
from movate.guardrails import check_input as _guardrails_check_input
from movate.guardrails import check_output as _guardrails_check_output
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from movate.providers.pricing import PricingTable
from movate.providers.registry import ProviderRegistry, UnregisteredRuntimeError
from movate.storage.base import StorageProvider
from movate.tracing.base import SpanCtx, Tracer

log = logging.getLogger(__name__)

_COST_DRIFT_THRESHOLD = 0.05  # 5%

# Hard cap on tool-use turns. If the model keeps emitting tool calls
# instead of producing a final answer, the loop bails after this many
# iterations and surfaces the last response as the result. 10 is the
# ADR 002 default — generous for real agents (web-search + lookup
# typically resolves in 2-3 turns) and tight enough that a runaway
# loop fails loud rather than silently burning budget.
_MAX_TOOL_TURNS_DEFAULT = 10


class Executor:
    def __init__(
        self,
        *,
        provider: BaseLLMProvider | None = None,
        registry: ProviderRegistry | None = None,
        pricing: PricingTable,
        storage: StorageProvider,
        tracer: Tracer,
        tenant_id: str = "local",
        policy: ModelPolicy | None = None,
        runtime_policy: RuntimePolicy | None = None,
        skill_policy: SkillPolicy | None = None,
        guardrails: GuardrailsConfig | None = None,
    ) -> None:
        """One of ``provider`` (legacy single-runtime) OR ``registry``
        (multi-runtime, v0.6+) must be set. Passing ``provider`` is
        equivalent to ``registry=ProviderRegistry(default_litellm=provider)``
        and is preserved so the existing 100+ test sites keep working
        unchanged. New code passes ``registry=`` so it can wire up
        native-SDK adapters alongside LiteLLM."""
        if provider is None and registry is None:
            raise ValueError("Executor needs either provider= or registry=")
        if provider is not None and registry is not None:
            raise ValueError("pass either provider= OR registry=, not both")
        if registry is not None:
            self._registry = registry
        else:
            # mypy: provider is not None here (one of the two must be set).
            assert provider is not None
            self._registry = ProviderRegistry(default_litellm=provider)
        self._pricing = pricing
        self._storage = storage
        self._tracer = tracer
        self._tenant_id = tenant_id
        # Permissive default — an executor built without a policy enforces
        # nothing, preserving v0.1-style behavior for callers that haven't
        # opted in yet (tests, downstream embedders).
        self._policy = policy or ModelPolicy()
        # RuntimePolicy gates which AgentRuntime values are permitted —
        # belt-and-braces against an agent.yaml that skipped `movate
        # validate` (e.g. loaded over HTTP by a worker). Permissive default.
        self._runtime_policy = runtime_policy or RuntimePolicy()
        # SkillPolicy gates which skill side_effects categories are
        # permitted. Same belt-and-braces shape as RuntimePolicy —
        # enforced at the top of execute() so a bundle that bypasses
        # `mdk validate` can't sneak past the gate.
        self._skill_policy = skill_policy or SkillPolicy()
        # Safe-AI guardrails (PII / topic / content) for input + output.
        # Permissive default — every sub-block ``enabled: false`` means
        # the input/output check fast-paths to allow. Wired at execute()
        # entry (input) and exit (output). See GuardrailsConfig for the
        # full schema and ``movate.guardrails`` for the engine.
        self._guardrails = guardrails or GuardrailsConfig()

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
        history: list[Message] | None = None,
        tenant_id_override: str | None = None,
        skill_fixture: dict[str, Any] | None = None,
        thread_id: str | None = None,
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
        through to the next provider in the chain).

        ``history`` is an optional list of prior conversation messages
        (user/assistant pairs from previous turns) — prepended to the
        provider call so multi-turn agents see context. The CURRENT
        request's input still goes through prompt rendering + input
        schema validation the same as a one-shot run; history is purely
        conversational context the model uses for continuity.
        ``movate chat`` is the primary caller; one-shot ``movate run``
        invocations leave it ``None``.

        ``tenant_id_override`` lets the caller pass the tenant this run
        belongs to. When the executor is shared across tenants (e.g. a
        ``movate worker`` draining the multi-tenant job queue), each job
        has its own tenant_id and the persisted ``RunRecord`` + budget
        checks must use *that* id, not the executor's construction-time
        default. Local-CLI callers (``movate run``) pass nothing and
        fall back to ``self._tenant_id`` (``"local"``). Cross-tenant
        ``GET /runs/<id>`` would return 404 if this didn't propagate."""
        job_id = job_id or str(uuid4())
        run_id = str(uuid4())
        spec = bundle.spec
        effective_model = model_override or spec.model
        # Resolve the tenant context for this run. Worker dispatch passes
        # job.tenant_id explicitly; local CLI passes nothing → keeps
        # self._tenant_id (which is "local" by construction for the
        # local-CLI runtime).
        tenant_id = tenant_id_override or self._tenant_id

        span = self._tracer.start_span(
            "agent.execute",
            {
                "agent": spec.name,
                "agent_version": spec.version,
                "provider": effective_model.provider,
                "tenant_id": tenant_id,
                "job_id": job_id,
                "run_id": run_id,
                "model_override": model_override is not None,
            },
        )

        started = time.monotonic()
        try:
            # Runtime POLICY check first — if the project bans this
            # runtime (e.g. movate.yaml: runtime.allowed: [litellm]),
            # surface as PolicyViolationError so the failure trail
            # matches model-policy violations.
            runtime_violation = self._runtime_policy.check_agent(spec)
            if runtime_violation is not None:
                raise PolicyViolationError(runtime_violation)

            # Skill POLICY check — if the project restricts which skill
            # ``side_effects`` categories are allowed, every resolved
            # skill on the bundle gets checked. Same belt-and-braces
            # shape as runtime_policy: ``mdk validate`` catches this
            # statically, but bundles loaded over HTTP (worker) can
            # skip validate, so we re-check here.
            if not self._skill_policy.is_permissive():
                skill_violations = self._skill_policy.check_agent_skills(bundle.skills)
                if skill_violations:
                    raise PolicyViolationError("; ".join(skill_violations))

            # Runtime AVAILABILITY check — if the agent declared a
            # runtime we don't have an adapter for (e.g. opted into a
            # native runtime whose optional extra isn't installed),
            # fail fast with a SchemaError before doing any side
            # effects or budget checks.
            try:
                provider_for_run = self._registry.get(spec.runtime)
            except UnregisteredRuntimeError as exc:
                # SchemaError is the closest fit in our taxonomy — the
                # YAML declares a runtime that doesn't exist in this
                # build. Retries won't help.
                raise SchemaError(str(exc)) from exc

            # Tenant-budget check — if the tenant's monthly cap is
            # breached, no run should fire (not even a doomed one).
            # Cheap PK lookup + a single SUM aggregate; the index on
            # (tenant_id, created_at) is the perf path.
            await self._check_tenant_budget(tenant_id)

            # Prompt-injection INPUT GUARDRAIL — runs BEFORE any provider
            # call (and before schema validation / prompt rendering) so a
            # detected injection incurs zero LLM cost. Enabled when the
            # project policy lists "prompt_injection" in input_guardrails.
            # The detector scans all string fields in the raw input dict
            # recursively and raises GuardrailViolationError on first match.
            if "prompt_injection" in self._policy.input_guardrails:
                from movate.core.guardrails.prompt_injection import (  # noqa: PLC0415
                    PromptInjectionDetector,
                )

                _detector = PromptInjectionDetector()
                _result = _detector.detect(request.input)
                if _result is not None:
                    self._tracer.log_event(
                        span,
                        {
                            "guardrail": "prompt_injection.block",
                            "matched_pattern": _result.matched_pattern,
                            "matched_value": _result.matched_value[:200],
                        },
                    )
                    raise GuardrailViolationError(
                        f"prompt injection detected: pattern={_result.matched_pattern!r}"
                    )

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

            # Safe-AI INPUT guardrails — fired AFTER prompt rendering so
            # they see the actual text headed for the model (catches
            # template-injected content too). A ``block`` verdict raises
            # ContentFilterError which the existing pipeline maps to
            # ``status="safety_blocked"``; a ``redact`` verdict modifies
            # the rendered text in place; a ``warn`` verdict logs a
            # tracer event and continues. Fast-path early-exit when
            # guardrails are fully disabled (the common case for
            # projects that haven't opted in).
            if not self._guardrails.input.is_permissive():
                v = _guardrails_check_input(rendered, self._guardrails.input)
                if v.action == "block":
                    self._tracer.log_event(
                        span,
                        {
                            "guardrail": "input.block",
                            "triggered_by": list(v.triggered_by),
                            "reason": v.reason,
                        },
                    )
                    raise ContentFilterError(f"input blocked: {v.reason}")
                if v.action == "redact":
                    self._tracer.log_event(
                        span,
                        {
                            "guardrail": "input.redact",
                            "triggered_by": list(v.triggered_by),
                        },
                    )
                    rendered = v.redacted_text or rendered
                elif v.action == "warn":
                    self._tracer.log_event(
                        span,
                        {
                            "guardrail": "input.warn",
                            "triggered_by": list(v.triggered_by),
                            "reason": v.reason,
                        },
                    )

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
            # Skill cost accumulates across every successful tool call
            # in the tool-use loop. Added to the total run cost below
            # so existing budget enforcement covers skills without
            # extra plumbing. ADR 002 — cost participates in budget.
            skill_cost_usd = 0.0
            # Pre-compute tool specs from the agent's resolved skills.
            # Empty list = no tool-use loop, single-shot path runs
            # identically to v0.5. The provider's to_tool_spec
            # converts each SkillBundle into the model's native
            # tool format (OpenAI-style by default; native-SDK
            # adapters override).
            tool_specs: list[dict[str, Any]] | None = None
            if bundle.skills:
                tool_specs = [provider_for_run.to_tool_spec(s) for s in bundle.skills]

            for provider_str, params in chain:
                # Prepend conversation history (chat memory) before the
                # newly-rendered current turn. History entries are passed
                # through as-is; the renderer only sees the current
                # request.input (history isn't fed to the Jinja template).
                conversation: list[Message] = [
                    *(history or []),
                    Message(role="user", content=rendered),
                ]
                try:
                    (
                        completion,
                        skill_cost_usd,
                    ) = await self._run_with_tool_use(
                        provider_for_run=provider_for_run,
                        bundle=bundle,
                        provider_str=provider_str,
                        params=params,
                        initial_messages=conversation,
                        on_token=on_token,
                        span=span,
                        tool_specs=tool_specs,
                        run_id=run_id,
                        tenant_id=tenant_id,
                        skill_fixture=skill_fixture,
                    )
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

            # Pricing-key dance: each adapter knows the canonical key for
            # its provider strings (LiteLLM passes the agent's
            # ``model.provider`` through unchanged; native_anthropic /
            # native_openai prepend the family prefix; langchain returns
            # None because the model is opaque). When None or the lookup
            # misses we record cost=0 with an event — better than
            # crashing on a runtime where pricing isn't applicable.
            pricing_key = provider_for_run.pricing_key(chosen_provider)
            if pricing_key is None:
                cost = 0.0
                self._tracer.log_event(
                    span,
                    {"cost_skipped": True, "reason": "runtime has no pricing key"},
                )
            else:
                try:
                    cost = self._pricing.cost_for(provider=pricing_key, tokens=completion.tokens)
                except KeyError:
                    cost = 0.0
                    self._tracer.log_event(
                        span,
                        {"cost_skipped": True, "reason": f"no pricing for {pricing_key!r}"},
                    )
            self._check_cost_drift(span, completion, cost)

            # Add the skill-cost accumulator from the tool-use loop.
            # Skill cost is a flat per-call USD figure declared in each
            # skill.yaml; it doesn't go through pricing-table lookup
            # because skills can be anything (a free Python function, a
            # paid HTTP API, an internal MCP server). Drift check above
            # is still on the LLM cost only.
            if skill_cost_usd > 0:
                cost += skill_cost_usd
                self._tracer.log_event(
                    span,
                    {"skill_cost_added": skill_cost_usd},
                )

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

            # Safe-AI OUTPUT guardrails — fired on the raw completion
            # text BEFORE JSON parsing / schema validation, so a leaky
            # output is caught even when the output isn't well-formed
            # JSON (the safety_blocked path takes priority over
            # schema_error in that case). Same fast-path skip as input.
            completion_text = completion.text
            if not self._guardrails.output.is_permissive():
                v = _guardrails_check_output(completion_text, self._guardrails.output)
                if v.action == "block":
                    self._tracer.log_event(
                        span,
                        {
                            "guardrail": "output.block",
                            "triggered_by": list(v.triggered_by),
                            "reason": v.reason,
                        },
                    )
                    raise ContentFilterError(f"output blocked: {v.reason}")
                if v.action == "redact":
                    self._tracer.log_event(
                        span,
                        {
                            "guardrail": "output.redact",
                            "triggered_by": list(v.triggered_by),
                        },
                    )
                    completion_text = v.redacted_text or completion_text
                elif v.action == "warn":
                    self._tracer.log_event(
                        span,
                        {
                            "guardrail": "output.warn",
                            "triggered_by": list(v.triggered_by),
                            "reason": v.reason,
                        },
                    )

            output = _parse_json_output(completion_text)
            try:
                bundle.output_validator.validate(output)
            except JsonSchemaError as exc:
                raise SchemaError(f"model output failed schema: {exc.message}") from exc

            # Reflection loop (Phase J-1). When the agent's
            # ``reflection.enabled`` is true, run the output through a
            # judge. On a ``revise`` verdict, re-prompt the same
            # provider with the judge's feedback as a correction
            # directive. Bounded by ``reflection.max_iterations`` — a
            # judge that keeps rejecting can never block the run.
            if spec.reflection.enabled:
                # The agent-call params from the chain entry that
                # succeeded — reflection uses the same agent config
                # for the revision re-prompt (only the user message
                # changes, not temperature/max_tokens).
                successful_params = next(
                    (p for prov, p in chain if prov == chosen_provider),
                    chain[0][1],
                )
                output, reflection_cost = await self._reflect(
                    spec=spec,
                    bundle=bundle,
                    provider_for_run=provider_for_run,
                    provider_str=chosen_provider,
                    params=successful_params,
                    initial_output=output,
                    initial_output_text=completion.text,
                    user_message=rendered,
                    history=history,
                    span=span,
                )
                cost += reflection_cost
                # Cost-cap re-check after reflection — a runaway loop
                # would have blown the budget without this.
                if cost > effective_ceiling:
                    raise BudgetExceededError(
                        f"run cost ${cost:.4f} exceeds ceiling "
                        f"${effective_ceiling:.4f} after reflection "
                        f"(agent budget ${spec.budget.max_cost_usd_per_run:.4f})"
                    )

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
                tenant_id=tenant_id,
                request=request,
                response=response,
                chosen_provider=chosen_provider,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
                thread_id=thread_id,
            )
            self._tracer.end_span(span, status="ok")
            return response

        except MovateError as exc:
            return await self._handle_failure(
                span=span,
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                tenant_id=tenant_id,
                started=started,
                err=exc,
            )
        except RetryExhaustedError as exc:
            return await self._handle_failure(
                span=span,
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                tenant_id=tenant_id,
                started=started,
                err=exc.last_error,
            )

    async def _run_with_tool_use(
        self,
        *,
        provider_for_run: BaseLLMProvider,
        bundle: AgentBundle,
        provider_str: str,
        params: dict[str, Any],
        initial_messages: list[Message],
        on_token: Callable[[str], None] | None,
        span: SpanCtx,
        tool_specs: list[dict[str, Any]] | None,
        run_id: str,
        tenant_id: str,
        skill_fixture: dict[str, Any] | None = None,
    ) -> tuple[CompletionResponse, float]:
        """Drive the tool-use loop for one provider in the fallback chain.

        Returns ``(final_completion, accumulated_skill_cost_usd)``. The
        final completion has ``kind == "final"`` and its ``text`` is
        the model's answer; its ``tokens`` field is the SUM across
        every turn the loop took, so downstream cost accounting reads
        the full token usage without changing.

        Loop body per ADR 002 D1 (loop owned by Executor):

        1. Build CompletionRequest with the running message history +
           the tool_specs.
        2. Call ``run_with_retries(_invoke)``. The retry layer is
           per-turn — a transient failure on turn 3 doesn't blow away
           turns 1-2.
        3. If response is ``kind="final"``, exit and return.
        4. Otherwise dispatch the named skill via the backend
           registry. On success, append the assistant's tool_use turn
           + a ``role="tool"`` result message and loop. On
           ``SkillError``, append the same shapes but encode the
           error type/message in the tool result so the model can
           recover.

        Single-shot agents (``tool_specs is None``) take this path
        too — the loop immediately exits after one ``run_with_retries``
        call. Zero overhead vs the v0.5 inline path.
        """
        # Local imports keep skill_backend out of executor's hot-path
        # module-load graph, and avoid a cycle with skill_loader.
        from movate.core.skill_backend import (  # noqa: PLC0415
            SkillError,
            SkillErrorType,
            SkillExecutionContext,
            dispatch_skill,
        )

        # Importing each backend for its side-effect (registers itself
        # with the dispatch table). Done here rather than at module load
        # so these imports don't fire for agents without skills. Adding
        # a new backend is a single line + the new module — no other
        # wiring required.
        from movate.core.skill_backend import agent as _agent_backend  # noqa: F401, PLC0415
        from movate.core.skill_backend import http as _http_backend  # noqa: F401, PLC0415
        from movate.core.skill_backend import mcp as _mcp_backend  # noqa: F401, PLC0415
        from movate.core.skill_backend import python as _python_backend  # noqa: F401, PLC0415

        # Build a name → SkillBundle map for quick lookup inside the loop.
        skill_index: dict[str, Any] = {s.spec.name: s for s in bundle.skills}

        messages: list[Message] = list(initial_messages)
        accumulated_tokens = TokenUsage()
        accumulated_raw: dict[str, Any] = {}
        accumulated_skill_cost = 0.0
        turns_taken = 0
        agent_call_ms = bundle.spec.timeouts.call_ms

        while True:
            req = CompletionRequest(
                provider=provider_str,
                messages=messages,
                params=params,
                tools=tool_specs,
            )

            async def _invoke(req: CompletionRequest = req) -> CompletionResponse:
                if on_token is None:
                    return await provider_for_run.complete(req)
                return await self._invoke_streaming(provider_for_run, req, on_token)

            completion = await run_with_retries(_invoke)
            # Aggregate tokens + raw so the post-loop cost path sees
            # the full picture even when multiple turns happened.
            accumulated_tokens = TokenUsage(
                input=accumulated_tokens.input + completion.tokens.input,
                output=accumulated_tokens.output + completion.tokens.output,
                cached_input=accumulated_tokens.cached_input + completion.tokens.cached_input,
            )
            if completion.raw:
                accumulated_raw.update(completion.raw)

            if completion.kind == "final":
                # Inject the accumulated tokens + raw into the final
                # response so the downstream cost calc reads the
                # full-loop total, not just the last turn.
                return (
                    completion.model_copy(
                        update={"tokens": accumulated_tokens, "raw": accumulated_raw},
                    ),
                    accumulated_skill_cost,
                )

            # Tool-use turn. Resolve, dispatch, append to history.
            turns_taken += 1
            if turns_taken > _MAX_TOOL_TURNS_DEFAULT:
                # Hard cap. Don't try to invoke the skill again — the
                # last completion's text (if any) becomes our final
                # answer, and the loop terminates so we don't burn
                # cost forever. Operator-facing event is logged so
                # this is visible in traces.
                self._tracer.log_event(
                    span,
                    {
                        "tool_use_max_turns_hit": True,
                        "max_turns": _MAX_TOOL_TURNS_DEFAULT,
                    },
                )
                final = completion.model_copy(
                    update={
                        "kind": "final",
                        "tokens": accumulated_tokens,
                        "raw": accumulated_raw,
                    },
                )
                return final, accumulated_skill_cost

            tool_name = completion.tool_name
            tool_id = completion.tool_id
            tool_input = completion.tool_input

            skill = skill_index.get(tool_name)
            if skill is None:
                # The model invented a tool name. Emit a NOT_FOUND
                # tool_result and let the model recover.
                err = SkillError(
                    type=SkillErrorType.NOT_FOUND,
                    message=(f"unknown tool {tool_name!r}; available: {sorted(skill_index)}"),
                )
                tool_result_content = json.dumps({"error": err.type.value, "message": err.message})
            elif skill_fixture is not None and tool_name in skill_fixture:
                # Fixture short-circuit: eval dataset provided a canned
                # response for this skill. Return it immediately without
                # any real network/python dispatch. Cost stays zero —
                # fixtures are deterministic stand-ins, not real calls.
                tool_result_content = json.dumps(skill_fixture[tool_name])
                self._tracer.log_event(
                    span,
                    {"skill_fixture_used": tool_name, "turn": turns_taken},
                )
            else:
                # Effective per-call budget is skill override OR agent
                # inheritance (ADR 002 D3).
                call_ms = skill.spec.timeout_call_ms or agent_call_ms
                # ``agent_name`` + ``storage`` + ``retrieval`` plumbed
                # through for skills that introspect the calling agent
                # (the ``kb-vector-lookup`` skill needs all three to
                # query the agent's KB chunks with the operator's
                # configured retrieval pipeline — PR-I).
                ctx = SkillExecutionContext(
                    trace_id=span.trace_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    call_ms_budget=call_ms,
                    agent_name=bundle.spec.name,
                    storage=self._storage,
                    retrieval=bundle.spec.retrieval,
                )
                try:
                    output = await dispatch_skill(skill, tool_input, ctx)
                    tool_result_content = json.dumps(output)
                    # Add this skill's cost to the run total.
                    accumulated_skill_cost += skill.spec.cost.per_call_usd
                    self._tracer.log_event(
                        span,
                        {
                            "skill_invoked": tool_name,
                            "skill_cost_usd": skill.spec.cost.per_call_usd,
                            "turn": turns_taken,
                        },
                    )
                except SkillError as exc:
                    tool_result_content = json.dumps(
                        {"error": exc.type.value, "message": exc.message}
                    )
                    self._tracer.log_event(
                        span,
                        {
                            "skill_error": tool_name,
                            "skill_error_type": exc.type.value,
                            "turn": turns_taken,
                        },
                    )

            # Append assistant's tool_use turn + the matching tool
            # result. The assistant turn carries the original tool_call
            # so the model sees its own action when we re-prompt.
            assistant_turn = Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_input),
                        },
                    }
                ],
            )
            tool_turn = Message(
                role="tool",
                content=tool_result_content,
                tool_call_id=tool_id,
            )
            messages.extend([assistant_turn, tool_turn])

    async def _invoke_streaming(
        self,
        provider: BaseLLMProvider,
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
        survivable; the cost-drift check downstream will flag it.

        Takes ``provider`` explicitly (rather than via ``self``) so
        the executor can dispatch per-agent across multiple
        registered providers — see :class:`ProviderRegistry`."""
        text_parts: list[str] = []
        final_tokens: TokenUsage | None = None
        raw: dict[str, Any] = {}
        async for chunk in provider.stream(req):
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

    async def _reflect(
        self,
        *,
        spec: object,
        bundle: AgentBundle,
        provider_for_run: BaseLLMProvider,
        provider_str: str,
        params: dict[str, Any],
        initial_output: dict[str, Any],
        initial_output_text: str,
        user_message: str,
        history: list[Message] | None,
        span: SpanCtx,
    ) -> tuple[dict[str, Any], float]:
        """Run the reflection loop (judge → maybe revise) over the
        primary completion.

        Returns ``(final_output, reflection_cost_usd)``. The output
        dict is either the original (judge accepted) or the revised
        one (judge said revise + executor re-prompted + revision
        re-validated).

        Bounded by ``spec.reflection.max_iterations`` — the loop
        terminates after that many AGENT calls (including the
        original), regardless of whether the judge is still rejecting.
        A flaky judge can never block the run.

        Tracer spans: each judge call + each revision re-prompt
        surface as separate events under the agent's main span so the
        operator can see reflection overhead in Langfuse.
        """
        # mypy: AgentSpec for ``spec`` (typed as ``object`` here to
        # avoid a circular type import). We access ``.reflection``
        # which is the runtime contract.
        reflect_cfg = spec.reflection  # type: ignore[attr-defined]
        # Resolve the judge provider. For MVP we route every judge
        # call through LiteLLM regardless of the agent's runtime —
        # ``reflection.judge_model`` is documented as a LiteLLM-style
        # ``<family>/<model>`` string. Native-SDK judges land later
        # alongside the equivalent agent-side adapters.
        from movate.core.models import AgentRuntime  # noqa: PLC0415  -- avoid TYPE_CHECKING

        try:
            judge_provider = self._registry.get(AgentRuntime.LITELLM)
        except Exception:
            log.warning("reflection: no LiteLLM provider in registry; skipping reflection")
            return (initial_output, 0.0)

        current_output = initial_output
        current_output_text = initial_output_text
        total_judge_cost = 0.0

        # max_iterations = 1 means: judge ONCE, never re-prompt
        # (audit-only). max_iterations = 2 means: judge, re-prompt
        # once if revise, judge again, return whatever (don't loop
        # past that). Etc.
        for iteration in range(reflect_cfg.max_iterations):
            verdict = await call_judge(
                config=reflect_cfg,
                output_text=current_output_text,
                judge_provider=judge_provider,
                pricing_lookup=self._pricing,
            )
            total_judge_cost += verdict.cost_usd
            self._tracer.log_event(
                span,
                {
                    "reflection.iteration": iteration,
                    "reflection.verdict": verdict.verdict,
                    "reflection.feedback": verdict.feedback[:200],
                    "reflection.judge_cost_usd": verdict.cost_usd,
                    "reflection.judge_tokens_in": verdict.tokens_in,
                    "reflection.judge_tokens_out": verdict.tokens_out,
                },
            )

            if verdict.verdict in {"accept", "parse_error"}:
                # parse_error is treated as soft-accept (flaky judge
                # shouldn't block the run; warning already logged in
                # reflection.py).
                break

            # verdict == "revise". If we have iterations left, re-prompt
            # the agent with the judge's feedback. The agent uses the
            # SAME provider + params as the original call — only the
            # user message changes.
            if iteration + 1 >= reflect_cfg.max_iterations:
                # No iterations left. Return the most recent output
                # with a warning event — the operator sees the
                # accumulated rejection trail.
                self._tracer.log_event(
                    span,
                    {
                        "reflection.exhausted": True,
                        "reflection.final_verdict": "revise (returning anyway)",
                    },
                )
                break

            revision_prompt = build_revision_prompt(user_message, verdict.feedback)
            revision_messages: list[Message] = [
                *(history or []),
                Message(role="user", content=user_message),
                Message(role="assistant", content=current_output_text),
                Message(role="user", content=revision_prompt),
            ]
            revision_request = CompletionRequest(
                provider=provider_str,
                messages=revision_messages,
                params=params,
            )
            revision_response = await provider_for_run.complete(revision_request)
            revision_cost = self._pricing.cost_for(
                provider=provider_str,
                tokens=revision_response.tokens,
            )
            total_judge_cost += revision_cost
            self._tracer.log_event(
                span,
                {
                    "reflection.revision_cost_usd": revision_cost,
                    "reflection.revision_tokens_in": revision_response.tokens.input,
                    "reflection.revision_tokens_out": revision_response.tokens.output,
                },
            )

            # Parse + validate the revised output. If parsing or
            # validation fails, we return the PREVIOUS (accepted by
            # schema) output rather than a broken one — a revision
            # that breaks schema is worse than no revision.
            try:
                revised_output = _parse_json_output(revision_response.text)
                bundle.output_validator.validate(revised_output)
            except (JsonSchemaError, ValueError) as exc:
                log.warning(
                    "reflection: revision failed schema/parse (%s); keeping previous output",
                    exc,
                )
                self._tracer.log_event(
                    span,
                    {
                        "reflection.revision_failed_validation": True,
                        "reflection.revision_error": str(exc)[:200],
                    },
                )
                break

            current_output = revised_output
            current_output_text = revision_response.text

        return (current_output, total_judge_cost)

    async def _check_tenant_budget(self, tenant_id: str) -> None:
        """Abort the run if the tenant has hit its monthly cap.

        Reads :meth:`StorageProvider.get_tenant_budget` (PK lookup —
        cheap). If no row exists for this tenant or
        ``monthly_usd_limit`` is ``None``, returns immediately (the
        default-unlimited case, backwards compatible with every
        pre-budget deployment).

        Takes ``tenant_id`` as a positional so the caller (Executor.execute)
        can pass the correct per-run tenant (which may differ from
        ``self._tenant_id`` when a worker is draining a multi-tenant
        queue).

        Race window: under high concurrency two requests can both
        observe "under budget" simultaneously and both succeed,
        pushing combined cost over the cap. The overrun is bounded
        by the in-flight call count — not catastrophic, but
        operators should set the cap slightly below the hard cost
        ceiling they actually want to enforce.
        """
        budget = await self._storage.get_tenant_budget(tenant_id)
        if budget is None or budget.monthly_usd_limit is None:
            return
        current = await self._storage.sum_tenant_cost_current_month(tenant_id)
        if current >= budget.monthly_usd_limit:
            raise TenantBudgetExceededError(
                f"tenant {tenant_id!r} has spent ${current:.2f} of "
                f"${budget.monthly_usd_limit:.2f} this month; runs are paused. "
                f"Operator can raise the budget with "
                f"`movate tenants set-budget {tenant_id} --monthly-usd <new>` "
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
        tenant_id: str,
        request: RunRequest,
        response: RunResponse,
        chosen_provider: str,
        workflow_run_id: str | None = None,
        node_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        record = RunRecord(
            run_id=run_id,
            job_id=job_id,
            tenant_id=tenant_id,
            agent=bundle.spec.name,
            agent_version=bundle.spec.version,
            prompt_hash=bundle.prompt_hash,
            provider=chosen_provider,
            # provider_version stamps which adapter class produced this
            # run — look up via the registry so multi-runtime executors
            # record the right version per agent.
            provider_version=self._registry.get(bundle.spec.runtime).version,
            pricing_version=self._pricing.version,
            status=JobStatus.SUCCESS,
            input=request.input,
            output=response.data,
            metrics=response.metrics,
            workflow_run_id=workflow_run_id,
            node_id=node_id,
            thread_id=thread_id,
        )
        await self._storage.save_run(record)

    async def _handle_failure(
        self,
        *,
        span: SpanCtx,
        bundle: AgentBundle,
        run_id: str,
        job_id: str,
        tenant_id: str,
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
                tenant_id=tenant_id,
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
