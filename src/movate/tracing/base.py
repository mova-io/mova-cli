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
