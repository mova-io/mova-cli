"""OpenTelemetry tracer — opt-in, env-gated, fail-soft.

Install with::

    uv sync --extra otel

Activation precedence (see :func:`movate.tracing.build_tracer`):

1. ``MOVATE_TRACE_SINK=otlp`` (or ``both``) — explicit *deployment* sink
   (ADR 015). A missing OTel extra here is a hard, actionable error.
2. ``MOVATE_TRACER=otel`` — explicit opt-in (legacy; fails soft to stdout).
3. ``OTEL_EXPORTER_OTLP_ENDPOINT`` set — implicit opt-in (legacy; fails soft).

The legacy paths fall back to stdout (with a stderr warning) when the OTel
packages aren't installed or the SDK rejects construction. Tracing must
never break a run.

The exporter is the **generic** OpenTelemetry OTLP exporter, configured by the
standard OTel env vars — no cloud-specific dependency (ADR 001):

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` — the OTLP receiver URL. Point this at Azure
  Monitor / Application Insights' OTLP ingestion endpoint, or at Grafana
  Tempo / SigNoz / Honeycomb / a local collector (``http://localhost:4318``).
* ``OTEL_EXPORTER_OTLP_HEADERS`` — auth/metadata headers (e.g. the App Insights
  ingestion key header). Read natively by the SDK.
* ``OTEL_EXPORTER_OTLP_PROTOCOL`` — ``http/protobuf`` (default) or ``grpc``.

Span hierarchy mirrors the runtime: workflow → agent.execute → provider
call. Inside the executor, ``span.set_attribute`` mirrors metadata
(provider, tokens, cost) to OTel attributes; ``log_event`` becomes
``span.add_event``.

Implementation notes:

* The local ``SpanCtx`` stays a pure dataclass; OTel ``Span`` handles
  live in a private dict keyed by ``span_id``. Same pattern as the
  Langfuse tracer.
* ``trace_id`` and ``span_id`` on ``SpanCtx`` are formatted from OTel's
  internal int representation so they're consistent with the rest of
  movate (hex strings).
* OTel attributes are restricted to primitive types plus lists thereof.
  ``_otel_value`` coerces dicts and other complex types to JSON strings
  so ``log_event`` and ``set_attribute`` never raise on a typed value.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any
from uuid import uuid4

from movate.tracing.base import SpanCtx, Tracer

# Import OTel lazily so the module loads even when the optional dep isn't
# installed. Tests that inject a fake tracer don't need the real SDK.
_otel_trace: Any = None
_OTEL_AVAILABLE = False
try:
    import opentelemetry.trace as _otel_trace_module

    _otel_trace = _otel_trace_module
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - covered by env tests
    pass


class OtelUnavailableError(Exception):
    """Raised when OTel packages are missing or provider construction fails."""


class OtelTracer(Tracer):
    """Forwards :class:`Tracer` Protocol calls to an OTel SDK ``Tracer``."""

    name = "otel"

    def __init__(
        self,
        *,
        tracer: Any | None = None,
        provider: Any | None = None,
    ) -> None:
        """Construct from an existing OTel tracer/provider, or build from env.

        ``tracer=`` is the test seam: pass a stub exposing ``start_span(
        name, context=..., attributes=...) -> Span``. The Span stub must
        expose ``set_attribute``, ``add_event``, ``set_status``,
        ``get_span_context``, ``end``.

        ``provider=`` is held only for ``flush()`` (calls
        ``force_flush(timeout_millis=…)`` if available).
        """
        if tracer is None:
            provider = provider or _build_provider_from_env()
            tracer = provider.get_tracer("movate")
        self._tracer = tracer
        self._provider = provider
        # span_id → otel Span. ``end_span`` pops; orphans (rare) drop.
        self._spans: dict[str, Any] = {}

    # ----- start ------------------------------------------------------------

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        attributes = dict(attrs or {})
        # OTel attribute coercion — primitives only.
        otel_attrs = {k: _otel_value(v) for k, v in attributes.items()}

        kwargs: dict[str, Any] = {"attributes": otel_attrs}
        if parent is not None:
            parent_span = self._spans.get(parent.span_id)
            if parent_span is not None and _OTEL_AVAILABLE and _otel_trace is not None:
                kwargs["context"] = _otel_trace.set_span_in_context(parent_span)

        otel_span = self._tracer.start_span(name, **kwargs)
        ctx = _to_span_ctx(name, attributes, otel_span, parent)
        self._spans[ctx.span_id] = otel_span
        return ctx

    # ----- end --------------------------------------------------------------

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        otel_span = self._spans.pop(span.span_id, None)
        if otel_span is None:
            return
        if status != "ok":
            _set_error_status(otel_span, status)
        otel_span.end()

    # ----- events / attributes ---------------------------------------------

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        otel_span = self._spans.get(span.span_id)
        if otel_span is None:
            return
        # Flatten the event dict to OTel-acceptable attribute values.
        otel_span.add_event("event", attributes={k: _otel_value(v) for k, v in event.items()})

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value
        otel_span = self._spans.get(span.span_id)
        if otel_span is None:
            return
        otel_span.set_attribute(key, _otel_value(value))

    # ----- lifecycle --------------------------------------------------------

    def flush(self) -> None:
        """Force-flush the SDK so spans land before process exit."""
        if self._provider is None:
            return
        force = getattr(self._provider, "force_flush", None)
        if not callable(force):
            return
        with contextlib.suppress(Exception):  # pragma: no cover - never break shutdown
            force(timeout_millis=2000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_span_ctx(
    name: str,
    attributes: dict[str, Any],
    otel_span: Any,
    parent: SpanCtx | None,
) -> SpanCtx:
    """Build a movate :class:`SpanCtx` mirroring an OTel Span's identity.

    ``get_span_context()`` returns a SpanContext with ``trace_id`` /
    ``span_id`` as ints; we format them as fixed-width hex so they read
    the same way they do in OTel exporters.
    """
    sctx = otel_span.get_span_context()
    trace_id_int = getattr(sctx, "trace_id", 0) or 0
    span_id_int = getattr(sctx, "span_id", 0) or 0
    trace_id = format(trace_id_int, "032x") if trace_id_int else str(uuid4())
    span_id = format(span_id_int, "016x") if span_id_int else str(uuid4())
    return SpanCtx(
        span_id=span_id,
        trace_id=trace_id,
        parent_id=parent.span_id if parent else None,
        name=name,
        attributes=attributes,
    )


def _set_error_status(otel_span: Any, status_message: str) -> None:
    """Set OTel error status if the SDK is available; otherwise no-op."""
    if not _OTEL_AVAILABLE or _otel_trace is None:
        return
    try:
        from opentelemetry.trace import (  # noqa: PLC0415 - lazy by design
            Status,
            StatusCode,
        )

        otel_span.set_status(Status(StatusCode.ERROR, status_message))
    except ImportError:  # pragma: no cover - api always ships these
        pass


_OTEL_PRIMITIVES = (str, bool, int, float)


def _otel_value(value: Any) -> Any:
    """Coerce ``value`` to something OTel attributes accept.

    OTel attributes only accept primitives + lists of primitives. We map
    dicts and other complex types to JSON strings so callers can pass
    rich values without thinking.
    """
    if value is None or isinstance(value, _OTEL_PRIMITIVES):
        return value
    if isinstance(value, list | tuple):
        if all(isinstance(v, _OTEL_PRIMITIVES) for v in value):
            return list(value)
        return json.dumps(list(value), default=str)
    return json.dumps(value, default=str)


def _build_provider_from_env() -> Any:
    """Construct a real OTel :class:`TracerProvider` from env vars.

    Reads ``OTEL_EXPORTER_OTLP_ENDPOINT`` (required) and ``OTEL_SERVICE_NAME``
    (default ``"movate-runtime"``). The generic OTLP exporter also honors the
    standard transport env vars natively — ``OTEL_EXPORTER_OTLP_HEADERS`` (e.g.
    an Azure Monitor / App Insights auth header) and
    ``OTEL_EXPORTER_OTLP_PROTOCOL`` (``http/protobuf`` vs ``grpc``). We pick the
    HTTP vs gRPC exporter from ``OTEL_EXPORTER_OTLP_PROTOCOL`` (default HTTP —
    fewer transitive deps, easier to debug locally) and let the SDK read the
    endpoint + headers from the environment so no cloud-specific code is needed
    (ADR 001: Azure Monitor is just an OTLP endpoint).
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        raise OtelUnavailableError(
            "OTEL_EXPORTER_OTLP_ENDPOINT must be set (e.g. http://localhost:4318)"
        )
    try:
        # Lazy imports — only needed when actually building from env.
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            BatchSpanProcessor,
        )
    except ImportError as exc:
        raise OtelUnavailableError(
            "opentelemetry packages not installed; `uv sync --extra otel`"
        ) from exc

    exporter_cls = _otlp_exporter_class()
    resource = Resource.create(_resource_attributes())
    provider = TracerProvider(resource=resource)
    try:
        # No explicit endpoint/headers kwargs — the SDK reads
        # OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS from the
        # environment. This keeps Azure Monitor / App Insights config purely
        # env-driven (portable; no azure-specific dependency).
        exporter = exporter_cls()
    except Exception as exc:
        raise OtelUnavailableError(f"OTLP exporter init failed: {exc}") from exc
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def _otlp_exporter_class() -> Any:
    """Return the generic OTLP span-exporter class for the configured transport.

    ``OTEL_EXPORTER_OTLP_PROTOCOL`` selects HTTP (``http/protobuf``, the default
    — fewer transitive deps, easier to debug) or gRPC. Either way it is the
    *generic* ``opentelemetry-exporter-otlp`` (already in the ``otel`` extra) —
    no cloud-specific exporter (ADR 001).
    """
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    try:
        if protocol in ("grpc", "grpc/protobuf"):
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter as _GrpcExporter,
            )

            return _GrpcExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter as _HttpExporter,
        )

        return _HttpExporter
    except ImportError as exc:
        raise OtelUnavailableError(
            "opentelemetry-exporter-otlp not installed; `uv sync --extra otel`"
        ) from exc


def _resource_attributes() -> dict[str, str]:
    """OTel resource attributes for the runtime's tracer provider.

    ``service.name`` defaults to ``movate-runtime`` (overridable via
    ``OTEL_SERVICE_NAME``); ``service.version`` carries ``movate.__version__``;
    ``deployment.environment`` is read from ``MOVATE_ENV`` /
    ``OTEL_DEPLOYMENT_ENVIRONMENT`` when set so dashboards can split prod from
    dev.
    """
    service_name = os.environ.get("OTEL_SERVICE_NAME", "movate-runtime").strip() or "movate-runtime"
    attrs: dict[str, str] = {"service.name": service_name}

    try:
        from movate import __version__  # noqa: PLC0415

        attrs["service.version"] = __version__
    except Exception:  # pragma: no cover - __version__ always present
        pass

    environment = (
        os.environ.get("MOVATE_ENV", "").strip()
        or os.environ.get("OTEL_DEPLOYMENT_ENVIRONMENT", "").strip()
    )
    if environment:
        attrs["deployment.environment"] = environment

    return attrs
