"""Stdout tracer: structured JSON events. Langfuse-compatible event shape."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from movate.tracing.base import SpanCtx, Tracer


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StdoutTracer(Tracer):
    name = "stdout"

    def __init__(self, stream: Any = sys.stdout) -> None:
        self._stream = stream

    def _emit(self, payload: dict[str, Any]) -> None:
        self._stream.write(json.dumps(payload) + "\n")
        self._stream.flush()

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        span = SpanCtx(
            trace_id=parent.trace_id if parent else str(uuid4()),
            parent_id=parent.span_id if parent else None,
            name=name,
            attributes=dict(attrs or {}),
        )
        self._emit(
            {
                "ts": _now(),
                "kind": "span_start",
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "parent_id": span.parent_id,
                "name": span.name,
                "attrs": span.attributes,
            }
        )
        return span

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        self._emit(
            {
                "ts": _now(),
                "kind": "span_end",
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "name": span.name,
                "status": status,
            }
        )

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        self._emit(
            {
                "ts": _now(),
                "kind": "event",
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "event": event,
            }
        )

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value
