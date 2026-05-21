"""Composite tracer — fan span calls out to multiple backends.

Useful when you want to write traces to Langfuse (for the curated agent
view) and OTel (for the cross-service view) at the same time. Each backend
maintains its own span identifiers; the composite returns a single
:class:`SpanCtx` that owns a private mapping back to per-backend contexts
so :meth:`end_span` / :meth:`log_event` / :meth:`set_attribute` fan out
correctly.

Failure of one backend never affects the others: we wrap each delegate
call in a try/except so a misbehaving Langfuse client doesn't block OTel
spans from being recorded.
"""

from __future__ import annotations

from typing import Any

from movate.tracing.base import SpanCtx, Tracer


class CompositeTracer(Tracer):
    """Fan-out :class:`Tracer` that delegates to a list of underlying tracers."""

    name = "composite"

    def __init__(self, tracers: list[Tracer]) -> None:
        if not tracers:
            raise ValueError("CompositeTracer requires at least one underlying tracer")
        self._tracers: list[Tracer] = list(tracers)
        # composite span_id → list of (delegate_tracer, delegate_span_ctx)
        self._mappings: dict[str, list[tuple[Tracer, SpanCtx]]] = {}

    @property
    def tracers(self) -> list[Tracer]:
        return list(self._tracers)

    # ----- start ------------------------------------------------------------

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        # Resolve the per-tracer parent for each delegate.
        parent_per_tracer: dict[Tracer, SpanCtx] = {}
        if parent is not None:
            for delegate, delegate_ctx in self._mappings.get(parent.span_id, []):
                parent_per_tracer[delegate] = delegate_ctx

        per_tracer: list[tuple[Tracer, SpanCtx]] = []
        for delegate in self._tracers:
            try:
                ctx = delegate.start_span(name, attrs=attrs, parent=parent_per_tracer.get(delegate))
                per_tracer.append((delegate, ctx))
            except Exception:  # pragma: no cover - one bad delegate must not kill others
                continue

        if not per_tracer:
            # All delegates failed: return a usable SpanCtx so callers don't crash.
            return SpanCtx(
                name=name,
                attributes=dict(attrs or {}),
                parent_id=parent.span_id if parent else None,
            )

        # Use the first delegate's identifiers for the composite SpanCtx so
        # logs / IDs stay consistent with whatever backend is treated as
        # primary (typically the one configured first).
        first_ctx = per_tracer[0][1]
        composite_ctx = SpanCtx(
            span_id=first_ctx.span_id,
            trace_id=first_ctx.trace_id,
            parent_id=parent.span_id if parent else None,
            name=name,
            attributes=dict(attrs or {}),
        )
        self._mappings[composite_ctx.span_id] = per_tracer
        return composite_ctx

    # ----- end --------------------------------------------------------------

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        for delegate, delegate_ctx in self._mappings.pop(span.span_id, []):
            try:
                delegate.end_span(delegate_ctx, status=status)
            except Exception:  # pragma: no cover - swallow
                continue

    # ----- events / attributes ---------------------------------------------

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        for delegate, delegate_ctx in self._mappings.get(span.span_id, []):
            try:
                delegate.log_event(delegate_ctx, event)
            except Exception:  # pragma: no cover
                continue

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value
        for delegate, delegate_ctx in self._mappings.get(span.span_id, []):
            try:
                delegate.set_attribute(delegate_ctx, key, value)
            except Exception:  # pragma: no cover
                continue

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
        for delegate, delegate_ctx in self._mappings.get(span.span_id, []):
            try:
                delegate.log_generation(
                    delegate_ctx,
                    model=model,
                    input_messages=input_messages,
                    output_text=output_text,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            except Exception:  # pragma: no cover
                continue

    # ----- score_trace (Langfuse extension, fan-out) -----------------------

    async def score_trace(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> str | None:
        """Fan-out :meth:`LangfuseTracer.score_trace` to all delegates that
        support it. Returns the first non-None score id, or None if none
        do. Fail-soft: exceptions from individual delegates are swallowed."""
        for delegate in self._tracers:
            fn = getattr(delegate, "score_trace", None)
            if not callable(fn):
                continue
            try:
                result = await fn(
                    trace_id=trace_id,
                    name=name,
                    value=value,
                    comment=comment,
                )
                if result is not None:
                    return result
            except Exception:  # pragma: no cover
                continue
        return None

    # ----- lifecycle --------------------------------------------------------

    def flush(self) -> None:
        for delegate in self._tracers:
            flush = getattr(delegate, "flush", None)
            if not callable(flush):
                continue
            try:
                flush()
            except Exception:  # pragma: no cover
                continue
