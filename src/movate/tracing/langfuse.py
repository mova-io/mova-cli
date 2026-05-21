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

import contextlib
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
        # Pop Langfuse-native first-class fields before forwarding attrs as
        # metadata. Keys prefixed with ``_`` are executor-private signals
        # that should map to Langfuse's dedicated trace parameters rather
        # than landing in the generic metadata blob.
        session_id: str | None = attributes.pop("_session_id", None) or None
        user_id: str | None = attributes.pop("_user_id", None) or None
        tags: list[str] = attributes.pop("_tags", None) or []
        if parent is None:
            # Build kwargs for client.trace() selectively so we don't pass
            # None for optional fields the SDK might reject.
            trace_kwargs: dict[str, Any] = {"name": name, "metadata": attributes}
            if session_id:
                trace_kwargs["session_id"] = session_id
            if user_id:
                trace_kwargs["user_id"] = user_id
            if tags:
                trace_kwargs["tags"] = tags
            handle = self._client.trace(**trace_kwargs)
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
        # Langfuse v2 capability split: child spans (StatefulSpanClient)
        # expose ``.end()``; the trace ROOT (StatefulTraceClient) does NOT —
        # it has no ``.end()`` at all and is finalized via ``.update()`` +
        # flushed at shutdown. Route by which method the handle actually
        # has so a trace root doesn't raise AttributeError (which used to
        # abort the whole run). Wrapped fail-soft: tracing must never break
        # a run.
        with contextlib.suppress(Exception):
            end = getattr(handle, "end", None)
            if callable(end):
                try:
                    end(metadata={"status": status})
                except TypeError:  # SDK quirk: some .end() take no kwargs
                    end()
            else:
                handle.update(metadata={"status": status})

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
        """Emit a Langfuse Generation object for the LLM completion.

        This populates the Generations tab in Langfuse UI and feeds the
        model-level token-usage + cost dashboards. Called once per
        ``executor.execute()`` call after the final response is received.
        Fail-soft: any SDK exception is swallowed so tracing never breaks
        a run.
        """
        handle = self._handles.get(span.span_id)
        if handle is None:
            return
        with contextlib.suppress(Exception):  # never let tracing break a run
            handle.generation(
                name="llm-completion",
                model=model,
                input=input_messages,
                output=output_text,
                usage={
                    "input": input_tokens,
                    "output": output_tokens,
                    "total": input_tokens + output_tokens,
                    "unit": "TOKENS",
                },
                metadata={"cost_usd": cost_usd} if cost_usd else None,
            )

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

    async def score_trace(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> str | None:
        """Push a named numeric score to an existing Langfuse trace.

        Used by the eval engine to record the accuracy dimension score so
        it appears on the Langfuse Generations / Traces view alongside the
        per-run token usage. Dispatched via ``asyncio.to_thread`` so the
        synchronous Langfuse v2 ``score()`` call doesn't block the eval
        event loop. Fail-soft: returns ``None`` on any error.

        This method is NOT part of the :class:`Tracer` Protocol — it's a
        Langfuse-specific extension. Callers access it via ``getattr``
        (or ``isinstance(tracer, LangfuseTracer)``) so other tracers don't
        need to implement it.
        """
        if not trace_id:
            return None

        import asyncio  # noqa: PLC0415

        def _do_score() -> str | None:
            try:
                score_obj = self._client.score(
                    trace_id=trace_id,
                    name=name,
                    value=value,
                    comment=comment,
                )
            except Exception:
                return None
            return getattr(score_obj, "id", None)

        try:
            return await asyncio.to_thread(_do_score)
        except Exception:
            return None


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
            "langfuse package not installed; "
            "install with: uv tool install --reinstall movate-cli --extra langfuse"
        ) from exc
    # Accept LANGFUSE_HOST (canonical) or LANGFUSE_BASE_URL (Langfuse SDK
    # alias) so both spellings work when stored in ~/.movate/credentials.
    host = (
        os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    )
    try:
        return Langfuse(secret_key=secret, public_key=public, host=host)
    except Exception as exc:
        raise LangfuseUnavailableError(f"langfuse client init failed: {exc}") from exc
