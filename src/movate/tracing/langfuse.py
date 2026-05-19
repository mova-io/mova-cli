"""Langfuse tracer — opt-in, env-gated, fail-soft.

Langfuse is an optional dependency. Install with::

    uv sync --extra langfuse

Activation precedence (see :func:`movate.tracing.build_tracer`):

1. ``MOVATE_TRACER=langfuse`` — explicit opt-in.
2. ``LANGFUSE_SECRET_KEY`` set in the environment — implicit opt-in.

Either path will gracefully fall back to the stdout tracer (with a stderr
warning) if the langfuse package isn't importable or if the SDK rejects
our keys at construction time. We never let tracing break a run.

Span model:

* The first ``start_span`` on a fresh tracer instance creates a Langfuse
  *trace*. Nested spans (those passed a ``parent``) become Langfuse spans
  under that trace.
* ``log_event`` maps to ``span.event(name="event", metadata=event)``.
* ``set_attribute`` mirrors the value into the span's metadata via
  ``span.update(metadata={key: value})``.
* ``end_span`` closes the underlying Langfuse object with ``status_message``.

The local ``SpanCtx`` stays a pure dataclass — Langfuse handles are kept
in a private dict keyed by ``span_id`` so callers never see SDK objects.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from movate.tracing.base import SpanCtx, Tracer


class LangfuseUnavailableError(Exception):
    """Raised when the langfuse package isn't installed or the client can't init."""


class LangfuseTracer(Tracer):
    """Forwards :class:`Tracer` Protocol calls to Langfuse v2 SDK objects."""

    name = "langfuse"

    def __init__(self, *, client: Any | None = None) -> None:
        """Construct from an existing client, or build one from env vars.

        ``client=`` is the test seam: pass a stub that exposes
        ``trace(...)`` returning an object with ``span(...)``, ``event(...)``,
        ``update(...)``, ``end(...)``, plus a top-level ``flush()``.
        """
        if client is None:
            client = _build_client_from_env()
        self._client = client
        # span_id → langfuse handle (trace or span). Lookups are O(1) and
        # unbounded growth is bounded by the lifetime of the run since
        # ``end_span`` pops.
        self._handles: dict[str, Any] = {}

    # ----- start ------------------------------------------------------------

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        attributes = dict(attrs or {})
        if parent is None:
            handle = self._client.trace(name=name, metadata=attributes)
            trace_id = getattr(handle, "id", None) or str(uuid4())
            ctx = SpanCtx(
                trace_id=trace_id,
                parent_id=None,
                name=name,
                attributes=attributes,
            )
        else:
            parent_handle = self._handles.get(parent.span_id)
            if parent_handle is None:
                # Parent's already ended — fall back to a top-level trace
                # rather than dropping the span on the floor.
                handle = self._client.trace(name=name, metadata=attributes)
                trace_id = getattr(handle, "id", None) or parent.trace_id
            else:
                handle = parent_handle.span(name=name, metadata=attributes)
                trace_id = parent.trace_id
            ctx = SpanCtx(
                trace_id=trace_id,
                parent_id=parent.span_id,
                name=name,
                attributes=attributes,
            )
        self._handles[ctx.span_id] = handle
        return ctx

    # ----- end --------------------------------------------------------------

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        handle = self._handles.pop(span.span_id, None)
        if handle is None:
            return
        # Langfuse ``end`` shape varies between trace-roots and child spans.
        # Try the rich form first, fall back to the simple one.
        try:
            handle.end(metadata={"status": status})
        except TypeError:  # pragma: no cover - SDK quirk
            handle.end()

    # ----- events / attributes ---------------------------------------------

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        handle = self._handles.get(span.span_id)
        if handle is None:
            return
        handle.event(name="event", metadata=event)

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        # Mutate the local ctx so callers reading ``span.attributes`` see it.
        span.attributes[key] = value
        handle = self._handles.get(span.span_id)
        if handle is None:
            return
        handle.update(metadata={key: value})

    # ----- lifecycle --------------------------------------------------------

    def flush(self) -> None:
        """Flush queued events. Called by ``shutdown_runtime`` at CLI exit."""
        flush = getattr(self._client, "flush", None)
        if callable(flush):
            flush()

    async def push_run_feedback_score(self, run_record: Any, feedback: Any) -> str | None:
        """Mirror :class:`FeedbackRecord` to Langfuse as a trace-level score.

        Called best-effort from the feedback endpoint after the row is
        about to be saved. The runtime's feedback endpoint catches any
        exception we raise; we still try/except internally so a bad
        Langfuse client doesn't surface as an opaque error in the
        endpoint logs.

        Langfuse v2 SDK shape::

            client.score(
                trace_id=<run's trace id>,
                name="user_feedback",
                value=<numeric>,
                comment=<optional str>,
            ) -> Score object with .id

        Mapping:

        * trace id: ``run_record.metrics.langfuse_trace_id`` if the
          executor wrote it there, else None → we skip the push
          (no trace = nothing to attach to).
        * score value: ``feedback.score`` (raw -1/+1 or 1-5).
        * name: ``user_feedback`` (operators can filter on this in
          Langfuse UI; ``mdk_*`` namespace is reserved for system scores).
        * comment: ``feedback.comment`` (truncated to Langfuse's
          configured limit; the SDK accepts None).
        """
        # Pull trace id from the run's metrics. Different code paths
        # write it under slightly different keys (legacy vs current);
        # try both.
        metrics = getattr(run_record, "metrics", None)
        if metrics is None:
            return None
        trace_id = getattr(metrics, "langfuse_trace_id", None) or getattr(metrics, "trace_id", None)
        if not trace_id:
            return None

        # The Langfuse client's score() is synchronous in v2 SDK. We
        # don't want to block the feedback endpoint's event loop on
        # a network call to Langfuse, so dispatch via ``asyncio.to_thread``.
        import asyncio  # noqa: PLC0415

        def _do_score() -> str | None:
            try:
                score_obj = self._client.score(
                    trace_id=trace_id,
                    name="user_feedback",
                    value=float(feedback.score),
                    comment=feedback.comment,
                )
            except Exception:
                return None
            return getattr(score_obj, "id", None)

        try:
            score_id = await asyncio.to_thread(_do_score)
        except Exception:
            # Last-resort guard: even ``to_thread`` shouldn't fail in
            # normal operation, but if it does, swallow and proceed.
            # The feedback row in Postgres is the source of truth.
            return None
        return score_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client_from_env() -> Any:
    """Construct a real Langfuse client from environment variables.

    Raises :class:`LangfuseUnavailableError` if the package is missing or the
    keys aren't usable. Caller (``build_tracer``) catches and falls back
    to stdout.
    """
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    if not secret or not public:
        raise LangfuseUnavailableError(
            "LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY must both be set"
        )
    try:
        # Lazy import: langfuse is an optional dep, only pulled here.
        from langfuse import Langfuse  # noqa: PLC0415 - lazy by design
    except ImportError as exc:
        raise LangfuseUnavailableError(
            "langfuse package not installed; `uv sync --extra langfuse`"
        ) from exc
    host = os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com"
    try:
        return Langfuse(secret_key=secret, public_key=public, host=host)
    except Exception as exc:
        raise LangfuseUnavailableError(f"langfuse client init failed: {exc}") from exc
