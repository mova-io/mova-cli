"""Tracing layer: pluggable Tracer interface, stdout default for local dev."""

import sys

from movate.tracing.base import SpanCtx, Tracer
from movate.tracing.stdout import StdoutTracer

__all__ = ["SpanCtx", "StdoutTracer", "Tracer", "build_tracer"]


def build_tracer() -> Tracer:
    """Auto-select a tracer.

    v0.1: always stdout (writing to stderr so it doesn't pollute the
    JSON-on-stdout contract that ``movate run`` uses). Langfuse + OTel
    land in v0.4 — at that point this dispatch grows env-driven branches.
    """
    return StdoutTracer(stream=sys.stderr)
