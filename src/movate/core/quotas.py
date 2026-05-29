"""Per-tenant quota config + admission decision (ADR 036 D2).

The **billing-ceiling** companion to ADR 036 D1's usage metering. D1 *measures*
per-tenant requests / tokens / cost (``core.reporting.build_usage``); D2
*enforces* per-tenant ceilings at the admission edge — before a run / ingest is
accepted — returning ``429`` when over a hard ceiling (``deny`` mode) or simply
attaching an ``X-Quota-Warning`` header in passthrough ``warn`` mode.

Distinct from burst rate-limiting (ADR 013 / item 25 / ``core/rate_limit``),
which is **requests/sec**. Quotas are **aggregate ceilings** over a billing
window (per day for tokens / runs; per month for cost).

Design rules:

* **Pure / backend-agnostic.** No I/O at decision time, no concrete storage
  backend, no ``cli`` import. :func:`check_quota` is a pure reducer over
  config + an already-computed :class:`~movate.core.reporting.Usage` rollup —
  the runtime middleware fetches the usage via the existing
  ``build_usage(...)`` aggregator (no new measurement plumbing). The YAML
  config IS read here (small, file-system-only, no DB) — but only at load /
  save time, not per request.
* **Opt-in.** No config file → no enforcement (existing customers byte-for-byte
  unaffected). The config path is resolved from ``MDK_QUOTA_CONFIG`` (env)
  else ``quotas.yaml`` in CWD; both absent = quotas disabled.
* **Per-route-class** in D2 (``runs`` / ``kb_ingest`` / ``evals``) — one limit
  per kind, kept small. Per-route or per-tool granularity is a future seam.
* **Modes.** ``warn`` (default) = log + header + allow (the safe rollout
  posture); ``deny`` = 429 + block. Per-tenant configurable — commercial
  decision per ADR 036 (Deva sign-off on policy).
* **Admin bypass.** A tenant id listed in ``admin_tenant_ids`` is never
  blocked. Lets operators stage rollout without lock-out risk on their own
  tooling.
* **Empty / partial config degrades gracefully.** A missing limit field on a
  ``TenantQuota`` means "no ceiling for this kind" (``None``). An unknown
  ``mode`` falls back to ``warn`` (failure-mode rule — a typo never silently
  escalates to ``deny``).

See ``runtime/middleware.py`` (``make_quota_dependency``) for the FastAPI
dependency that wires this onto the write routes, and ``cli/tenants.py`` for
the ``mdk tenants quota`` surface that writes the config.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from movate.core.reporting import Usage

logger = logging.getLogger(__name__)

# Environment variable that points at the quota config file. Absent → look
# for ``quotas.yaml`` in CWD; absent too → quotas disabled (no enforcement).
QUOTA_CONFIG_ENV = "MDK_QUOTA_CONFIG"
DEFAULT_QUOTA_CONFIG_NAME = "quotas.yaml"

# Quota mode literal — :class:`QuotaMode` is the StrEnum used inside the code;
# the YAML config stores the plain string. ``warn`` is the safe default for
# rollout; ``deny`` is the hard ceiling.
QuotaModeStr = Literal["warn", "deny"]


class QuotaMode(StrEnum):
    """How a quota breach should be surfaced.

    * ``WARN`` — log + attach ``X-Quota-Warning`` header + allow the request
      (200 path). The safe rollout posture: visibility before enforcement so
      a misconfigured ceiling doesn't lock out a tenant.
    * ``DENY`` — 429 ``quota_exceeded``, the request never reaches the
      handler. The hard commercial ceiling.

    An unknown / malformed mode falls back to ``WARN`` (failure-mode rule).
    """

    WARN = "warn"
    DENY = "deny"

    @classmethod
    def parse(cls, raw: str | None) -> QuotaMode:
        """Lenient parser for YAML / CLI input.

        A missing / unknown mode falls back to :attr:`WARN` rather than
        raising — a typo in the config must never silently escalate to a
        hard 429.
        """
        if not raw:
            return cls.WARN
        try:
            return cls(raw.strip().lower())
        except ValueError:
            logger.warning("unknown quota mode %r; falling back to warn", raw)
            return cls.WARN


class RouteClass(StrEnum):
    """Coarse classes of write routes that share a quota counter (D2 keeps the
    surface small — one limit per kind).

    * ``RUNS`` — agent runs (``POST /api/v1/agents/{name}/runs`` and the
      generic ``POST /api/v1/run``). Counts against ``daily_request_limit`` /
      ``daily_token_limit`` / ``monthly_cost_usd_limit``.
    * ``KB_INGEST`` — knowledge-base writes (``POST/PUT /api/v1/agents/
      {name}/kb``, ``POST .../kb/reindex``). Same three counters apply (KB
      ingest also consumes tokens via the embedder).
    * ``EVALS`` — eval-suite kickoffs. Same counters.

    Future per-route or per-tool granularity is an additive seam — the YAML
    keys stay stable, new ones just get added.
    """

    RUNS = "runs"
    KB_INGEST = "kb_ingest"
    EVALS = "evals"


class TenantQuota(BaseModel):
    """Per-tenant ceiling config (one row per tenant in the YAML file).

    Every limit is **optional** (``None`` = "no ceiling for this counter") so a
    tenant can opt into one dimension (e.g. just monthly cost) without being
    forced to set the others. An unset limit never trips :func:`check_quota`.

    * ``daily_token_limit`` — sum of ``tokens_in + tokens_out`` per UTC day.
    * ``daily_request_limit`` — request count per UTC day.
    * ``monthly_cost_usd_limit`` — sum of ``cost_usd`` over the billing month.

    The window the runtime measures against is determined by the route's call
    to ``build_usage(window_days=...)`` — daily limits read a 1-day window,
    monthly limits read a 30-day window. Both windows hit the same
    ``RunRecord``\\ s, so this is a config-side knob, not new measurement.

    ``mode`` defaults to :attr:`QuotaMode.WARN` — the safe rollout posture.
    Flip to ``deny`` per-tenant when the commercial decision is made.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    daily_token_limit: int | None = Field(default=None, ge=0)
    daily_request_limit: int | None = Field(default=None, ge=0)
    monthly_cost_usd_limit: float | None = Field(default=None, ge=0.0)
    mode: QuotaMode = QuotaMode.WARN


class QuotaConfig(BaseModel):
    """Top-level shape of the YAML config file.

    Two sections:

    * ``tenants`` — list of :class:`TenantQuota` rows. Absent / empty → no
      tenant has a configured ceiling → no enforcement happens (everything
      passes through).
    * ``admin_tenant_ids`` — tenants that are NEVER blocked, regardless of
      configured ceilings. Operator tools and the platform's own tenant land
      here so a misconfigured cap can't lock them out.
    """

    model_config = ConfigDict(extra="forbid")

    tenants: list[TenantQuota] = Field(default_factory=list)
    admin_tenant_ids: list[str] = Field(default_factory=list)

    def get(self, tenant_id: str) -> TenantQuota | None:
        """Return the row for ``tenant_id`` or ``None`` if absent.

        ``None`` means the tenant has no configured ceiling — quotas are
        opt-in per tenant (matches ``TenantBudget``\\ 's posture). Callers
        treat ``None`` as "no enforcement for this tenant".
        """
        for row in self.tenants:
            if row.tenant_id == tenant_id:
                return row
        return None

    def is_admin(self, tenant_id: str) -> bool:
        """True when ``tenant_id`` is in the admin bypass list."""
        return tenant_id in self.admin_tenant_ids


@dataclass(frozen=True)
class QuotaDecision:
    """The outcome of a :func:`check_quota` call.

    * ``allow`` — let the request through (``True``) or block it (``False``).
      In :attr:`QuotaMode.WARN` this is **always ``True``** even when over a
      ceiling (warn = log + header + pass); in :attr:`QuotaMode.DENY` it's
      ``False`` only when at least one configured ceiling has been met /
      exceeded.
    * ``mode`` — the effective mode for this decision (mirrors the tenant's
      configured mode; carried so the middleware can branch without re-reading
      config).
    * ``reason`` — short human-readable string (e.g. ``"daily_tokens 12000/
      10000"``) for logging + the warn header / 429 message. Empty when
      ``allow=True`` and not warning.
    * ``remaining`` — per-counter remainder (configured - current), clamped to
      ``>=0``. Counters with no configured limit are absent. Sent back to the
      client in the 429 body so it can budget.
    * ``over`` — names of the counters that tripped (subset of
      ``daily_tokens`` / ``daily_requests`` / ``monthly_cost_usd``). Empty
      when no ceiling was hit. Used by tests + structured logs.
    """

    allow: bool
    mode: QuotaMode
    reason: str = ""
    remaining: dict[str, float] = field(default_factory=dict)
    over: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Decision (pure)
# ---------------------------------------------------------------------------


def _check_one(limit: float | None, current: float) -> tuple[bool, float]:
    """Return ``(over, remaining)`` for a single counter.

    ``limit=None`` (no ceiling) → ``(False, +inf)``. Negative computed
    remainders clamp to ``0`` so the client never sees a confusing negative
    "remaining". A counter is "over" when current is **greater than or equal
    to** the limit (the boundary belongs to the blocker — 100% spent is
    spent).
    """
    if limit is None:
        return (False, float("inf"))
    remaining = max(0.0, limit - current)
    return (current >= limit, remaining)


def check_quota(
    quota: TenantQuota | None,
    *,
    daily_usage: Usage,
    monthly_usage: Usage,
    is_admin: bool = False,
) -> QuotaDecision:
    """Decide whether to admit a request given the tenant's quota + current usage.

    Pure: no I/O. The caller (the runtime middleware) is responsible for
    fetching ``daily_usage`` (a 1-day :func:`~movate.core.reporting.build_usage`
    rollup, for the daily token + request counters) and ``monthly_usage`` (a
    30-day rollup, for the monthly cost counter) — typically through the same
    cached :class:`~movate.core.reporting.Usage` if both windows can share.
    Splitting them by argument keeps :func:`check_quota` pure (no time / I/O)
    and lets tests inject deterministic usage values.

    Decision tree:

    1. ``is_admin=True`` → always allow (admin bypass; see :class:`QuotaConfig`).
    2. ``quota=None`` → always allow (no row for this tenant = no ceiling).
    3. Otherwise, evaluate each configured counter; the request is "over"
       when **any** of them has been hit. The mode determines whether "over"
       blocks (``deny``) or warns (``warn``).

    The same :class:`QuotaDecision` shape is returned in every branch so the
    middleware can render headers / 429s uniformly.
    """
    if is_admin:
        return QuotaDecision(allow=True, mode=QuotaMode.WARN)
    if quota is None:
        return QuotaDecision(allow=True, mode=QuotaMode.WARN)

    daily_tokens_used = daily_usage.totals.tokens_in + daily_usage.totals.tokens_out
    daily_requests_used = daily_usage.totals.requests
    monthly_cost_used = monthly_usage.totals.cost_usd

    tokens_over, tokens_left = _check_one(
        float(quota.daily_token_limit) if quota.daily_token_limit is not None else None,
        float(daily_tokens_used),
    )
    requests_over, requests_left = _check_one(
        float(quota.daily_request_limit) if quota.daily_request_limit is not None else None,
        float(daily_requests_used),
    )
    cost_over, cost_left = _check_one(
        quota.monthly_cost_usd_limit,
        monthly_cost_used,
    )

    remaining: dict[str, float] = {}
    if quota.daily_token_limit is not None:
        remaining["daily_tokens"] = tokens_left
    if quota.daily_request_limit is not None:
        remaining["daily_requests"] = requests_left
    if quota.monthly_cost_usd_limit is not None:
        remaining["monthly_cost_usd"] = cost_left

    over_names: list[str] = []
    reasons: list[str] = []
    if tokens_over:
        over_names.append("daily_tokens")
        reasons.append(f"daily_tokens {daily_tokens_used}/{quota.daily_token_limit}")
    if requests_over:
        over_names.append("daily_requests")
        reasons.append(f"daily_requests {daily_requests_used}/{quota.daily_request_limit}")
    if cost_over:
        over_names.append("monthly_cost_usd")
        reasons.append(f"monthly_cost_usd {monthly_cost_used:.4f}/{quota.monthly_cost_usd_limit}")

    any_over = bool(over_names)
    reason = "; ".join(reasons)

    # In WARN mode we ALWAYS allow (the whole point of warn is visibility, not
    # block). In DENY mode we block iff a ceiling was met.
    allow = (not any_over) if quota.mode == QuotaMode.DENY else True

    return QuotaDecision(
        allow=allow,
        mode=quota.mode,
        reason=reason,
        remaining=remaining,
        over=tuple(over_names),
    )


# ---------------------------------------------------------------------------
# Config I/O — small, file-system only, NOT called per request
# ---------------------------------------------------------------------------


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Resolve the quota config file path.

    Precedence:

    1. ``explicit`` argument (tests / CLI ``--config`` flag pass this).
    2. ``MDK_QUOTA_CONFIG`` environment variable.
    3. :data:`DEFAULT_QUOTA_CONFIG_NAME` in CWD — only if it exists.

    Returns ``None`` when no config is configured AND the default file is
    absent — that's the "quotas disabled" state (opt-in posture). When env or
    explicit is set, the returned path may not yet exist (the CLI uses this
    to know *where to write* on first ``mdk tenants quota set``).
    """
    if explicit is not None:
        return Path(explicit)
    env_val = os.environ.get(QUOTA_CONFIG_ENV, "").strip()
    if env_val:
        return Path(env_val)
    default_path = Path.cwd() / DEFAULT_QUOTA_CONFIG_NAME
    if default_path.is_file():
        return default_path
    return None


def load_quota_config(path: Path | None = None) -> QuotaConfig | None:
    """Read + parse the quota YAML file.

    Returns ``None`` when no file is configured (opt-in: no file = no
    enforcement). Returns ``None`` ALSO when the path is configured but
    missing on disk — the operator pointed at a non-existent file, treat it
    the same as "not configured" rather than crashing the runtime boot.

    A malformed YAML / pydantic-invalid file raises — that IS a misconfig the
    operator must see at boot, not a silent passthrough.
    """
    resolved = path if path is not None else resolve_config_path()
    if resolved is None or not resolved.is_file():
        return None
    with resolved.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"quota config {resolved} must be a YAML mapping at top level, got {type(raw).__name__}"
        )
    # Lenient mode parsing on each tenant row — a typo never silently escalates.
    tenants_raw = raw.get("tenants") or []
    parsed_tenants: list[dict[str, Any]] = []
    for row in tenants_raw:
        if not isinstance(row, dict):
            raise ValueError(f"quota config: each tenant row must be a mapping, got {row!r}")
        norm = dict(row)
        if "mode" in norm:
            norm["mode"] = QuotaMode.parse(norm.get("mode"))
        parsed_tenants.append(norm)
    return QuotaConfig.model_validate({**raw, "tenants": parsed_tenants})


def save_quota_config(config: QuotaConfig, path: Path) -> None:
    """Write ``config`` back to ``path`` as YAML.

    Used by ``mdk tenants quota set``. Round-trips cleanly through
    :func:`load_quota_config`. Modes are written as the plain string
    (``warn`` / ``deny``) for human-friendliness — the lenient parser on read
    handles either form.
    """
    data: dict[str, Any] = {
        "tenants": [
            {
                "tenant_id": t.tenant_id,
                **(
                    {"daily_token_limit": t.daily_token_limit}
                    if t.daily_token_limit is not None
                    else {}
                ),
                **(
                    {"daily_request_limit": t.daily_request_limit}
                    if t.daily_request_limit is not None
                    else {}
                ),
                **(
                    {"monthly_cost_usd_limit": t.monthly_cost_usd_limit}
                    if t.monthly_cost_usd_limit is not None
                    else {}
                ),
                "mode": str(t.mode.value),
            }
            for t in config.tenants
        ],
    }
    if config.admin_tenant_ids:
        data["admin_tenant_ids"] = list(config.admin_tenant_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, sort_keys=False)


def upsert_tenant_quota(config: QuotaConfig, quota: TenantQuota) -> QuotaConfig:
    """Return a new :class:`QuotaConfig` with ``quota`` inserted / replaced.

    Pure: doesn't mutate ``config`` (pydantic models are mutable but we never
    rely on that). Existing rows for the same ``tenant_id`` are replaced;
    new rows are appended. ``admin_tenant_ids`` are carried over unchanged.
    """
    new_rows = [t for t in config.tenants if t.tenant_id != quota.tenant_id]
    new_rows.append(quota)
    return QuotaConfig(tenants=new_rows, admin_tenant_ids=list(config.admin_tenant_ids))


__all__ = [
    "DEFAULT_QUOTA_CONFIG_NAME",
    "QUOTA_CONFIG_ENV",
    "QuotaConfig",
    "QuotaDecision",
    "QuotaMode",
    "QuotaModeStr",
    "RouteClass",
    "TenantQuota",
    "check_quota",
    "load_quota_config",
    "resolve_config_path",
    "save_quota_config",
    "upsert_tenant_quota",
]
