"""SkillBackend Protocol + the SkillError taxonomy.

The Protocol defines the one method the executor's tool-use loop calls
to dispatch a skill invocation. Three backends will implement it
(python, http, mcp); v0.6 ships only the Python backend.

``dispatch_skill`` is the top-level entry the executor uses — it
selects the backend that matches ``SkillSpec.implementation.kind``,
validates the input against the skill's schema, calls the backend,
and validates the output. Every failure mode maps to one of five
:class:`SkillErrorType` values (D2 in ADR 002) so the LLM sees a
consistent vocabulary in ``tool_result`` blocks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from jsonschema import ValidationError as JSONSchemaValidationError

from movate.core.models import SkillImplementationKind

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle


class SkillErrorType(StrEnum):
    """The five error categories surfaced to the LLM in tool_result blocks.

    Mirrors :class:`ErrorInfo.type` on :class:`RunResponse` so the
    operator vocabulary stays consistent — an operator triaging a
    failed run already knows what each ``type`` means. ADR 002 D2.
    """

    NOT_FOUND = "not_found"
    """Skill name doesn't resolve to a registered skill. Surfaces only
    if the agent loader's pre-flight check is skipped (e.g. dynamically
    constructed agent). Normal path catches this earlier."""

    VALIDATION_FAILED = "validation_failed"
    """Either the input the LLM provided doesn't conform to the skill's
    input schema, OR the backend returned an output that doesn't
    conform to the output schema. Both directions are this type so
    the operator gets one error to triage rather than two."""

    BACKEND_ERROR = "backend_error"
    """The backend raised an unhandled exception (Python ``impl.py``
    raised, HTTP returned non-2xx, MCP returned error). The original
    exception message is preserved in ``SkillError.message``."""

    TIMEOUT = "timeout"
    """The backend exceeded the skill's timeout budget — either the
    skill's own ``timeout_call_ms`` or the agent's inherited
    ``timeouts.call_ms``."""

    BUDGET_EXCEEDED = "budget_exceeded"
    """The cumulative skill cost for this run pushed past
    ``policy.max_cost_per_run_usd`` or the agent's
    ``budget.max_cost_usd_per_run``. The skill call doesn't execute —
    the LLM sees this in the tool_result and can fall back to a
    different strategy or terminate."""


@dataclass(frozen=True)
class SkillError(Exception):
    """Structured error returned to the LLM in a tool_result block.

    Frozen + structured rather than a free-form Exception subclass so
    the tool-use loop has one shape to forward to the model. The
    ``type`` field is what gates the LLM's recovery strategy; the
    ``message`` is human-readable detail.
    """

    type: SkillErrorType
    message: str

    def __str__(self) -> str:
        return f"[{self.type.value}] {self.message}"


@dataclass
class SkillExecutionContext:
    """Side-channel information a backend may need.

    Trace ids + tenant id flow in so a Python backend can attach
    them to its own outbound calls (a `tool_call` span gets parented
    to the agent run span, etc.). ``call_ms_budget`` is the
    effective timeout for this specific invocation, already resolved
    from skill override or agent inheritance.

    ``mock`` signals that the calling agent was invoked in mock/eval
    mode (``mdk eval --mock``). Backends that call external resources
    (HTTP, MCP, remote agents) should short-circuit to a deterministic
    stub response when this is ``True``.

    ``agent_name`` + ``storage`` were added in PR-I so the
    ``kb-vector-lookup`` skill can query the agent's KB chunks
    without building its own storage. ``retrieval`` carries the
    agent's :class:`RetrievalConfig` (hybrid / rewrite / rerank /
    multi_hop flags from ``agent.yaml``) so the skill applies the
    operator's configured pipeline. All three are typed as ``Any``
    to keep the import surface flat (no circular imports against
    storage / models), and the kb-lookup skill is the only consumer
    today.

    Kept narrow on purpose — the backend is a function, not a god
    object. If a backend needs more, we add a field deliberately.
    """

    trace_id: str = ""
    tenant_id: str = "local"
    run_id: str = ""
    call_ms_budget: int = 30_000
    mock: bool = False
    agent_name: str = ""
    storage: Any = None
    retrieval: Any = None
    tracer: Any = None
    """The active :class:`movate.tracing.base.Tracer` for this run
    (PR-V). Skills that produce nested observability (e.g.
    ``kb-vector-lookup``'s retrieval stages) emit child spans against
    this. ``None`` for backends that don't need it."""
    parent_span: Any = None
    """The :class:`movate.tracing.base.SpanCtx` to parent child spans
    under (PR-V). Typically the skill's own dispatch span if the
    backend creates one, or the agent run span otherwise."""


class SkillBackend(Protocol):
    """One backend per :class:`SkillImplementationKind` value.

    Implementations live in sibling modules (``python.py``, eventually
    ``http.py`` + ``mcp.py``). Each is registered with the executor by
    name; the dispatch helper below picks the right one per skill.
    """

    kind: SkillImplementationKind

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        """Run the skill against ``input``, return the raw output dict.

        Schema validation (both directions) happens in :func:`dispatch_skill`
        before/after the backend call — backends focus on the actual
        execution, not contract enforcement.

        Raises :class:`SkillError` directly for backend-specific
        failures (e.g. HTTP 5xx, MCP error response). Unhandled
        exceptions are caught by ``dispatch_skill`` and wrapped as
        ``SkillError(BACKEND_ERROR)``.
        """
        ...


# ---------------------------------------------------------------------------
# Backend registry (populated by `register_backend` from each backend module)
# ---------------------------------------------------------------------------

_BACKENDS: dict[SkillImplementationKind, SkillBackend] = {}


def register_backend(backend: SkillBackend) -> None:
    """Add a backend to the dispatch registry.

    Called from each backend module's import. Idempotent — re-registering
    the same kind overwrites the previous binding, which is what we want
    for tests that swap in a stub backend."""
    _BACKENDS[backend.kind] = backend


def _backend_for(kind: SkillImplementationKind) -> SkillBackend | None:
    """Return the registered backend for ``kind``, or None if unsupported."""
    return _BACKENDS.get(kind)


# ---------------------------------------------------------------------------
# Top-level dispatch — what the executor's tool-use loop calls
# ---------------------------------------------------------------------------


async def dispatch_skill(
    skill: SkillBundle,
    input: dict[str, Any],
    ctx: SkillExecutionContext,
) -> dict[str, Any]:
    """Validate, dispatch, validate. Returns the validated output dict.

    All failures map to :class:`SkillError`; the executor catches and
    forwards to the LLM as a ``tool_result``. The five
    :class:`SkillErrorType` values cover the exhaustive failure
    space. ADR 002 D2.

    Wraps the backend call in ``asyncio.wait_for`` so a skill that
    hangs doesn't tie up the whole tool-use loop — ``call_ms_budget``
    is the effective timeout (skill-override or agent-inherited).
    """
    # Input schema check — fail fast before invoking the backend.
    # Validation errors here are the LLM's fault (its tool_input
    # didn't match the schema we told it about).
    try:
        skill.input_validator.validate(input)
    except JSONSchemaValidationError as exc:
        raise SkillError(
            type=SkillErrorType.VALIDATION_FAILED,
            message=f"input did not match skill {skill.spec.name!r} input schema: {exc.message}",
        ) from exc

    backend = _backend_for(skill.spec.implementation.kind)
    if backend is None:
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=(
                f"no backend registered for skill kind "
                f"{skill.spec.implementation.kind.value!r}; only "
                f"{sorted(b.value for b in _BACKENDS)} available"
            ),
        )

    timeout_seconds = ctx.call_ms_budget / 1000.0
    try:
        output = await asyncio.wait_for(
            backend.execute(skill, input, ctx),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise SkillError(
            type=SkillErrorType.TIMEOUT,
            message=(f"skill {skill.spec.name!r} exceeded timeout {ctx.call_ms_budget}ms"),
        ) from exc
    except SkillError:
        # Backend raised a structured SkillError — pass through unchanged.
        raise
    except Exception as exc:
        # Anything else is a backend bug, wrap it. Don't lose the
        # original message — operators need it for debug.
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=f"skill {skill.spec.name!r} raised: {type(exc).__name__}: {exc}",
        ) from exc

    # Output schema check — the backend's output is the model contract.
    # Validation errors here are a *skill* bug; the LLM didn't do
    # anything wrong. We still surface as VALIDATION_FAILED so the
    # taxonomy stays consistent.
    try:
        skill.output_validator.validate(output)
    except JSONSchemaValidationError as exc:
        raise SkillError(
            type=SkillErrorType.VALIDATION_FAILED,
            message=(
                f"skill {skill.spec.name!r} returned output that didn't match "
                f"its declared output schema: {exc.message}"
            ),
        ) from exc

    return output
