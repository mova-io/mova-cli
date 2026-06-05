"""LangSmith trace export adapter — forward mdk per-node spans to LangSmith.

LangSmith is LangChain's observability platform. This adapter wraps the mdk
:class:`~movate.tracing.base.Tracer` Protocol and additionally sends every
span to LangSmith as a *run*, preserving the parent-child hierarchy so the
full workflow tree renders in the LangSmith UI.

Install with::

    uv sync --extra langchain

Enable LangSmith tracing::

    export LANGSMITH_API_KEY=ls-...
    export LANGSMITH_PROJECT=my-project
    export MOVATE_TRACER=langsmith
    mdk run my-workflow --runtime langgraph

Or via the deployment sink selector (ADR 015)::

    export MOVATE_TRACE_SINK=langsmith

Activation precedence (see :func:`movate.tracing.build_tracer`):

1. ``MOVATE_TRACE_SINK=langsmith`` — explicit deployment opt-in.
2. ``MOVATE_TRACER=langsmith`` — explicit opt-in (legacy path).

Both paths gracefully fall back to the silent tracer (with a stderr warning)
if the ``langsmith`` package isn't importable or if the API key is missing.
Tracing must never break a run.

Run-type mapping:

* ``run_type="chain"``  — workflow-level spans (the outermost orchestration).
* ``run_type="llm"``    — agent-node spans (an individual LLM call).
* ``run_type="tool"``   — skill spans (tool invocations inside an agent).

The local :class:`~movate.tracing.base.SpanCtx` stays a pure dataclass; the
LangSmith run ids live in a private dict keyed by ``span_id`` so callers
never see SDK objects.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from movate.tracing.base import SpanCtx, Tracer

logger = logging.getLogger(__name__)

# Attribute keys the adapter inspects to choose the LangSmith run_type.
# Callers (executor / runtime) set these on span attrs before start_span.
_SPAN_KIND_KEY = "mdk.span_kind"

# Mapping from mdk span-kind values to LangSmith run_type strings.
_RUN_TYPE_MAP: dict[str, str] = {
    "workflow": "chain",
    "chain": "chain",
    "agent": "llm",
    "llm": "llm",
    "skill": "tool",
    "tool": "tool",
}

_DEFAULT_RUN_TYPE = "chain"


class LangSmithUnavailableError(Exception):
    """Raised when the langsmith package isn't installed or the client can't init."""


class LangSmithTracer(Tracer):
    """Forward :class:`Tracer` Protocol calls to LangSmith as runs.

    Each mdk span becomes a LangSmith run. Parent-child relationships are
    preserved so the LangSmith UI renders the full workflow tree.
    """

    name = "langsmith"

    def __init__(self, *, client: Any | None = None) -> None:
        """Construct from an existing client, or build one from env vars.

        ``client=`` is the test seam: pass a stub that mirrors the subset
        of the ``langsmith.Client`` API we use — ``create_run(...)`` and
        ``update_run(...)``.
        """
        if client is None:
            client = _build_client_from_env()
        self._client = client
        self._project = os.environ.get("LANGSMITH_PROJECT", "default").strip() or "default"
        # span_id -> (run_id: uuid, start_time: datetime)
        self._runs: dict[str, tuple[str, datetime]] = {}
        # span_id -> parent run_id (for linking LangSmith parent/child)
        self._parent_run_ids: dict[str, str | None] = {}

    # ----- start ------------------------------------------------------------

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        attributes = dict(attrs or {})
        run_id = str(uuid4())
        now = datetime.now(UTC)

        # Resolve the LangSmith parent run id from the mdk parent span.
        parent_run_id: str | None = None
        trace_id: str
        if parent is not None:
            entry = self._runs.get(parent.span_id)
            parent_run_id = entry[0] if entry is not None else None
            trace_id = parent.trace_id
        else:
            trace_id = str(uuid4())

        # Build the mdk SpanCtx first (always succeeds).
        ctx = SpanCtx(
            trace_id=trace_id,
            parent_id=parent.span_id if parent else None,
            name=name,
            attributes=attributes,
        )

        # Determine the LangSmith run_type from span attributes.
        span_kind = str(attributes.get(_SPAN_KIND_KEY, "")).lower()
        run_type = _RUN_TYPE_MAP.get(span_kind, _DEFAULT_RUN_TYPE)

        # Create the LangSmith run — fail-soft.
        with contextlib.suppress(Exception):
            self._client.create_run(
                name=name,
                run_type=run_type,
                run_id=run_id,
                parent_run_id=parent_run_id,
                project_name=self._project,
                inputs=_safe_metadata(attributes),
                extra={"metadata": _safe_metadata(attributes)},
                start_time=now,
            )

        self._runs[ctx.span_id] = (run_id, now)
        self._parent_run_ids[ctx.span_id] = parent_run_id
        return ctx

    # ----- end --------------------------------------------------------------

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        run_entry = self._runs.pop(span.span_id, None)
        self._parent_run_ids.pop(span.span_id, None)
        if run_entry is None:
            return
        run_id, _start_time = run_entry
        now = datetime.now(UTC)
        error = status if status != "ok" else None

        with contextlib.suppress(Exception):
            self._client.update_run(
                run_id=run_id,
                end_time=now,
                error=error,
                outputs={"status": status},
            )

    # ----- events / attributes ---------------------------------------------

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        run_entry = self._runs.get(span.span_id)
        if run_entry is None:
            return
        run_id, _ = run_entry
        # LangSmith doesn't have a first-class "event" API on runs, so we
        # append events to the run's extra metadata via update_run.
        with contextlib.suppress(Exception):
            self._client.update_run(
                run_id=run_id,
                extra={"events": [event]},
            )

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value
        run_entry = self._runs.get(span.span_id)
        if run_entry is None:
            return
        run_id, _ = run_entry
        with contextlib.suppress(Exception):
            self._client.update_run(
                run_id=run_id,
                extra={"metadata": {key: _safe_value(value)}},
            )

    # ----- generation -------------------------------------------------------

    def log_generation(
        self,
        span: SpanCtx,
        *,
        model: str,
        input_messages: list[dict[str, Any]],
        output_text: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        """Record an LLM completion on the LangSmith run.

        Maps the generation data to LangSmith's run outputs and extra
        metadata so token usage and model info appear in the UI.
        """
        run_entry = self._runs.get(span.span_id)
        if run_entry is None:
            return
        run_id, _ = run_entry
        with contextlib.suppress(Exception):
            self._client.update_run(
                run_id=run_id,
                outputs={"output": output_text},
                extra={
                    "metadata": {
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                        "cost_usd": cost_usd,
                    },
                },
            )

    # ----- lifecycle --------------------------------------------------------

    def flush(self) -> None:
        """Flush any pending requests in the LangSmith client's background queue."""
        # The LangSmith Client manages its own background thread + queue.
        # Calling ``flush()`` (if available) ensures spans land before
        # process exit.
        flush_fn = getattr(self._client, "flush", None)
        if callable(flush_fn):
            with contextlib.suppress(Exception):
                flush_fn()

    def shutdown(self) -> None:
        """Flush pending data. LangSmith client cleanup is minimal."""
        self.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_value(value: Any) -> Any:
    """Coerce a value to something JSON-serializable for LangSmith."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}
    return str(value)


def _safe_metadata(attrs: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-safe metadata dict from span attributes."""
    return {str(k): _safe_value(v) for k, v in attrs.items()}


def _build_client_from_env() -> Any:
    """Construct a LangSmith ``Client`` from environment variables.

    Reads the standard LangSmith env vars:

    * ``LANGSMITH_API_KEY`` — required.
    * ``LANGSMITH_ENDPOINT`` — optional; defaults to the LangSmith cloud.

    Raises :class:`LangSmithUnavailableError` if the package is missing or
    the API key is unset. The caller catches and falls back to silent/stdout.
    """
    api_key = os.environ.get("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        raise LangSmithUnavailableError("LANGSMITH_API_KEY must be set to enable LangSmith tracing")
    try:
        from langsmith import Client  # noqa: PLC0415 — lazy by design
    except ImportError as exc:
        raise LangSmithUnavailableError(
            "langsmith package not installed; install with: uv sync --extra langchain"
        ) from exc
    try:
        return Client(api_key=api_key)
    except Exception as exc:
        raise LangSmithUnavailableError(f"langsmith Client init failed: {exc}") from exc
