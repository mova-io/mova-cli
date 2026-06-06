"""Temporal compiler — lower a WorkflowGraph IR to a Temporal ``@workflow.defn`` module.

[bold]Phase 1 of ADR 054[/bold] — Temporal as an opt-in deterministic, durable
workflow backend behind the runner Protocol. This module is the *core
architectural piece* of Phase 1 Track B: a pure code-generation lowering
from mdk's :class:`WorkflowGraph` (the IR produced from ``workflow.yaml``)
into a Python source string that declares an equivalent Temporal workflow
class (``@workflow.defn``) plus references to the activity wrappers that
Track C installs around the existing ``Executor`` and ``SkillBackend``.

Selection seam (per ADR 054 D2): a workflow author flips
``workflow.yaml: runtime: temporal`` and the runner Protocol dispatches the
spec to this compiler instead of the native runner. The native runner stays
the default and the portable floor (CLAUDE.md §6 + ADR 030 D1 / ADR 054 D1
— three runners are peers behind one seam).

Execution-model reuse (ADR 054 D3): every emitted activity call eventually
forwards to the *same* ``Executor.execute(...)`` the native runner uses; no
second execution model is introduced. The activity wrappers (
``call_agent_activity``, ``call_skill_activity``, ``call_gate_activity``,
``call_judge_activity``) live in ``movate.core.workflow.temporal_activities``
(Track C, parallel PR — referenced by name only here).

Determinism (ADR 054 D5): Phase 1 ships a **linter-mode** determinism
check (:meth:`TemporalCompiler.lint`) that emits warnings — never errors —
when the spec contains a non-deterministic primitive (``time.time()``,
``random.``, ``datetime.now()`` in workflow-scope expressions),
non-deterministic skill capability flags, HUMAN nodes (Phase 2), or
unbounded loops. Phase 2 promotes those warnings to compile-time errors;
keeping them warnings in Phase 1 lets existing workflows compile without
schema breaks.

Node-type mapping (ADR 054 D4) — each mdk node lowers to a deterministic
Temporal construct (clocks / RNG / IO move *into* activities so replay
reproduces them):

* AGENT      → ``await workflow.execute_activity(call_agent_activity, ...)``
* SKILL      → ``await workflow.execute_activity(call_skill_activity, ...)``
* GATE / INTENT_ROUTER → activity returns a routing decision; workflow branches.
* JUDGE      → activity returns ``{terminate, verdict}``; workflow gates.
* SUPERVISOR → Python-level orchestration in the workflow body.
* HUMAN      → durable HITL (ADR 062): a ``call_human_activity`` persists the
  awaiting-human pause record, then the workflow parks on
  ``workflow.wait_condition`` until a ``human_response`` signal arrives (or an
  optional durable ``timeout`` fires the ``on_timeout`` route). The pause is
  durable across worker/runtime restarts — no poller, no re-walk.
* Bounded loop (``max_iterations``) → ``for _ in range(max_iterations): ...``
* Bounded fan-out → ``await asyncio.gather(*[workflow.execute_activity(...) ...])``

Import isolation: ``temporalio`` is a lazy import (gated on the ``[temporal]``
extra). This module is import-safe without the extra installed — callers that
never invoke :meth:`TemporalCompiler.compile` pay zero cost. The contract
is asserted by :func:`test_lazy_temporalio_import`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.spec import WorkflowSpec

# ---------------------------------------------------------------------------
# Defaults (per ADR 054 D9 — per-activity timeouts; Phase 1 picks safe
# defaults, Phase 3 lifts them to workflow.yaml-configurable).
# ---------------------------------------------------------------------------

DEFAULT_SCHEDULE_TO_CLOSE_SECONDS = 300  # 5 minutes; ADR 054 D9 default.
DEFAULT_HEARTBEAT_SECONDS = 30  # ADR 054 D9 — liveness for long LLM calls.
DEFAULT_MAX_ITERATIONS = 2  # Mirrors the structural cap in pattern_goal_oriented.

# Linter codes — stable strings tests can pin on.
LINT_NONDETERMINISTIC_TIME = "TEMPORAL_NONDETERMINISTIC_TIME"
LINT_NONDETERMINISTIC_SKILL = "TEMPORAL_NONDETERMINISTIC_SKILL"
LINT_UNBOUNDED_LOOP = "TEMPORAL_UNBOUNDED_LOOP"

# Patterns matched by the linter against free-text expressions / prompts.
# Phase 1 catches the most common non-deterministic primitives; the list is
# intentionally short and easy to grow without breaking the API.
_NONDET_TIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\btime\.time\s*\("),
    re.compile(r"\brandom\.[A-Za-z_]+\s*\("),
    re.compile(r"\bdatetime\.now\s*\("),
    re.compile(r"\bdatetime\.utcnow\s*\("),
)


# ---------------------------------------------------------------------------
# Lazy temporalio import — only loaded when the compiler is actually invoked.
# This module is import-safe without the [temporal] extra installed.
# ---------------------------------------------------------------------------


def _require_temporalio() -> tuple[Any, Any]:
    """Lazy-import ``temporalio`` and return ``(temporalio, workflow)``.

    Phase 1 compiler invocations only need the module references symbolically
    (the emitted source string is parsed/executed by a Temporal worker, not
    here), but we still gate on importability so an operator running
    ``mdk compile --runtime temporal`` without the extra installed gets the
    install instruction immediately instead of an obscure ``ImportError`` at
    worker-launch time. Tests assert this module is importable without
    ``temporalio`` present (see ``test_lazy_temporalio_import``).
    """
    try:
        import temporalio  # noqa: PLC0415 — intentional lazy import
        from temporalio import workflow  # noqa: PLC0415

        return temporalio, workflow
    except ImportError as exc:  # pragma: no cover — exercised in test_lazy
        raise RuntimeError(
            "The [temporal] extra is not installed. "
            "Install with: uv tool install --editable '.[temporal]' --force"
        ) from exc


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LintIssue:
    """One linter finding.

    Phase 1 emits only ``severity='warning'`` — Phase 2 promotes the same
    codes to ``'error'`` once the activity wrappers are wired up.
    """

    severity: str
    code: str
    message: str
    file_path: str | None = None
    line: int | None = None
    node_id: str | None = None


@dataclass(frozen=True)
class CompiledWorkflow:
    """Output of :meth:`TemporalCompiler.compile`.

    Mirrors the LangGraph compiler's return-shape philosophy (a string the
    operator writes to disk + a small manifest the runner uses for
    bookkeeping) but stays explicit about *what* was compiled so future
    phases (Phase 2 HUMAN, Phase 3 per-activity policies) can extend the
    manifest without renaming fields.
    """

    module_source: str
    """Python source declaring an ``@workflow.defn`` class. Importable as-is
    against a worker process that has the [temporal] extra + the activity
    wrappers from Track C."""

    workflow_class_name: str
    """The name of the emitted ``@workflow.defn`` class — used by
    ``mdk worker --backend temporal`` to register the workflow with the
    Temporal worker."""

    activity_names: tuple[str, ...]
    """Activity reference names the workflow body calls. Track C's worker
    registers these on the same task queue."""

    lint_issues: tuple[LintIssue, ...] = ()
    """Linter findings carried alongside the compiled output. Phase 1 these
    are warnings only (see :meth:`TemporalCompiler.lint`)."""

    manifest: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runner Protocol (Phase 1 placeholder — see runner.py docstring; ADR 054 D1)
# ---------------------------------------------------------------------------


class CompilerProtocol(Protocol):
    """Subset of the runner-Protocol contract every backend compiler honors.

    The native runner consumes the IR directly; the LangGraph and Temporal
    backends lower the IR to a runtime-native form first. This Protocol
    pins the ``compile`` shape so the three backends remain swappable
    behind the same seam (ADR 054 D1 / ADR 030 D1). Phase 1 intentionally
    keeps it small — :meth:`compile` + :meth:`lint`.
    """

    def compile(self, spec: WorkflowGraph) -> CompiledWorkflow: ...

    def lint(self, spec: WorkflowGraph) -> list[LintIssue]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _class_name_from_workflow(name: str) -> str:
    """Turn ``goal-oriented`` into ``GoalOrientedWorkflow`` (PEP-8 class name)."""
    parts = re.split(r"[^A-Za-z0-9]", name)
    pascal = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not pascal:
        pascal = "Mdk"
    return f"{pascal}Workflow"


def _safe_method_name(node_id: str) -> str:
    """Map a node id to a valid Python method-name (used inside generated workflows)."""
    safe = "".join(c if c.isalnum() else "_" for c in node_id)
    if safe and safe[0].isdigit():
        safe = f"n_{safe}"
    return safe or "node"


def _is_bounded_loop(node_id: str, graph: WorkflowGraph) -> bool:
    """Heuristic: a node carries a bounded-loop ``max_iterations`` annotation.

    Phase 1 only inspects ``node.metadata`` — workflow authors can attach
    ``max_iterations`` to a SUPERVISOR node, mirroring the structural bound
    in ``pattern_goal_oriented`` / ``pattern_simulation``. The lint pass
    warns when a back-edge exists but no node carries the bound (see
    :meth:`TemporalCompiler.lint`).
    """
    node = graph.nodes.get(node_id)
    if node is None:
        return False
    return bool(node.metadata.get("max_iterations"))


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------


class TemporalCompiler:
    """Lower a :class:`WorkflowGraph` to a Temporal ``@workflow.defn`` module.

    Phase 1 of ADR 054 — pure code generation. The output module is a
    Python source string the operator writes to disk; an ``mdk worker
    --backend temporal`` process imports + registers it (Track C / Phase 3).
    The compiler itself never instantiates the Temporal SDK at runtime; the
    lazy ``_require_temporalio()`` call is a *gate* (fail-loud if the extra
    is missing), not a runtime dependency of the lowering itself.

    See the module docstring for the node-type mapping table (ADR 054 D4)
    and the determinism contract (ADR 054 D5).
    """

    def __init__(
        self,
        *,
        schedule_to_close_seconds: int = DEFAULT_SCHEDULE_TO_CLOSE_SECONDS,
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
        default_max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self._schedule_to_close_seconds = schedule_to_close_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._default_max_iterations = default_max_iterations

    # -- public API --------------------------------------------------------

    def compile(self, spec: WorkflowGraph) -> CompiledWorkflow:
        """Lower ``spec`` to a Temporal workflow module + manifest.

        Asserts the ``[temporal]`` extra is importable (via
        ``_require_temporalio``) so an operator using this compiler without
        the extra installed gets a clear install hint immediately, not at
        worker-launch time. The generated *output* still works on any
        machine with the extra; this gate is for the *invoker*.

        HUMAN nodes are first-class as of ADR 062 (durable HITL): they lower to
        ``workflow.wait_condition`` + a ``human_response`` signal. The remaining
        determinism concerns are warnings only (the contract D5 promises).
        """
        _require_temporalio()
        lint_issues = self.lint(spec)
        return self._emit_workflow_defn(spec, lint_issues)

    def lint(self, spec: WorkflowGraph) -> list[LintIssue]:
        """Walk ``spec`` for non-deterministic primitives (Phase 1: warnings only).

        Emits :class:`LintIssue` for:

        * ``time.time()`` / ``random.*`` / ``datetime.now()`` in any
          prompt-like free-text expression on a node's metadata. Workflow
          scope must be deterministic (ADR 054 D5).
        * Skills whose ``capabilities.deterministic`` flag is false — the
          activity's output may vary on replay and the operator should
          confirm idempotency.
        * Cyclic / unbounded loops — workflows that contain a back-edge
          without any node carrying a ``max_iterations`` bound. Without a
          structural cap, the workflow history can grow without limit.

        Phase 2 promotes warnings to errors. Phase 1 returns the list so
        callers (CLI / IDE plugins) can surface findings without breaking
        existing workflows.
        """
        issues: list[LintIssue] = []

        # Free-text / capability scan over every node.
        for nid, node in spec.nodes.items():
            for key, value in node.metadata.items():
                if isinstance(value, str):
                    for pat in _NONDET_TIME_PATTERNS:
                        if pat.search(value):
                            issues.append(
                                LintIssue(
                                    severity="warning",
                                    code=LINT_NONDETERMINISTIC_TIME,
                                    message=(
                                        f"node {nid!r} field {key!r} references a "
                                        "non-deterministic primitive; move it into an activity "
                                        "(ADR 054 D5)."
                                    ),
                                    node_id=nid,
                                )
                            )
                            break
                # Skill capability check — only fires when an author
                # explicitly opts a skill into ``deterministic: false``.
                if key == "capabilities" and isinstance(value, dict):
                    det = value.get("deterministic")
                    if det is False:
                        issues.append(
                            LintIssue(
                                severity="warning",
                                code=LINT_NONDETERMINISTIC_SKILL,
                                message=(
                                    f"node {nid!r} carries capabilities.deterministic=false; "
                                    "output may vary on replay (ADR 054 D5)."
                                ),
                                node_id=nid,
                            )
                        )

        # Cycle / unbounded-loop scan. A back-edge with no node carrying
        # ``max_iterations`` is the canonical Phase 1 unbounded-loop case.
        back_edges = spec.find_back_edges()
        if back_edges:
            any_bound = any(_is_bounded_loop(nid, spec) for nid in spec.nodes)
            if not any_bound:
                for edge in back_edges:
                    issues.append(
                        LintIssue(
                            severity="warning",
                            code=LINT_UNBOUNDED_LOOP,
                            message=(
                                f"back-edge {edge.from_id!r}→{edge.to_id!r} has no "
                                "max_iterations bound; workflow history could grow without "
                                "limit (ADR 054 D5)."
                            ),
                            node_id=edge.from_id,
                        )
                    )
        return issues

    # -- private emit helpers ---------------------------------------------

    def _emit_workflow_defn(
        self, spec: WorkflowGraph, lint_issues: list[LintIssue]
    ) -> CompiledWorkflow:
        """Emit the ``@workflow.defn`` class wrapping ``spec``.

        The module is a self-contained string; the worker process imports it
        directly. The body's structure mirrors the IR's topological order so
        the file reads in execution order (operator-friendly), which also
        matches the LangGraph emitter's convention.
        """
        cls_name = _class_name_from_workflow(spec.name)
        order = self._ordered_node_ids(spec)
        activity_names: set[str] = set()
        # HUMAN nodes need the pause-record activity imported + a signal handler
        # emitted on the class (ADR 062). Gate both on presence so a workflow
        # without a HUMAN node emits byte-for-byte the Phase-1 output.
        has_human = any(node.type is NodeType.HUMAN for node in spec.nodes.values())

        header = [
            '"""Auto-generated by `movate.core.workflow.compilers.temporal`.',
            "",
            "Do not edit by hand — re-run the compiler when workflow.yaml changes.",
            "",
            f"Source workflow: {spec.name} (v{spec.version})",
            "Runtime: temporal (ADR 054 Phase 1)",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "import asyncio",
            "from datetime import timedelta",
            "from typing import Any",
            "",
            "from temporalio import workflow",
            "from temporalio.common import RetryPolicy",
            "from temporalio.exceptions import ApplicationError",
            "",
            "# Phase 1 retry/timeout defaults — ADR 054 D9. Per-node overrides land",
            "# in Phase 3 (per-activity policies declared in workflow.yaml).",
            f"_SCHEDULE_TO_CLOSE = timedelta(seconds={self._schedule_to_close_seconds})",
            f"_HEARTBEAT = timedelta(seconds={self._heartbeat_seconds})",
            "_RETRY_POLICY = RetryPolicy(maximum_attempts=3)",
            "",
            "# Activity-wrapper references — implemented in",
            "# movate.core.workflow.temporal_activities (Track C, parallel PR).",
            "# The worker registers them alongside this workflow.",
            "with workflow.unsafe.imports_passed_through():",
            "    from movate.core.workflow.temporal_activities import (",
            "        call_agent_activity,",
            "        call_skill_activity,",
            "        call_gate_activity,",
            "        call_judge_activity,",
            "    )",
            "",
            "",
        ]
        if has_human:
            # Import the pause-record activity alongside the four Phase-1 wrappers.
            header.insert(
                header.index("        call_judge_activity,") + 1,
                "        call_human_activity,",
            )

        body_lines: list[str] = [
            "@workflow.defn",
            f"class {cls_name}:",
            f'    """Temporal workflow for {spec.name!r} (v{spec.version}).',
            "",
            "    Control flow is a DISPATCH LOOP over node ids that mirrors the",
            "    native runner's dynamic traversal (runner._walk): each node sets",
            "    the next ``current`` node id — AGENT/SKILL nodes advance to their",
            "    sequential successor, GATE/INTENT_ROUTER nodes branch to",
            "    ``routes[label]`` (or ``fallback``) on the classifier's decision.",
            "    Branch decisions are recorded in Temporal history (the gate",
            "    activity's result), so replay reaches the same branch (ADR 054 D4/D5).",
            "",
            "    State (ADR 054 D10): workflow-scope holds CONTROL FLOW only —",
            "    activity results, the current-node id, the visited set, run id.",
            "    Conversation state lives in the session store (ADR 045 D10), read",
            "    + written by the activity through the existing Executor.",
            '    """',
            "",
            "    @workflow.run",
            "    async def run(self, initial_state: dict[str, Any]) -> dict[str, Any]:",
            f'        """Run from entrypoint {spec.entrypoint!r}, following the chosen branch.',
            "",
            "        ``initial_state`` is the parsed JSON state object the native",
            "        runner also takes. Determinism: every clock/RNG/IO call is",
            "        inside an activity (ADR 054 D5); the loop branches only on",
            "        recorded activity results, so replay is deterministic.",
            '        """',
            "        state: dict[str, Any] = dict(initial_state)",
            "        run_id = workflow.info().workflow_id",
            f"        current: str | None = {spec.entrypoint!r}",
            "        # Cycle guard — mirrors the native runner; a revisited node means",
            "        # a non-deterministic loop the bounded patterns never produce.",
            "        visited: set[str] = set()",
            "        while current is not None:",
            "            if current in visited:",
            "                raise ApplicationError(",
            '                    f"workflow cycle detected at node {current!r}"',
            "                )",
            "            visited.add(current)",
        ]

        if has_human:
            # Durable-HITL plumbing (ADR 062 D1): a per-node buffer the signal
            # handler writes and each HUMAN node's wait_condition reads. Spliced
            # in before @workflow.run so the run method stays the single
            # entrypoint. A workflow with no HUMAN node emits neither, keeping
            # the Phase-1 output byte-for-byte identical.
            signal_block = [
                "    def __init__(self) -> None:",
                "        # ADR 062 — durable HITL signal buffer, keyed by HUMAN node id.",
                "        self._human: dict[str, dict[str, Any]] = {}",
                "",
                "    @workflow.signal",
                "    def human_response(self, node_id: str, payload: dict[str, Any]) -> None:",
                "        # Idempotent: last write wins (a re-delivered signal is harmless).",
                "        self._human[node_id] = payload",
                "",
            ]
            run_idx = body_lines.index("    @workflow.run")
            body_lines[run_idx:run_idx] = signal_block

        # Emit each node as an ``if/elif current == <id>:`` branch of the
        # dispatch loop. The per-node emitters return zero-indented statement
        # lines (the work + the ``current = <next>`` advance); we wrap each in
        # its branch and indent the body uniformly. Topological order only
        # affects the file layout (operator-friendly) — the loop dispatches by
        # id, so any order is correct.
        first = True
        for nid in order:
            node = spec.nodes[nid]
            stmts, used = self._emit_node(nid, node, spec)
            keyword = "if" if first else "elif"
            body_lines.append(f"            {keyword} current == {nid!r}:")
            body_lines.extend(("                " + ln if ln else "") for ln in stmts)
            activity_names.update(used)
            first = False

        if order:
            # Defensive else — every route target / successor is a validated
            # node id, so this is unreachable in practice; it fails loud rather
            # than spinning the loop forever on an unknown id.
            body_lines.append("            else:")
            body_lines.append(
                '                raise ApplicationError(f"unknown workflow node {current!r}")'
            )
        else:  # pragma: no cover — compile_workflow guarantees a valid entrypoint.
            body_lines.append("            current = None")

        body_lines.append("")
        body_lines.append("        return state")

        source = "\n".join(header + body_lines) + "\n"

        manifest: dict[str, Any] = {
            "workflow_name": spec.name,
            "workflow_version": spec.version,
            "node_count": len(spec.nodes),
            "entrypoint": spec.entrypoint,
            "phase": "1",
            "adr": "054",
        }

        return CompiledWorkflow(
            module_source=source,
            workflow_class_name=cls_name,
            activity_names=tuple(sorted(activity_names)),
            lint_issues=tuple(lint_issues),
            manifest=manifest,
        )

    def _emit_node(self, nid: str, node: Any, spec: WorkflowGraph) -> tuple[list[str], set[str]]:
        """Dispatch by node type, returning ``(stmt_lines, activity_names_used)``.

        ``stmt_lines`` are *zero-indented* statements (the node's work plus the
        ``current = <next>`` advance); :meth:`_emit_workflow_defn` wraps them in
        the dispatch loop's ``if/elif current == <id>:`` branch and indents.
        """
        node_type = node.type
        if node_type is NodeType.AGENT:
            return self._emit_agent_node(nid, node, spec)
        if node_type is NodeType.INTENT_ROUTER:
            return self._emit_gate_node(nid, node, spec)
        if node_type is NodeType.HUMAN:
            return self._emit_human_node(nid, node, spec)
        if node_type is NodeType.TOOL:
            # TOOL nodes haven't shipped yet (v1.1); the spec validator
            # rejects them, but the compiler is still total: emit a skill
            # call so a future TOOL adoption needs only a metadata field.
            return self._emit_skill_node(nid, node, spec)
        if node_type is NodeType.SUB_WORKFLOW:
            # Sub-workflow support is Phase 3+. Emit a placeholder so the
            # compiler stays total and the operator gets a clear failure.
            lines = [
                f"# SUB_WORKFLOW {nid!r}: deferred to a later phase.",
                (
                    f'raise NotImplementedError("sub_workflow node {nid!r} '
                    'is not supported in Phase 1")'
                ),
            ]
            return lines, set()
        # Default — FUNCTION or unknown future type. Stub.
        lines = [
            f"# node {nid!r} (type={node_type.value}): generic activity stub.",
            (
                f'raise NotImplementedError("node type {node_type.value!r} '
                'is not supported in Phase 1")'
            ),
        ]
        return lines, set()

    def _emit_agent_node(
        self, nid: str, node: Any, spec: WorkflowGraph
    ) -> tuple[list[str], set[str]]:
        """AGENT → ``await workflow.execute_activity(call_agent_activity, ...)``.

        Per ADR 054 D4: each agent call lowers to a single activity call; the
        node then advances to its sequential successor (the native runner's
        ``_sequential_successor`` rule). Per D11: metering wraps the activity,
        so retries don't over-meter.
        """
        method = _safe_method_name(nid)
        nxt = self._sequential_successor(spec, nid)
        body = [
            f"# node {nid!r} — AGENT (ADR 054 D4 row 1)",
            "# Activity body forwards to Executor.execute(...) via Track C wrapper.",
            f"{method}_result = await workflow.execute_activity(",
            "    call_agent_activity,",
            f"    args=[{nid!r}, {node.ref!r}, state, run_id],",
            "    schedule_to_close_timeout=_SCHEDULE_TO_CLOSE,",
            "    heartbeat_timeout=_HEARTBEAT,",
            "    retry_policy=_RETRY_POLICY,",
            ")",
            f"state.update({method}_result)",
            f"current = {nxt!r}",
        ]
        return body, {"call_agent_activity"}

    def _emit_skill_node(
        self, nid: str, node: Any, spec: WorkflowGraph
    ) -> tuple[list[str], set[str]]:
        """SKILL → ``await workflow.execute_activity(call_skill_activity, ...)``."""
        method = _safe_method_name(nid)
        nxt = self._sequential_successor(spec, nid)
        body = [
            f"# node {nid!r} — SKILL (ADR 054 D4 row 7)",
            f"{method}_result = await workflow.execute_activity(",
            "    call_skill_activity,",
            f"    args=[{nid!r}, {node.ref!r}, state, run_id],",
            "    schedule_to_close_timeout=_SCHEDULE_TO_CLOSE,",
            "    heartbeat_timeout=_HEARTBEAT,",
            "    retry_policy=_RETRY_POLICY,",
            ")",
            f"state.update({method}_result)",
            f"current = {nxt!r}",
        ]
        return body, {"call_skill_activity"}

    def _emit_gate_node(
        self, nid: str, node: Any, spec: WorkflowGraph
    ) -> tuple[list[str], set[str]]:
        """GATE / INTENT_ROUTER → activity returns a decision; workflow branches.

        Emits REAL conditional control flow (ADR 054 D4 row 3): the gate
        activity returns a decision dict carrying ``"label"``; the workflow
        selects the next node as ``routes[label]`` (or ``fallback`` when the
        label is unknown) — byte-for-byte the native runner's
        ``_run_intent_router`` routing-table semantics. The decision is recorded
        in Temporal history (the activity result), so replay reaches the same
        branch (D5).

        The decision is NOT merged into ``state``: the native runner stamps
        nothing at a gate (it only chooses the next node), so merging it would
        diverge the final state. The gate records control flow only (D10). The
        classifier ref is resolved by ``call_gate_activity`` against the
        workflow dir baked into the args (the IR leaves ``classifier_agent``
        relative, unlike the absolutized AGENT ``node.ref``).
        """
        method = _safe_method_name(nid)
        routes: dict[str, str] = node.metadata.get("routes", {}) or {}
        fallback: str = node.metadata.get("fallback", "")
        classifier: str = node.metadata.get("classifier_agent", "")
        input_field: str = node.metadata.get("input_field", "")
        workflow_dir = str(spec.workflow_dir)
        routes_literal = "{" + ", ".join(f"{k!r}: {v!r}" for k, v in routes.items()) + "}"
        labels_literal = "[" + ", ".join(repr(k) for k in routes) + "]"
        body = [
            f"# node {nid!r} — GATE / INTENT_ROUTER (ADR 054 D4 row 3)",
            "# The classifier activity returns a decision dict carrying 'label';",
            "# we branch to routes[label] (or fallback) — mirroring the native",
            "# runner's _run_intent_router. The decision is NOT merged into state",
            "# (native parity: a gate records control flow only, ADR 054 D10).",
        ]
        # Documentary route table — kept so the generated file explains the
        # routing inline (and pins the operator-readable shape in tests).
        for label, target in routes.items():
            body.append(f"# route {label!r} → next node {target!r}")
        if fallback:
            body.append(f"# fallback → {fallback!r}")
        body += [
            f"{method}_decision = await workflow.execute_activity(",
            "    call_gate_activity,",
            f"    args=[{nid!r}, {classifier!r}, state, run_id, {workflow_dir!r}, "
            f"{input_field!r}, {labels_literal}],",
            "    schedule_to_close_timeout=_SCHEDULE_TO_CLOSE,",
            "    heartbeat_timeout=_HEARTBEAT,",
            "    retry_policy=_RETRY_POLICY,",
            ")",
            f"{method}_label = str({method}_decision.get('label', ''))",
            f"{method}_routes = {routes_literal}",
            f"current = {method}_routes.get({method}_label, {fallback!r})",
        ]
        return body, {"call_gate_activity"}

    def _emit_judge_node(
        self, nid: str, node: Any, spec: WorkflowGraph
    ) -> tuple[list[str], set[str]]:
        """JUDGE → activity returns ``{terminate, verdict}``; workflow gates.

        Unused in Phase 1 because the IR doesn't (yet) carry a dedicated
        JUDGE node type — judges currently ride as ``intent-router`` nodes
        (see ``pattern_simulation`` ``turn-judge``). Kept here as the
        canonical shape so the row-4 mapping in ADR 054 D4 is encoded once.
        """
        method = _safe_method_name(nid)
        body = [
            f"        # node {nid!r} — JUDGE (ADR 054 D4 row 4)",
            f"        {method}_verdict = await workflow.execute_activity(",
            "            call_judge_activity,",
            f"            args=[{nid!r}, state, run_id],",
            "            schedule_to_close_timeout=_SCHEDULE_TO_CLOSE,",
            "            heartbeat_timeout=_HEARTBEAT,",
            "            retry_policy=_RETRY_POLICY,",
            "        )",
            f"        state['{method}_verdict'] = {method}_verdict",
            f"        if {method}_verdict.get('terminate'):",
            "            return state",
        ]
        return body, {"call_judge_activity"}

    def _emit_human_node(
        self, nid: str, node: Any, spec: WorkflowGraph
    ) -> tuple[list[str], set[str]]:
        """HUMAN → durable HITL: persist a pause record, then park on a signal.

        ADR 062 D1. Emits, in dispatch-loop scope:

        1. ``call_human_activity`` — persists the awaiting-human pause record
           (status PAUSED, ``runtime='temporal'``) so an operator can list it
           (``?status=paused``) and a transport can render the approval.
        2. ``await workflow.wait_condition(lambda: nid in self._human, ...)`` —
           a DURABLE pause: the workflow parks in Temporal history and a
           worker/runtime restart re-hydrates it and keeps waiting (no poller,
           no re-walk — the durable analogue of the native runner's snapshot
           pause, ADR 017 D5).
        3. On signal: merge the declared ``output_contract`` keys into ``state``
           (native parity — the native HUMAN node merges the same keys) and
           advance to the sequential successor.
        4. On an optional durable ``timeout`` (D4): take the ``on_timeout``
           route instead. Native has no durable timer, so this is purely
           additive — there is no native behavior to diverge from.

        Gate-style ``routes`` are intentionally NOT emitted here: the native
        HUMAN node advances to its sequential successor (it does not branch on
        the decision), and cross-backend parity (ADR 055 D7) is load-bearing.
        Routed HUMAN nodes would need a matching native change and are deferred.
        """
        method = _safe_method_name(nid)
        successor = self._sequential_successor(spec, nid)
        prompt = str(node.metadata.get("prompt", ""))
        output_contract = list(node.metadata.get("output_contract", []))
        approvers = list(node.metadata.get("approvers", []))
        timeout = node.metadata.get("timeout")
        on_timeout = node.metadata.get("on_timeout", "")

        oc_literal = "[" + ", ".join(repr(k) for k in output_contract) + "]"
        appr_literal = "[" + ", ".join(repr(a) for a in approvers) + "]"

        body = [
            f"# node {nid!r} — HUMAN (ADR 062 — durable HITL)",
            "# Persist the awaiting-human pause record (durable, listable via",
            "# ?status=paused); the workflow then parks until a human_response signal.",
            "await workflow.execute_activity(",
            "    call_human_activity,",
            f"    args=[{nid!r}, state, run_id, {prompt!r}, {oc_literal}, {appr_literal}, "
            f"{spec.name!r}, {spec.version!r}],",
            "    schedule_to_close_timeout=_SCHEDULE_TO_CLOSE,",
            "    heartbeat_timeout=_HEARTBEAT,",
            "    retry_policy=_RETRY_POLICY,",
            ")",
        ]

        # On a delivered signal: merge the human's response (the output_contract
        # keys) into state and advance to the sequential successor — byte-for-byte
        # the native runner's resume merge + _sequential_successor (ADR 055 D7).
        merge_lines = [
            f"{method}_response = self._human.pop({nid!r})",
            f"state.update({{k: {method}_response[k] for k in {oc_literal} "
            f"if k in {method}_response}})",
            f"current = {successor!r}",
        ]

        if timeout is None:
            # Wait forever — resolves only when the human_response signal arrives.
            body.append(f"await workflow.wait_condition(lambda: {nid!r} in self._human)")
            body.extend(merge_lines)
        else:
            # Durable deadline (ADR 062 D4): on expiry take the on_timeout route.
            seconds = float(timeout)
            if on_timeout:
                timeout_target = f"current = {on_timeout!r}"
            else:
                timeout_target = (
                    f'raise ApplicationError("HUMAN node {nid!r} timed out with no '
                    'on_timeout route")'
                )
            body += [
                "try:",
                "    await workflow.wait_condition(",
                f"        lambda: {nid!r} in self._human, timeout=timedelta(seconds={seconds}),",
                "    )",
                "except asyncio.TimeoutError:",
                f"    {timeout_target}",
                "else:",
                *[f"    {ln}" for ln in merge_lines],
            ]

        return body, {"call_human_activity"}

    @staticmethod
    def _sequential_successor(graph: WorkflowGraph, node_id: str) -> str | None:
        """The single sequential successor of ``node_id`` (or ``None`` at a sink).

        A faithful copy of :meth:`movate.core.workflow.runner.WorkflowRunner.
        _sequential_successor`: it filters out ``synthetic`` edges (the
        compiler-injected intent-router fan-out edges) so an AGENT/SKILL node
        advances down its real next-in-chain edge only. Keeping the rule
        identical to the native runner is what makes the dispatch loop's
        traversal match native node-for-node (ADR 055 D7).
        """
        seq = [e.to_id for e in graph.successors(node_id) if not e.metadata.get("synthetic")]
        return seq[0] if seq else None

    def _emit_activity_call(
        self,
        activity_name: str,
        node_id: str,
        args: tuple[str, ...],
        result_var: str,
    ) -> list[str]:
        """Emit the canonical ``await workflow.execute_activity(...)`` block.

        Centralised so the timeout / retry-policy / heartbeat defaults
        (ADR 054 D9) live in exactly one place. Phase 3 widens this to
        accept per-node overrides from ``workflow.yaml``.
        """
        joined = ", ".join(args)
        return [
            f"        # node {node_id!r} — activity={activity_name}",
            f"        {result_var} = await workflow.execute_activity(",
            f"            {activity_name},",
            f"            args=[{joined}],",
            "            schedule_to_close_timeout=_SCHEDULE_TO_CLOSE,",
            "            heartbeat_timeout=_HEARTBEAT,",
            "            retry_policy=_RETRY_POLICY,",
            "        )",
        ]

    def emit_bounded_loop(self, max_iterations: int, body_lines: list[str]) -> list[str]:
        """Emit ``for _i in range(max_iterations): ...`` (ADR 054 D4 row 6).

        Surfaced as a method so tests can assert the exact emitted shape
        without parsing an entire workflow.
        """
        indented = [f"        {ln}" if ln else "" for ln in body_lines]
        return [
            f"        for _i in range({max_iterations}):",
            *indented,
        ]

    def emit_fan_out(
        self,
        activity_name: str,
        node_ids: list[str],
        per_call_args: str = "node_id, state, run_id",
    ) -> list[str]:
        """Emit ``await asyncio.gather(*[workflow.execute_activity(...) ...])``.

        Per ADR 054 D4 row 8 — bounded, task-oriented fan-out. Used by the
        task-oriented pattern (see pattern_task_oriented governance: a
        FIXED roster of two task branches).
        """
        items = ", ".join(repr(n) for n in node_ids)
        return [
            "        # bounded fan-out — ADR 054 D4 row 8 (task-oriented pattern)",
            "        _fanout_results = await asyncio.gather(*[",
            "            workflow.execute_activity(",
            f"                {activity_name},",
            f"                args=[node_id, {per_call_args.split(', ', 1)[1]}],",
            "                schedule_to_close_timeout=_SCHEDULE_TO_CLOSE,",
            "                heartbeat_timeout=_HEARTBEAT,",
            "                retry_policy=_RETRY_POLICY,",
            "            )",
            f"            for node_id in [{items}]",
            "        ])",
        ]

    # -- ordering --------------------------------------------------------

    @staticmethod
    def _ordered_node_ids(graph: WorkflowGraph) -> list[str]:
        """Topological order when possible; insertion order for cyclic graphs.

        Mirrors :func:`langgraph._ordered_node_ids` so the two backends
        produce comparable file layouts (operator-friendly + makes
        cross-backend diffing simpler).
        """
        if not graph.nodes:
            return []
        if not graph.has_cycle():
            return graph.topological_order()
        # Cyclic — fall back to insertion order so the emit is still total.
        return list(graph.nodes.keys())


# ---------------------------------------------------------------------------
# Module-level wrappers (parity with compile_langgraph for symmetry).
# ---------------------------------------------------------------------------


def compile_temporal(graph: WorkflowGraph) -> CompiledWorkflow:
    """Convenience wrapper: ``TemporalCompiler().compile(graph)``.

    Mirrors :func:`compile_langgraph` so callers can pick a backend by
    swapping one import. Construct the class directly when you need to
    override timeouts / retry policy (Phase 3 surface).
    """
    return TemporalCompiler().compile(graph)


def lint_temporal(graph: WorkflowGraph) -> list[LintIssue]:
    """Convenience wrapper: ``TemporalCompiler().lint(graph)``.

    Useful from CLI (``mdk lint --runtime temporal``) and IDE plugins that
    want the lint findings without paying for the full compile.
    """
    return TemporalCompiler().lint(graph)


def supports_spec(spec: WorkflowSpec) -> bool:
    """Cheap readiness check for ``workflow.yaml: runtime: temporal``.

    HUMAN nodes are first-class as of ADR 062 (durable HITL), so every node
    type the validator accepts now compiles. Kept as a seam: a future
    not-yet-supported node type would gate here, letting the runner Protocol
    route to the native backend with a clear message.
    """
    return True


__all__ = [
    "LINT_NONDETERMINISTIC_SKILL",
    "LINT_NONDETERMINISTIC_TIME",
    "LINT_UNBOUNDED_LOOP",
    "CompiledWorkflow",
    "CompilerProtocol",
    "LintIssue",
    "TemporalCompiler",
    "compile_temporal",
    "lint_temporal",
    "supports_spec",
]
