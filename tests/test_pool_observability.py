"""ADR 034 D3 — Postgres connection-pool observability.

Two layers:

1. **`PostgresProvider.pool_stats()`** — reads the live asyncpg pool's counters
   (`get_size` / `get_idle_size` / `get_max_size` / `get_min_size`) + derives
   `in_use` and `waiting`, with no real Postgres (a fake pool object). Degrades
   to `None` before `init()` and never raises.
2. **`register_pool_metrics`** — the OTel observable gauges sample a callback on
   each collection cycle; with the real SDK + an `InMemoryMetricReader` we assert
   the five `mdk.db.pool.*` datapoints reflect the callback's snapshot, and that
   the seam is a safe no-op when metrics are off.
"""

from __future__ import annotations

import collections
from typing import Any

import pytest

import movate.tracing.metrics as metrics_mod
from movate.storage.postgres import PoolStats, PostgresProvider
from movate.tracing.metrics import _State, register_pool_metrics


def _otel_installed() -> bool:
    try:
        import opentelemetry.sdk.metrics  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# PostgresProvider.pool_stats()
# ---------------------------------------------------------------------------


class _FakeQueue:
    """Stand-in for the pool's asyncio.LifoQueue: only `_getters` is read."""

    def __init__(self, waiters: int) -> None:
        # asyncio.Queue._getters is a deque of blocked getter futures; we only
        # need its length, so a deque of N sentinels matches the read path.
        self._getters: collections.deque[object] = collections.deque(range(waiters))


class _FakePool:
    """Minimal asyncpg.Pool stand-in exposing the public sizing getters."""

    def __init__(self, *, size: int, idle: int, max_size: int, min_size: int, waiters: int) -> None:
        self._size = size
        self._idle = idle
        self._max = max_size
        self._min = min_size
        self._queue = _FakeQueue(waiters)

    def get_size(self) -> int:
        return self._size

    def get_idle_size(self) -> int:
        return self._idle

    def get_max_size(self) -> int:
        return self._max

    def get_min_size(self) -> int:
        return self._min


@pytest.mark.unit
def test_pool_stats_none_before_init() -> None:
    """No pool yet (init not called) → None, never an attribute error."""
    p = PostgresProvider(dsn="postgresql://u@h/db")
    assert p.pool_stats() is None


@pytest.mark.unit
def test_pool_stats_reads_live_counters() -> None:
    """Snapshot reflects the pool's getters; in_use = size - idle, waiting from queue."""
    p = PostgresProvider(dsn="postgresql://u@h/db", min_size=2, max_size=10)
    p._pool = _FakePool(size=7, idle=3, max_size=10, min_size=2, waiters=4)  # type: ignore[assignment]
    stats = p.pool_stats()
    assert stats == PoolStats(size=7, idle=3, in_use=4, waiting=4, max_size=10, min_size=2)


@pytest.mark.unit
def test_pool_stats_in_use_never_negative() -> None:
    """A transient idle > size race can't produce a negative in_use."""
    p = PostgresProvider(dsn="postgresql://u@h/db")
    p._pool = _FakePool(size=2, idle=5, max_size=10, min_size=1, waiters=0)  # type: ignore[assignment]
    stats = p.pool_stats()
    assert stats is not None
    assert stats.in_use == 0


@pytest.mark.unit
def test_pool_stats_waiting_degrades_to_zero_without_queue_internals() -> None:
    """If asyncpg's internal queue shape changes, waiting falls back to 0 (gauge stays useful)."""

    class _NoQueuePool(_FakePool):
        def __init__(self) -> None:
            super().__init__(size=5, idle=1, max_size=8, min_size=1, waiters=0)
            self._queue = object()  # type: ignore[assignment]  # no _getters attr

    p = PostgresProvider(dsn="postgresql://u@h/db")
    p._pool = _NoQueuePool()  # type: ignore[assignment]
    stats = p.pool_stats()
    assert stats is not None
    assert stats.waiting == 0
    assert stats.in_use == 4


@pytest.mark.unit
def test_pool_stats_never_raises_on_broken_getters() -> None:
    """A getter that raises degrades to None rather than crashing the sampler."""

    class _BrokenPool:
        def get_size(self) -> int:
            raise RuntimeError("pool closing")

        def get_idle_size(self) -> int:  # pragma: no cover - not reached
            return 0

        def get_max_size(self) -> int:  # pragma: no cover
            return 0

        def get_min_size(self) -> int:  # pragma: no cover
            return 0

    p = PostgresProvider(dsn="postgresql://u@h/db")
    p._pool = _BrokenPool()  # type: ignore[assignment]
    assert p.pool_stats() is None


# ---------------------------------------------------------------------------
# register_pool_metrics — observable gauges
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics_module() -> object:
    saved = metrics_mod._state
    metrics_mod._state = _State()
    yield
    metrics_mod._state = saved


@pytest.mark.unit
def test_register_pool_metrics_noop_when_meter_absent() -> None:
    """No active meter (metrics off) → register is a clean no-op, never raises."""
    # _state.meter is None (fresh _State from the fixture).
    calls: list[int] = []

    def _cb() -> dict[str, int] | None:
        calls.append(1)
        return {"size": 1, "idle": 1, "in_use": 0, "waiting": 0, "max": 10}

    register_pool_metrics(_cb)
    assert metrics_mod._state.pool_gauges_registered is True
    # Callback never invoked (no gauges were created).
    assert calls == []


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_pool_gauges_emit_callback_snapshot() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    from movate.tracing.metrics import init_metrics  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)

    snapshot = {"size": 8, "idle": 2, "in_use": 6, "waiting": 3, "max": 10}
    register_pool_metrics(lambda: snapshot)

    data = reader.get_metrics_data()
    points: dict[str, float] = {}
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for dp in metric.data.data_points:
                    points[metric.name] = dp.value

    assert points["mdk.db.pool.size"] == 8
    assert points["mdk.db.pool.idle"] == 2
    assert points["mdk.db.pool.in_use"] == 6
    assert points["mdk.db.pool.waiting"] == 3
    assert points["mdk.db.pool.max"] == 10


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_pool_gauges_record_nothing_when_callback_returns_none() -> None:
    """Callback returning None (no pool this cycle) → gauges emit no datapoints."""
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    from movate.tracing.metrics import init_metrics  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    register_pool_metrics(lambda: None)

    data = reader.get_metrics_data()
    names: set[str] = set()
    if data is not None:
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if list(metric.data.data_points):
                        names.add(metric.name)
    assert not any(n.startswith("mdk.db.pool") for n in names)


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_register_pool_metrics_is_idempotent() -> None:
    """A second register call is a no-op (no duplicate-instrument crash)."""
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    from movate.tracing.metrics import init_metrics  # noqa: PLC0415

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    register_pool_metrics(lambda: {"size": 1, "idle": 1, "in_use": 0, "waiting": 0, "max": 1})
    register_pool_metrics(lambda: {"size": 2, "idle": 2, "in_use": 0, "waiting": 0, "max": 2})
    assert metrics_mod._state.pool_gauges_registered is True


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_pool_gauge_callback_failure_is_swallowed() -> None:
    """A throwing callback can't break metric collection."""
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    from movate.tracing.metrics import init_metrics  # noqa: PLC0415

    def _boom() -> dict[str, int] | None:
        raise RuntimeError("pool gone")

    reader = InMemoryMetricReader()
    init_metrics(reader=reader)
    register_pool_metrics(_boom)
    # Collection must not raise even though the callback throws.
    data: Any = reader.get_metrics_data()
    # No pool datapoints, but no exception either.
    names: set[str] = set()
    if data is not None:
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if list(metric.data.data_points):
                        names.add(metric.name)
    assert not any(n.startswith("mdk.db.pool") for n in names)
