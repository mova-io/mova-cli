"""Temporal activity wrappers — Phase 1 of ADR 054 Track C.

This module ships **two** activities — :func:`call_agent_activity` and
:func:`call_skill_activity` — that the ADR 054 Temporal compiler emits as the
**only** way an mdk workflow node touches an LLM or a skill when
``workflow.yaml: runtime: temporal`` is selected.

The single load-bearing property of this module is **ADR 054 D3: activities
REUSE the existing mdk execution path**. Concretely:

* :func:`call_agent_activity` forwards to
  :meth:`movate.core.executor.Executor.execute` exactly as the native runner
  and the LangGraph backend do — same tool-use loop, same provider chain, same
  retry/fallback, same tracing, same metering, same BYOK, same session input
  assembly. The :class:`~movate.core.executor.Executor` does NOT know it is
  running under Temporal. There is no second execution model, no bypass route,
  no Temporal-specific Executor.
* :func:`call_skill_activity` forwards to
  :func:`movate.core.skill_backend.dispatch_skill` exactly as the executor's
  tool-use loop does for an inside-agent tool call — same four backends
  (python, http, mcp, agent), same :class:`SkillExecutionContext`, same
  :class:`SkillError` taxonomy. Used by the compiler for **structural**
  skill-call nodes (a skill that the workflow itself wires in, vs. a skill
  the agent's LLM picks via tool-use — those still run *inside*
  :func:`call_agent_activity` via the existing tool-use loop).

Phase 1 wiring details:

* **Parent-span propagation** — the caller (the compiler-generated workflow
  body) passes ``parent_span_context`` so the activity's
  ``agent.execute`` / ``dispatch_skill`` spans nest under the workflow root,
  preserving the ADR 024 trace tree across the workflow/activity boundary.
  Same pattern Track B (#594) shipped for the SkillBackend python backend.
* **session_id threading** (ADR 054 D10 — sessions hold conversation state,
  Temporal holds control flow) — the activity accepts a ``session_id`` and
  threads it onto the :class:`RunRequest` so conversation history lives in
  the session store, not in Temporal workflow history.
* **workflow_id == run_id** (ADR 054 D6) — the caller passes the Temporal
  ``workflow_id`` (which IS the mdk ``workflow_run_id``); the activity passes
  it through to :meth:`Executor.execute` as ``workflow_run_id=``, so persisted
  :class:`RunRecord`s carry the same id Temporal Web shows for the workflow.
* **Heartbeating** — for long LLM calls (deep-research / reflection) the
  activity spawns a background heartbeat task at 10s cadence so Temporal's
  ``heartbeat_timeout`` (D9) does not fire spuriously. ``temporalio.activity``
  raises if heartbeat is called outside an activity context, which is exactly
  what we want for the import-safe / unit-test path: the task only runs when
  the activity is actually inside a Temporal worker.
* **Lazy temporalio import** — this module is import-safe without the
  ``mdk[temporal]`` extra installed (mirrors ADR 030 D1 for LangGraph). The
  ``@activity.defn`` decorator binds lazily inside the helpers, and the
  ``_require_temporalio`` shim raises a clear remediation message if the
  extra is missing.

Nothing in this module touches the native runner, the LangGraph backend, or
any storage / tracing / metering primitive that the existing path does not
already touch. Adding a backend never grows the seam — that is the
non-negotiable D3 boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.executor import Executor

log = logging.getLogger(__name__)

# Heartbeat cadence for long activity bodies. Paired with the default
# ``heartbeat_timeout = 30s`` (ADR 054 D9) — a 10s cadence gives Temporal
# three signals per timeout window, so a single dropped heartbeat does not
# cause a spurious retry. The task fires inside ``call_agent_activity`` for
# the duration of ``Executor.execute`` and is cancelled on completion.
_HEARTBEAT_CADENCE_S = 10.0


def _require_temporalio() -> Any:
    """Lazy-import ``temporalio.activity`` so this module is safe to import
    without the ``mdk[temporal]`` extra installed.

    Mirrors the lazy-import contract ADR 030 D1 established for LangGraph:
    the only thing that pulls in the heavy SDK is calling the activity
    body or wrapping a function with the decorator. The decorator binding
    in this module's body falls through ``_NoopActivityModule`` if
    ``temporalio`` is absent so module import never fails — the activity
    just is not registered with any worker.
    """
    try:
        from temporalio import activity  # noqa: PLC0415

        return activity
    except ImportError as exc:  # pragma: no cover - exercised by lazy-import test
        raise RuntimeError(
            "The [temporal] extra is not installed. "
            "Install with: uv tool install --editable '.[temporal]' --force"
        ) from exc


class _NoopActivityModule:
    """Stand-in for :mod:`temporalio.activity` when the extra is missing.

    Provides a no-op ``defn`` decorator + a no-op ``heartbeat`` so the
    module *imports* cleanly without ``temporalio`` installed. Calling the
    decorated activity body still works (it is just a coroutine); only
    *registering* it with a Temporal worker requires the real SDK, and
    that happens in the worker boot path (out of scope for Phase 1
    Track C, lands in Track A's ``mdk worker --backend temporal``).
    """

    @staticmethod
    def defn(*_args: Any, **_kwargs: Any) -> Any:
        def _decorator(func: Any) -> Any:
            return func

        return _decorator

    @staticmethod
    def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        # No-op outside a Temporal worker context. Production calls go
        # through the real ``temporalio.activity.heartbeat`` which raises
        # outside an activity — we want the unit-test / import-safe path
        # to be silent.
        return None


def _activity_module() -> Any:
    """Return :mod:`temporalio.activity` if available, else the no-op stub.

    Centralizes the lazy-import so the two ``@activity.defn`` bindings
    below resolve at module-load time without forcing the dependency.
    Tests that want to assert the real-SDK path stub ``temporalio.activity``
    directly via ``unittest.mock.patch``.
    """
    try:
        from temporalio import activity  # noqa: PLC0415

        return activity
    except ImportError:
        return _NoopActivityModule()


# Bound once at module load; ``@activity.defn`` runs once per function and
# either registers with the real SDK or no-ops via the stub. Either way the
# wrapped coroutine is callable directly (the production path goes through
# Temporal's worker; the unit-test path calls the coroutine directly with
# mocked collaborators — see tests/test_temporal_activities.py).
_activity = _activity_module()


def _spanctx_from_dict(parent_span_context: dict[str, Any] | None) -> Any:
    """Reconstruct a :class:`movate.tracing.base.SpanCtx` from a serialized
    dict so a Temporal-marshalled trace context can re-parent the activity's
    inner spans.

    Temporal serializes activity inputs as JSON-safe payloads. The compiler
    captures the workflow's root SpanCtx as ``{"trace_id", "span_id", ...}``
    and passes it through; this helper rebuilds the dataclass on the
    activity side so it can be passed directly to
    ``Executor.execute(parent_span=...)`` and ``Tracer.start_span(parent=...)``.

    Returns ``None`` when no context is supplied — every standalone caller
    (including the unit tests for this module) gets the byte-for-byte
    no-parent behavior of the existing native runner.
    """
    if parent_span_context is None:
        return None
    # Lazy import — keeps this module import-light + matches the seam rule
    # (workflow modules don't pull tracing at module scope).
    from movate.tracing.base import SpanCtx  # noqa: PLC0415

    return SpanCtx(
        span_id=parent_span_context.get("span_id", ""),
        trace_id=parent_span_context.get("trace_id", ""),
        parent_id=parent_span_context.get("parent_id"),
        name=parent_span_context.get("name", ""),
        attributes=parent_span_context.get("attributes", {}) or {},
    )


async def _heartbeat_loop(cadence_s: float = _HEARTBEAT_CADENCE_S) -> None:
    """Heartbeat task fired inside ``call_agent_activity`` for long bodies.

    Emits a Temporal heartbeat every ``cadence_s`` seconds so Temporal's
    ``heartbeat_timeout`` (ADR 054 D9, default 30s) does not fire a
    spurious retry on a still-running long LLM call. Cancelled on activity
    completion. Outside a real worker, ``activity.heartbeat`` is the
    no-op stub above (or raises in the real SDK, which we swallow) — the
    loop is harmless in tests.
    """
    while True:
        try:
            await asyncio.sleep(cadence_s)
        except asyncio.CancelledError:
            raise
        # ``heartbeat`` only does anything inside an active Temporal worker;
        # outside one, swallow any error so the loop is harmless in tests.
        with contextlib.suppress(Exception):
            _activity.heartbeat()


def _build_executor() -> Executor:
    """Construct an Executor wired to the same stack the runtime + native
    runner use.

    Track C Phase 1 ships the activity *body*; the worker bootstrap (which
    will inject a long-lived Executor via dependency injection, mirroring
    ``mdk worker``'s existing scaffolding) lands in Track A's
    ``mdk worker --backend temporal``. Until then this helper builds a
    fresh Executor per activity invocation against the production stack
    so the activity body is fully exercised end-to-end. Tests bypass this
    helper entirely by patching :class:`movate.core.executor.Executor`.

    Lazy imports keep this module's import graph light — wiring a provider /
    pricing table / storage at module scope would pull half of mdk through
    every time this file is loaded.
    """
    from movate.core.executor import Executor as _Executor  # noqa: PLC0415
    from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415
    from movate.tracing import build_tracer  # noqa: PLC0415

    return _Executor(
        provider=LiteLLMProvider(),
        pricing=load_pricing(),
        storage=InMemoryStorage(),
        tracer=build_tracer(),
    )


@_activity.defn(name="mdk.call_agent")  # type: ignore[untyped-decorator]
async def call_agent_activity(
    agent_ref: str,
    request_json: dict[str, Any],
    session_id: str | None = None,
    parent_span_context: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    tenant_id: str | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Wrap a full mdk agent run as a Temporal activity (ADR 054 D3).

    Reuses :meth:`movate.core.executor.Executor.execute` unchanged — same
    skills, same tracing, same metering, same BYOK, same fallback chain.
    The Executor does not know it is running under Temporal; this function
    is a thin shim that translates Temporal's serialized inputs into the
    native execution call and serializes the :class:`RunResponse` back.

    ADR 054 wirings honored:

    * D3 — call goes straight to ``Executor.execute(bundle, request, ...)``.
    * D6 — ``workflow_id`` is forwarded as ``workflow_run_id=`` so the
      persisted :class:`RunRecord` shares its identity with Temporal Web.
    * D9 — a background heartbeat task fires every 10s for the duration of
      the underlying call so a long LLM round-trip does not trigger a
      spurious retry.
    * D10 — ``session_id`` is threaded onto the :class:`RunRequest` so
      conversation state lives in the session store, not in workflow history.
    * D11 — tracing + metering flow through ``Executor.execute`` exactly as
      they do today; no metering shim lives in this file.

    :param agent_ref: Filesystem path (or registry name) of the agent bundle
        to load — same shape :func:`movate.core.loader.load_agent` accepts.
    :param request_json: Serialized :class:`RunRequest` payload (``input``,
        optional ``user_id``, ``request_id``, etc.).
    :param session_id: Optional session id; when set, propagates to the
        ``RunRequest.session_id`` field so the executor's tracing carries it
        and the session store can be looked up downstream.
    :param parent_span_context: Optional serialized
        :class:`~movate.tracing.base.SpanCtx`; when supplied, the activity's
        root ``agent.execute`` span nests under it.
    :param workflow_id: The Temporal workflow id (== mdk ``workflow_run_id``
        per D6). Forwarded as ``workflow_run_id=`` to the executor.
    :param tenant_id: The tenant id for this run; forwarded as
        ``tenant_id_override=`` so persistence / budget checks use it.
    :param node_id: Optional workflow node id; forwarded as ``node_id=`` to
        stamp the persisted :class:`RunRecord`.

    :returns: Serialized :class:`RunResponse` dict.
    """
    # Lazy imports — module import stays light + adapter-seam rule is
    # preserved (the activity reaches into core only at call time).
    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.core.models import RunRequest  # noqa: PLC0415

    bundle = load_agent(agent_ref)

    # Reconstruct the RunRequest from the wire payload. Threading
    # ``session_id`` through the request (D10) keeps conversation state
    # in the session store; nothing lands in Temporal history.
    request_kwargs: dict[str, Any] = dict(request_json)
    # Ensure the request matches the loaded bundle so the executor's
    # downstream agent-name check stays correct even if the caller passed
    # a slightly different alias.
    request_kwargs.setdefault("agent", bundle.spec.name)
    if session_id is not None:
        request_kwargs["session_id"] = session_id
    request = RunRequest(**request_kwargs)

    parent_span = _spanctx_from_dict(parent_span_context)
    executor = _build_executor()

    # Heartbeat loop guards long activity bodies (D9). Cancelled on
    # completion; the suppress around heartbeat() keeps the loop harmless
    # outside a real worker context.
    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    try:
        response = await executor.execute(
            bundle,
            request,
            workflow_run_id=workflow_id,
            node_id=node_id,
            parent_span=parent_span,
            tenant_id_override=tenant_id,
        )
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat_task

    # Pydantic v2 ``model_dump`` returns a JSON-safe dict — Temporal can
    # marshal it directly without bespoke converters.
    return response.model_dump(mode="json")


@_activity.defn(name="mdk.call_skill")  # type: ignore[untyped-decorator]
async def call_skill_activity(
    skill_ref: str,
    input_json: dict[str, Any],
    parent_span_context: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    tenant_id: str | None = None,
    agent_ref: str | None = None,
    call_ms_budget: int | None = None,
) -> dict[str, Any]:
    """Wrap a single skill call as a Temporal activity.

    Reuses :func:`movate.core.skill_backend.dispatch_skill` unchanged — same
    four backends (python, http, mcp, agent), same :class:`SkillError`
    taxonomy, same input/output validation. Used by the compiler when a
    workflow node is a **structural** skill call (vs. a skill the agent's
    LLM picks via tool-use; those still run *inside* :func:`call_agent_activity`
    through the existing tool-use loop, never this entrypoint).

    Skill resolution order:

    * ``skill_ref`` as a filesystem path containing a ``skill.yaml`` →
      :func:`movate.core.skill_loader.load_skill`.
    * ``skill_ref`` as a bare skill name → resolved against the bundle at
      ``agent_ref`` if provided (matches the executor's tool-use loop
      semantics: skills live on an agent), else raises a clear error.

    :param skill_ref: Filesystem path to a skill bundle OR a bare skill name
        (resolved against ``agent_ref``'s bundle when supplied).
    :param input_json: Skill input dict (validated against the skill's
        ``input`` schema by :func:`dispatch_skill`).
    :param parent_span_context: Optional serialized
        :class:`~movate.tracing.base.SpanCtx`; when supplied, the skill's
        backend-side spans nest under it.
    :param workflow_id: The Temporal workflow id (== mdk ``workflow_run_id``
        per D6). Forwarded into the :class:`SkillExecutionContext`.
    :param tenant_id: The tenant id for this skill call; forwarded into the
        :class:`SkillExecutionContext` so backends scoped to a tenant
        (HTTP / MCP) honor it.
    :param agent_ref: Optional agent bundle to resolve ``skill_ref`` against
        when it is a bare name rather than a path.
    :param call_ms_budget: Optional override for the call timeout (ms);
        defaults to the skill's own ``timeout_call_ms`` or 30s when neither
        side specifies.

    :returns: The validated skill output dict.
    """
    # Lazy imports — same import-graph discipline as ``call_agent_activity``.
    from pathlib import Path  # noqa: PLC0415

    from movate.core.skill_backend import (  # noqa: PLC0415
        SkillExecutionContext,
        dispatch_skill,
    )

    # Side-effect-only imports register each backend with the dispatch
    # registry. Same pattern the executor uses (executor.py imports them
    # inside ``_run_with_tool_use``) so we stay symmetric.
    from movate.core.skill_backend import agent as _agent_backend  # noqa: F401, PLC0415
    from movate.core.skill_backend import http as _http_backend  # noqa: F401, PLC0415
    from movate.core.skill_backend import mcp as _mcp_backend  # noqa: F401, PLC0415
    from movate.core.skill_backend import python as _python_backend  # noqa: F401, PLC0415
    from movate.core.skill_loader import load_skill  # noqa: PLC0415

    skill_path = Path(skill_ref)
    if skill_path.is_dir() and (skill_path / "skill.yaml").exists():
        skill = load_skill(skill_path)
    elif agent_ref is not None:
        # Bare name + agent context: resolve against the agent bundle's
        # already-loaded skills list. This is the same lookup shape the
        # executor's tool-use loop does (skill_index in _run_with_tool_use).
        from movate.core.loader import load_agent  # noqa: PLC0415

        bundle = load_agent(agent_ref)
        match = next((s for s in bundle.skills if s.spec.name == skill_ref), None)
        if match is None:
            raise ValueError(
                f"skill {skill_ref!r} is not declared on agent {agent_ref!r}; "
                f"available: {sorted(s.spec.name for s in bundle.skills)}"
            )
        skill = match
    else:
        # No agent context + not a directory: surface as a clear error rather
        # than silently guessing. The compiler is responsible for picking the
        # right ref shape.
        raise ValueError(
            f"skill_ref {skill_ref!r} is not a skill directory and no agent_ref "
            "was provided to resolve it against"
        )

    parent_span = _spanctx_from_dict(parent_span_context)
    # Construct the SkillExecutionContext with the same fields the executor's
    # tool-use loop populates so backends see an identical shape across the
    # native + Temporal paths.
    effective_budget = (
        call_ms_budget
        if call_ms_budget is not None
        else (skill.spec.timeout_call_ms if skill.spec.timeout_call_ms else 30_000)
    )
    ctx = SkillExecutionContext(
        trace_id=parent_span.trace_id if parent_span is not None else "",
        tenant_id=tenant_id or "local",
        run_id=workflow_id or "",
        call_ms_budget=effective_budget,
        agent_name=agent_ref or "",
        parent_span=parent_span,
    )

    output = await dispatch_skill(skill, input_json, ctx)
    return output


__all__ = [
    "call_agent_activity",
    "call_skill_activity",
]
