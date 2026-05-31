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

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from movate.memory import MemoryStore

from jsonschema import ValidationError as JsonSchemaError

from movate.core.cache import (
    CachedResponse,
    CacheProvider,
    NoOpCache,
    cache_ttl_s,
    compute_cache_key,
    is_cacheable,
)
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
    ToolError,
)
from movate.core.loader import AgentBundle
from movate.core.models import (
    AgentRuntime,
    AgentSpec,
    ErrorInfo,
    FailureRecord,
    JobStatus,
    Metrics,
    ModelConfig,
    ModelFallback,
    RunRecord,
    RunRequest,
    RunResponse,
    SkillCallRecord,
    TokenUsage,
    TurnRecord,
)
from movate.core.provider_keys import ProviderKeyResolver
from movate.core.reflection import build_revision_prompt, call_judge
from movate.core.retry import RetryExhaustedError, run_with_retries
from movate.guardrails import check_input as _guardrails_check_input
from movate.guardrails import check_output as _guardrails_check_output
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
    ToolCallSpec,
)
from movate.providers.pricing import PricingTable
from movate.providers.registry import ProviderRegistry, UnregisteredRuntimeError
from movate.storage.base import StorageProvider
from movate.tracing.base import SpanCtx, Tracer

log = logging.getLogger(__name__)

# 15% — informational cross-check only. The pricing table
# (providers/pricing.yaml) is canonical for billing; litellm's bundled
# prices can lag, so a gap here does not mean our reported cost is wrong.
_COST_DRIFT_THRESHOLD = 0.15

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
        memory_store: MemoryStore | None = None,
        cache: CacheProvider | None = None,
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
        # Optional per-agent memory store. When set, every successful run
        # writes {input, output, run_id} under key "last_run" for the
        # agent — visible via `mdk memory get <agent> last_run`. Failure
        # to write is non-fatal (logged at WARNING, run still succeeds).
        self._memory_store = memory_store
        # Safe-AI guardrails (PII / topic / content) for input + output.
        # Permissive default — every sub-block ``enabled: false`` means
        # the input/output check fast-paths to allow. Wired at execute()
        # entry (input) and exit (output). See GuardrailsConfig for the
        # full schema and ``movate.guardrails`` for the engine.
        self._guardrails = guardrails or GuardrailsConfig()
        # LLM response cache (ADR-free, mirrors RateLimiter). Default
        # NoOpCache → always-miss, never-store → byte-for-byte
        # unchanged behavior. Only deterministic (temperature==0) calls
        # are ever cached; a hit returns the stored completion at $0
        # cost / ~0 latency. In-process now; shared backends
        # (Redis/Postgres) slot in behind the CacheProvider Protocol
        # later. Wired at the executor↔provider boundary in
        # _run_with_tool_use.
        self._cache: CacheProvider = cache or NoOpCache()
        self._cache_ttl_s = cache_ttl_s()
        # ADR 018 — per-tenant BYOK provider keys. Lazily built (only when a
        # tenant has a stored key) off the same storage the executor already
        # holds, so no construction-site change is needed. Reads the calling
        # tenant's encrypted key at run time and threads it into the
        # provider's ``api_key`` param; falls through to the env-default key
        # when there's no tenant key (default-on shared fallback) — the
        # no-config path stays byte-for-byte unchanged.
        self._provider_key_resolver: ProviderKeyResolver | None = None

    @property
    def tracer(self) -> Tracer:
        """Expose the underlying tracer for callers that need to push scores."""
        return self._tracer

    async def execute(
        self,
        bundle: AgentBundle,
        request: RunRequest,
        *,
        job_id: str | None = None,
        model_override: ModelConfig | None = None,
        workflow_run_id: str | None = None,
        node_id: str | None = None,
        parent_span: SpanCtx | None = None,
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

        ``parent_span`` optionally nests this run's root ``agent.execute`` span
        under a caller-provided span (ADR 024 D4). The
        :class:`~movate.core.workflow.WorkflowRunner` opens one
        ``workflow.execute`` span per workflow run and threads its
        :class:`~movate.tracing.base.SpanCtx` in here, so every node's
        ``agent.execute`` becomes a child of that workflow root — a multi-node
        workflow renders as ONE nested trace tree (Langfuse / OTel) instead of
        N disconnected roots. ``None`` (the default, every standalone caller)
        leaves ``agent.execute`` a root span exactly as before — back-compat is
        byte-for-byte for non-workflow runs.

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
                # _-prefixed keys are LangfuseTracer-private: extracted
                # from attrs before metadata is forwarded so they hit
                # Langfuse's first-class trace fields (user_id / session_id
                # / tags) rather than the metadata blob. Other tracers
                # receive them as regular attributes (harmless).
                "_session_id": request.session_id,
                "_user_id": request.user_id,
                "_tags": spec.tags or [],
            },
            parent=parent_span,
        )

        started = time.monotonic()
        # Per-step trail (ADR 024 D1/D2), declared BEFORE the try so it is
        # always in scope for the failure handler — a run that errors mid-loop
        # still persists the partial turns/skills captured so far (offline-first
        # partial record; test-matrix case 7). The tool-use loop and the ADR 023
        # pre-retrieval phase append into these in place.
        pre_retrieval_calls: list[SkillCallRecord] = []
        skill_calls: list[SkillCallRecord] = []
        turns: list[TurnRecord] = []
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

            # Pre-retrieval (ADR 023, "turn 0") cost, accounted into the run
            # total below. The retrieval SkillCallRecord goes into the
            # ``pre_retrieval_calls`` list declared above — kept SEPARATE from
            # the tool-use loop's records so a fallback reset can't wipe it.
            pre_retrieval_cost_usd = 0.0

            # ADR 023 — opt-in declarative pre-retrieval (auto-RAG).
            # Runs AFTER input validation and BEFORE prompt render, in
            # this ONE shared Executor, so grounding is identical across
            # planes (local `mdk run`, runtime inline, worker) by
            # construction. Entirely gated behind `retrieval.auto_into`
            # — with no `retrieval:` block (the dominant non-RAG path),
            # ``_maybe_pre_retrieve`` returns immediately and the run is
            # byte-for-byte unchanged. On success it mutates
            # ``request.input[auto_into]`` and RE-VALIDATES the input
            # against the schema so the populated field still conforms.
            if bundle.spec.retrieval.auto_retrieval_enabled:
                # ADR 024 D1/D5 — pre-retrieval is "turn 0": its cost is
                # accounted into the run total and it emits a ``retrieval.*``
                # child span + a turn-0 SkillCallRecord (offline-first).
                pre_retrieval_cost_usd = await self._maybe_pre_retrieve(
                    bundle=bundle,
                    request=request,
                    span=span,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    skill_calls=pre_retrieval_calls,
                )
                try:
                    bundle.input_validator.validate(request.input)
                except JsonSchemaError as exc:
                    raise SchemaError(
                        f"input failed schema after pre-retrieval: {exc.message}"
                    ) from exc

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

            self._tracer.log_event(
                span,
                {
                    "prompt_hash": bundle.prompt_hash,
                    # Full rendered system prompt actually sent to the model
                    # (contexts already prepended) — so traces show the real
                    # text, not just a hash. Names of injected contexts are
                    # logged alongside as the context-injection step.
                    "rendered_prompt": rendered,
                    "contexts": [name for name, _ in bundle.contexts],
                },
            )

            chain: list[tuple[str, dict[str, Any]]] = [
                (effective_model.provider, dict(effective_model.params))
            ]
            if model_override is None:
                for fb in spec.model.fallback:
                    merged = dict(spec.model.params)
                    merged.update(fb.params)
                    chain.append((fb.provider, merged))

            # ADR 018 — per-tenant BYOK. Resolve the calling tenant's own
            # provider key for each chain entry and thread it into the
            # provider call via the existing ``api_key`` param. When the
            # resolver returns None (no tenant key + shared-key fallback on,
            # the default) NOTHING is added, so the provider uses its env
            # default exactly as before BYOK — the no-config path is
            # byte-for-byte unchanged. Mutates the params dicts in place.
            await self._inject_tenant_provider_keys(chain, tenant_id, spec.runtime)

            completion: CompletionResponse | None = None
            chosen_provider = ""
            last_error: MovateError | None = None
            # Skill cost accumulates across every successful tool call
            # in the tool-use loop. Added to the total run cost below
            # so existing budget enforcement covers skills without
            # extra plumbing. ADR 002 — cost participates in budget.
            # (Pre-retrieval cost is tracked separately above.)
            skill_cost_usd = 0.0
            # ``skill_calls`` / ``turns`` (declared before the try) carry the
            # tool-use loop's per-step records. ``_run_with_tool_use`` APPENDS
            # to them in place so a mid-loop failure still persists the partial
            # trail; on a fallback attempt they are RESET (cleared) at the top
            # of each chain iteration so only the successful (or final-failing)
            # provider's steps survive. The pre-retrieval (turn-0) records live
            # in ``pre_retrieval_calls`` and are prepended at persist time —
            # fallback resets don't touch them.
            # Σ of per-turn LLM costs from the loop (ADR 024 D5). Computed
            # where each completion happens so multi-turn / tool runs report
            # the honest sum, not just the final completion's cost.
            turn_cost_usd = 0.0
            # Pre-compute tool specs from the agent's resolved skills.
            # Empty list = no tool-use loop, single-shot path runs
            # identically to v0.5. The provider's to_tool_spec
            # converts each SkillBundle into the model's native
            # tool format (OpenAI-style by default; native-SDK
            # adapters override).
            tool_specs: list[dict[str, Any]] | None = None
            if bundle.skills:
                tool_specs = [provider_for_run.to_tool_spec(s) for s in bundle.skills]

            # Track the messages sent to the LLM so log_generation can
            # emit the prompt to Langfuse after the run completes.
            conversation: list[Message] = []
            for provider_str, params in chain:
                # Prepend conversation history (chat memory) before the
                # newly-rendered current turn. History entries are passed
                # through as-is; the renderer only sees the current
                # request.input (history isn't fed to the Jinja template).
                conversation = [
                    *(history or []),
                    Message(role="user", content=rendered),
                ]
                # Reset the per-attempt step records. If a prior chain entry
                # failed and we fell back, its partial turns/skills must not
                # leak into the successful (or final-failing) provider's trail.
                skill_calls.clear()
                turns.clear()
                try:
                    (
                        completion,
                        skill_cost_usd,
                        turn_cost_usd,
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
                        turns=turns,
                        skill_calls=skill_calls,
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

            # ADR 024 D5 — run-level LLM cost is now the SUM of per-turn
            # costs computed inside the tool-use loop (one ``provider.complete``
            # per turn), not a single pricing-table lookup over the accumulated
            # tokens. For the single-turn / no-skill path the sum equals the
            # historical figure exactly (one turn, same tokens, same pricing
            # key) — a value-preserving refactor guarded by the regression
            # test. For multi-turn / tool runs it is the honest, larger total.
            # The per-turn cache-hit / no-pricing-key ``cost_skipped`` events
            # are emitted in the loop where each completion happens.
            cost = turn_cost_usd
            self._check_cost_drift(span, completion, cost)

            # Add the skill-cost accumulators (tool-use loop + ADR 023
            # pre-retrieval "turn 0"). Skill cost is a flat per-call USD figure
            # declared in each skill.yaml; it doesn't go through the
            # pricing-table lookup because skills can be anything (a free Python
            # function, a paid HTTP API, an internal MCP server). Drift check
            # above is still on the LLM cost only. ADR 024 D5: the run cost is
            # the honest Σ(turn costs) + Σ(skill costs).
            total_skill_cost = skill_cost_usd + pre_retrieval_cost_usd
            if total_skill_cost > 0:
                cost += total_skill_cost
                self._tracer.log_event(
                    span,
                    {"skill_cost_added": total_skill_cost},
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

            # Grounding check (M2 gap closure). Runs after schema
            # validation so we know the output shape is correct. Only
            # meaningful for RAG agents with grounding_enforcement != "off".
            # ``warn`` logs events without blocking; ``strict`` raises
            # GroundingViolationError which the outer try/except converts
            # to safety_blocked. Imported locally to avoid circular imports
            # (grounding.py only imports from stdlib + core.models).
            _grounding_mode = spec.grounding_enforcement
            if _grounding_mode != "off":
                from movate.core.grounding import (  # noqa: PLC0415
                    check_grounding,
                    kb_call_count_from_records,
                    max_valid_citation_index_from_records,
                    ocr_cited_indices_from_records,
                )

                _kb_calls = kb_call_count_from_records(skill_calls)
                _max_idx = max_valid_citation_index_from_records(skill_calls)
                _citations = output.get("citations") if isinstance(output, dict) else None
                _ocr_idx = ocr_cited_indices_from_records(skill_calls, _citations)
                _g_report = check_grounding(
                    output,
                    kb_call_count=_kb_calls,
                    max_valid_citation_index=_max_idx,
                    ocr_cited_indices=_ocr_idx,
                    enforcement=_grounding_mode,
                )
                if not _g_report.ok:
                    # warn mode: log each violation as a tracer event,
                    # then continue. strict mode already raised above.
                    for _v in _g_report.violations:
                        log.warning("grounding violation [%s]: %s", _v.code, _v.message)
                        self._tracer.log_event(
                            span,
                            {
                                "grounding_violation": _v.code,
                                "message": _v.message,
                                "enforcement": _grounding_mode,
                            },
                        )

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
                trace_id=span.trace_id,
            )

            # Emit a Langfuse Generation object so token-usage charts and
            # the Generations tab populate. No-op on non-Langfuse tracers.
            self._tracer.log_generation(
                span,
                model=chosen_provider,
                input_messages=[{"role": m.role, "content": m.content or ""} for m in conversation],
                output_text=completion.text or "",
                input_tokens=completion.tokens.input,
                output_tokens=completion.tokens.output,
                cost_usd=cost,
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
                # Pre-retrieval (turn-0) calls first, then the tool-use loop's.
                skill_calls=[*pre_retrieval_calls, *skill_calls],
                turns=turns,
            )

            # Persist the run's input + output to the memory store so
            # `mdk memory get <agent> last_run` always shows the most
            # recent successful output. Non-fatal — a full disk or
            # misconfigured store must not kill an otherwise-successful
            # run.
            if self._memory_store is not None:
                try:
                    await self._memory_store.set(
                        spec.name,
                        "last_run",
                        {
                            "input": request.input,
                            "output": output,
                            "run_id": run_id,
                        },
                    )
                except Exception:  # broad-catch intentional — must not kill the run
                    log.warning(
                        "memory_store.set failed — last_run not persisted for agent %r",
                        spec.name,
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
                skill_calls=[*pre_retrieval_calls, *skill_calls],
                turns=turns,
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
                skill_calls=[*pre_retrieval_calls, *skill_calls],
                turns=turns,
            )

    async def _maybe_pre_retrieve(
        self,
        *,
        bundle: AgentBundle,
        request: RunRequest,
        span: SpanCtx,
        run_id: str,
        tenant_id: str,
        skill_calls: list[SkillCallRecord],
    ) -> float:
        """ADR 023 pre-retrieval phase — invoked only when
        ``retrieval.auto_into`` is set (gated by the caller).

        Builds the retrieval skill's input from ``query_from`` (+
        ``top_k`` if set), invokes ``retrieval.skill`` THROUGH the
        existing :func:`dispatch_skill` seam (no second retrieval code
        path; no concrete storage backend imported into ``core``), and
        writes the returned chunk texts into ``request.input[auto_into]``
        as a ``list[string]``. Mutates ``request.input`` in place; the
        caller re-validates against the schema afterwards.

        ADR 024 D1/D2/D5: emits a ``retrieval.<skill>`` child span under the
        run's root span (no-op when tracing is off), appends a turn-0
        :class:`SkillCallRecord` to ``skill_calls``, and RETURNS the
        retrieval skill's ``cost.per_call_usd`` so the caller folds it into
        the run cost sum. Returns ``0.0`` on every no-op / skip / warn path.

        Failure modes (ADR 023 D4):

        * ``when: if_empty`` (default) — skip if ``auto_into`` already
          holds a non-empty value (respects an explicitly-passed value;
          preserves eval determinism). ``always`` retrieves regardless.
        * No retriever / empty KB — NO-OP with one stderr notice; the
          run proceeds (the prompt's empty-context branch handles it).
          Never a hard failure regardless of ``on_error``.
        * Empty results — set ``auto_into`` to ``[]`` and proceed.
        * Retrieval/embedding error — surfaced via the ADR 002
          ``SkillError`` taxonomy. ``on_error: warn`` (default) proceeds
          ungrounded with a notice; ``on_error: fail`` re-raises so the
          run aborts with a typed error.
        """
        # Local imports keep skill_backend / loader out of the hot-path
        # module-load graph (same pattern as the tool-use loop) and the
        # ``core`` boundary clean — we touch only the SkillBackend
        # Protocol + StorageProvider handle, never a concrete backend.
        from movate.core.skill_backend import (  # noqa: PLC0415
            SkillError,
            SkillExecutionContext,
            dispatch_skill,
        )
        from movate.core.skill_backend import agent as _agent_backend  # noqa: F401, PLC0415
        from movate.core.skill_backend import http as _http_backend  # noqa: F401, PLC0415
        from movate.core.skill_backend import mcp as _mcp_backend  # noqa: F401, PLC0415
        from movate.core.skill_backend import python as _python_backend  # noqa: F401, PLC0415

        cfg = bundle.spec.retrieval
        auto_into = cfg.auto_into
        assert auto_into is not None  # gated by the caller (auto_retrieval_enabled)

        # ``when: if_empty`` (default) respects an explicitly-passed
        # value — skip retrieval entirely so eval/test determinism holds.
        if cfg.when == "if_empty":
            existing = request.input.get(auto_into)
            if existing:  # non-empty list / string / dict — caller supplied grounding
                self._tracer.log_event(
                    span,
                    {"pre_retrieval_skipped": "field_non_empty", "auto_into": auto_into},
                )
                return 0.0

        # Resolve the retrieval skill from the agent's declared skills.
        # `mdk validate` enforces this resolves; at run time a missing
        # skill is a no-op notice (KB-less / misconfigured environments
        # must not hard-fail the dominant path — D4).
        skill = next(
            (s for s in bundle.skills if s.spec.name == cfg.auto_skill),
            None,
        )
        if skill is None:
            self._pre_retrieval_notice(
                span,
                auto_into,
                f"retrieval skill {cfg.auto_skill!r} is not wired on agent "
                f"{bundle.spec.name!r}; proceeding ungrounded",
            )
            request.input.setdefault(auto_into, [])
            return 0.0

        # Build the retrieval query from `query_from` (or the resolved
        # primary text field). An empty/missing query is a no-op notice.
        query = self._resolve_retrieval_query(bundle, request, cfg)
        if not query:
            self._pre_retrieval_notice(
                span,
                auto_into,
                "no retrieval query resolved from input; proceeding ungrounded",
            )
            request.input.setdefault(auto_into, [])
            return 0.0

        skill_input: dict[str, Any] = {"question": query}
        if cfg.top_k is not None:
            skill_input["k"] = cfg.top_k

        # ADR 024 D1 — pre-retrieval ("turn 0") gets a ``retrieval.<skill>``
        # child span under the run root. No-op on Null/Silent tracers. The
        # skill backend's own sub-spans nest under it via ``ctx.parent_span``.
        retrieval_span = self._tracer.start_span(
            f"retrieval.{cfg.auto_skill}",
            {"skill": cfg.auto_skill, "turn": 0, "auto_into": auto_into},
            parent=span,
        )
        ctx = SkillExecutionContext(
            trace_id=span.trace_id,
            tenant_id=tenant_id,
            run_id=run_id,
            call_ms_budget=skill.spec.timeout_call_ms or bundle.spec.timeouts.call_ms,
            agent_name=bundle.spec.name,
            storage=self._storage,
            retrieval=cfg,
            tracer=self._tracer,
            parent_span=retrieval_span,
        )
        cost = float(skill.spec.cost.per_call_usd)

        _t0 = time.monotonic()
        try:
            output = await dispatch_skill(skill, skill_input, ctx)
        except SkillError as exc:
            lat = (time.monotonic() - _t0) * 1000
            self._tracer.set_attribute(retrieval_span, "latency_ms", round(lat, 1))
            self._tracer.end_span(retrieval_span, status="error")
            # Retain the failed turn-0 retrieval as a SkillCallRecord so the
            # offline trail shows the grounding attempt + its failure.
            skill_calls.append(
                SkillCallRecord(
                    step=0,
                    turn=0,
                    skill=cfg.auto_skill or "",
                    input=skill_input,
                    error=f"{exc.type.value}: {exc.message}",
                    latency_ms=round(lat, 1),
                    cost_usd=0.0,
                )
            )
            # No retriever / empty KB surfaces as a benign skill outcome
            # (the kb-vector-lookup skill returns empty rather than
            # raising) — so a SkillError here is a genuine failure
            # (embedding error, backend down, timeout, …). Honor
            # `on_error`: warn → ungrounded + notice; fail → abort.
            if cfg.on_error == "fail":
                self._tracer.log_event(
                    span,
                    {
                        "pre_retrieval_error": exc.type.value,
                        "auto_into": auto_into,
                        "on_error": "fail",
                    },
                )
                # Surface through the MovateError taxonomy so the
                # executor's existing failure handler maps it to a clean
                # failed RunResponse (status + ErrorInfo), preserving the
                # ADR 002 SkillError type/message. Non-retryable: a failing
                # KB won't recover within the run, and fallback would just
                # re-fail the same way.
                raise ToolError(
                    f"pre-retrieval skill {cfg.auto_skill!r} failed "
                    f"[{exc.type.value}]: {exc.message}",
                    retryable=False,
                ) from exc
            self._pre_retrieval_notice(
                span,
                auto_into,
                f"retrieval failed ({exc.type.value}: {exc.message}); proceeding ungrounded",
                error_type=exc.type.value,
            )
            request.input.setdefault(auto_into, [])
            # warn path: no cost charged for a failed retrieval.
            return 0.0
        lat = (time.monotonic() - _t0) * 1000

        # Extract the chunk texts → `list[string]` (the shape the
        # `auto_into` field is validated to accept). The kb-vector-lookup
        # skill returns {chunks: [{text, source, score, ...}], ...}; we
        # take the `text` of each. Empty results → [] (deterministic; the
        # template's "no context" branch fires — D4).
        chunks = output.get("chunks") if isinstance(output, dict) else None
        texts: list[str] = []
        if isinstance(chunks, list):
            for c in chunks:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    texts.append(c["text"])
                elif isinstance(c, str):
                    texts.append(c)

        request.input[auto_into] = texts
        self._tracer.set_attribute(retrieval_span, "cost_usd", cost)
        self._tracer.set_attribute(retrieval_span, "latency_ms", round(lat, 1))
        self._tracer.set_attribute(retrieval_span, "chunks_merged", len(texts))
        self._tracer.end_span(retrieval_span, status="ok")
        # Retain the turn-0 retrieval as a SkillCallRecord (offline-first).
        skill_calls.append(
            SkillCallRecord(
                step=0,
                turn=0,
                skill=cfg.auto_skill or "",
                input=skill_input,
                output=output if isinstance(output, dict) else None,
                latency_ms=round(lat, 1),
                cost_usd=cost,
            )
        )
        self._tracer.log_event(
            span,
            {
                "pre_retrieval": "merged",
                "auto_into": auto_into,
                "skill": cfg.auto_skill,
                "chunks_merged": len(texts),
            },
        )
        if not texts:
            # Distinct stderr notice so a KB that exists but returns
            # nothing is debuggable (the #1 silent RAG failure).
            log.warning(
                "pre-retrieval: %s returned 0 chunks for agent %r; "
                "%s set to [] (run proceeds ungrounded)",
                cfg.auto_skill,
                bundle.spec.name,
                auto_into,
            )
        return cost

    def _resolve_retrieval_query(
        self,
        bundle: AgentBundle,
        request: RunRequest,
        cfg: Any,
    ) -> str:
        """Resolve the text query for pre-retrieval.

        Uses ``cfg.query_from`` when set; otherwise the agent's primary
        string input field (canonical names first, then the sole string
        field). ``mdk validate`` rejects an ambiguous default at load
        time, so at run time we degrade gracefully (return "" → no-op
        notice) rather than guessing wrong.
        """
        if cfg.query_from:
            val = request.input.get(cfg.query_from)
            return val.strip() if isinstance(val, str) and val.strip() else ""

        # Default resolution mirrors the canonical query-field heuristic
        # used elsewhere (cli.knowledge_cmd._extract_query_from_input):
        # canonical names first, then a sole string field.
        for key in ("query", "question", "text", "message"):
            val = request.input.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        string_values = [v for v in request.input.values() if isinstance(v, str) and v.strip()]
        if len(string_values) == 1:
            return string_values[0].strip()
        return ""

    def _pre_retrieval_notice(
        self,
        span: SpanCtx,
        auto_into: str,
        message: str,
        *,
        error_type: str | None = None,
    ) -> None:
        """Emit the single stderr notice + tracer event for a
        non-fatal pre-retrieval outcome (no-op / warn). Keeps the
        notice path consistent across the no-retriever, no-query, and
        on_error=warn branches (D4)."""
        log.warning("pre-retrieval: %s", message)
        event: dict[str, Any] = {"pre_retrieval_notice": message, "auto_into": auto_into}
        if error_type is not None:
            event["pre_retrieval_error"] = error_type
        self._tracer.log_event(span, event)

    def _turn_cost(
        self,
        *,
        provider_for_run: BaseLLMProvider,
        provider_str: str,
        completion: CompletionResponse,
        span: SpanCtx,
    ) -> float:
        """Cost of ONE turn's completion (ADR 024 D5).

        Mirrors the historical run-level pricing logic, applied per turn:
        an LLM-cache hit is $0; a runtime with no pricing key (or a missing
        pricing entry) is $0 with an observable ``cost_skipped`` event;
        otherwise the pricing-table lookup over THIS turn's tokens. Summing
        these across turns reproduces the legacy single-turn figure exactly
        (one turn, same tokens, same key) while giving multi-turn / tool runs
        the honest total."""
        if completion.raw.get("llm_cache_hit"):
            self._tracer.log_event(span, {"cost_skipped": True, "reason": "llm cache hit ($0)"})
            return 0.0
        pricing_key = provider_for_run.pricing_key(provider_str)
        if pricing_key is None:
            self._tracer.log_event(
                span, {"cost_skipped": True, "reason": "runtime has no pricing key"}
            )
            return 0.0
        try:
            return self._pricing.cost_for(provider=pricing_key, tokens=completion.tokens)
        except KeyError:
            self._tracer.log_event(
                span, {"cost_skipped": True, "reason": f"no pricing for {pricing_key!r}"}
            )
            return 0.0

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
        turns: list[TurnRecord],
        skill_calls: list[SkillCallRecord],
    ) -> tuple[CompletionResponse, float, float]:
        """Drive the tool-use loop for one provider in the fallback chain.

        Returns ``(final_completion, accumulated_skill_cost_usd,
        accumulated_turn_cost_usd)``. The per-turn :class:`TurnRecord`s and
        per-skill :class:`SkillCallRecord`s are APPENDED to the caller-owned
        ``turns`` / ``skill_calls`` lists in place — so when a turn raises
        mid-loop the partial trail survives for ``_handle_failure`` to persist
        (ADR 024 D2, offline-first partial record).

        ADR 024 D1 — each LLM round-trip opens a child ``agent.turn[i]`` span
        under the run's root ``agent.execute`` span; each dispatched skill
        opens a ``skill.<name>`` span under that turn. Tracing stays wired at
        the edges (CLAUDE.md §6): only the executor touches the tracer, and
        non-Langfuse/Null tracers no-op the child spans (zero cost when
        tracing is off).

        The final completion has ``kind == "final"`` and its ``text`` is
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
        accumulated_turn_cost = 0.0
        turns_taken = 0
        # 1-based index over EVERY completion (LLM round-trip), including the
        # final-answer turn — drives both the ``agent.turn[i]`` span name and
        # ``TurnRecord.index`` so each ``skill.*`` child links to the exact
        # turn that requested it (ADR 024 D1/D2).
        turn_index = 0
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
                    return await self._complete_cached(
                        provider_for_run, req, span=span, tenant_id=tenant_id
                    )
                # Streaming bypasses the cache: a hit can't be replayed
                # as a live token stream without synthesizing chunks,
                # and streaming is a UX concern (live render) rather
                # than a cost-of-repeat one. Always calls the provider.
                return await self._invoke_streaming(provider_for_run, req, on_token)

            # ADR 024 D1 — one child span per LLM round-trip, under the run's
            # root span. No-op on Null/Silent tracers (child spans are free
            # when tracing is off). The span carries the turn's model / tokens
            # / cost so the trace UI shows the per-turn breakdown.
            turn_index += 1
            turn_span = self._tracer.start_span(
                f"agent.turn[{turn_index}]",
                {"turn": turn_index, "model": provider_str},
                parent=span,
            )
            turn_t0 = time.monotonic()
            try:
                completion = await run_with_retries(_invoke)
            except BaseException:
                # End the turn span on the way out (e.g. RetryExhaustedError
                # → fallback / failure) so it never dangles unclosed.
                self._tracer.end_span(turn_span, status="error")
                raise
            turn_latency_ms = int((time.monotonic() - turn_t0) * 1000)
            # Aggregate tokens + raw so the post-loop cost path sees
            # the full picture even when multiple turns happened.
            accumulated_tokens = TokenUsage(
                input=accumulated_tokens.input + completion.tokens.input,
                output=accumulated_tokens.output + completion.tokens.output,
                cached_input=accumulated_tokens.cached_input + completion.tokens.cached_input,
                cache_write=accumulated_tokens.cache_write + completion.tokens.cache_write,
            )
            if completion.raw:
                accumulated_raw.update(completion.raw)

            # ADR 024 D5 — cost of THIS turn's completion, recorded on the
            # span + retained in the TurnRecord; summed for the run total.
            this_turn_cost = self._turn_cost(
                provider_for_run=provider_for_run,
                provider_str=provider_str,
                completion=completion,
                span=turn_span,
            )
            accumulated_turn_cost += this_turn_cost
            self._tracer.set_attribute(turn_span, "cost_usd", this_turn_cost)
            self._tracer.set_attribute(turn_span, "latency_ms", turn_latency_ms)
            self._tracer.set_attribute(turn_span, "input_tokens", completion.tokens.input)
            self._tracer.set_attribute(turn_span, "output_tokens", completion.tokens.output)
            turns.append(
                TurnRecord(
                    index=turn_index,
                    model=provider_str,
                    input_tokens=completion.tokens.input,
                    output_tokens=completion.tokens.output,
                    cost_usd=this_turn_cost,
                    latency_ms=turn_latency_ms,
                    finish_reason=completion.kind,
                )
            )

            if completion.kind == "final":
                self._tracer.end_span(turn_span, status="ok")
                # Inject the accumulated tokens + raw into the final
                # response so the downstream cost calc reads the
                # full-loop total, not just the last turn.
                return (
                    completion.model_copy(
                        update={"tokens": accumulated_tokens, "raw": accumulated_raw},
                    ),
                    accumulated_skill_cost,
                    accumulated_turn_cost,
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
                self._tracer.end_span(turn_span, status="ok")
                final = completion.model_copy(
                    update={
                        "kind": "final",
                        "tokens": accumulated_tokens,
                        "raw": accumulated_raw,
                    },
                )
                return final, accumulated_skill_cost, accumulated_turn_cost

            # Collect all tool calls for this turn.  parallel_tool_calls
            # is always populated for kind="tool_use" turns (all three
            # providers now set it); fall back to the singular fields in
            # case an older/custom provider only sets those.
            calls: list[ToolCallSpec] = completion.parallel_tool_calls or [
                ToolCallSpec(
                    name=completion.tool_name,
                    call_id=completion.tool_id,
                    input=completion.tool_input,
                )
            ]

            async def _dispatch_one_call(
                call: ToolCallSpec,
                _turn: int = turns_taken,
                _turn_index: int = turn_index,
                _turn_span: SpanCtx = turn_span,
            ) -> tuple[str, str, float, SkillCallRecord | None]:
                """Dispatch one tool call. Returns (call_id, result_json, cost_usd, record).

                Extracted from the loop body so ``asyncio.gather`` can run
                parallel calls concurrently when the model emits more than
                one tool call in a single turn (e.g. Claude Sonnet or GPT-4o
                with parallel_tool_calls enabled).
                """
                _name = call.name
                _input = call.input

                skill = skill_index.get(_name)
                if skill is None:
                    # Model invented a tool name — emit NOT_FOUND so it can
                    # recover on the next turn (surface all available names).
                    err = SkillError(
                        type=SkillErrorType.NOT_FOUND,
                        message=f"unknown tool {_name!r}; available: {sorted(skill_index)}",
                    )
                    err_result = json.dumps({"error": err.type.value, "message": err.message})
                    return call.call_id, err_result, 0.0, None

                # ADR 024 D1 — one ``skill.<name>`` child span per dispatched
                # tool call, nested under THIS turn's span. No-op on Null/Silent
                # tracers. The skill backend's own sub-spans (e.g. a KB lookup)
                # parent under this span via ``ctx.parent_span``.
                skill_span = self._tracer.start_span(
                    f"skill.{_name}",
                    {"skill": _name, "turn": _turn_index},
                    parent=_turn_span,
                )

                if skill_fixture is not None and _name in skill_fixture:
                    # Fixture short-circuit: eval provided a canned response.
                    # Cost stays zero — fixtures are deterministic stand-ins.
                    result = json.dumps(skill_fixture[_name])
                    self._tracer.log_event(span, {"skill_fixture_used": _name, "turn": _turn})
                    self._tracer.end_span(skill_span, status="ok")
                    return call.call_id, result, 0.0, None

                # Real dispatch.  Per-call budget = skill override OR agent default.
                call_ms = skill.spec.timeout_call_ms or agent_call_ms
                ctx = SkillExecutionContext(
                    trace_id=span.trace_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    call_ms_budget=call_ms,
                    agent_name=bundle.spec.name,
                    storage=self._storage,
                    retrieval=bundle.spec.retrieval,
                    tracer=self._tracer,
                    parent_span=skill_span,
                )
                _t0 = time.monotonic()
                try:
                    output = await dispatch_skill(skill, _input, ctx)
                    lat = (time.monotonic() - _t0) * 1000
                    result = json.dumps(output)
                    cost = skill.spec.cost.per_call_usd
                    self._tracer.log_event(
                        span, {"skill_invoked": _name, "skill_cost_usd": cost, "turn": _turn}
                    )
                    self._tracer.set_attribute(skill_span, "cost_usd", cost)
                    self._tracer.set_attribute(skill_span, "latency_ms", round(lat, 1))
                    self._tracer.end_span(skill_span, status="ok")
                    rec = SkillCallRecord(
                        step=_turn,
                        skill=_name,
                        input=_input,
                        output=output,
                        latency_ms=round(lat, 1),
                        cost_usd=cost,
                        turn=_turn_index,
                    )
                    return call.call_id, result, cost, rec
                except SkillError as exc:
                    lat = (time.monotonic() - _t0) * 1000
                    result = json.dumps({"error": exc.type.value, "message": exc.message})
                    self._tracer.log_event(
                        span,
                        {"skill_error": _name, "skill_error_type": exc.type.value, "turn": _turn},
                    )
                    self._tracer.set_attribute(skill_span, "latency_ms", round(lat, 1))
                    self._tracer.end_span(skill_span, status="error")
                    rec = SkillCallRecord(
                        step=_turn,
                        skill=_name,
                        input=_input,
                        error=f"{exc.type.value}: {exc.message}",
                        latency_ms=round(lat, 1),
                        cost_usd=0.0,
                        turn=_turn_index,
                    )
                    return call.call_id, result, 0.0, rec

            # Dispatch: parallel when model issued multiple calls in one
            # turn, sequential (still uses gather for code-path unity)
            # for single calls.
            if len(calls) > 1:
                self._tracer.log_event(
                    span, {"parallel_tool_calls": len(calls), "turn": turns_taken}
                )
            dispatch_results = list(await asyncio.gather(*[_dispatch_one_call(c) for c in calls]))

            # Accumulate costs and records from all dispatched calls. The
            # records are appended to the caller-owned ``skill_calls`` list so
            # a later mid-loop failure still persists this turn's partial trail.
            for _cid, _res, _cost, _rec in dispatch_results:
                accumulated_skill_cost += _cost
                if _rec is not None:
                    skill_calls.append(_rec)

            # This tool-use turn is complete (all its skills dispatched).
            self._tracer.end_span(turn_span, status="ok")

            # Append one assistant turn carrying ALL tool calls, then one
            # tool result message per call.  The OpenAI / Anthropic spec
            # requires this exact structure so the model can correlate
            # each result to its originating call.
            assistant_turn = Message(
                role="assistant",
                content=completion.text or "",
                tool_calls=[
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.input),
                        },
                    }
                    for c in calls
                ],
            )
            tool_turns = [
                Message(role="tool", content=_res, tool_call_id=_cid)
                for _cid, _res, _cost, _rec in dispatch_results
            ]
            messages.extend([assistant_turn, *tool_turns])

    async def _complete_cached(
        self,
        provider: BaseLLMProvider,
        req: CompletionRequest,
        *,
        span: SpanCtx,
        tenant_id: str,
    ) -> CompletionResponse:
        """Thin read-through cache wrapper around ``provider.complete``.

        Only deterministic calls (``temperature == 0``, per
        :func:`movate.core.cache.is_cacheable`) are eligible — a
        sampled (``temperature > 0``) response is one draw from a
        distribution and replaying it would be wrong, so those calls
        skip the cache entirely and always hit the provider.

        Cache key = sha256 of the request signature (provider +
        rendered messages + params + tools) folded with ``tenant_id``
        so there's no cross-tenant leakage. On a HIT we rebuild a
        :class:`CompletionResponse` from the stored value, log a
        ``llm_cache.hit`` tracer event (so the hit is observable), and
        return without calling the provider — the downstream cost
        calc reads the stored token usage but the run pays nothing for
        this call (no provider round-trip). On a MISS we call the
        provider and store the result under the same key.

        With the default :class:`NoOpCache` this collapses to a plain
        ``provider.complete`` call (get is always-miss, set is a
        no-op) — byte-for-byte unchanged behavior.
        """
        if not is_cacheable(req.params) or req.tools:
            # Non-deterministic (sampled) call — never cached. Tool-use
            # requests (``req.tools`` set) also bypass: a cached entry
            # is a flat text/tokens value, so replaying it would lose
            # the tool_use kind/call fields and corrupt the loop. The
            # single-shot final-answer path — by far the common, most
            # repeated call — is what we cache.
            return await provider.complete(req)

        key = compute_cache_key(
            provider=req.provider,
            messages=req.messages,
            params=req.params,
            tools=req.tools,
            tenant_id=tenant_id,
        )
        cached = self._cache.get(key)
        if cached is not None:
            # HIT — replay the stored completion. Mark it on the raw
            # payload so the cost path (and traces) can see this call
            # was free, and log an observable tracer event.
            self._tracer.log_event(
                span,
                {
                    "llm_cache.hit": True,
                    "llm_cache.backend": getattr(self._cache, "name", "unknown"),
                    "llm_cache.provider": req.provider,
                },
            )
            raw = dict(cached.raw)
            raw["llm_cache_hit"] = True
            return CompletionResponse(text=cached.text, tokens=cached.tokens, raw=raw)

        # MISS — call the provider, then store the result for next time.
        completion = await provider.complete(req)
        self._cache.set(
            key,
            CachedResponse(text=completion.text, tokens=completion.tokens, raw=completion.raw),
            ttl_s=self._cache_ttl_s,
        )
        self._tracer.log_event(
            span,
            {
                "llm_cache.miss": True,
                "llm_cache.backend": getattr(self._cache, "name", "unknown"),
                "llm_cache.provider": req.provider,
            },
        )
        return completion

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
        spec: AgentSpec,
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
        reflect_cfg = spec.reflection
        # Resolve the judge provider. For MVP we route every judge
        # call through LiteLLM regardless of the agent's runtime —
        # ``reflection.judge_model`` is documented as a LiteLLM-style
        # ``<family>/<model>`` string. Native-SDK judges land later
        # alongside the equivalent agent-side adapters.
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

    async def _inject_tenant_provider_keys(
        self,
        chain: list[tuple[str, dict[str, Any]]],
        tenant_id: str,
        runtime: AgentRuntime,
    ) -> None:
        """Thread the tenant's BYOK provider key into each chain entry (ADR 018).

        For each ``(provider_str, params)`` in the fallback chain, resolves
        the calling tenant's own provider key (tenant-key-first, shared-key
        fallback per :class:`ProviderKeyResolver`) and, when a tenant key is
        found, sets ``params["api_key"]`` so the provider call uses it. When
        the resolver returns ``None`` (no tenant key + shared fallback on, the
        default) **nothing** is added — the provider uses its env-default key
        exactly as before BYOK, so the no-config run path is byte-for-byte
        unchanged.

        Only the **LiteLLM** runtime is wired today: ``litellm.acompletion``
        honours a per-call ``api_key=`` kwarg (passed through from ``params``).
        The native_openai / native_anthropic SDKs construct their client from
        the env at startup and don't accept a per-call key, so a BYOK key
        can't be threaded through their existing surface without a provider
        signature change (out of scope here) — those runtimes fall through to
        the env default unchanged. An agent that pre-set ``api_key`` in its
        own ``model.params`` is left untouched (explicit config wins).

        Mutates the ``params`` dicts in ``chain`` in place. The resolved
        plaintext key lives only in the params dict for the duration of the
        provider call and is never logged or persisted.
        """
        # Only LiteLLM can take a per-call api_key today (see docstring). Skip
        # entirely for native runtimes → byte-for-byte unchanged.
        if runtime != AgentRuntime.LITELLM:
            return
        if self._provider_key_resolver is None:
            self._provider_key_resolver = ProviderKeyResolver(self._storage)
        for provider_str, params in chain:
            # Respect an explicit api_key the agent baked into model.params —
            # never override operator-set config.
            if params.get("api_key"):
                continue
            resolved = await self._provider_key_resolver.resolve(tenant_id, provider_str)
            if resolved is not None:
                params["api_key"] = resolved

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
                "cost cross-check: litellm=$%.6f differs >%.0f%% from canonical "
                "pricing-table=$%.6f (the table is authoritative for billing; "
                "litellm's bundled prices can lag — refresh providers/pricing.yaml "
                "only if the table itself is stale)",
                litellm_cost,
                _COST_DRIFT_THRESHOLD * 100,
                our_cost,
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
        skill_calls: list[SkillCallRecord] | None = None,
        turns: list[TurnRecord] | None = None,
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
            skill_calls=skill_calls or [],
            # ADR 024 D2 — per-turn breakdown retained alongside skill_calls
            # so `mdk explain` reconstructs the step tree without a backend.
            turns=turns or [],
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
        skill_calls: list[SkillCallRecord] | None = None,
        turns: list[TurnRecord] | None = None,
    ) -> RunResponse:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _safety_types = {"content_filter", "grounding_violation"}
        status: Literal["safety_blocked", "error"] = (
            "safety_blocked" if err.failure_type.value in _safety_types else "error"
        )
        info = ErrorInfo(type=err.failure_type.value, message=str(err), retryable=err.retryable)
        self._tracer.log_event(span, {"error": info.model_dump()})
        self._tracer.end_span(span, status="error")

        metrics = Metrics(latency_ms=elapsed_ms, tokens=TokenUsage(), trace_id=span.trace_id)

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

        # ADR 024 D2 (test-matrix case 7) — when the run failed mid-loop AFTER
        # capturing some turns / skill calls, persist a partial ERROR RunRecord
        # so `mdk explain` can show the failure point and the steps taken so
        # far. Only when there IS a partial trail — the common no-skill /
        # pre-loop failure path stays unchanged (FailureRecord only, no
        # RunRecord), preserving prior behavior + the tenant-budget aggregate
        # (the partial record's ``metrics.cost_usd`` is left at 0; the per-step
        # costs live on the retained turn/skill records, not the run aggregate).
        skill_calls = skill_calls or []
        turns = turns or []
        if skill_calls or turns:
            try:
                await self._storage.save_run(
                    RunRecord(
                        run_id=run_id,
                        job_id=job_id,
                        tenant_id=tenant_id,
                        agent=bundle.spec.name,
                        agent_version=bundle.spec.version,
                        prompt_hash=bundle.prompt_hash,
                        provider=bundle.spec.model.provider,
                        provider_version=self._registry.get(bundle.spec.runtime).version,
                        pricing_version=self._pricing.version,
                        status=JobStatus(status),
                        input={},
                        output=None,
                        metrics=metrics,
                        error=info,
                        skill_calls=skill_calls,
                        turns=turns,
                    )
                )
            except Exception:  # broad-catch: persistence must not mask the real error
                log.warning(
                    "partial RunRecord persist failed for run %r — failure surfaced anyway",
                    run_id,
                )
        # job_id reserved for the workflow + server phases.
        _ = job_id

        return RunResponse(
            status=status,
            run_id=run_id,
            data={},
            human_readable=f"**Error**: {err}",
            trace_id=span.trace_id,
            metrics=metrics,
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
