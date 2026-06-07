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
from collections.abc import Callable
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


# ---------------------------------------------------------------------------
# Metric-name source of truth.
#
# The OTel instrument names live here as constants (not inline string literals
# in ``init_metrics``) so there is ONE place a name is written. Anything that
# must reference a metric by name off-process — the in-repo Grafana / Prometheus
# / Azure dashboards (ADR 031 D2, ``dashboards/``) and their drift test — imports
# these constants instead of hard-coding the strings, so a rename here is caught
# (the dashboards test cross-checks every referenced metric against this set).
#
# Names are the dot-form OTel instrument names. Prometheus scrapes them with
# dots → underscores (``mdk.jobs.completed`` → ``mdk_jobs_completed``) and the
# unit suffix the OTLP→Prometheus convention appends (``_total`` for monotonic
# counters, ``_milliseconds`` for the ms histogram); Azure Monitor's
# ``azuremonitor`` exporter preserves the dot-names verbatim in ``AppMetrics``.
# The dashboards encode those per-backend transforms; this set is the canonical
# instrument-name vocabulary they all derive from.
METRIC_JOBS_COMPLETED = "mdk.jobs.completed"
METRIC_JOB_DURATION_MS = "mdk.job.duration_ms"
METRIC_JOBS_IN_FLIGHT = "mdk.jobs.in_flight"
METRIC_RUN_TOKENS = "mdk.run.tokens"
METRIC_RUN_COST_USD = "mdk.run.cost_usd"

# ADR 034 D3 — Postgres connection-pool saturation. Observable gauges sampled
# from the LIVE per-pod asyncpg pool at collection time (see
# :func:`register_pool_metrics`), not recorded at a hot edge. The scale risk
# they surface: under KEDA autoscale ``N_pods x pool.max_size`` can exceed Azure
# Postgres ``max_connections`` → connection exhaustion (invisible until load).
# ``in_use`` rising toward ``max`` (per pod) and a sustained non-zero ``waiting``
# are the early-warning signals; the ``mdk doctor`` capacity check (ADR 034 D1)
# does the static ceiling math. Postgres-only — the SQLite local backend has no
# pool, so these stay flat/zero there.
METRIC_DB_POOL_SIZE = "mdk.db.pool.size"
METRIC_DB_POOL_IDLE = "mdk.db.pool.idle"
METRIC_DB_POOL_IN_USE = "mdk.db.pool.in_use"
METRIC_DB_POOL_WAITING = "mdk.db.pool.waiting"
METRIC_DB_POOL_MAX = "mdk.db.pool.max"

# ADR 035 D3 — number of SSE subscribers currently holding open a
# ``GET /api/v1/events/stream`` connection. UpDownCounter (not a gauge):
# incremented when a connection opens, decremented when it closes — the
# delta is the source of truth, no separate observer callback. Surfaces
# the runtime's primary cost from D3 (one polling task per active
# subscriber) so an operator can spot a runaway client owning a slice
# of the pool, and pair the count with the advisory per-tenant cap.
METRIC_SSE_CONNECTIONS_ACTIVE = "mdk.sse.connections_active"

# Voice turn latency (ADR 024/036/073) — the voice subsystem stamps per-stage
# ``at_ms`` offsets on its event stream and computes a ``VoiceTurnLatency`` at the
# WS edge (``compute_turn_latency``). Until now those numbers lived only in the
# per-turn latency badge and an in-process MetricsObserver — never exported. These
# bridge them to OTel so an operator sees voice health (the headline first-audio
# latency, STT endpoint + TTS first-audio breakdown, turn volume, and barge-in
# rate) alongside the agent/job signals. Recorded once per turn at the edge.
METRIC_VOICE_RESPONDED_MS = "mdk.voice.responded_ms"  # turn start → first audio (headline)
METRIC_VOICE_STT_FINAL_MS = "mdk.voice.stt_final_ms"  # turn start → STT endpoint
METRIC_VOICE_TTS_FIRST_AUDIO_MS = "mdk.voice.tts_first_audio_ms"  # turn start → first TTS frame
METRIC_VOICE_TURNS = "mdk.voice.turns"  # completed voice turns (attr: interrupted = barge-in)

#: Every OTel instrument name this module emits. The single source of truth the
#: dashboards-as-code drift guard cross-checks against (a dashboard may only
#: reference a metric that appears here).
METRIC_NAMES: frozenset[str] = frozenset(
    {
        METRIC_JOBS_COMPLETED,
        METRIC_JOB_DURATION_MS,
        METRIC_JOBS_IN_FLIGHT,
        METRIC_RUN_TOKENS,
        METRIC_RUN_COST_USD,
        METRIC_DB_POOL_SIZE,
        METRIC_DB_POOL_IDLE,
        METRIC_DB_POOL_IN_USE,
        METRIC_DB_POOL_WAITING,
        METRIC_DB_POOL_MAX,
        METRIC_SSE_CONNECTIONS_ACTIVE,
        METRIC_VOICE_RESPONDED_MS,
        METRIC_VOICE_STT_FINAL_MS,
        METRIC_VOICE_TTS_FIRST_AUDIO_MS,
        METRIC_VOICE_TURNS,
    }
)


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
    sse_connections_active: Any = None  # UpDownCounter[int]
    voice_responded_ms: Any = None  # Histogram[float]
    voice_stt_final_ms: Any = None  # Histogram[float]
    voice_tts_first_audio_ms: Any = None  # Histogram[float]
    voice_turns: Any = None  # Counter[int]

    # ADR 034 D3 — DB pool observable gauges are registered lazily by
    # ``register_pool_metrics`` (after storage.init at the edge), not in
    # ``init_metrics``, because they need a callback that reads the live pool.
    # The flag makes that registration idempotent (a re-register is a no-op).
    pool_gauges_registered: bool = False


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
        METRIC_JOBS_COMPLETED,
        unit="1",
        description="Jobs reaching a terminal status (success/error/safety_blocked/dead_letter).",
    )
    _state.job_duration_ms = meter.create_histogram(
        METRIC_JOB_DURATION_MS,
        unit="ms",
        description="Wall-clock duration of a job from claim to terminal status.",
    )
    _state.jobs_in_flight = meter.create_up_down_counter(
        METRIC_JOBS_IN_FLIGHT,
        unit="1",
        description="Jobs currently being dispatched by a worker (claimed, not yet terminal).",
    )
    _state.run_tokens = meter.create_counter(
        METRIC_RUN_TOKENS,
        unit="1",
        description="Total LLM tokens consumed by executed runs.",
    )
    _state.run_cost_usd = meter.create_counter(
        METRIC_RUN_COST_USD,
        unit="usd",
        description="Total LLM cost (USD) of executed runs.",
    )
    _state.sse_connections_active = meter.create_up_down_counter(
        METRIC_SSE_CONNECTIONS_ACTIVE,
        unit="1",
        description=(
            "SSE event-stream subscribers currently holding open a "
            "GET /api/v1/events/stream connection (ADR 035 D3)."
        ),
    )
    _state.voice_responded_ms = meter.create_histogram(
        METRIC_VOICE_RESPONDED_MS,
        unit="ms",
        description="Voice turn latency: turn start → first audio the user hears (headline).",
    )
    _state.voice_stt_final_ms = meter.create_histogram(
        METRIC_VOICE_STT_FINAL_MS,
        unit="ms",
        description="Voice turn latency: turn start → STT endpoint (user words final).",
    )
    _state.voice_tts_first_audio_ms = meter.create_histogram(
        METRIC_VOICE_TTS_FIRST_AUDIO_MS,
        unit="ms",
        description="Voice turn latency: turn start → first synthesized audio frame.",
    )
    _state.voice_turns = meter.create_counter(
        METRIC_VOICE_TURNS,
        unit="1",
        description="Completed voice turns (attr: interrupted=true for a barge-in).",
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

    # ADR 039 Phase 2 — opt-in dual export. When MDK_TELEMETRY_ENDPOINT is
    # set (paired with MDK_TELEMETRY_CUSTOMER_ID), attach a SECOND
    # PeriodicExportingMetricReader pointing at Movate's central Collector.
    # The two readers share the MeterProvider's instruments — each instrument
    # record fans out to both readers, so the primary stream is unaffected.
    # The dual builder returns None on any failure path (SDK unavailable,
    # endpoint unreachable, half-configured env) so the primary list is
    # always intact.
    readers: list[Any] = [metric_reader]
    from movate.tracing.dual_export import (  # noqa: PLC0415 - lazy by design
        build_dual_metric_reader,
    )

    dual_reader = build_dual_metric_reader()
    if dual_reader is not None:
        readers.append(dual_reader)

    return MeterProvider(resource=resource, metric_readers=readers)


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
# ADR 034 D3 — DB connection-pool observable gauges
# ---------------------------------------------------------------------------
#: Callback signature: returns a snapshot of the live pool's counts, or ``None``
#: when no pool exists this cycle (SQLite backend, or before ``storage.init``).
#: Keys: ``size`` / ``idle`` / ``in_use`` / ``waiting`` / ``max``. Kept a plain
#: ``dict[str, int]`` so ``tracing`` never imports ``storage`` (boundary rule 6)
#: — the edge converts its backend-specific ``PoolStats`` into this mapping.
PoolStatsCallback = Callable[[], "dict[str, int] | None"]


def register_pool_metrics(stats_callback: PoolStatsCallback) -> None:
    """Register the DB pool observable gauges (ADR 034 D3). Idempotent, fail-soft.

    Called once at the runtime edge (``mdk serve`` / ``mdk worker``) AFTER
    ``storage.init()`` and ``init_metrics()``, passing a zero-arg callback that
    returns a ``{"size","idle","in_use","waiting","max": int}`` snapshot of the
    live asyncpg pool (or ``None`` when there's no pool — SQLite, or
    not-yet-initialised). OTel invokes the callback on each metric collection
    cycle, so the gauges always reflect the *current* pool, never a stale value.

    Wiring it at the edge (not inside ``init_metrics``) keeps tracing decoupled
    from storage: ``init_metrics`` has no storage handle, and ``storage`` never
    imports ``tracing``. A complete no-op when metrics aren't initialised (OTel
    absent / sink off / ``init_metrics`` never ran) or already registered. Never
    raises — observability must not break the runtime.
    """
    if _state.pool_gauges_registered:
        return
    meter = _state.meter
    if meter is None:
        # init_metrics didn't build a meter (OTel absent / sink off). Mark
        # registered so a retry from the other edge is also a clean no-op.
        _state.pool_gauges_registered = True
        return

    try:
        from opentelemetry.metrics import Observation  # noqa: PLC0415
    except Exception:  # pragma: no cover - OTel present if meter is not None
        _state.pool_gauges_registered = True
        return

    def _make_observer(key: str) -> Any:
        """Build the per-metric callback OTel polls each collection cycle."""

        def _observe(_options: Any) -> list[Any]:
            try:
                snapshot = stats_callback()
            except Exception:  # pragma: no cover - defensive; never break collect
                return []
            if not snapshot or key not in snapshot:
                return []
            return [Observation(snapshot[key])]

        return _observe

    # One observable gauge per pool dimension. No attributes: a pod emits one
    # pool, and the per-pod identity comes from the OTel ``Resource``
    # (service.instance / pod name) the collector already stamps — keeping
    # cardinality at zero on the instrument itself.
    meter.create_observable_gauge(
        METRIC_DB_POOL_SIZE,
        callbacks=[_make_observer("size")],
        unit="1",
        description="Connections currently held (open) by the per-pod asyncpg pool.",
    )
    meter.create_observable_gauge(
        METRIC_DB_POOL_IDLE,
        callbacks=[_make_observer("idle")],
        unit="1",
        description="Idle (checked-in, available) connections in the per-pod asyncpg pool.",
    )
    meter.create_observable_gauge(
        METRIC_DB_POOL_IN_USE,
        callbacks=[_make_observer("in_use")],
        unit="1",
        description="Checked-out connections in the per-pod asyncpg pool (size - idle).",
    )
    meter.create_observable_gauge(
        METRIC_DB_POOL_WAITING,
        callbacks=[_make_observer("waiting")],
        unit="1",
        description="Callers blocked waiting for a free connection (pool acquire queue).",
    )
    meter.create_observable_gauge(
        METRIC_DB_POOL_MAX,
        callbacks=[_make_observer("max")],
        unit="1",
        description="Configured per-pod pool ceiling (create_pool max_size); saturation denom.",
    )
    _state.pool_gauges_registered = True


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


def record_voice_turn(
    *,
    tenant_id: str,
    responded_ms: float | None = None,
    stt_final_ms: float | None = None,
    tts_first_audio_ms: float | None = None,
    interrupted: bool = False,
) -> None:
    """Record one completed voice turn's latency breakdown + barge-in flag.

    Bridges the voice subsystem's per-turn ``VoiceTurnLatency`` (computed at the
    WS edge via ``compute_turn_latency``) to OTel: the headline first-audio
    latency plus the STT-endpoint / TTS-first-audio milestones (each a histogram;
    ``None`` milestones are skipped, e.g. a turn that errored at STT), and a turn
    counter tagged ``interrupted`` so an operator sees barge-in rate. No-op when
    metrics are off / OTel absent; never raises.
    """
    if _state.voice_turns is None:
        return
    if responded_ms is not None:
        _state.voice_responded_ms.record(float(responded_ms), {"tenant": tenant_id})
    if stt_final_ms is not None:
        _state.voice_stt_final_ms.record(float(stt_final_ms), {"tenant": tenant_id})
    if tts_first_audio_ms is not None:
        _state.voice_tts_first_audio_ms.record(float(tts_first_audio_ms), {"tenant": tenant_id})
    _state.voice_turns.add(1, {"tenant": tenant_id, "interrupted": str(interrupted).lower()})


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


def inc_sse_connections(*, tenant_id: str) -> None:
    """Increment the active SSE subscriber gauge (attrs: ``tenant``).

    Bumped on every ``GET /api/v1/events/stream`` connection open (ADR
    035 D3). Pair with :func:`dec_sse_connections` in a try/finally so a
    client disconnect / handler exception still drops the count — leaks
    here turn the gauge into a fake-news number very quickly.
    """
    if _state.sse_connections_active is None:
        return
    _state.sse_connections_active.add(1, {"tenant": tenant_id})


def dec_sse_connections(*, tenant_id: str) -> None:
    """Decrement the active SSE subscriber gauge (attrs: ``tenant``).

    No-op when metrics are off. Always invoke in the matching
    :func:`inc_sse_connections` call's ``finally`` block so disconnects
    (the common terminal state for SSE) decrement reliably.
    """
    if _state.sse_connections_active is None:
        return
    _state.sse_connections_active.add(-1, {"tenant": tenant_id})


__all__ = [
    "METRIC_DB_POOL_IDLE",
    "METRIC_DB_POOL_IN_USE",
    "METRIC_DB_POOL_MAX",
    "METRIC_DB_POOL_SIZE",
    "METRIC_DB_POOL_WAITING",
    "METRIC_JOBS_COMPLETED",
    "METRIC_JOBS_IN_FLIGHT",
    "METRIC_JOB_DURATION_MS",
    "METRIC_NAMES",
    "METRIC_RUN_COST_USD",
    "METRIC_RUN_TOKENS",
    "METRIC_SSE_CONNECTIONS_ACTIVE",
    "METRIC_VOICE_RESPONDED_MS",
    "METRIC_VOICE_STT_FINAL_MS",
    "METRIC_VOICE_TTS_FIRST_AUDIO_MS",
    "METRIC_VOICE_TURNS",
    "PoolStatsCallback",
    "dec_in_flight",
    "dec_sse_connections",
    "inc_in_flight",
    "inc_sse_connections",
    "init_metrics",
    "record_job_completed",
    "record_run_usage",
    "record_voice_turn",
    "register_pool_metrics",
]
