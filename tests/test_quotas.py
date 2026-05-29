"""Unit tests for the per-tenant quota decision (ADR 036 D2).

The middleware integration lives in
``tests/test_runtime_quotas_mw.py``; this file tests the **pure decision**
behind it (``check_quota``) + the YAML config loader / saver.

Coverage:

* ``check_quota`` with synthetic usage values:
  - admin bypass → always allow (mode-irrelevant)
  - no row for tenant → always allow (mode-irrelevant)
  - under all ceilings → allow + empty ``over``
  - over a daily-token ceiling in ``deny`` mode → ``allow=False``
  - over a daily-token ceiling in ``warn`` mode → ``allow=True`` (mode-respect)
  - multiple ceilings tripped at once → all named in ``over`` + ``reason``
  - boundary (current == limit) → counted as over (the ceiling belongs to
    the blocker)
  - ``remaining`` only present for configured counters; clamped to ``>=0``
* YAML round-trip: write a config, read it back, ensure rows are preserved
  + an unknown mode gets normalized to ``warn``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.quotas import (
    DEFAULT_QUOTA_CONFIG_NAME,
    QuotaConfig,
    QuotaMode,
    RouteClass,
    TenantQuota,
    check_quota,
    load_quota_config,
    save_quota_config,
    upsert_tenant_quota,
)
from movate.core.reporting import Usage, UsageRollup


def _usage(
    *,
    requests: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
) -> Usage:
    """Build a synthetic :class:`Usage` rollup — the only field
    :func:`check_quota` actually reads is ``totals``."""
    return Usage(
        tenant_id="t",
        window_days=1,
        agent_filter=None,
        totals=UsageRollup(
            key="t",
            requests=requests,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        ),
        by_agent=[],
        by_provider=[],
    )


# ---------------------------------------------------------------------------
# check_quota — decision matrix
# ---------------------------------------------------------------------------


def test_check_quota_admin_bypass_always_allows() -> None:
    """Admin tenants are never blocked, regardless of configured ceilings."""
    quota = TenantQuota(
        tenant_id="t",
        daily_token_limit=10,
        mode=QuotaMode.DENY,
    )
    decision = check_quota(
        quota,
        daily_usage=_usage(tokens_in=999_999),
        monthly_usage=_usage(),
        is_admin=True,
    )
    assert decision.allow is True
    assert decision.over == ()


def test_check_quota_no_row_always_allows() -> None:
    """A tenant without a configured row passes through (opt-in posture)."""
    decision = check_quota(
        None,
        daily_usage=_usage(requests=999_999),
        monthly_usage=_usage(cost=999.99),
    )
    assert decision.allow is True
    assert decision.over == ()
    assert decision.reason == ""


def test_check_quota_under_all_ceilings_allows() -> None:
    quota = TenantQuota(
        tenant_id="t",
        daily_token_limit=1000,
        daily_request_limit=100,
        monthly_cost_usd_limit=10.0,
        mode=QuotaMode.DENY,
    )
    decision = check_quota(
        quota,
        daily_usage=_usage(requests=50, tokens_in=500, tokens_out=400),
        monthly_usage=_usage(cost=5.0),
    )
    assert decision.allow is True
    assert decision.over == ()
    # remaining is positive on every configured counter
    assert decision.remaining["daily_tokens"] == 100  # 1000 - (500+400)
    assert decision.remaining["daily_requests"] == 50
    assert decision.remaining["monthly_cost_usd"] == pytest.approx(5.0)


def test_check_quota_over_token_ceiling_deny_blocks() -> None:
    quota = TenantQuota(
        tenant_id="t",
        daily_token_limit=1000,
        mode=QuotaMode.DENY,
    )
    decision = check_quota(
        quota,
        daily_usage=_usage(tokens_in=600, tokens_out=500),
        monthly_usage=_usage(),
    )
    assert decision.allow is False
    assert decision.over == ("daily_tokens",)
    assert "daily_tokens" in decision.reason
    # remaining clamps to zero (current > limit), never negative.
    assert decision.remaining["daily_tokens"] == 0


def test_check_quota_over_token_ceiling_warn_allows_with_reason() -> None:
    """``warn`` mode logs + reports but never blocks — the rollout posture."""
    quota = TenantQuota(
        tenant_id="t",
        daily_token_limit=1000,
        mode=QuotaMode.WARN,
    )
    decision = check_quota(
        quota,
        daily_usage=_usage(tokens_in=600, tokens_out=500),
        monthly_usage=_usage(),
    )
    assert decision.allow is True
    assert decision.over == ("daily_tokens",)
    assert "daily_tokens" in decision.reason
    assert decision.mode == QuotaMode.WARN


def test_check_quota_multiple_ceilings_tripped() -> None:
    quota = TenantQuota(
        tenant_id="t",
        daily_token_limit=100,
        daily_request_limit=10,
        monthly_cost_usd_limit=1.0,
        mode=QuotaMode.DENY,
    )
    decision = check_quota(
        quota,
        daily_usage=_usage(requests=20, tokens_in=200, tokens_out=0),
        monthly_usage=_usage(cost=2.5),
    )
    assert decision.allow is False
    # all three named, in canonical order (tokens, requests, cost)
    assert decision.over == ("daily_tokens", "daily_requests", "monthly_cost_usd")
    assert "daily_tokens" in decision.reason
    assert "daily_requests" in decision.reason
    assert "monthly_cost_usd" in decision.reason


def test_check_quota_boundary_equal_is_over() -> None:
    """current == limit is treated as 'over' — the ceiling belongs to the
    blocker, not the last-allowed call."""
    quota = TenantQuota(
        tenant_id="t",
        daily_request_limit=100,
        mode=QuotaMode.DENY,
    )
    decision = check_quota(
        quota,
        daily_usage=_usage(requests=100),
        monthly_usage=_usage(),
    )
    assert decision.allow is False
    assert decision.over == ("daily_requests",)
    assert decision.remaining["daily_requests"] == 0


def test_check_quota_remaining_only_for_configured_counters() -> None:
    """Counters without a configured limit are absent from ``remaining``."""
    quota = TenantQuota(
        tenant_id="t",
        daily_token_limit=1000,  # only this one configured
        mode=QuotaMode.DENY,
    )
    decision = check_quota(
        quota,
        daily_usage=_usage(tokens_in=10, tokens_out=10, requests=999_999),
        monthly_usage=_usage(cost=999.99),
    )
    assert decision.allow is True
    assert set(decision.remaining.keys()) == {"daily_tokens"}
    assert decision.remaining["daily_tokens"] == 980


# ---------------------------------------------------------------------------
# RouteClass — string enum sanity
# ---------------------------------------------------------------------------


def test_route_class_values() -> None:
    """The three D2 route classes are stable strings the YAML uses."""
    assert RouteClass.RUNS.value == "runs"
    assert RouteClass.KB_INGEST.value == "kb_ingest"
    assert RouteClass.EVALS.value == "evals"


def test_quota_mode_parse_lenient() -> None:
    """Unknown / missing modes fall back to ``warn`` — never silently
    escalates to ``deny``."""
    assert QuotaMode.parse("warn") == QuotaMode.WARN
    assert QuotaMode.parse("DENY") == QuotaMode.DENY  # case-insensitive
    assert QuotaMode.parse(None) == QuotaMode.WARN
    assert QuotaMode.parse("") == QuotaMode.WARN
    assert QuotaMode.parse("strict") == QuotaMode.WARN  # unknown → warn


# ---------------------------------------------------------------------------
# YAML config — round-trip + edge cases
# ---------------------------------------------------------------------------


def test_yaml_round_trip_preserves_rows(tmp_path: Path) -> None:
    """Write a config, read it back, ensure rows + admin list survive."""
    path = tmp_path / DEFAULT_QUOTA_CONFIG_NAME
    cfg = QuotaConfig(
        tenants=[
            TenantQuota(
                tenant_id="t1",
                daily_token_limit=10_000,
                monthly_cost_usd_limit=50.0,
                mode=QuotaMode.DENY,
            ),
            TenantQuota(
                tenant_id="t2",
                daily_request_limit=200,
                mode=QuotaMode.WARN,
            ),
        ],
        admin_tenant_ids=["ops-tenant"],
    )
    save_quota_config(cfg, path)
    loaded = load_quota_config(path)
    assert loaded is not None
    assert {t.tenant_id for t in loaded.tenants} == {"t1", "t2"}
    t1 = loaded.get("t1")
    assert t1 is not None
    assert t1.daily_token_limit == 10_000
    assert t1.monthly_cost_usd_limit == 50.0
    assert t1.mode == QuotaMode.DENY
    t2 = loaded.get("t2")
    assert t2 is not None
    assert t2.daily_request_limit == 200
    assert t2.mode == QuotaMode.WARN
    assert loaded.is_admin("ops-tenant") is True


def test_load_quota_config_missing_file_is_none(tmp_path: Path) -> None:
    """Pointing at a non-existent file returns ``None`` (the 'opt-in
    disabled' state) — never raises."""
    assert load_quota_config(tmp_path / "absent.yaml") is None


def test_load_quota_config_unknown_mode_falls_back_to_warn(tmp_path: Path) -> None:
    """A typo in ``mode:`` must NOT silently escalate to ``deny``."""
    path = tmp_path / "q.yaml"
    path.write_text(
        "tenants:\n  - tenant_id: t\n    daily_token_limit: 100\n    mode: strict\n",  # typo
        encoding="utf-8",
    )
    cfg = load_quota_config(path)
    assert cfg is not None
    row = cfg.get("t")
    assert row is not None
    assert row.mode == QuotaMode.WARN


def test_load_quota_config_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    """A YAML list at the top level is a misconfig the operator must see."""
    path = tmp_path / "bad.yaml"
    path.write_text("- foo\n- bar\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_quota_config(path)


def test_upsert_replaces_existing_row() -> None:
    """``upsert_tenant_quota`` replaces an existing tenant's row in place."""
    cfg = QuotaConfig(
        tenants=[
            TenantQuota(tenant_id="t", daily_token_limit=100, mode=QuotaMode.WARN),
        ],
    )
    new = upsert_tenant_quota(
        cfg,
        TenantQuota(tenant_id="t", daily_token_limit=500, mode=QuotaMode.DENY),
    )
    assert len(new.tenants) == 1
    assert new.tenants[0].daily_token_limit == 500
    assert new.tenants[0].mode == QuotaMode.DENY


def test_upsert_appends_new_row() -> None:
    cfg = QuotaConfig(
        tenants=[TenantQuota(tenant_id="t1")],
        admin_tenant_ids=["ops"],
    )
    new = upsert_tenant_quota(cfg, TenantQuota(tenant_id="t2", daily_token_limit=10))
    assert {t.tenant_id for t in new.tenants} == {"t1", "t2"}
    assert new.admin_tenant_ids == ["ops"]
