"""Temporal worker 'registered workflows' health gauge (#784).

The temporal worker can connect to Temporal but register **0** workflows (an
image that shipped without ``workflows/`` — the silent drift that broke the
refund-approval demo). ``register_temporal_worker_metrics`` exposes the count as
an observable gauge so a Grafana panel + the worker's structured health log can
catch it. The registration is fail-soft + idempotent.
"""

from __future__ import annotations

import pytest

from movate.tracing import metrics as m


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    m._state.temporal_worker_gauge_registered = False


@pytest.mark.unit
def test_register_is_noop_without_meter() -> None:
    """No meter (OTel absent / sink off) → a clean no-op that marks itself done."""
    m._state.meter = None
    m.register_temporal_worker_metrics(registered=0, build="test")
    assert m._state.temporal_worker_gauge_registered is True


@pytest.mark.unit
def test_register_is_idempotent_and_never_raises() -> None:
    m._state.meter = None
    m.register_temporal_worker_metrics(registered=3, build="2026.6.8.13")
    # Second call is a no-op (already registered) — must not raise or re-register.
    m.register_temporal_worker_metrics(registered=99, build="other")
    assert m._state.temporal_worker_gauge_registered is True


@pytest.mark.unit
def test_metric_name_is_in_source_of_truth() -> None:
    """The gauge name is in METRIC_NAMES so the dashboard drift-guard tracks it."""
    assert m.METRIC_TEMPORAL_REGISTERED_WORKFLOWS == "mdk.temporal.worker.registered_workflows"
    assert m.METRIC_TEMPORAL_REGISTERED_WORKFLOWS in m.METRIC_NAMES


@pytest.mark.unit
def test_register_creates_gauge_when_meter_present() -> None:
    """With a fake meter, the observable gauge is created with the metric name + callback."""
    created: dict[str, object] = {}

    class _FakeMeter:
        def create_observable_gauge(self, name: str, *, callbacks: list, **kw: object) -> None:
            created["name"] = name
            created["callbacks"] = callbacks

    m._state.meter = _FakeMeter()  # type: ignore[assignment]
    try:
        m.register_temporal_worker_metrics(registered=2, build="b1")
    finally:
        m._state.meter = None
    assert created["name"] == "mdk.temporal.worker.registered_workflows"
    # The callback returns the count as an Observation when OTel is importable.
    obs = created["callbacks"][0](None)  # type: ignore[index]
    assert obs and obs[0].value == 2
