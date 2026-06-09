"""Workflow runtime-selection seam — the single dispatch fork (ADR 055 D2).

This is the **one** place that knows more than one workflow execution backend
exists. Both the local run path (:mod:`movate.cli.run`) and the worker job
path (:mod:`movate.runtime.dispatch`) route through here, so override
precedence (D3), the fail-loud availability rule (D6), and the Temporal
connection read (D5) are decided once — not re-derived per call site.

Selection (per ADR 055 D2/D3), effective runtime = ``override or graph.runtime``:

* ``native``   → the caller runs today's :class:`WorkflowRunner` unchanged
  (no compile). This module is **not** on the native path — the caller
  branches to the runner directly so the default path is byte-for-byte
  untouched and pays zero import cost here.
* ``temporal`` → :func:`run_temporal_workflow` compiles via Track B
  (:class:`TemporalCompiler`) and executes on Temporal via Track C activities.
* ``langgraph``→ **fail loud** (:class:`WorkflowBackendError`): execution is
  not wired until ADR 055 step 3. NEVER silently fall back to native (D6).

Import isolation (ADR 055 Boundaries / ADR 054 D7): ``temporalio`` is imported
lazily, only inside the selected ``temporal`` branch. Importing this module
costs nothing on a native-only install; ``core`` never imports ``temporalio``
at module scope.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from movate.core.workflow.runner import WorkflowResult

if TYPE_CHECKING:  # pragma: no cover — typing only.
    from movate.core.workflow.ir import WorkflowGraph
    from movate.providers.base import BaseLLMProvider
    from movate.providers.pricing import PricingTable
    from movate.storage.base import StorageProvider
    from movate.tracing.base import Tracer

# Valid runtime selectors (mirrors the WorkflowSpec.runtime Literal).
VALID_RUNTIMES = ("auto", "native", "langgraph", "temporal")

# Node types the Temporal compiler can't emit yet (raise NotImplementedError):
# an ``auto`` workflow that uses one stays on native rather than failing (ADR 091
# D2). Kept as bare names to avoid importing the IR enum at module scope.
# ``supervisor`` (ADR 092 D4) runs on native today; its Temporal lowering is
# Phase 3b, so an ``auto`` supervisor workflow prefers native until then.
_TEMPORAL_UNSUPPORTED_NODE_TYPES = frozenset({"function", "sub_workflow", "supervisor"})

# The task queue the local-run ephemeral worker and the compiled workflow share.
# ``mdk worker --backend temporal`` uses the same default (one queue name keeps
# the run path and the long-lived worker interoperable).
DEFAULT_TASK_QUEUE = "mdk-workflows"


class WorkflowBackendError(Exception):
    """Raised at *selection time* when a non-native backend cannot run.

    Carries an actionable message (install hint / connection hint / not-yet-
    wired). The caller surfaces it as a user-facing error — it is NEVER
    swallowed into a silent native fallback (ADR 055 D6).
    """


def resolve_effective_runtime(graph: WorkflowGraph, override: str | None) -> str:
    """Return the effective runtime: override > graph.runtime > auto-resolve.

    ``override`` is the ``--runtime`` CLI flag (read-only selection — it never
    mutates the spec/graph). ``None`` ⇒ use the workflow's declared runtime.
    Validates the value so a typo fails loud rather than silently defaulting.

    ADR 091 — the default runtime is ``auto``: Temporal when it can actually run
    (extra + ``TEMPORAL_HOST`` configured AND the graph compiles on Temporal),
    else native. Explicit ``temporal`` / ``native`` / ``langgraph`` are returned
    verbatim (explicit ``temporal`` still fail-loud-on-unavailable downstream).
    """
    effective = override or getattr(graph, "runtime", "auto") or "auto"
    if effective not in VALID_RUNTIMES:
        raise WorkflowBackendError(
            f"unknown runtime {effective!r}; expected one of {', '.join(VALID_RUNTIMES)}"
        )
    if effective == "auto":
        return _auto_runtime(graph)
    return effective


def _temporal_available() -> bool:
    """Non-throwing probe (ADR 091 D2): is Temporal actually usable right now?

    True iff the ``[temporal]`` extra imports AND a ``TEMPORAL_HOST`` is
    configured. Distinct from :func:`require_backend_available` (which RAISES) —
    this drives the graceful ``auto`` fallback, so it must never throw.
    """
    try:
        _require_temporal_extra()
        _resolve_temporal_connection()
    except Exception:
        return False
    return True


def _temporal_compilable(graph: WorkflowGraph) -> bool:
    """True iff every node in ``graph`` is one the Temporal compiler can emit.

    ``FUNCTION`` / ``SUB_WORKFLOW`` nodes raise ``NotImplementedError`` on the
    Temporal compiler today, so an ``auto`` workflow that uses one stays native
    (ADR 091 D2) instead of failing at runtime.
    """
    nodes = getattr(graph, "nodes", {}) or {}
    for node in nodes.values():
        node_type = getattr(getattr(node, "type", None), "value", None)
        if node_type in _TEMPORAL_UNSUPPORTED_NODE_TYPES:
            return False
    return True


def _auto_runtime(graph: WorkflowGraph) -> str:
    """Resolve ``runtime: auto`` → 'temporal' when usable, else 'native' (D2)."""
    if _temporal_available() and _temporal_compilable(graph):
        return "temporal"
    return "native"


def require_backend_available(effective_runtime: str) -> None:
    """Fail loud (D6) if the selected backend cannot execute — BEFORE any run.

    * ``native``    → always available; no-op.
    * ``langgraph`` → wired (ADR 030 D1); the ``[langgraph]`` extra must be
      importable, else raise the install hint.
    * ``temporal``  → the ``[temporal]`` extra must be importable AND a
      connection configured; raise with the matching hint otherwise.

    NEVER downgrades to native — a workflow that asked for determinism/
    durability must not silently get best-effort in-process execution.
    """
    if effective_runtime == "native":
        return
    if effective_runtime == "langgraph":
        _require_langgraph_extra()
        return
    if effective_runtime == "temporal":
        _require_temporal_extra()
        _resolve_temporal_connection()  # raises if no TEMPORAL_HOST configured.
        return
    # Unreachable — resolve_effective_runtime validated the value.
    raise WorkflowBackendError(f"unknown runtime {effective_runtime!r}")


def _require_temporal_extra() -> None:
    """Raise the install hint if the ``[temporal]`` extra is absent (D6)."""
    try:
        import temporalio  # noqa: F401, PLC0415 — availability probe only.
    except ImportError as exc:
        raise WorkflowBackendError(
            "The [temporal] extra is not installed. "
            "Install with: uv tool install --editable '.[temporal]' --force"
        ) from exc


def _require_langgraph_extra() -> None:
    """Raise the install hint if the ``[langgraph]`` extra is absent (D6)."""
    try:
        import langgraph  # noqa: F401, PLC0415 — availability probe only.
    except ImportError as exc:
        raise WorkflowBackendError(
            "The [langgraph] extra is not installed. "
            "Install with: uv tool install --editable '.[langgraph]' --force"
        ) from exc


class TemporalConnection:
    """Resolved Temporal connection details (ADR 054 D8 / ADR 055 D5).

    Read from env / credentials autoload ONLY when the effective runtime is
    ``temporal`` — a native/langgraph workflow never touches these.
    """

    __slots__ = ("host", "namespace", "tls_cert_path")

    def __init__(self, host: str, namespace: str, tls_cert_path: str | None) -> None:
        self.host = host
        self.namespace = namespace
        self.tls_cert_path = tls_cert_path


def _resolve_temporal_connection() -> TemporalConnection:
    """Resolve ``TEMPORAL_HOST`` / ``TEMPORAL_NAMESPACE`` / ``TEMPORAL_TLS_CERT``.

    Same BYOK seam as every provider key (ADR 018): the credentials autoload
    already exports these into ``os.environ`` at CLI startup. ``TEMPORAL_HOST``
    is required; ``TEMPORAL_NAMESPACE`` defaults to ``default``; the TLS cert
    is optional (Temporal Cloud / mTLS self-hosted).

    Raises :class:`WorkflowBackendError` (the D6 fail-loud) when no host is
    configured — a temporal workflow must not silently run somewhere undefined.
    """
    host = os.environ.get("TEMPORAL_HOST", "").strip()
    if not host:
        raise WorkflowBackendError(
            "runtime 'temporal' selected but no Temporal connection is configured. "
            "Set TEMPORAL_HOST (e.g. localhost:7233 for `temporal server start-dev`, "
            "or <ns>.tmprl.cloud:7233 for Temporal Cloud), optionally "
            "TEMPORAL_NAMESPACE and TEMPORAL_TLS_CERT. These ride the same BYOK "
            "autoload as provider keys (ADR 054 D8)."
        )
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "").strip() or "default"
    tls_cert = os.environ.get("TEMPORAL_TLS_CERT", "").strip() or None
    return TemporalConnection(host=host, namespace=namespace, tls_cert_path=tls_cert)


async def get_temporal_client() -> Any:
    """Connect to Temporal from the resolved BYOK config and return the client.

    Factors the connect + optional-TLS pattern inlined in
    :func:`run_temporal_workflow` / :func:`run_temporal_worker` so callers that
    only need a client — notably the runtime's resume-on-signal endpoint, which
    signals a paused durable run's handle (ADR 062 D2) — get one without
    duplicating the connection plumbing. Reads ``TEMPORAL_HOST`` /
    ``TEMPORAL_NAMESPACE`` / ``TEMPORAL_TLS_CERT`` via
    :func:`_resolve_temporal_connection` (fail-loud if ``TEMPORAL_HOST`` is
    unset). Lazy ``temporalio`` import keeps the native-only install at zero
    cost (ADR 054 D7).
    """
    from temporalio.client import Client  # noqa: PLC0415
    from temporalio.service import TLSConfig  # noqa: PLC0415

    conn = _resolve_temporal_connection()
    tls: Any = False
    if conn.tls_cert_path:
        from pathlib import Path as _Path  # noqa: PLC0415

        tls = TLSConfig(server_root_ca_cert=_Path(conn.tls_cert_path).read_bytes())
    return await Client.connect(
        conn.host,
        namespace=conn.namespace,
        tls=tls,
        interceptors=_tracing_interceptors(),
    )


def _tracing_interceptors() -> list[Any]:
    """The Temporal OTel **tracing** interceptor — the trace companion to
    :func:`_build_temporal_metrics_runtime`'s metrics runtime.

    Without this, a ``runtime: temporal`` workflow's activity spans are *orphan
    traces*: each ``call_*_activity`` runs the Executor in a fresh root span with
    no link back to the workflow, so a multi-agent run shows up as N disconnected
    traces instead of one (the native job path already propagates context via
    ``dispatch.py``'s ``continue_trace_context``). ``temporalio.contrib.
    opentelemetry.TracingInterceptor`` fixes this the idiomatic way: attached to
    the client, it injects the W3C trace context into Temporal headers at
    workflow/activity start and re-extracts it on the worker side, so the
    interceptor's StartWorkflow → RunWorkflow → RunActivity spans — and the mdk
    Executor spans nested inside each activity — form ONE connected trace.

    Gated on the same OTLP-sink condition the metrics runtime uses (so an
    operator who turned the sink off doesn't pay for it), and fully fail-soft:
    returns ``[]`` when the ``otel`` extra is absent, the sink is off, or
    anything errors — observability must never stop a workflow from running.
    """
    try:
        from movate.tracing.metrics import _otlp_metrics_enabled  # noqa: PLC0415

        if not _otlp_metrics_enabled():
            return []
        from temporalio.contrib.opentelemetry import TracingInterceptor  # noqa: PLC0415

        return [TracingInterceptor()]
    except Exception:  # pragma: no cover - fail-soft; tracing never blocks a run
        return []


def _build_temporal_metrics_runtime() -> Any | None:
    """A ``temporalio`` Runtime that exports the SDK's built-in metrics to OTLP.

    The Temporal Python SDK emits worker/client telemetry — task-queue backlog
    (``temporal_*_schedule_to_start_latency``), worker slot availability, poll
    success, sticky-cache hit rate, request latency/failures — that mdk's own
    instruments can't see (ADR 082 follow-on). Wiring a Runtime with an
    OpenTelemetry metrics exporter pushes them to the SAME OTLP collector the
    app's spans/metrics use (ADR 020), so they land in App Insights / Prometheus
    as ``temporal_*`` series alongside the ``mdk.*`` ones.

    Returns ``None`` (→ caller uses temporalio's default Runtime, zero SDK
    metrics, unchanged behavior) when no OTLP endpoint / metrics sink is
    configured, AND on any error building the Runtime — telemetry must never stop
    a worker from starting (fail-soft, mirrors ``init_metrics``). Only the
    long-lived worker calls this: one Runtime per process (a fresh Runtime per
    short-lived client would re-init core telemetry needlessly).
    """
    endpoint = (
        os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "").strip()
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )
    if not endpoint:
        return None
    # Gate on the same condition mdk's own metrics use, so an operator who turned
    # the sink off (MOVATE_TRACE_SINK=none / langfuse) doesn't get Temporal metrics.
    try:
        from movate.tracing.metrics import _otlp_metrics_enabled  # noqa: PLC0415

        if not _otlp_metrics_enabled():
            return None
    except Exception:  # pragma: no cover - tracing import shouldn't fail
        pass
    try:
        from temporalio.runtime import (  # noqa: PLC0415
            OpenTelemetryConfig,
            Runtime,
            TelemetryConfig,
        )

        protocol = (
            os.environ.get("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "").strip().lower()
            or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
        )
        is_http = protocol in ("http/protobuf", "http", "httpprotobuf")
        return Runtime(
            telemetry=TelemetryConfig(metrics=OpenTelemetryConfig(url=endpoint, http=is_http))
        )
    except Exception as exc:  # pragma: no cover - fail-soft like init_metrics
        import sys  # noqa: PLC0415

        sys.stderr.write(
            f"[movate] Temporal SDK metrics unavailable, using default runtime: {exc}\n"
        )
        return None


def load_compiled_workflow_class(module_source: str, class_name: str) -> Any:
    """Exec a :class:`CompiledWorkflow.module_source` and return its workflow class.

    The Track-B compiler emits a self-contained Python module *string* that
    declares an ``@workflow.defn`` class plus the imports it needs (the four
    Track-C activities via ``workflow.unsafe.imports_passed_through()``). The
    worker must turn that string into a live class object to register it.

    The Temporal **workflow sandbox** re-imports the workflow's defining module
    by name when it (re)creates the workflow instance, so the compiled module
    must be a real, importable entry in ``sys.modules`` — exec'ing into a
    throwaway namespace is not enough. We therefore build a proper
    :class:`types.ModuleType`, register it under a deterministic name, and exec
    the source into it. The ``@workflow.defn`` decorator (already imported by
    the emitted source) runs as part of the exec.

    The module name is derived from the class so re-compiling the same workflow
    reuses one entry (idempotent across worker re-registration).
    """
    import sys  # noqa: PLC0415
    import types  # noqa: PLC0415

    module_name = f"mdk_compiled_workflow_{class_name}"
    module = types.ModuleType(module_name)
    module.__dict__["__name__"] = module_name
    sys.modules[module_name] = module
    try:
        exec(compile(module_source, module_name, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    cls = module.__dict__.get(class_name)
    if cls is None:
        sys.modules.pop(module_name, None)
        raise WorkflowBackendError(
            f"compiled Temporal module did not define expected class {class_name!r}"
        )
    return cls


async def run_temporal_workflow(
    graph: WorkflowGraph,
    initial_state: dict[str, Any],
    *,
    storage: StorageProvider,
    pricing: PricingTable,
    tracer: Tracer,
    provider: BaseLLMProvider,
    tenant_id: str = "local",
    workflow_run_id: str | None = None,
    mock: bool = False,
    defaults: Any = None,
    detached: bool = False,
) -> WorkflowResult:
    """Compile ``graph`` to Temporal and execute it, returning a WorkflowResult.

    Self-contained execution path for ``mdk run --runtime temporal`` (and the
    worker job path): it connects to the configured Temporal service (D5),
    installs the Track-C :class:`ActivityContext` (the SAME Executor wiring
    ``dispatch.py`` builds — ADR 054 D3), spins up an in-process worker on the
    shared task queue, registers the compiled ``@workflow.defn`` + the four
    activities, executes the workflow by id (== the mdk ``workflow_run_id``,
    ADR 054 D6), and shuts the worker down.

    The returned :class:`WorkflowResult` mirrors the native runner's shape so
    callers render it identically (the conformance contract, ADR 055 D7): the
    Temporal workflow returns the final ``state`` dict, which we wrap as a
    SUCCESS result. Per-node ``RunRecord``s are persisted by the activities via
    the Executor (workflow_run_id + node_id stamped, D6); the in-memory
    ``runs`` list is left empty here because the Temporal path records them
    through storage, not through the workflow return value.

    Availability is assumed already checked by :func:`require_backend_available`
    at the selection point; this re-resolves the connection (cheap) so a direct
    caller still fails loud rather than connecting to nowhere.
    """
    import time  # noqa: PLC0415
    from uuid import uuid4  # noqa: PLC0415

    from temporalio.client import Client  # noqa: PLC0415
    from temporalio.service import TLSConfig  # noqa: PLC0415
    from temporalio.worker import Worker  # noqa: PLC0415

    from movate.core.workflow.compilers.temporal import TemporalCompiler  # noqa: PLC0415
    from movate.core.workflow.temporal_activities import (  # noqa: PLC0415
        call_agent_activity,
        call_gate_activity,
        call_human_activity,
        call_judge_activity,
        call_skill_activity,
        configure_activities,
        persist_workflow_result_activity,
    )

    wf_id = workflow_run_id or str(uuid4())
    started = time.monotonic()

    conn = _resolve_temporal_connection()
    compiled = TemporalCompiler().compile(graph)
    workflow_cls = load_compiled_workflow_class(
        compiled.module_source, compiled.workflow_class_name
    )

    # Install the activity context — the SAME provider/pricing/tracer/storage/
    # tenant the native runner's Executor uses (ADR 054 D3, one execution model).
    configure_activities(
        storage=storage,
        pricing=pricing,
        tracer=tracer,
        provider=provider,
        tenant_id=tenant_id,
        defaults=defaults,
    )

    tls: Any = False
    if conn.tls_cert_path:
        from pathlib import Path as _Path  # noqa: PLC0415

        tls = TLSConfig(server_root_ca_cert=_Path(conn.tls_cert_path).read_bytes())

    client = await Client.connect(
        conn.host,
        namespace=conn.namespace,
        tls=tls,
        interceptors=_tracing_interceptors(),
    )

    # Stamp tenant + mock into the initial state so the activities resolve the
    # right Executor per attempt (mirrors dispatch.py's per-job handling).
    run_state = dict(initial_state)
    run_state.setdefault("tenant_id", tenant_id)
    if mock:
        run_state["mock"] = True

    # ADR 089 / #759: for a durable HITL workflow dispatched in "detached" mode,
    # do NOT spin up an ephemeral worker and await the result — that holds the
    # dispatcher's slot for the entire (unbounded) human pause and starves the
    # queue. Instead START the workflow non-blocking and return immediately; the
    # long-lived `mdk worker --backend temporal` hosts it, the pause record
    # (call_human_activity) makes it listable, and ADR 080 terminal-sync writes
    # the final state on resume. Only HUMAN-node graphs take this path — a
    # non-pausing temporal workflow finishes in seconds, so blocking is cheap and
    # keeps the inline result (the job output IS the workflow output).
    if detached and _graph_has_human_node(graph):
        final_state, status, error = await _start_on_temporal(
            client=client, workflow_cls=workflow_cls, wf_id=wf_id, run_state=run_state
        )
    else:
        final_state, status, error = await _execute_on_temporal(
            client=client,
            worker_cls=Worker,
            workflow_cls=workflow_cls,
            activities=[
                call_agent_activity,
                call_skill_activity,
                call_gate_activity,
                call_judge_activity,
                call_human_activity,
                persist_workflow_result_activity,
            ],
            wf_id=wf_id,
            run_state=run_state,
        )

    finished = time.monotonic()
    return WorkflowResult(
        workflow_run_id=wf_id,
        status=status,
        initial_state=initial_state,
        final_state=final_state,
        runs=[],
        error_node_id=None,
        error=error,
        started_at=started,
        finished_at=finished,
    )


async def _execute_on_temporal(
    *,
    client: Any,
    worker_cls: Any,
    workflow_cls: Any,
    activities: list[Any],
    wf_id: str,
    run_state: dict[str, Any],
) -> tuple[dict[str, Any], Any, Any]:
    """Run ``workflow_cls`` on an ephemeral in-process worker; map the outcome.

    Returns ``(final_state, WorkflowStatus, ErrorInfo|None)``. A workflow that
    raises (an activity surfaced a non-success RunResponse as an exception, per
    ``temporal_activities``) is mapped to a terminal ERROR result with the
    failure message — NOT a silent partial success.
    """
    from temporalio.worker import UnsandboxedWorkflowRunner  # noqa: PLC0415

    from movate.core.models import ErrorInfo, WorkflowStatus  # noqa: PLC0415

    async with worker_cls(
        client,
        task_queue=DEFAULT_TASK_QUEUE,
        workflows=[workflow_cls],
        activities=activities,
        # The compiled workflow module is generated at runtime (a source
        # string the Track-B compiler emits), so it is not a file the Temporal
        # workflow *sandbox* can re-import. We run it unsandboxed: determinism
        # is enforced at COMPILE time (ADR 054 D5 linter — clocks/RNG/IO are
        # lowered into activities) and every side effect lives in an activity,
        # so the workflow body is already deterministic by construction.
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            result = await client.execute_workflow(
                workflow_cls.run,
                run_state,
                id=wf_id,
                task_queue=DEFAULT_TASK_QUEUE,
            )
        except Exception as exc:  # map any workflow failure to a terminal ERROR result.
            return (
                dict(run_state),
                WorkflowStatus.ERROR,
                ErrorInfo(
                    type="temporal_workflow_failed",
                    message=str(exc),
                    retryable=False,
                ),
            )

    final_state = result if isinstance(result, dict) else {"result": result}
    return final_state, WorkflowStatus.SUCCESS, None


def _graph_has_human_node(graph: WorkflowGraph) -> bool:
    """True if ``graph`` contains a HUMAN node (a durable HITL pause point).

    Detected from the IR node types the compiler already populates — no new
    metadata. Used to scope ADR 089's non-blocking dispatch to exactly the
    workflows that can park for an unbounded human wait.
    """
    from movate.core.workflow.ir import NodeType  # noqa: PLC0415

    return any(node.type == NodeType.HUMAN for node in graph.nodes.values())


async def _start_on_temporal(
    *,
    client: Any,
    workflow_cls: Any,
    wf_id: str,
    run_state: dict[str, Any],
) -> tuple[dict[str, Any], Any, Any]:
    """Start ``workflow_cls`` on Temporal NON-blocking (ADR 089 / #759).

    No ephemeral worker, no await: the long-lived ``mdk worker --backend
    temporal`` hosts the workflow + its activities (the workflow type name is
    identical because both compile the same graph deterministically). Returns a
    ``PAUSED`` outcome immediately — ``_workflow_result_to_outcome`` maps that to
    a successful (accepted) job, and the durable run-record lifecycle (ADR 062
    pause record + ADR 080 terminal sync) is the source of truth for the result.

    ``REJECT_DUPLICATE`` id-reuse makes a re-dispatched job (same workflow_run_id,
    ADR 054 D6) a no-op rather than a second execution.
    """
    from temporalio.common import WorkflowIDReusePolicy  # noqa: PLC0415

    from movate.core.models import ErrorInfo, WorkflowStatus  # noqa: PLC0415

    try:
        await client.start_workflow(
            workflow_cls.run,
            run_state,
            id=wf_id,
            task_queue=DEFAULT_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except Exception as exc:  # connect/start failure — fail loud, don't pretend paused.
        return (
            dict(run_state),
            WorkflowStatus.ERROR,
            ErrorInfo(type="temporal_workflow_start_failed", message=str(exc), retryable=True),
        )
    return dict(run_state), WorkflowStatus.PAUSED, None


async def run_temporal_worker(
    workflows: dict[str, WorkflowGraph],
    *,
    storage: StorageProvider,
    pricing: PricingTable,
    tracer: Tracer,
    provider: BaseLLMProvider,
    tenant_id: str = "local",
    stop_event: Any = None,
    defaults: Any = None,
) -> list[str]:
    """Run a long-lived Temporal worker (``mdk worker --backend temporal``, D4).

    Connects to the configured Temporal service (D5), installs the Track-C
    :class:`ActivityContext` (the SAME Executor wiring ``dispatch.py`` builds —
    ADR 054 D3), compiles every ``runtime: temporal`` workflow in ``workflows``
    to its ``@workflow.defn`` class, and registers those workflow classes + the
    four activities on the shared task queue, then polls until ``stop_event``
    is set (or forever).

    Returns the list of registered workflow names (for the caller's startup
    banner). Raises :class:`WorkflowBackendError` if the extra/connection is
    missing — fail loud, never a silent no-op.

    Kept thin: all the lowering lives in the Track-B compiler; this just wires
    the SDK worker around it.
    """
    import asyncio  # noqa: PLC0415

    from temporalio.client import Client  # noqa: PLC0415
    from temporalio.service import TLSConfig  # noqa: PLC0415
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: PLC0415

    from movate.core.workflow.compilers.temporal import TemporalCompiler  # noqa: PLC0415
    from movate.core.workflow.temporal_activities import (  # noqa: PLC0415
        call_agent_activity,
        call_gate_activity,
        call_human_activity,
        call_judge_activity,
        call_skill_activity,
        configure_activities,
        persist_workflow_result_activity,
    )

    _require_temporal_extra()
    conn = _resolve_temporal_connection()

    # Compile every temporal-declared workflow to a registrable class. A
    # workflow that fails to compile (an unsupported node type — sub-workflow /
    # function, ADR 054) is surfaced loudly rather than silently dropped.
    compiler = TemporalCompiler()
    workflow_classes: list[Any] = []
    registered: list[str] = []
    for name, graph in sorted(workflows.items()):
        # ADR 091 — resolve through the same auto-fallback logic the dispatch
        # fork uses, so an ``auto`` workflow that resolves to Temporal is hosted
        # here (a raw ``== "temporal"`` check would skip it).
        if resolve_effective_runtime(graph, None) != "temporal":
            continue  # native/langgraph (or auto→native) aren't hosted by this worker.
        compiled = compiler.compile(graph)
        workflow_classes.append(
            load_compiled_workflow_class(compiled.module_source, compiled.workflow_class_name)
        )
        registered.append(name)

    # Install the activity context (D3) regardless — the worker is the place
    # the ActivityContext is configured (temporal_activities._get_context()).
    configure_activities(
        storage=storage,
        pricing=pricing,
        tracer=tracer,
        provider=provider,
        tenant_id=tenant_id,
        defaults=defaults,
    )

    tls: Any = False
    if conn.tls_cert_path:
        from pathlib import Path as _Path  # noqa: PLC0415

        tls = TLSConfig(server_root_ca_cert=_Path(conn.tls_cert_path).read_bytes())

    # Export the Temporal SDK's built-in worker/client metrics to OTLP (ADR 082
    # follow-on) when an OTLP endpoint is configured — one Runtime for this
    # long-lived worker process; None → temporalio's default Runtime (unchanged).
    metrics_runtime = _build_temporal_metrics_runtime()
    connect_kwargs: dict[str, Any] = {
        "namespace": conn.namespace,
        "tls": tls,
        # Trace-context propagation across the workflow→activity boundary so a
        # multi-agent durable run is ONE connected trace, not N orphan spans.
        "interceptors": _tracing_interceptors(),
    }
    if metrics_runtime is not None:
        connect_kwargs["runtime"] = metrics_runtime
    client = await Client.connect(conn.host, **connect_kwargs)

    worker = Worker(
        client,
        task_queue=DEFAULT_TASK_QUEUE,
        workflows=workflow_classes,
        activities=[
            call_agent_activity,
            call_skill_activity,
            call_gate_activity,
            call_judge_activity,
            call_human_activity,
            persist_workflow_result_activity,
        ],
        # Run compiled workflows unsandboxed — the source is generated at
        # runtime (not a file the sandbox can re-import); determinism is
        # enforced at compile time (ADR 054 D5) and all IO lives in activities.
        workflow_runner=UnsandboxedWorkflowRunner(),
    )

    async with worker:
        if stop_event is None:
            stop_event = asyncio.Event()
        await stop_event.wait()

    return registered


__all__ = [
    "DEFAULT_TASK_QUEUE",
    "VALID_RUNTIMES",
    "TemporalConnection",
    "WorkflowBackendError",
    "get_temporal_client",
    "load_compiled_workflow_class",
    "require_backend_available",
    "resolve_effective_runtime",
    "run_temporal_worker",
    "run_temporal_workflow",
]
