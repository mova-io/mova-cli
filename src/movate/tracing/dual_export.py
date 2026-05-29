"""ADR 039 Phase 2 — opt-in dual OTLP export to Movate's central Collector.

Phase 2 (declared deferred-by-ADR-039 but implementation-prepared here) ships a
**second** OTLP exporter alongside the primary one. The primary stream remains
unchanged — customers keep their per-tenant Azure Monitor sink and full visibility.
A copy of metrics + spans is additionally exported to a Movate-operated Collector
**when, and only when, the customer opts in** by setting ``MDK_TELEMETRY_ENDPOINT``.

This module is the single seam for that wiring. Everything is **additive**,
**default-off**, and **fail-soft** — failure of the dual exporter MUST NOT affect
the primary stream, and an unreachable Movate endpoint MUST NOT break the
runtime.

Env contract (additive; preserves CLAUDE.md rule 5 — backward compat)
=====================================================================

``MDK_TELEMETRY_ENDPOINT``
    OTLP endpoint URL for Movate's central Collector. *Unset = Phase 2
    disabled* (the default). The endpoint is the OTLP receiver — same shape as
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` (e.g. ``https://otel.movate.example:4318``).

``MDK_TELEMETRY_CUSTOMER_ID``
    Opaque per-customer identifier — should be a hash, not a name. Stamped as
    the ``customer`` Resource attribute on the dual-exported stream so Movate
    can group fleet emissions by customer without learning the customer's
    real name. **Required** when ``MDK_TELEMETRY_ENDPOINT`` is set; if missing
    we log one stderr warning and disable Phase 2 (no half-configured state).

``MDK_TELEMETRY_INSECURE``
    Truthy values (``1`` / ``true`` / ``yes``) enable plaintext OTLP for dev /
    self-signed clusters. Default false — TLS required. Mirrors the
    ``insecure`` flag on the underlying OTLP exporter.

What is exported (the contract that makes opt-in safe)
======================================================

ADR 039 D3 defines an **allow-list** of metric instruments + span-attribute
names. The current MDK instrumentation already restricts emissions to that
shape:

* Metrics in ``METRIC_NAMES`` (counters / histograms / gauges; no labels that
  carry prompts or completions).
* Spans in :mod:`movate.tracing.otel` set primitive attributes — provider,
  model, duration, token counts, status — never prompt text / completion text /
  retrieved chunk content.

The :class:`PiiFilteringSpanProcessor` below applies a defense-in-depth
allow-list on top of that: if a future caller adds a new attribute that hasn't
been audited, the dual stream **drops** it (allow-list, not deny-list) so the
boundary is fail-closed. The primary stream is **unaffected** by the filter —
customer dashboards continue to see full attributes inside their tenant.

Boundaries (CLAUDE.md rules 4 to 8)
===================================

* Phase 2 is a second adapter behind the existing tracer Protocol seam — no
  Protocol changes. (Rule 7.)
* No new dependency. The ``opentelemetry-exporter-otlp`` package already in
  the ``otel`` extra ships both HTTP + gRPC exporters. (Rule 8.)
* The module loads lazily — when the OTel extra isn't installed, every entry
  point is a fail-soft no-op (matching the rest of ``tracing/``). (Rule 11.)
* ``cli ⊥ runtime`` preserved: this lives in ``tracing/`` and is consumed by
  both planes via the shared init paths. (Rule 6.)

This file intentionally does NOT wire the second reader/processor into the
provider — that's the caller's job (in ``otel._build_provider_from_env`` and
``metrics._build_meter_provider``). Keeping the wiring at the build sites makes
the second stream visible inline at the place the first is built — easier to
reason about, easier to remove, no hidden import side-effects.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Env vars — additive, default-off (CLAUDE.md rule 5)
# ---------------------------------------------------------------------------

#: OTLP endpoint URL for Movate's central Collector. Unset = Phase 2 disabled.
ENV_TELEMETRY_ENDPOINT = "MDK_TELEMETRY_ENDPOINT"

#: Opaque per-customer ID (hash, NOT a name). Stamped as the ``customer``
#: Resource attribute on the dual-exported stream. Required when the
#: endpoint is set.
ENV_TELEMETRY_CUSTOMER_ID = "MDK_TELEMETRY_CUSTOMER_ID"

#: Optional. Truthy = allow plaintext OTLP (dev clusters / self-signed). The
#: default is TLS-required, matching the safest posture for a cross-tenant
#: link. Mirrors the underlying exporter's ``insecure`` flag.
ENV_TELEMETRY_INSECURE = "MDK_TELEMETRY_INSECURE"

#: Truthy values for ``MDK_TELEMETRY_INSECURE``. Case-insensitive.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Span-attribute allow-list (ADR 039 D3)
#
# The Resource attribute the dual stream stamps to identify the customer
# never crosses span attributes (it lives on the Resource), so it's not in
# this list. Anything not in this list is dropped from the dual stream
# *before* it leaves the process — defense-in-depth on top of MDK's already
# narrow attribute surface.
# ---------------------------------------------------------------------------

#: Allow-listed span attribute names for the dual-exported stream. Sourced
#: from ADR 039 D3's "Spans" table. New attributes added in
#: ``src/movate/core/`` MUST be reviewed before being added here — the
#: default for an un-listed attribute is "drop from the dual stream", not
#: "leak".
ALLOWED_SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # workflow.execute
        "workflow",
        "workflow_version",
        # agent.execute
        "agent",
        "agent_version",
        "provider",
        "model_override",
        "status",
        # agent.turn[N] / skill.<name> / retrieval.<skill>
        "turn",
        "model",
        "skill",
        "auto_into",
        # kb_search (+ stages)
        "stage_count",
        "total_ms",
        "duration_ms",
        "input_count",
        "output_count",
        "chunk_count",
        # Generic — provider/model/duration timing primitives that the
        # OtelTracer's _otel_value emits inside spans.
        "name",
        # OTel exception type (NOT the message — message can embed values)
        "exception.type",
    }
)


# ---------------------------------------------------------------------------
# Module-level state — only used to suppress a repeated warning so a hot
# init path (eval loops) doesn't flood stderr.
# ---------------------------------------------------------------------------

_warned_keys: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Emit ``message`` to stderr at most once per process for ``key``.

    Matches the same pattern :mod:`movate.tracing` uses for the legacy
    fallback warnings — so a multi-case eval doesn't flood stderr with the
    same misconfiguration line.
    """
    if key in _warned_keys:
        return
    _warned_keys.add(key)
    sys.stderr.write(message + "\n")


# ---------------------------------------------------------------------------
# Public predicate
# ---------------------------------------------------------------------------


def dual_export_enabled() -> bool:
    """True when Phase 2 dual export should be wired into a provider.

    The contract is **both** envs set: an endpoint without a customer ID is a
    half-configured state we refuse to ship (we log one warning then disable).
    """
    endpoint = (os.environ.get(ENV_TELEMETRY_ENDPOINT) or "").strip()
    if not endpoint:
        return False

    customer = (os.environ.get(ENV_TELEMETRY_CUSTOMER_ID) or "").strip()
    if not customer:
        _warn_once(
            "phase2-missing-customer-id",
            (
                "[movate] Phase 2 telemetry disabled: "
                f"{ENV_TELEMETRY_ENDPOINT} is set but "
                f"{ENV_TELEMETRY_CUSTOMER_ID} is unset — dual export requires "
                "both. Primary export unaffected."
            ),
        )
        return False
    return True


def telemetry_endpoint() -> str:
    """Return the configured Movate telemetry endpoint (stripped). Empty when unset."""
    return (os.environ.get(ENV_TELEMETRY_ENDPOINT) or "").strip()


def telemetry_customer_id() -> str:
    """Return the configured customer-id hash (stripped). Empty when unset."""
    return (os.environ.get(ENV_TELEMETRY_CUSTOMER_ID) or "").strip()


def telemetry_insecure() -> bool:
    """True when the operator opted in to plaintext OTLP for the dual stream.

    Default false — TLS is required for the cross-tenant link. Toggle only
    for local dev clusters or self-signed test rigs.
    """
    raw = (os.environ.get(ENV_TELEMETRY_INSECURE) or "").strip().lower()
    return raw in _TRUTHY


def dual_resource_attributes() -> dict[str, str]:
    """Resource attrs unique to the dual-exported stream.

    Today: a single ``customer`` attribute with the configured customer-id
    hash. Merged on top of the primary Resource by the caller — the primary
    Resource is intentionally NOT carried verbatim onto the dual stream
    (e.g. an internal ``deployment.environment`` like ``dev-acme`` may
    encode a name) — callers build a minimized Resource for Phase 2 (see
    :func:`build_dual_resource`).
    """
    return {"customer": telemetry_customer_id()}


def build_dual_resource(base_attrs: dict[str, str] | None = None) -> Any | None:
    """Build the OTel ``Resource`` for the dual-exported stream.

    Starts from a copy of ``base_attrs`` (the same dict the primary Resource
    is built from — typically :func:`movate.tracing.otel._resource_attributes`'s
    return value), drops attributes that could leak a customer name
    (``deployment.environment`` — operator-set, may be ``prod-acme``), then
    layers :func:`dual_resource_attributes` on top. Returns ``None`` when
    the OTel SDK isn't available (matches the lazy-import pattern).
    """
    try:
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    except ImportError:
        return None

    safe = dict(base_attrs or {})
    # `deployment.environment` is operator-set and historically free-text
    # (e.g. "prod-acme") — drop from the dual stream. The fleet view uses
    # the `customer` Resource attribute, which is the hash.
    safe.pop("deployment.environment", None)
    safe.update(dual_resource_attributes())
    return Resource.create(safe)


# ---------------------------------------------------------------------------
# Lazy SDK accessors — both span + metric paths import the same way the
# primary builders in otel.py / metrics.py do.
# ---------------------------------------------------------------------------


def _is_grpc_protocol() -> bool:
    """True when ``OTEL_EXPORTER_OTLP_PROTOCOL`` selects the gRPC transport.

    The HTTP and gRPC exporters share most of their constructor signature but
    diverge on a couple of transport-specific knobs (``insecure`` is gRPC-only
    — HTTP is "plaintext iff the URL is http://"). The exporter-builder
    branches on this to avoid passing an unknown kwarg into the HTTP path.
    """
    protocol = (os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "").strip().lower()
    return protocol in ("grpc", "grpc/protobuf")


def _otlp_span_exporter_class() -> Any | None:
    """Return the OTLP span-exporter class for the configured protocol.

    Mirrors :func:`movate.tracing.otel._otlp_exporter_class`. Honors the same
    standard env (``OTEL_EXPORTER_OTLP_PROTOCOL``) so the dual stream uses the
    same transport as the primary unless an operator explicitly overrides it.
    """
    try:
        if _is_grpc_protocol():
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter as _GrpcExporter,
            )

            return _GrpcExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter as _HttpExporter,
        )

        return _HttpExporter
    except ImportError:
        return None


def _otlp_metric_exporter_class() -> Any | None:
    """Return the OTLP metric-exporter class for the configured protocol.

    Mirrors :func:`movate.tracing.metrics._otlp_metric_exporter_class`.
    """
    try:
        if _is_grpc_protocol():
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: PLC0415
                OTLPMetricExporter as _Grpc,
            )

            return _Grpc
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter as _Http,
        )

        return _Http
    except ImportError:
        return None


def _exporter_kwargs(endpoint: str) -> dict[str, Any]:
    """Build the constructor kwargs for a Phase-2 OTLP exporter.

    Common across span + metric exporters. The HTTP exporter has no
    ``insecure`` kwarg (HTTP plaintext is implied by an ``http://`` URL); we
    only forward it for gRPC. Documented at:
    https://opentelemetry.io/docs/languages/python/exporters/#configuration
    """
    kwargs: dict[str, Any] = {"endpoint": endpoint}
    if telemetry_insecure() and _is_grpc_protocol():
        kwargs["insecure"] = True
    return kwargs


# ---------------------------------------------------------------------------
# PII-filtering SpanProcessor — defense-in-depth allow-list. Wraps a regular
# BatchSpanProcessor and strips disallowed attributes from each span before
# it's handed off for export. The primary span processor is NOT wrapped, so
# customer dashboards still see the full attribute set inside their tenant.
# ---------------------------------------------------------------------------


def _build_pii_filtering_processor_class() -> Any | None:
    """Build the :class:`PiiFilteringSpanProcessor` class lazily.

    Returns ``None`` when the OTel SDK isn't installed — the caller treats
    that as "Phase 2 unavailable, primary unaffected".

    The class is built inside this function (rather than at module import)
    so importing ``dual_export`` never requires the OTel SDK — same lazy
    pattern as :mod:`movate.tracing.otel` /
    :mod:`movate.tracing.metrics`.
    """
    try:
        from opentelemetry.sdk.trace import ReadableSpan  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            BatchSpanProcessor,
            SpanExporter,
        )
    except ImportError:
        return None

    class PiiFilteringSpanProcessor(BatchSpanProcessor):
        """A ``BatchSpanProcessor`` that drops non-allow-listed span attributes.

        OTel's ``BatchSpanProcessor.on_end`` receives a ``ReadableSpan`` and
        passes it to the exporter. We override ``on_end`` to first redact the
        span's attributes against :data:`ALLOWED_SPAN_ATTRIBUTES`, then
        delegate to the parent.

        The redaction is in-place on the ``ReadableSpan`` — but because each
        :class:`BatchSpanProcessor` receives its own per-processor invocation
        of ``on_end`` against a per-span ``ReadableSpan`` produced from the
        SDK ``Span``'s shared state, in practice the SDK exposes the
        attribute mapping by reference. To avoid mutating the underlying
        span (which would leak the redaction into the primary stream), we
        instead build a thin wrapper :class:`_RedactedReadableSpan` that
        substitutes a filtered attribute dict and pass that down.
        """

        def on_end(self, span: ReadableSpan) -> None:
            try:
                filtered = _filter_attrs(getattr(span, "attributes", None) or {})
            except Exception:  # pragma: no cover - defensive; never raise on shutdown
                filtered = {}
            wrapped = _RedactedReadableSpan(span, filtered)
            super().on_end(wrapped)  # type: ignore[arg-type]

    class _RedactedReadableSpan:
        """Read-only wrapper around a ``ReadableSpan`` with filtered attributes.

        Passes through every attribute the exporter touches (name, context,
        events, links, status, kind, resource, instrumentation_scope, start
        + end times, parent) to the wrapped span — only ``attributes`` is
        overridden to the filtered mapping. This avoids mutating the
        underlying span, so the primary processor sees the original.

        OTel's exporters call into ``ReadableSpan``-shaped objects via
        attribute access, so duck-typed wrapping is sufficient.
        """

        __slots__ = ("_attrs", "_wrapped")

        def __init__(self, wrapped: Any, filtered_attrs: dict[str, Any]) -> None:
            self._wrapped = wrapped
            self._attrs = filtered_attrs

        @property
        def attributes(self) -> dict[str, Any]:
            return self._attrs

        def __getattr__(self, name: str) -> Any:
            # __getattr__ only fires when normal lookup fails — so the
            # `attributes` property above wins.
            return getattr(self._wrapped, name)

    # Re-export so callers can isinstance/check.
    PiiFilteringSpanProcessor._RedactedReadableSpan = _RedactedReadableSpan  # type: ignore[attr-defined]
    PiiFilteringSpanProcessor._SpanExporter = SpanExporter  # type: ignore[attr-defined]
    return PiiFilteringSpanProcessor


def _filter_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Return ``attrs`` with only the allow-listed keys retained.

    Pure function — easy to unit-test without an SDK. Tagged here rather
    than inside the closure-built class so a test can call it directly.
    """
    return {k: v for k, v in attrs.items() if k in ALLOWED_SPAN_ATTRIBUTES}


# ---------------------------------------------------------------------------
# Builders consumed by otel.py / metrics.py at provider-build time.
# Each returns ``None`` on any failure so the caller can `if x is not None:
# provider.add_*(x)` without try/except — failure of the dual stream MUST
# NOT break the primary.
# ---------------------------------------------------------------------------


def build_dual_span_processor(
    *,
    base_resource_attrs: dict[str, str] | None = None,
) -> Any | None:
    """Build a second :class:`BatchSpanProcessor` for the Movate Collector.

    The returned processor MUST be attached to the SAME ``TracerProvider``
    that already has the primary :class:`BatchSpanProcessor` — both streams
    then share the underlying spans, but each processor batches + exports
    independently. A failure to export on one path is invisible to the other
    (OTel's ``BatchSpanProcessor`` swallows export errors and retries on the
    next batch; we additionally disable retry-on-failure for the dual path
    so a hosed Movate endpoint doesn't pile up memory pressure).

    Returns ``None`` when the OTel SDK isn't available or when the OTLP
    span-exporter class can't be imported — the caller treats that as
    "Phase 2 unavailable; primary unaffected".

    ``base_resource_attrs`` is the same dict the primary Resource was built
    from. We do NOT use it for the processor itself (Resource lives on the
    provider) — the dual ``Resource`` is built by the caller via
    :func:`build_dual_resource` and attached to a *separate* provider only
    if the caller chooses; the simpler attachment (recommended) is to share
    the primary provider and let the ``customer`` Resource attribute be
    stamped on the SDK ``Span`` via a ``SpanProcessor`` chain — which is
    what we do, by setting it as a span attribute on each filtered span
    inside the processor. See ADR 039 D4 for hash semantics.
    """
    if not dual_export_enabled():
        return None

    exporter_cls = _otlp_span_exporter_class()
    if exporter_cls is None:
        _warn_once(
            "phase2-span-exporter-import",
            "[movate] Phase 2 telemetry: OTLP span exporter unavailable; "
            "primary export unaffected.",
        )
        return None

    processor_cls = _build_pii_filtering_processor_class()
    if processor_cls is None:
        _warn_once(
            "phase2-processor-import",
            "[movate] Phase 2 telemetry: SDK trace export missing; primary export unaffected.",
        )
        return None

    endpoint = telemetry_endpoint()
    try:
        # Build the exporter with the explicit Phase-2 endpoint. The
        # primary exporter uses the env-driven endpoint
        # (OTEL_EXPORTER_OTLP_ENDPOINT) — the dual exporter takes an
        # explicit kwarg so the two streams have independent destinations.
        exporter = exporter_cls(**_exporter_kwargs(endpoint))
    except Exception as exc:
        _warn_once(
            "phase2-span-exporter-init",
            f"[movate] Phase-2 telemetry endpoint unreachable; primary export unaffected: {exc}",
        )
        return None

    # Conservative batching — short interval, small queue, NO retry on a
    # cross-tenant link so failures don't pile up memory pressure. The
    # primary stream's batching is unchanged.
    try:
        return processor_cls(
            exporter,
            max_queue_size=512,
            schedule_delay_millis=2000,
            export_timeout_millis=5000,
            max_export_batch_size=128,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _warn_once(
            "phase2-span-processor-init",
            f"[movate] Phase-2 telemetry processor init failed; primary export unaffected: {exc}",
        )
        return None


def build_dual_metric_reader() -> Any | None:
    """Build a second :class:`PeriodicExportingMetricReader` for the Movate Collector.

    Attached to the SAME ``MeterProvider`` as the primary reader (via the
    provider's ``metric_readers=[...]`` list at construction time, OR — for
    an already-built provider — by reconstructing the provider, which our
    callers do because the MDK builder is the only construction site).

    Returns ``None`` on any unavailable / unreachable path so the caller's
    only contract is "if the result is not None, include it in the readers
    list".
    """
    if not dual_export_enabled():
        return None

    exporter_cls = _otlp_metric_exporter_class()
    if exporter_cls is None:
        _warn_once(
            "phase2-metric-exporter-import",
            "[movate] Phase 2 telemetry: OTLP metric exporter unavailable; "
            "primary export unaffected.",
        )
        return None

    try:
        from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
            PeriodicExportingMetricReader,
        )
    except ImportError:
        _warn_once(
            "phase2-metric-reader-import",
            "[movate] Phase 2 telemetry: SDK metric reader missing; primary export unaffected.",
        )
        return None

    endpoint = telemetry_endpoint()
    try:
        exporter = exporter_cls(**_exporter_kwargs(endpoint))
    except Exception as exc:
        _warn_once(
            "phase2-metric-exporter-init",
            f"[movate] Phase-2 telemetry endpoint unreachable; primary export unaffected: {exc}",
        )
        return None

    try:
        # Short interval, conservative timeout. The reader doesn't retry —
        # one failed export drops a single collection cycle's metrics on the
        # dual stream and the next cycle goes again.
        return PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=15000,
            export_timeout_millis=5000,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _warn_once(
            "phase2-metric-reader-init",
            f"[movate] Phase-2 telemetry reader init failed; primary export unaffected: {exc}",
        )
        return None


__all__ = [
    "ALLOWED_SPAN_ATTRIBUTES",
    "ENV_TELEMETRY_CUSTOMER_ID",
    "ENV_TELEMETRY_ENDPOINT",
    "ENV_TELEMETRY_INSECURE",
    "build_dual_metric_reader",
    "build_dual_resource",
    "build_dual_span_processor",
    "dual_export_enabled",
    "dual_resource_attributes",
    "telemetry_customer_id",
    "telemetry_endpoint",
    "telemetry_insecure",
]
