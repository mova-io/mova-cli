"""SilentTracer — no-op tracer used as the default CLI backend.

Emits nothing; the executor still records spans internally (cost, status),
but they are discarded rather than written to any stream. Operators who want
trace output set ``MOVATE_TRACER=stdout`` (or configure Langfuse / OTel).
"""

from __future__ import annotations

from typing import Any

from movate.tracing.base import SpanCtx, Tracer


class SilentTracer(Tracer):
    name = "silent"

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        return SpanCtx(
            name=name,
            attributes=dict(attrs or {}),
            parent_id=parent.span_id if parent else None,
        )

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        pass

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        pass

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value
