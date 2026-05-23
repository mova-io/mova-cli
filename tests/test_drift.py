"""Drift detection + baseline selection + alert wiring (ADR 016 D2).

Covers the comparator (``detect_drift``), the baseline picker
(``select_baseline``), and the alert dispatch (``alert_on_drift``) with a
fake :class:`NotificationDispatcher` so the alert path is asserted without
SMTP. Pure-logic tests — no storage, no executor, no LLM.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from movate.core.drift import alert_on_drift, detect_drift, select_baseline
from movate.core.models import EvalRecord, JudgeMethod


def _eval(
    *,
    eval_id: str,
    agent: str = "demo",
    mean_score: float,
    pass_rate: float,
    dataset_hash: str = "h1",
    created_at: datetime | None = None,
    tenant_id: str = "tenant-a",
) -> EvalRecord:
    return EvalRecord(
        eval_id=eval_id,
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        dataset_hash=dataset_hash,
        judge_method=JudgeMethod.EXACT,
        judge_provider=None,
        runs_per_case=1,
        gate_mode="mean",
        threshold=0.7,
        mean_score=mean_score,
        pass_rate=pass_rate,
        sample_count=10,
        total_cost_usd=0.0,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# detect_drift
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_regression_when_score_drop_exceeds_tolerance() -> None:
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90)
    cur = _eval(eval_id="c", mean_score=0.80, pass_rate=0.90)  # -0.10 mean
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is True
    assert "mean_score" in result.regressed_metrics
    assert result.mean_score_delta == pytest.approx(-0.10)


@pytest.mark.unit
def test_not_regressed_within_tolerance() -> None:
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90)
    cur = _eval(eval_id="c", mean_score=0.87, pass_rate=0.88)  # -0.03 / -0.02
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is False
    assert result.regressed_metrics == []


@pytest.mark.unit
def test_pass_rate_regression_detected_independently() -> None:
    """A pass_rate drop alone (mean_score steady) still flags drift."""
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90)
    cur = _eval(eval_id="c", mean_score=0.90, pass_rate=0.80)  # -0.10 pass_rate
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is True
    assert result.regressed_metrics == ["pass_rate"]


@pytest.mark.unit
def test_improvement_is_never_a_regression() -> None:
    base = _eval(eval_id="b", mean_score=0.70, pass_rate=0.70)
    cur = _eval(eval_id="c", mean_score=0.95, pass_rate=0.95)
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.regressed is False


@pytest.mark.unit
def test_no_baseline_no_false_alarm() -> None:
    cur = _eval(eval_id="c", mean_score=0.10, pass_rate=0.10)
    result = detect_drift(cur, None, tolerance=0.05)
    assert result.regressed is False
    assert result.has_baseline is False
    assert "no baseline" in result.summary()


@pytest.mark.unit
def test_dataset_change_flagged_but_not_a_regression_by_itself() -> None:
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90, dataset_hash="h1")
    cur = _eval(eval_id="c", mean_score=0.90, pass_rate=0.90, dataset_hash="h2")
    result = detect_drift(cur, base, tolerance=0.05)
    assert result.dataset_changed is True
    assert result.regressed is False


# ---------------------------------------------------------------------------
# select_baseline
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_select_prior_eval_as_baseline() -> None:
    now = datetime.now(UTC)
    older = _eval(eval_id="old", mean_score=0.9, pass_rate=0.9, created_at=now - timedelta(days=2))
    mid = _eval(eval_id="mid", mean_score=0.85, pass_rate=0.85, created_at=now - timedelta(days=1))
    cur = _eval(eval_id="cur", mean_score=0.80, pass_rate=0.80, created_at=now)
    chosen = select_baseline(current=cur, candidates=[cur, older, mid])
    # Newest prior (mid), excluding cur itself.
    assert chosen is not None
    assert chosen.eval_id == "mid"


@pytest.mark.unit
def test_select_pinned_baseline_id() -> None:
    now = datetime.now(UTC)
    pinned = _eval(eval_id="pin", mean_score=0.9, pass_rate=0.9, created_at=now - timedelta(days=5))
    mid = _eval(eval_id="mid", mean_score=0.85, pass_rate=0.85, created_at=now - timedelta(days=1))
    cur = _eval(eval_id="cur", mean_score=0.80, pass_rate=0.80, created_at=now)
    chosen = select_baseline(current=cur, candidates=[cur, pinned, mid], baseline_id="pin")
    assert chosen is not None
    assert chosen.eval_id == "pin"


@pytest.mark.unit
def test_select_no_prior_returns_none() -> None:
    cur = _eval(eval_id="cur", mean_score=0.8, pass_rate=0.8)
    assert select_baseline(current=cur, candidates=[cur]) is None


@pytest.mark.unit
def test_select_ignores_other_agents() -> None:
    now = datetime.now(UTC)
    other = _eval(
        eval_id="other",
        agent="not-demo",
        mean_score=0.9,
        pass_rate=0.9,
        created_at=now - timedelta(days=1),
    )
    cur = _eval(eval_id="cur", agent="demo", mean_score=0.8, pass_rate=0.8, created_at=now)
    assert select_baseline(current=cur, candidates=[cur, other]) is None


# ---------------------------------------------------------------------------
# alert_on_drift — fake dispatcher
# ---------------------------------------------------------------------------


class _FakeDispatcher:
    name = "fake"

    def __init__(self) -> None:
        self.alerts: list[dict[str, str | None]] = []
        self.terminal_calls = 0

    async def notify_terminal(self, job) -> None:  # type: ignore[no-untyped-def]
        self.terminal_calls += 1

    async def notify_alert(self, *, subject: str, body: str, email: str | None) -> None:
        self.alerts.append({"subject": subject, "body": body, "email": email})


@pytest.mark.unit
async def test_alert_fires_on_regression() -> None:
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90)
    cur = _eval(eval_id="c", mean_score=0.70, pass_rate=0.90)
    result = detect_drift(cur, base, tolerance=0.05)
    disp = _FakeDispatcher()
    fired = await alert_on_drift(result, notifier=disp, notify_email="ops@example.com")
    assert fired is True
    assert len(disp.alerts) == 1
    alert = disp.alerts[0]
    assert "demo" in alert["subject"]
    assert alert["email"] == "ops@example.com"
    assert "c" in alert["body"] and "b" in alert["body"]


@pytest.mark.unit
async def test_no_alert_without_regression() -> None:
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90)
    cur = _eval(eval_id="c", mean_score=0.89, pass_rate=0.90)
    result = detect_drift(cur, base, tolerance=0.05)
    disp = _FakeDispatcher()
    fired = await alert_on_drift(result, notifier=disp, notify_email="ops@example.com")
    assert fired is False
    assert disp.alerts == []


@pytest.mark.unit
async def test_no_alert_without_baseline() -> None:
    cur = _eval(eval_id="c", mean_score=0.10, pass_rate=0.10)
    result = detect_drift(cur, None, tolerance=0.05)
    disp = _FakeDispatcher()
    fired = await alert_on_drift(result, notifier=disp, notify_email="ops@example.com")
    assert fired is False
    assert disp.alerts == []


@pytest.mark.unit
async def test_structured_log_event_on_regression(caplog) -> None:
    """The eval_drift_detected structured log fires even without a notifier."""
    base = _eval(eval_id="b", mean_score=0.90, pass_rate=0.90)
    cur = _eval(eval_id="c", mean_score=0.50, pass_rate=0.90)
    result = detect_drift(cur, base, tolerance=0.05)
    with caplog.at_level(logging.WARNING, logger="movate.core.drift"):
        fired = await alert_on_drift(result, notifier=None)
    assert fired is True
    assert any("eval_drift_detected" in rec.message for rec in caplog.records)
