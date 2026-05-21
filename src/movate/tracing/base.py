"""Tracer Protocol. Span schema is OTel-shaped from day one."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4


@dataclass
class SpanCtx:
    span_id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    parent_id: str | None = None
    name: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


class Tracer(Protocol):
    name: str

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx: ...

    def end_span(self, span: SpanCtx, status: str = "ok") -> None: ...

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None: ...

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None: ...

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
        """Record an LLM completion as a first-class Langfuse Generation.

        No-op on non-Langfuse tracers — each backend provides its own
        implementation. Declared on the Protocol with a default body so
        all existing Tracer implementations stay valid without change.
        Callers (executor) always call this; only LangfuseTracer produces
        observable output from it.
        """
