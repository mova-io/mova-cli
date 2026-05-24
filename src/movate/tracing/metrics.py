"""OpenTelemetry metrics — opt-in, env-gated, fail-soft (R3, item 33).

The runtime already emits OTel *spans* (:mod:`movate.tracing.otel`); this module
adds the *metrics* half — a small set of counters/histograms for the job-queue
**golden signals** (throughput, error rate, latency, in-flight depth, plus per-run
token/cost volume). They're recorded at the worker/dispatch edges (never inside
``core``/the executor — boundary rule) and feed the future Azure Monitor alert
rules (item #27).

OTel-optional by design (mirrors the tracer + propagation modules):

* The OTel API + SDK are imported lazily. When the ``otel`` extra isn't
  installed, ``_OTEL_METRICS_AVAILABLE`` is ``False`` and **every** public
  function is a cheap no-op — no crash, no overhead beyond a single guard.
* :func:`init_metrics` only builds a real :class:`MeterProvider` when the OTLP
  sink/endpoint condition holds (the same condition under which the
  :class:`~movate.tracing.otel.OtelTracer` would be active — see
  :func:`_otlp_metrics_enabled`). Otherwise it leaves the instruments ``None``
  and the record helpers stay no-ops. It NEVER raises (a build failure emits at
  most one stderr line then degrades to no-op, consistent with the tracer's
  fail-soft logging).

Vendor-neutral (ADR 001): the exporter is the **generic** OTLP metric exporter,
configured by the standard OTel env vars (``OTEL_EXPORTER_OTLP_ENDPOINT`` /
``OTEL_EXPORTER_OTLP_HEADERS`` / ``OTEL_EXPORTER_OTLP_PROTOCOL``) — the exact
same env the span exporter uses. On Azure Container Apps, ACA's managed
OpenTelemetry auto-injects these into the container so metrics flow to the same
collector the spans do.

Scope notes (intentionally NOT implemented here):

* **Queue depth** (``mdk.queue.depth``) is a golden signal too, but it needs a
  periodic storage count query (a new ``StorageProvider`` method) which would put
  this PR on the storage Protocol. DEFERRED to item #27 to keep this change pure
  Python instrumentation off the storage seam.
* **Azure export destination + alert rules** are item #27. ACA managed-OTel's
  App Insights destination today takes traces + logs, not metrics; the metric
  collector → destination wiring (and the alert rules these instruments power)
  is #27's job and lives in ``infra/`` (out of scope here).
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Import the OTel metrics API + SDK lazily so this module loads even when the
# optional ``otel`` extra isn't installed (mirrors ``tracing/otel.py`` and
# ``tracing/propagation.py``). When absent, every helper degrades to a no-op.
_otel_metrics: Any = None
_OTEL_METRICS_AVAILABLE = False
try:
    import opentelemetry.metrics as _otel_metrics_module

    _otel_metrics = _otel_metrics_module
    _OTEL_METRICS_AVAILABLE = True
except ImportError:  # pragma: no cover - covered by the no-otel no-op tests
    pass


class _State:
    """Mutable module state holder.

    A single object whose *attributes* are mutated (rather than rebinding a set
    of module globals) so :func:`init_metrics` doesn't need the ``global``
    statement — matching the in-place-mutation pattern the rest of the codebase
    uses for one-shot process state. Instruments stay ``None`` until a successful
    :func:`init_metrics` under an active OTLP sink; every record helper guards on
    ``is None`` first so the uninitialized / OTel-absent / sink-off paths are all
    cheap no-ops.
    """

    initialized: bool = False
    provider: Any = None
    meter: Any = None

    jobs_completed: Any = None  # Counter[int]
    job_duration_ms: Any = None  # Histogram[float]
    jobs_in_flight: Any = None  # UpDownCounter[int]
    run_tokens: Any = None  # Counter[int]
    run_cost_usd: Any = None  # Counter[float]


_state = _State()


def _otlp_metrics_enabled() -> bool:
    """Should metrics initialize under the current env?

    Mirrors the condition under which the :class:`OtelTracer` would be active:

    * ``MOVATE_TRACE_SINK`` includes OTLP (``otlp`` / ``both``), the ADR-015
      deployment sink selector; OR
    * (legacy) ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set with no explicit sink that
      turns tracing off.

    ``MOVATE_TRACE_SINK=none`` wins → metrics off even if an endpoint is set
    (the operator explicitly turned observability off). A recognized sink that
    isn't OTLP-bearing (``langfuse``) also leaves metrics off.
    """
    sink = os.environ.get("MOVATE_TRACE_SINK", "").strip().lower()
    if sink:
        return sink in ("otlp", "both")
    # Legacy auto-detect: an OTLP endpoint alone is the implicit opt-in.
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def init_metrics(*, reader: Any | None = None) -> None:
    """Build the global :class:`MeterProvider` + instruments. Idempotent, fail-soft.

    Called once per process at the runtime edges (``mdk serve`` / ``mdk worker``
    startup), mirroring how the tracer is set up. A second call is a no-op (it
    won't double-register instruments or re-set the global provider).

    When OTel is available AND :func:`_otlp_metrics_enabled` holds, builds a
    ``MeterProvider(resource=…, metric_readers=[PeriodicExportingMetricReader(
    OTLPMetricExporter(...))])`` — the metric mirror of
    :func:`movate.tracing.otel._build_provider_from_env`, reusing
    :func:`~movate.tracing.otel._resource_attributes` and selecting the HTTP vs
    gRPC exporter from ``OTEL_EXPORTER_OTLP_PROTOCOL`` exactly like the span
    exporter. Otherwise the instruments stay ``None`` and the record helpers are
    no-ops.

    ``reader`` is the **test seam**: pass an injected
    :class:`~opentelemetry.sdk.metrics.export.InMemoryMetricReader` to assert on
    recorded datapoints without a real OTLP export. When supplied, the OTLP
    exporter/endpoint condition is bypassed and the provider is built around the
    injected reader.

    Azure note: on ACA the OTLP env is auto-injected so these metrics flow to the
    same collector the spans do; the metric collector → destination wiring + the
    alert rules these instruments power are item #27 (``infra/``), not here.

    Never raises: a build failure emits at most one stderr line, then degrades to
    a complete no-op so metrics never break the runtime.
    """
    if _state.initialized:
        return

    if not _OTEL_METRICS_AVAILABLE or _otel_metrics is None:
        # OTel extra absent — nothing to build; helpers stay no-ops.
        _state.initialized = True
        return

    if reader is None and not _otlp_metrics_enabled():
        # Sink off / no endpoint — same "no-op" state as the SilentTracer path.
        _state.initialized = True
        return

    try:
        provider = _build_meter_provider(reader=reader)
    except Exception as exc:  # pragma: no cover - fail-soft; mirrors tracer logging
        # Consistent with the tracer's fail-soft stderr line — one line, then
        # degrade to no-op rather than taking the runtime down.
        sys.stderr.write(f"[movate] OTel metrics unavailable, disabling: {exc}\n")
        _state.initialized = True
        return

    # Register as the process-global provider only on the real path. The
    # injected-``reader`` test seam keeps the meter local (and skips the global
    # set) so repeated test inits don't trip OTel's "Overriding of current
    # MeterProvider is not allowed" guard. Instruments are created from the
    # locally-built provider's meter either way, so recording still works.
    if reader is None:
        _otel_metrics.set_meter_provider(provider)
    _state.provider = provider
    meter = provider.get_meter("movate")
    _state.meter = meter

    # Instruments — ``mdk.`` namespace, low-cardinality attributes only (tenant
    # is acceptable; job_id / run_id are NEVER attributes — they'd explode
    # cardinality). Dead-letter rate is ``mdk.jobs.completed`` filtered to
    # ``status=dead_letter`` — no separate instrument needed.
    _state.jobs_completed = meter.create_counter(
        "mdk.jobs.completed",
        unit="1",
        description="Jobs reaching a terminal status (success/error/safety_blocked/dead_letter).",
    )
    _state.job_duration_ms = meter.create_histogram(
        "mdk.job.duration_ms",
        unit="ms",
        description="Wall-clock duration of a job from claim to terminal status.",
    )
    _state.jobs_in_flight = meter.create_up_down_counter(
        "mdk.jobs.in_flight",
        unit="1",
        description="Jobs currently being dispatched by a worker (claimed, not yet terminal).",
    )
    _state.run_tokens = meter.create_counter(
        "mdk.run.tokens",
        unit="1",
        description="Total LLM tokens consumed by executed runs.",
    )
    _state.run_cost_usd = meter.create_counter(
        "mdk.run.cost_usd",
        unit="usd",
        description="Total LLM cost (USD) of executed runs.",
    )

    _state.initialized = True


def _build_meter_provider(*, reader: Any | None) -> Any:
    """Construct a real :class:`MeterProvider`, mirroring the tracer's builder.

    Reuses :func:`movate.tracing.otel._resource_attributes` for an identical
    ``Resource`` (same ``service.name`` / version / ``deployment.environment``
    the spans carry) and, absent an injected ``reader``, wires a
    ``PeriodicExportingMetricReader`` around the generic OTLP metric exporter —
    HTTP by default, gRPC when ``OTEL_EXPORTER_OTLP_PROTOCOL`` selects it,
    exactly like :func:`movate.tracing.otel._otlp_exporter_class`. The SDK reads
    the endpoint + headers from the environment so no cloud-specific code is
    needed (ADR 001).
    """
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415

    # Reuse the tracer's resource attrs so metrics + spans share one Resource
    # (don't duplicate divergently — CLAUDE.md rule 4 / item-33 spec).
    from movate.tracing.otel import _resource_attributes  # noqa: PLC0415

    resource = Resource.create(_resource_attributes())

    if reader is not None:
        # Test seam: build around an injected reader (e.g. InMemoryMetricReader)
        # so tests assert on datapoints without a real OTLP export.
        return MeterProvider(resource=resource, metric_readers=[reader])

    from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
        PeriodicExportingMetricReader,
    )

    exporter_cls = _otlp_metric_exporter_class()
    # No explicit endpoint/headers kwargs — the SDK reads
    # OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS from the
    # environment (env-driven Azure Monitor / App Insights config; ADR 001).
    metric_reader = PeriodicExportingMetricReader(exporter_cls())
    return MeterProvider(resource=resource, metric_readers=[metric_reader])


def _otlp_metric_exporter_class() -> Any:
    """Return the generic OTLP *metric* exporter class for the configured transport.

    The metric-exporter analogue of
    :func:`movate.tracing.otel._otlp_exporter_class`: ``OTEL_EXPORTER_OTLP_PROTOCOL``
    selects HTTP (``http/protobuf``, the default — fewer transitive deps, easier
    to debug) or gRPC. Both ship in the **same** ``opentelemetry-exporter-otlp``
    package already in the ``otel`` extra (it pulls both proto-http and
    proto-grpc) — no new dependency, no cloud-specific exporter (ADR 001).
    """
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    if protocol in ("grpc", "grpc/protobuf"):
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter as _GrpcMetricExporter,
        )

        return _GrpcMetricExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
        OTLPMetricExporter as _HttpMetricExporter,
    )

    return _HttpMetricExporter


# ---------------------------------------------------------------------------
# Public record helpers — each a cheap no-op when its instrument is None
# (uninitialized, OTel absent, or sink off). None ever raises.
# ---------------------------------------------------------------------------


def record_job_completed(
    *,
    kind: str,
    status: str,
    duration_ms: int,
    tenant_id: str,
) -> None:
    """Record one job reaching a terminal status (golden signals: throughput +
    error rate + latency).

    Bumps ``mdk.jobs.completed`` (attrs: ``kind``, ``status``, ``tenant``) and
    records ``mdk.job.duration_ms`` (attrs: ``kind``, ``status``). ``status`` is
    the *final* persisted :class:`JobStatus` value (success / error /
    safety_blocked / dead_letter) — dead-letter rate is this counter filtered to
    ``status=dead_letter``.
    """
    if _state.jobs_completed is None or _state.job_duration_ms is None:
        return
    _state.jobs_completed.add(1, {"kind": kind, "status": status, "tenant": tenant_id})
    _state.job_duration_ms.record(float(duration_ms), {"kind": kind, "status": status})


def record_run_usage(
    *,
    tenant_id: str,
    tokens: int | None,
    cost_usd: float | None,
) -> None:
    """Record an executed run's token + cost volume (attrs: ``tenant``).

    ``tokens`` is the total (input + output); the input/output split isn't broken
    out as a ``direction`` attr because the run's terminal metrics object carries
    only the aggregate cheaply at this edge. ``None`` values are skipped so a run
    with no recorded usage adds nothing.
    """
    if _state.run_tokens is None or _state.run_cost_usd is None:
        return
    if tokens is not None:
        _state.run_tokens.add(int(tokens), {"tenant": tenant_id})
    if cost_usd is not None:
        _state.run_cost_usd.add(float(cost_usd), {"tenant": tenant_id})


def inc_in_flight(*, tenant_id: str) -> None:
    """Increment the in-flight job gauge (attrs: ``tenant``). No-op when off."""
    if _state.jobs_in_flight is None:
        return
    _state.jobs_in_flight.add(1, {"tenant": tenant_id})


def dec_in_flight(*, tenant_id: str) -> None:
    """Decrement the in-flight job gauge (attrs: ``tenant``). No-op when off.

    Pair with :func:`inc_in_flight` in a try/finally so the decrement always runs
    even if dispatch raises — otherwise the gauge leaks upward.
    """
    if _state.jobs_in_flight is None:
        return
    _state.jobs_in_flight.add(-1, {"tenant": tenant_id})


__all__ = [
    "dec_in_flight",
    "inc_in_flight",
    "init_metrics",
    "record_job_completed",
    "record_run_usage",
]
