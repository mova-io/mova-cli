"""ADR 034 D1 — `mdk doctor` connection-ceiling capacity check.

Pure-function tests for `compute_capacity_verdict`: the static math behind the
`pods x pool_max <= max_connections - headroom` sizing formula, plus the
observed > env > assumed input-resolution precedence and graceful degradation.
"""

from __future__ import annotations

import pytest

from movate.cli._pool_capacity import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_POOL_MAX,
    ENV_HEADROOM,
    ENV_MAX_CONNECTIONS,
    ENV_MAX_REPLICAS,
    ENV_POOL_MAX,
    SIZING_FORMULA,
    compute_capacity_verdict,
)
from movate.cli.doctor import _render_pool_capacity_section


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ENV_POOL_MAX, ENV_MAX_REPLICAS, ENV_MAX_CONNECTIONS, ENV_HEADROOM):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
def test_observed_inputs_fit_under_ceiling_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observed values that fit → green ok, no assumptions, demand math correct."""
    _clear_env(monkeypatch)
    v = compute_capacity_verdict(
        observed_pool_max=10,
        observed_max_connections=200,
        observed_max_replicas=4,
    )
    assert v.status == "ok"
    assert v.demand == 40  # 4 pods x 10 pool_max
    assert v.ceiling == 180  # 200 - 20 headroom default
    # Capacity inputs all observed → ok. Headroom is a defaulted reserve, shown
    # for transparency but it alone doesn't downgrade a fully-observed verdict.
    assert v.assumed == ["headroom=20"]
    assert not v.remediation


@pytest.mark.unit
def test_over_ceiling_warns_with_formula_and_remediation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Demand over the ceiling → yellow warn naming the formula + the three fixes."""
    _clear_env(monkeypatch)
    v = compute_capacity_verdict(
        observed_pool_max=50,
        observed_max_connections=100,
        observed_max_replicas=10,
    )
    assert v.status == "warn"
    assert v.demand == 500  # 10 x 50 — way over
    assert v.ceiling == 80  # 100 - 20
    assert "PgBouncer" in v.remediation
    assert ENV_POOL_MAX in v.remediation
    assert ENV_MAX_REPLICAS in v.remediation
    assert SIZING_FORMULA == "pods x pool_max <= max_connections - headroom"


@pytest.mark.unit
def test_warns_even_on_assumed_inputs_when_over_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A likely-real exhaustion risk is loud even if some inputs were assumed."""
    _clear_env(monkeypatch)
    # Observe only a large pool_max + a small ceiling; replicas assumed.
    v = compute_capacity_verdict(
        observed_pool_max=40,
        observed_max_connections=50,
        observed_max_replicas=None,  # assumed → 2x2 = 4 pods
    )
    assert v.status == "warn"
    assert v.demand == 160  # 4 x 40
    assert any("pods=" in a for a in v.assumed)


@pytest.mark.unit
def test_fully_assumed_but_fitting_is_informational(monkeypatch: pytest.MonkeyPatch) -> None:
    """No observed inputs + the defaults fit → dim info (advisory), not green ok."""
    _clear_env(monkeypatch)
    v = compute_capacity_verdict()
    # Defaults: 4 pods x 10 = 40 demand vs 100 - 20 = 80 ceiling → fits.
    assert v.status == "info"
    assert v.demand == 4 * DEFAULT_POOL_MAX  # api(2) + worker(2) pods x pool_max
    assert v.ceiling == DEFAULT_MAX_CONNECTIONS - 20
    # Every input was assumed.
    assert any("pool_max=" in a for a in v.assumed)
    assert any("max_connections=" in a for a in v.assumed)
    assert any("headroom=" in a for a in v.assumed)


@pytest.mark.unit
def test_env_overrides_feed_the_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars supply inputs when nothing is observed (env beats assumed)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_POOL_MAX, "25")
    monkeypatch.setenv(ENV_MAX_REPLICAS, "8")
    monkeypatch.setenv(ENV_MAX_CONNECTIONS, "150")
    monkeypatch.setenv(ENV_HEADROOM, "30")
    v = compute_capacity_verdict()
    assert v.pool_max == 25
    assert v.pods == 8
    assert v.max_connections == 150
    assert v.headroom == 30
    assert v.demand == 200  # 8 x 25
    assert v.ceiling == 120  # 150 - 30
    assert v.status == "warn"  # 200 > 120
    # All inputs came from env, so none is "assumed".
    assert v.assumed == []


@pytest.mark.unit
def test_observed_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An observed value wins over a conflicting env override."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_POOL_MAX, "99")
    monkeypatch.setenv(ENV_MAX_CONNECTIONS, "99")
    v = compute_capacity_verdict(observed_pool_max=5, observed_max_connections=500)
    assert v.pool_max == 5
    assert v.max_connections == 500


@pytest.mark.unit
def test_invalid_and_nonpositive_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage / non-positive env values are ignored → assumed default used."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_POOL_MAX, "not-a-number")
    monkeypatch.setenv(ENV_MAX_CONNECTIONS, "0")
    v = compute_capacity_verdict()
    assert v.pool_max == DEFAULT_POOL_MAX
    assert v.max_connections == DEFAULT_MAX_CONNECTIONS
    assert any("pool_max=" in a for a in v.assumed)
    assert any("max_connections=" in a for a in v.assumed)


# ---------------------------------------------------------------------------
# Doctor section rendering — graceful degradation without a DB
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doctor_capacity_section_renders_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no DB reachable, the section renders an informational row (assumed
    inputs) and never crashes — the probe degrades to (None, None)."""
    _clear_env(monkeypatch)
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)

    rows: list[tuple[str, str]] = []

    def _add(check: str, result: str, *extra: str) -> None:
        rows.append((check, result))

    _render_pool_capacity_section(_add)

    assert len(rows) == 1
    check, result = rows[0]
    assert check == "db pool capacity"
    # Default inputs (4 pods x 10 = 40) fit under 100 - 20 = 80 → informational.
    assert "within ceiling" in result
    assert "formula" in result


@pytest.mark.unit
def test_doctor_capacity_section_warns_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-driven inputs that violate the ceiling render a yellow ⚠ warn row."""
    _clear_env(monkeypatch)
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv(ENV_POOL_MAX, "30")
    monkeypatch.setenv(ENV_MAX_REPLICAS, "10")
    monkeypatch.setenv(ENV_MAX_CONNECTIONS, "100")

    rows: list[tuple[str, str]] = []

    def _add(check: str, result: str, *extra: str) -> None:
        rows.append((check, result))

    _render_pool_capacity_section(_add)

    assert len(rows) == 1
    _check, result = rows[0]
    assert "[yellow]" in result
    assert "exhaustion risk" in result
    assert "PgBouncer" in result
