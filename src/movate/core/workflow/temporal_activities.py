"""Temporal activity wrappers — Track C of ADR 054 Phase 1.

These are the four activity functions the Track-B compiler
(:mod:`movate.core.workflow.compilers.temporal`) emits ``execute_activity``
calls against *by name*:

* :func:`call_agent_activity`  — AGENT node       (compiler ``_emit_agent_node``)
* :func:`call_skill_activity`  — SKILL/TOOL node  (compiler ``_emit_skill_node``)
* :func:`call_gate_activity`   — GATE / INTENT_ROUTER (compiler ``_emit_gate_node``)
* :func:`call_judge_activity`  — JUDGE node       (compiler ``_emit_judge_node``)

[bold]Execution-model reuse (ADR 054 D3).[/bold] Each activity is a *thin
shim*: it forwards to the SAME :class:`movate.core.executor.Executor` /
:func:`movate.core.skill_backend.base.dispatch_skill` the native runner
(:mod:`movate.core.workflow.runner`) calls. There is no second execution
model, no Temporal-specific Executor, no bypass route. Tracing (ADR 024),
metering (ADR 036 — see D11 below), session state (ADR 045 D10 — see D10
below) and BYOK (ADR 018) all flow through that one place automatically,
because the Executor is the one place they are wired.

[bold]Metering (ADR 054 D11).[/bold] Metering wraps *the activity* by
construction: the Executor meters every ``execute(...)`` and every skill
dispatch already. Temporal's automatic retries re-invoke the activity, and
each attempt is metered as its own attempt — so this module builds **no new
meter and no idempotency / dedup code**. The ADR explicitly forbids bespoke
retry/idempotency here; Temporal's retry policy (emitted by the compiler)
owns retries.

[bold]Sessions (ADR 054 D10).[/bold] Conversation state lives in the session
store and is read/written by the Executor; Temporal history holds only
control flow (the ``state`` dict the workflow threads). Because the activity
reuses the Executor (D3), the session remains the conversation's home with
**no new wiring** here.

[bold]Timeouts / heartbeating (ADR 054 D9).[/bold] ``schedule_to_close_timeout``,
``heartbeat_timeout`` and the retry policy are set by the WORKFLOW (the
compiler emits them per ``execute_activity`` call). The activity body itself
just executes. Phase 3 adds activity-side heartbeating for long LLM calls
(D9 / Phase 3 row) — Phase 1 deliberately ships none.

[bold]Import isolation (ADR 054 D7).[/bold] ``temporalio`` is imported
defensively at module scope: when the ``[temporal]`` extra is absent the
``@activity.defn`` decorator degrades to an identity decorator so this module
still imports and the four functions stay plain async callables (tests call
them directly). The worker path goes through :func:`_require_temporalio`,
which fails loud with an install hint. This mirrors the compiler's contract,
asserted by ``test_lazy_temporalio_import`` for the compiler and by the Track
C import-safety test for this module.

[bold]Dependency injection.[/bold] The four activities are bare module-level
functions (the compiler imports them by name), but they need an
:class:`Executor`. :func:`configure_activities` installs an
:class:`ActivityContext` into a module global at worker startup
(``mdk worker --backend temporal``, a later track); :func:`_get_context`
reads it. An activity invoked before configuration raises a clear
``RuntimeError`` rather than silently building an unconfigured Executor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only, never imported at runtime here.
    from movate.providers.base import BaseLLMProvider
    from movate.providers.pricing import PricingTable
    from movate.storage.base import StorageProvider
    from movate.tracing.base import Tracer

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defensive temporalio import (ADR 054 D7 — import isolation).
#
# When the [temporal] extra is installed we use the real ``@activity.defn``
# decorator so the worker can register these functions. When it is absent we
# fall back to an identity decorator so this module still imports and the four
# functions remain plain async callables (the native install never pays for
# temporalio, and the unit tests can call the activities directly).
# ---------------------------------------------------------------------------

# Declared ``Any`` so the two bindings below (the real ``temporalio.activity``
# module vs the identity fallback) unify, and so ``@_activity.defn`` type-checks
# in both the extra-present and extra-absent mypy environments.
_activity: Any

try:
    from temporalio import activity as _activity  # module-scope, guarded by try/except

    _HAVE_TEMPORALIO = True
except ImportError:  # pragma: no cover — exercised by the import-safety test.
    _HAVE_TEMPORALIO = False

    class _IdentityActivity:
        """Stand-in for ``temporalio.activity`` when the extra is absent.

        Only ``defn`` is referenced at module-import time. It returns the
        function unchanged so the four activities stay plain async callables.
        """

        @staticmethod
        def defn(fn: Any) -> Any:
            return fn

    _activity = _IdentityActivity()


def _require_temporalio() -> Any:
    """Return the live ``temporalio.activity`` module or fail with an install hint.

    The worker path (``mdk worker --backend temporal``, a later track) calls
    this before registering the activities so an operator without the
    ``[temporal]`` extra gets the install instruction immediately rather than
    an obscure ``ImportError`` at registration time. Mirrors the compiler's
    :func:`movate.core.workflow.compilers.temporal._require_temporalio`.
    """
    if not _HAVE_TEMPORALIO:
        raise RuntimeError(
            "The [temporal] extra is not installed. "
            "Install with: uv tool install --editable '.[temporal]' --force"
        )
    return _activity


# ---------------------------------------------------------------------------
# Dependency injection — the activity context.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityContext:
    """The dependencies the four activities forward into.

    Built once at worker startup by :func:`configure_activities` and stored in
    a module global. Frozen so a worker can't mutate it out from under an
    in-flight activity. Mirrors the worker-side Executor construction in
    :mod:`movate.runtime.dispatch` (provider + pricing + tracer + storage +
    tenant_id) so the Temporal worker builds the SAME Executor the job-queue
    worker does (ADR 054 D3 — one execution model).
    """

    storage: StorageProvider
    pricing: PricingTable
    tracer: Tracer
    provider: BaseLLMProvider
    tenant_id: str = "local"
    defaults: Any = None
    """Optional :class:`movate.core.layered_defaults.AgentDefaults` threaded
    into :func:`load_agent` resolution (project-level model defaults). ``None``
    (the default) lets the loader read project config itself, matching every
    other library caller."""


_CONTEXT: ActivityContext | None = None


def configure_activities(
    *,
    storage: StorageProvider,
    pricing: PricingTable | None = None,
    tracer: Tracer | None = None,
    provider: BaseLLMProvider | None = None,
    tenant_id: str = "local",
    defaults: Any = None,
) -> None:
    """Install the :class:`ActivityContext` the four activities read.

    Called once at worker startup (``mdk worker --backend temporal``, a later
    track). Mirrors :mod:`movate.runtime.dispatch`'s Executor wiring:

    * ``pricing`` defaults to :func:`movate.providers.pricing.load_pricing`.
    * ``tracer`` defaults to :func:`movate.tracing.build_tracer`.
    * ``provider`` defaults to :class:`movate.providers.litellm.LiteLLMProvider`.
      A node whose ``state`` carries a truthy ``mock`` flag swaps in
      :class:`movate.providers.mock.MockProvider` per-activity (matching
      dispatch.py's mock handling) — see :func:`_executor_for`.

    Idempotent — re-calling replaces the context (workers re-register cleanly).
    """
    # Local imports keep this module import-cheap and avoid import cycles —
    # the rest of core/workflow follows the same convention.
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.tracing import build_tracer  # noqa: PLC0415

    if provider is None:
        from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

        provider = LiteLLMProvider()

    global _CONTEXT  # noqa: PLW0603 — module-global DI registry, set once at worker startup.
    _CONTEXT = ActivityContext(
        storage=storage,
        pricing=pricing if pricing is not None else load_pricing(),
        tracer=tracer if tracer is not None else build_tracer(),
        provider=provider,
        tenant_id=tenant_id,
        defaults=defaults,
    )


def _get_context() -> ActivityContext:
    """Return the configured :class:`ActivityContext` or fail loud.

    Raises ``RuntimeError`` if :func:`configure_activities` was never called —
    an activity must never silently run against a half-built Executor.
    """
    if _CONTEXT is None:
        raise RuntimeError(
            "Temporal activities are not configured. The worker must call "
            "movate.core.workflow.temporal_activities.configure_activities(...) "
            "at startup before any activity runs (ADR 054 D3)."
        )
    return _CONTEXT


def _resolve_tenant_id(ctx: ActivityContext, state: dict[str, Any]) -> str:
    """Resolve the tenant for this activity.

    A ``tenant_id`` carried in ``state`` wins (a multi-tenant worker stamps the
    right tenant per run, the same belt-and-braces the runner uses via
    ``tenant_id_override``); otherwise fall back to the context default.
    """
    state_tenant = state.get("tenant_id")
    if isinstance(state_tenant, str) and state_tenant:
        return state_tenant
    return ctx.tenant_id


def _executor_for(ctx: ActivityContext, state: dict[str, Any]) -> Any:
    """Build the Executor for one activity, mirroring ``dispatch.py``.

    A truthy ``mock`` flag in ``state`` swaps :class:`MockProvider` in for the
    configured provider (matching dispatch.py's per-job mock handling) so a
    ``mdk run --mock`` style state runs the whole Temporal pipeline without
    real spend. Everything else (pricing, tracer, storage, tenant) comes from
    the context.
    """
    from movate.core.executor import Executor  # noqa: PLC0415

    provider = ctx.provider
    if state.get("mock"):
        from movate.providers.mock import MockProvider  # noqa: PLC0415

        provider = MockProvider()

    return Executor(
        provider=provider,
        pricing=ctx.pricing,
        storage=ctx.storage,
        tracer=ctx.tracer,
        tenant_id=_resolve_tenant_id(ctx, state),
    )


def _project_state(state: dict[str, Any], bundle: Any) -> dict[str, Any]:
    """Filter ``state`` to the keys the agent's input schema names.

    A faithful copy of :func:`movate.core.workflow.runner._project_state` so
    the activity feeds the agent the SAME narrowed input the native runner
    would (no behavioral drift between backends). If the schema lists no
    ``properties`` the whole state is passed.
    """
    props = bundle.input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return dict(state)
    return {k: state[k] for k in props if k in state}


# ---------------------------------------------------------------------------
# The four activities (compiler-emitted call targets — ADR 054 D4).
#
# Arg order is fixed by the compiler's ``execute_activity(..., args=[...])``
# calls — do NOT reorder without updating compilers/temporal.py in lockstep.
# ---------------------------------------------------------------------------


@_activity.defn  # type: ignore[untyped-decorator]
async def call_agent_activity(
    node_id: str,
    ref: str,
    state: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    """AGENT node → run the agent through the Executor; return ``response.data``.

    Compiler contract (``_emit_agent_node``): the workflow does
    ``state.update(<result>)`` with this return value, so we return the
    agent's output dict (``RunResponse.data``) exactly as the native runner
    merges it (``runner.py`` ~L480).

    ADR 054 D3: forwards to ``Executor.execute(...)`` — the same call the
    native runner makes — with ``workflow_run_id`` + ``node_id`` stamped so
    the per-node RunRecord stitches to this Temporal workflow (D6: workflow
    id == run_id). D11: the Executor meters the call, so Temporal retries are
    metered per-attempt with no bespoke meter here.
    """
    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.core.models import RunRequest  # noqa: PLC0415

    ctx = _get_context()
    bundle = load_agent(ref, defaults=ctx.defaults)
    agent_input = _project_state(state, bundle)
    executor = _executor_for(ctx, state)

    response = await executor.execute(
        bundle,
        RunRequest(agent=bundle.spec.name, input=agent_input),
        workflow_run_id=run_id,
        node_id=node_id,
        tenant_id_override=_resolve_tenant_id(ctx, state),
    )
    if response.status != "success":
        # Surface as an exception so Temporal's retry policy (emitted by the
        # compiler) can retry per D11/D4, instead of silently merging a
        # failed run's (empty) data into workflow state.
        raise RuntimeError(
            f"agent node {node_id!r} ({ref}) failed: "
            f"{response.error.message if response.error else response.status}"
        )
    return dict(response.data)


@_activity.defn  # type: ignore[untyped-decorator]
async def call_skill_activity(
    node_id: str,
    ref: str,
    state: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    """SKILL node → dispatch the skill through ``SkillBackend``; return its output.

    Compiler contract (``_emit_skill_node``): the workflow does
    ``state.update(<result>)``, so we return the skill's output dict.

    ADR 054 D3: dispatches through the SAME
    :func:`movate.core.skill_backend.base.dispatch_skill` the Executor's
    tool-use loop uses — no second skill path. ``ref`` is the skill directory
    (the compiler emits ``node.ref``; for a SKILL/TOOL IR node that is the
    skill dir, resolved exactly like the agent loader resolves an agent dir).
    The skill input is the current ``state`` narrowed to the skill's input
    schema, same projection rule as agents.

    Note: standalone SKILL/TOOL workflow nodes are a v1.1 IR type the spec
    validator does not yet accept (``ir.py`` — TOOL is declared but rejected),
    so this path is reachable in Phase 1 only via a hand-built graph or a
    future IR. It is wired against the real ``dispatch_skill`` so it lights up
    for free when TOOL nodes ship — no stub.
    """
    from movate.core.skill_backend.base import (  # noqa: PLC0415
        SkillExecutionContext,
        dispatch_skill,
    )
    from movate.core.skill_loader import load_skill  # noqa: PLC0415

    ctx = _get_context()
    skill = load_skill(ref)

    # Narrow state to the skill's declared input (same projection rule as
    # agents) so the skill sees a contract-shaped input, not the whole state.
    props = skill.input_schema.get("properties")
    if isinstance(props, dict) and props:
        skill_input = {k: state[k] for k in props if k in state}
    else:
        skill_input = dict(state)

    tenant_id = _resolve_tenant_id(ctx, state)
    skill_ctx = SkillExecutionContext(
        run_id=run_id,
        tenant_id=tenant_id,
        mock=bool(state.get("mock")),
        storage=ctx.storage,
        tracer=ctx.tracer,
    )
    output = await dispatch_skill(skill, skill_input, skill_ctx)
    return dict(output)


def _resolve_classifier_ref(classifier_agent: str, workflow_dir: str) -> str:
    """Resolve a possibly-relative classifier ref against the workflow dir.

    Mirrors the native runner's intent-router resolution
    (:meth:`movate.core.workflow.runner.WorkflowRunner._run_intent_router`): an
    absolute ref is used as-is; a relative ref (e.g. ``./agents/goal-judge`` —
    the form the IR leaves ``classifier_agent`` in, unlike the absolutized AGENT
    ``node.ref``) is resolved against the compiled workflow's source dir, which
    the Track-B compiler bakes into the activity args (``_emit_gate_node``).

    Without this the worker would call :func:`load_agent` on a CWD-relative ref
    and raise ``AgentLoadError`` — the exact Phase-1 divergence the conformance
    suite caught (ADR 055 D7). ``workflow_dir`` empty (a hand-built direct call)
    falls back to passing the ref through unchanged.
    """
    from pathlib import Path  # noqa: PLC0415

    if not classifier_agent or not workflow_dir:
        return classifier_agent
    ref_path = Path(classifier_agent)
    if ref_path.is_absolute():
        return classifier_agent
    return str((Path(workflow_dir) / classifier_agent).resolve())


@_activity.defn  # type: ignore[untyped-decorator]
async def call_gate_activity(
    node_id: str,
    classifier_agent: str,
    state: dict[str, Any],
    run_id: str,
    workflow_dir: str = "",
    input_field: str = "",
    route_labels: list[str] | None = None,
) -> dict[str, Any]:
    """GATE / INTENT_ROUTER node → run the classifier agent; return its decision.

    Compiler contract (``_emit_gate_node``): the workflow branches on this
    return value's ``"label"`` (``current = routes[label] or fallback``) — the
    ``routes`` / ``fallback`` stay in workflow scope. So this activity's job is
    narrow: run the classifier agent and hand back its decision dict (which
    carries ``"label"``; see ``pattern_simulation``'s ``turn-judge`` output
    schema). The workflow then maps that label to the next node and does NOT
    merge the decision into state (native parity).

    The compiler passes ``(node_id, classifier_agent, state, run_id,
    workflow_dir, input_field, route_labels)``:

    * ``workflow_dir`` is the compiled workflow's source dir, used to resolve a
      relative ``classifier_agent`` ref the same way the native runner does
      (see :func:`_resolve_classifier_ref`).
    * ``input_field`` + ``route_labels`` build the classifier input
      ``{"text": str(state[input_field]), "labels": route_labels}`` — the EXACT
      shape the native runner sends (``runner._run_intent_router``), which the
      classifier agents' input schemas require (``text`` + ``labels``). They
      default so a direct caller passing an already-absolute ref needs only the
      original four args.

    Mirrors the native runner's intent-router path (``runner._run_intent_router``)
    but without the routing table (which the workflow owns): the classifier
    runs through the SAME Executor (ADR 054 D3) and its ``response.data``
    (carrying ``label``) is returned verbatim.
    """
    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.core.models import RunRequest  # noqa: PLC0415

    ctx = _get_context()
    clf_ref = _resolve_classifier_ref(classifier_agent, workflow_dir)
    bundle = load_agent(clf_ref, defaults=ctx.defaults)
    # Build the classifier input the SAME way the native runner does
    # (runner._run_intent_router): {text: <state[input_field]>, labels: <route
    # keys>}. The classifier agents' input schemas require both keys, so the
    # _project_state projection used for agent nodes is the wrong builder here.
    clf_input = {"text": str(state.get(input_field, "")), "labels": list(route_labels or [])}
    executor = _executor_for(ctx, state)

    response = await executor.execute(
        bundle,
        RunRequest(agent=bundle.spec.name, input=clf_input),
        workflow_run_id=run_id,
        node_id=node_id,
        tenant_id_override=_resolve_tenant_id(ctx, state),
    )
    if response.status != "success":
        raise RuntimeError(
            f"gate node {node_id!r} classifier {classifier_agent!r} failed: "
            f"{response.error.message if response.error else response.status}"
        )
    # Return the classifier's decision dict verbatim — it carries "label",
    # which the generated workflow branches on (recorded in history per D4).
    return dict(response.data)


@_activity.defn  # type: ignore[untyped-decorator]
async def call_judge_activity(
    node_id: str,
    judge_ref: str,
    judge_config: dict[str, Any],
    state: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    """JUDGE node → run the judge through the Executor; return the D2 verdict.

    Compiler contract (``_emit_judge_node``): the workflow does
    ``if <verdict>.get('terminate'): return state``, so we return the canonical
    ADR 056 D2 verdict dict ``{verdict, score, feedback, terminate}``.

    [bold]Resolves the ADR 054 §11 caveat.[/bold] Track C originally shipped
    this activity as a *state interpreter* (it read a ``verdict``/``label``
    already in ``state``) because the IR carried no JUDGE node and the compiler
    passed no agent ref. ADR 056 adds the JUDGE node + ref, so this activity
    now RUNS a real judge: it loads the judge bundle (``judge_ref`` or inline
    ``judge_config['criteria']``) and forwards to the SAME ``Executor.execute``
    the native runner uses (ADR 054 D3 / ADR 056 D3) — one execution model,
    tracing + metering + BYOK at the edges, no second judge engine. The verdict
    parsing + ``terminate`` derivation reuse ``movate.core.workflow.judge`` so
    the native runner and this activity arrive at the SAME verdict for the same
    judge output (the conformance contract, ADR 055 D7).

    Arg order is fixed by the compiler's ``_emit_judge_node`` (do NOT reorder
    without updating ``compilers/temporal.py`` in lockstep). ``judge_config``
    carries ``criteria`` / ``input_field`` / ``pass_threshold`` (the routing
    legs stay in workflow scope — the workflow gates on ``terminate``).
    """
    from movate.core.models import RunRequest  # noqa: PLC0415
    from movate.core.workflow.judge import (  # noqa: PLC0415
        build_judge_state_value,
        derive_terminate,
        load_judge_bundle,
        verdict_from_response_data,
    )

    ctx = _get_context()
    criteria = str(judge_config.get("criteria") or "")
    input_field = str(judge_config.get("input_field") or "text")
    pass_threshold = judge_config.get("pass_threshold")

    bundle = load_judge_bundle(judge_ref=judge_ref, criteria=criteria, defaults=ctx.defaults)
    artifact = state.get(input_field, "")
    judge_input = _project_state({"text": str(artifact)}, bundle)
    executor = _executor_for(ctx, state)

    response = await executor.execute(
        bundle,
        RunRequest(agent=bundle.spec.name, input=judge_input),
        workflow_run_id=run_id,
        node_id=node_id,
        tenant_id_override=_resolve_tenant_id(ctx, state),
    )
    if response.status != "success":
        # Surface as an exception so Temporal's retry policy (emitted by the
        # compiler) can retry, mirroring the agent activity's posture.
        raise RuntimeError(
            f"judge node {node_id!r} ({judge_ref or 'inline-criteria'}) failed: "
            f"{response.error.message if response.error else response.status}"
        )

    verdict, score, feedback = verdict_from_response_data(response.data)
    terminate = derive_terminate(
        verdict=verdict,
        score=score,
        pass_threshold=pass_threshold if isinstance(pass_threshold, (int, float)) else None,
    )
    return build_judge_state_value(
        verdict=verdict, score=score, feedback=feedback, terminate=terminate
    )


@_activity.defn  # type: ignore[untyped-decorator]
async def call_human_activity(
    node_id: str,
    state: dict[str, Any],
    run_id: str,
    prompt: str,
    output_contract: list[str],
    approvers: list[str],
    workflow_name: str,
    workflow_version: str,
) -> None:
    """HUMAN node → persist a durable awaiting-human pause record (ADR 062).

    Compiler contract (``_emit_human_node``): the workflow calls this activity,
    then parks on ``workflow.wait_condition`` until a ``human_response`` signal
    arrives. This activity records the pause in the mdk store so an operator can
    list it (``GET /workflow-runs?status=paused``) and a transport can render
    the approval; the HTTP signal endpoint (``POST /workflow-runs/{id}/signal``)
    reads this record, validates the decision against ``output_contract``, then
    signals the Temporal handle — which resolves the ``wait_condition``.

    Mirrors the native runner's pause write (``runner.py`` HUMAN branch): same
    PAUSED status, ``paused_node_id``, ``paused_state`` and ``human_task`` shape
    — plus ``runtime='temporal'`` so the signal endpoint routes the resume to
    the Temporal handle instead of enqueuing a native re-walk (ADR 062 D2). The
    activity returns nothing; its effect is the persisted checkpoint.
    """
    from movate.core.models import WorkflowRunRecord, WorkflowStatus  # noqa: PLC0415

    ctx = _get_context()
    human_task = {
        "prompt": prompt,
        "output_contract": list(output_contract),
        "approvers": list(approvers),
    }
    record = WorkflowRunRecord(
        workflow_run_id=run_id,
        tenant_id=_resolve_tenant_id(ctx, state),
        workflow=workflow_name,
        workflow_version=workflow_version,
        status=WorkflowStatus.PAUSED,
        initial_state=dict(state),
        final_state=dict(state),
        paused_node_id=node_id,
        paused_state=dict(state),
        human_task=human_task,
        runtime="temporal",
    )
    await ctx.storage.save_workflow_run(record)

    # Escalate to the approval channel (ADR 083) — parity with the native
    # runner's HUMAN-pause branch. Fire-and-forget + never raises (side effects
    # in activities, ADR 054 D10; the pause is already persisted). No-op until
    # MOVATE_NOTIFIER is configured.
    from movate.core.notifier import HumanPause, notify_human_pause_safe  # noqa: PLC0415

    await notify_human_pause_safe(
        HumanPause(
            run_id=run_id,
            workflow_name=workflow_name,
            workflow_version=workflow_version,
            node_id=node_id,
            prompt=prompt,
            output_contract=list(output_contract),
            approvers=list(approvers),
            tenant_id=record.tenant_id,
            runtime="temporal",
        )
    )


@_activity.defn  # type: ignore[untyped-decorator]
async def persist_workflow_result_activity(
    run_id: str,
    status: str,
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
    error: str | None,
    workflow_name: str,
    workflow_version: str,
    duration_ms: float | None = None,
) -> None:
    """Write the TERMINAL ``WorkflowRunRecord`` for a Temporal run (ADR 080 D2).

    The compiler emits a call to this around the workflow body: on success (and,
    via a handled exception, on error) the workflow persists its terminal state
    to the mdk store so ``mdk runs show`` is accurate and a resumed HITL run is
    flipped out of ``PAUSED`` (clearing the ``?status=paused`` approvals list).
    The native runner writes this record at end-of-run; the long-lived Temporal
    worker has no per-workflow completion callback, so the workflow persists its
    own terminal state from within an activity (side effects in activities,
    ADR 054 D10). Upserts on ``workflow_run_id`` — overwriting any prior PAUSED
    checkpoint under the same id (``run_id`` == the Temporal workflow id, D6).
    """
    from movate.core.models import (  # noqa: PLC0415
        ErrorInfo,
        WorkflowRunRecord,
        WorkflowStatus,
    )

    ctx = _get_context()
    record = WorkflowRunRecord(
        workflow_run_id=run_id,
        tenant_id=_resolve_tenant_id(ctx, final_state),
        workflow=workflow_name,
        workflow_version=workflow_version,
        status=WorkflowStatus(status),
        initial_state=dict(initial_state),
        final_state=dict(final_state),
        error=(
            ErrorInfo(type="temporal_workflow_error", message=error) if error is not None else None
        ),
        runtime="temporal",
    )
    await ctx.storage.save_workflow_run(record)

    # Operational signal (ADR 082): durable workflows never hit the native
    # dispatch edge that powers mdk.jobs.completed, so emit a first-class
    # completion counter here for the Temporal workbook. Fail-soft + lazy import
    # so a metrics hiccup never fails the terminal persist (the record above is
    # the source of truth; the metric is best-effort telemetry). No-op when the
    # OTLP sink is off / OTel absent.
    try:
        from movate.tracing import (  # noqa: PLC0415
            record_workflow_completed,
            record_workflow_duration,
        )

        record_workflow_completed(
            workflow=workflow_name,
            status=record.status.value,
            runtime="temporal",
            tenant_id=record.tenant_id,
        )
        # Latency companion (ADR 082 follow-on): the compiled workflow computes
        # duration_ms from workflow.info().start_time → workflow.now() (both
        # deterministic) and passes it here. Older compiled workflows (no
        # duration arg) pass None → skip; never fabricate a value.
        if duration_ms is not None:
            record_workflow_duration(
                workflow=workflow_name,
                status=record.status.value,
                runtime="temporal",
                tenant_id=record.tenant_id,
                duration_ms=duration_ms,
            )
    except Exception:  # pragma: no cover - telemetry must never break execution
        pass


__all__ = [
    "ActivityContext",
    "call_agent_activity",
    "call_gate_activity",
    "call_human_activity",
    "call_judge_activity",
    "call_skill_activity",
    "configure_activities",
    "persist_workflow_result_activity",
]
