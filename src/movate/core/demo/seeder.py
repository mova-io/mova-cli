"""Synthetic telemetry generator for the demo "wow pack".

Generates a believable fleet's worth of :class:`RunRecord`, :class:`EvalRecord`
and :class:`FailureRecord` rows so every in-repo dashboard lights up with a
story — varied cost/latency/tokens/status across agents, tenants and time, a
pass-rate that trends, one agent drifting, and two *injected anomalies* (a cost
spike on one agent, a latency regression correlated with a deploy) so the
anomaly-annotation + narrative panels have something to point at.

Design constraints (CLAUDE.md):

* **Pure + deterministic.** No storage, no async, no clock-now surprises that
  matter — the RNG is seeded (``SeedConfig.seed``) and the time window is
  derived from an explicit ``now`` so a demo reproduces byte-for-byte. The CLI
  layer (:mod:`movate.cli.demo_cmd`) owns persistence + the prod guard.
* **Tagged + purgeable.** See the module docstring of :mod:`movate.core.demo`:
  every row's ``tenant_id`` starts with :data:`DEMO_TENANT_PREFIX` and every
  run/eval input carries :data:`DEMO_MARKER_KEY` ``= True``. Nothing here can
  produce an untagged row.
* **Stdlib only.** ``random`` + ``datetime``; no new deps.

The numbers are illustrative, not modelled — the goal is "looks like a real
fleet under load" at a glance, not a faithful simulation.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from movate.core.models import (
    ErrorInfo,
    EvalRecord,
    FailureRecord,
    JobStatus,
    JudgeMethod,
    Metrics,
    RunRecord,
    TokenUsage,
)

# ---------------------------------------------------------------------------
# Safety markers — the single source of truth for "this row is synthetic".
# `mdk demo clear` keys off DEMO_TENANT_PREFIX; the input sentinel is a
# belt-and-braces second signal an operator can grep for in a row dump.
# ---------------------------------------------------------------------------

DEMO_TENANT_PREFIX = "demo-"
"""Every seeded ``tenant_id`` starts with this. Refuse to clear anything that
doesn't, and refuse to seed a tenant an operator names without it (the CLI
prepends it). This is the primary purge key."""

DEMO_MARKER_KEY = "__mdk_demo__"
"""Sentinel key set to ``True`` inside every seeded run/eval ``input`` dict.
A secondary, row-level marker so a human eyeballing a single record (not just
its tenant) can tell it's synthetic. ``RunRecord`` forbids extra *top-level*
fields, but ``input`` is ``dict[str, Any]`` — a legitimate place for it."""


def is_demo_tenant(tenant_id: str) -> bool:
    """True iff ``tenant_id`` is one this seeder would (or did) create."""
    return tenant_id.startswith(DEMO_TENANT_PREFIX)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedConfig:
    """Knobs for :func:`generate_bundle`. Mirrors the ``mdk demo seed`` flags.

    ``now`` is injected (not read from the clock inside the generator) so the
    output is fully reproducible for a given ``(seed, now)`` — demos can be
    re-run and screenshots will match. The CLI defaults it to ``datetime.now``.
    """

    agents: int = 6
    tenants: int = 3
    days: int = 30
    seed: int = 1337
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Roughly how many runs to generate per agent per day at the fleet's
    # baseline. The actual count wobbles with a day/agent multiplier so the
    # charts aren't flat. Kept modest * agents * tenants * days so the default
    # 6/3/30 lands in the low-thousands — plenty for nice charts, cheap to
    # batch-insert.
    runs_per_agent_per_day: int = 4


# --- generation tunables (named so the shape is legible + lint-clean) ---
_WEEKEND_FIRST_DAY = 5  # Mon=0 .. Sat=5, Sun=6 — weekend dip below.
_WEEKEND_VOLUME_MULT = 0.55
_MODEL_SPILLOVER_PROB = 0.15  # chance a run uses a non-home model.
_TIMEOUT_FAILURE_PROB = 0.6  # split between the two synthetic error types.

# Illustrative provider/model mix. Cost-per-1k-token deltas are what make the
# "effective $/1k tokens" panel interesting, so spread them.
_PROVIDERS: tuple[tuple[str, float], ...] = (
    # (provider/model, usd per 1k total tokens — rough blended rate)
    ("openai/gpt-4o-mini-2024-07-18", 0.0004),
    ("openai/gpt-4o-2024-08-06", 0.0075),
    ("anthropic/claude-3-5-sonnet-20241022", 0.0090),
    ("anthropic/claude-3-5-haiku-20241022", 0.0010),
    ("azure/gpt-4o", 0.0080),
)

# Human-ish agent names so the fleet reads like a real customer's, not agent_0.
_AGENT_NAMES: tuple[str, ...] = (
    "support-triage",
    "billing-assistant",
    "kb-search",
    "ticket-summarizer",
    "onboarding-guide",
    "sentiment-router",
    "contract-analyzer",
    "escalation-classifier",
)

# Tenant slugs. The CLI prepends DEMO_TENANT_PREFIX; these are the human part.
_TENANT_SLUGS: tuple[str, ...] = (
    "acme",
    "globex",
    "initech",
    "umbrella",
    "stark",
    "wayne",
)


@dataclass
class DemoEvent:
    """A point-in-time fleet event for the dashboard annotation feeds.

    Not persisted as a storage record (there is no event table on the
    ``StorageProvider`` Protocol today — see the scaffold note in the exec
    dashboard). Returned in the bundle so the CLI can print the demo's
    storyline and so a future event sink (ADR 047 insights store) has a shaped
    payload to ingest. ``kind`` is one of: ``deploy`` / ``drift_detected`` /
    ``canary_promotion`` / ``cost_anomaly`` / ``latency_anomaly``.
    """

    at: datetime
    kind: str
    agent: str
    tenant_id: str
    detail: str
    version: str = ""


@dataclass
class DemoBundle:
    """Everything :func:`generate_bundle` produces, ready for batch insert.

    The CLI persists ``runs`` + ``evals`` + ``failures`` through the storage
    Protocol and uses ``events`` + ``narrative`` for the demo's printed
    storyline. ``stats`` is a small summary for the CLI's success panel.
    """

    runs: list[RunRecord]
    evals: list[EvalRecord]
    failures: list[FailureRecord]
    events: list[DemoEvent]
    narrative: str
    stats: dict[str, float | int]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _stable_hash(*parts: str) -> str:
    """Short deterministic hex digest — for prompt/dataset hashes."""
    h = hashlib.sha256("\x00".join(parts).encode()).hexdigest()
    return h[:16]


def _rng_id(rng: random.Random) -> str:
    """A 32-hex id drawn from the seeded RNG (not uuid4).

    Using the seeded stream keeps the WHOLE bundle reproducible for a given
    ``(seed, now)`` — including run/eval/failure ids — so a demo re-run matches
    earlier screenshots exactly. ``getrandbits`` is plenty for synthetic ids;
    collision risk across one bundle is negligible.
    """
    return f"{rng.getrandbits(128):032x}"


def _demo_input(rng: random.Random, agent: str, idx: int) -> dict[str, object]:
    """A plausible run input dict carrying the demo marker.

    The marker key is set unconditionally — this is the row-level safety
    signal. The rest is cosmetic so a row dump reads like real traffic.
    """
    questions = (
        "How do I reset my password?",
        "Why was I charged twice this month?",
        "Can you summarize ticket #4821?",
        "What's the status of my refund?",
        "How do I upgrade my plan?",
    )
    return {
        DEMO_MARKER_KEY: True,
        "question": rng.choice(questions),
        "demo_seq": idx,
    }


def _pick_status(rng: random.Random, base_error_rate: float) -> JobStatus:
    """Mostly success, with a believable smear of error/safety/dead-letter."""
    roll = rng.random()
    if roll < base_error_rate * 0.7:
        return JobStatus.ERROR
    if roll < base_error_rate * 0.85:
        return JobStatus.SAFETY_BLOCKED
    if roll < base_error_rate:
        return JobStatus.DEAD_LETTER
    return JobStatus.SUCCESS


def _gen_runs(
    cfg: SeedConfig,
    rng: random.Random,
    agents: list[str],
    tenants: list[str],
    *,
    cost_spike_agent: str,
    latency_regression_agent: str,
    deploy_at: datetime,
) -> tuple[list[RunRecord], list[DemoEvent]]:
    """Generate the RunRecord fleet + the two injected anomaly events.

    Anomalies:

    * **Cost spike** — ``cost_spike_agent`` gets bumped onto the most
      expensive model for one day mid-window, ~4x its normal $/run, so the
      cost dashboard + the exec "spend forecast" show a believable jump.
    * **Latency regression** — ``latency_regression_agent``'s latency ~doubles
      *after* ``deploy_at`` (a CalVer deploy event is emitted at that instant),
      so the golden-signal latency chart's deploy annotation lines up with a
      step change — the classic "the deploy did it" demo moment.
    """
    runs: list[RunRecord] = []
    events: list[DemoEvent] = []
    window_start = cfg.now - timedelta(days=cfg.days)

    # One mid-window day for the cost spike.
    cost_spike_day = window_start + timedelta(days=cfg.days // 2)

    for agent in agents:
        # Each agent has a "home" provider so the model-mix panel is stable,
        # with occasional spillover to a second model.
        home_provider, home_rate = _PROVIDERS[hash(agent) % len(_PROVIDERS)]
        spike_provider, spike_rate = _PROVIDERS[-3]  # an expensive one
        agent_version = f"2026.{rng.randint(3, 5)}.{rng.randint(1, 28)}.{rng.randint(1, 3)}"
        # Per-agent baselines so the fleet isn't uniform.
        base_latency = rng.randint(450, 1400)
        base_error_rate = rng.uniform(0.005, 0.04)

        for day in range(cfg.days):
            day_start = window_start + timedelta(days=day)
            # Weekday/weekend wobble + a gentle upward adoption trend.
            weekday = day_start.weekday()
            volume_mult = (_WEEKEND_VOLUME_MULT if weekday >= _WEEKEND_FIRST_DAY else 1.0) * (
                1.0 + day / (cfg.days * 2.5)
            )
            n_runs = max(1, int(cfg.runs_per_agent_per_day * volume_mult * rng.uniform(0.7, 1.3)))

            is_cost_spike = agent == cost_spike_agent and day_start.date() == cost_spike_day.date()

            for i in range(n_runs):
                # Spread runs across the day.
                created = day_start + timedelta(
                    hours=rng.uniform(0, 23), minutes=rng.uniform(0, 59)
                )
                tenant = rng.choice(tenants)

                # Provider selection + the cost-spike injection.
                if is_cost_spike:
                    provider, rate = spike_provider, spike_rate
                elif rng.random() < _MODEL_SPILLOVER_PROB:
                    provider, rate = _PROVIDERS[rng.randrange(len(_PROVIDERS))]
                else:
                    provider, rate = home_provider, home_rate

                # Latency: baseline + jitter, doubled post-deploy for the
                # regression agent.
                latency = base_latency * rng.uniform(0.7, 1.6)
                if agent == latency_regression_agent and created >= deploy_at:
                    latency *= 2.1
                latency_ms = int(latency)

                # Tokens + cost. Cost spike both swaps the model AND fattens
                # the prompts a touch, so $/run jumps ~4x.
                in_tok = int(rng.uniform(400, 2200) * (1.5 if is_cost_spike else 1.0))
                out_tok = int(rng.uniform(80, 700) * (1.4 if is_cost_spike else 1.0))
                total_tok = in_tok + out_tok
                cost = (total_tok / 1000.0) * rate

                status = _pick_status(rng, base_error_rate)
                error = None
                if status in (JobStatus.ERROR, JobStatus.DEAD_LETTER):
                    error = ErrorInfo(
                        type="provider_timeout"
                        if rng.random() < _TIMEOUT_FAILURE_PROB
                        else "schema_validation",
                        message="(demo) synthetic failure for dashboard population",
                        retryable=status == JobStatus.DEAD_LETTER,
                    )

                runs.append(
                    RunRecord(
                        run_id=_rng_id(rng),
                        job_id=_rng_id(rng),
                        tenant_id=tenant,
                        agent=agent,
                        agent_version=agent_version,
                        prompt_hash=_stable_hash(agent, agent_version),
                        provider=provider,
                        provider_version="demo",
                        pricing_version="demo-2026.05",
                        status=status,
                        input=_demo_input(rng, agent, i),
                        output=None
                        if status != JobStatus.SUCCESS
                        else {DEMO_MARKER_KEY: True, "answer": "(demo) synthetic output"},
                        metrics=Metrics(
                            latency_ms=latency_ms,
                            tokens=TokenUsage(input=in_tok, output=out_tok),
                            cost_usd=round(cost, 6),
                            provider=provider,
                            pricing_version="demo-2026.05",
                        ),
                        error=error,
                        created_at=created,
                    )
                )

            if is_cost_spike:
                events.append(
                    DemoEvent(
                        at=day_start + timedelta(hours=10),
                        kind="cost_anomaly",
                        agent=agent,
                        tenant_id=tenants[0],
                        detail=(
                            f"{agent} cost ~4x baseline — model swapped to "
                            f"{spike_provider} and prompts grew"
                        ),
                    )
                )

    # The deploy + the latency anomaly it caused.
    events.append(
        DemoEvent(
            at=deploy_at,
            kind="deploy",
            agent=latency_regression_agent,
            tenant_id=tenants[0],
            detail=f"Deployed {latency_regression_agent}",
            version=f"2026.{deploy_at.month}.{deploy_at.day}.1",
        )
    )
    events.append(
        DemoEvent(
            at=deploy_at + timedelta(minutes=20),
            kind="latency_anomaly",
            agent=latency_regression_agent,
            tenant_id=tenants[0],
            detail=(
                f"{latency_regression_agent} p95 latency ~2x after deploy — "
                "traced to a slower rerank stage"
            ),
        )
    )
    return runs, events


def _gen_evals(
    cfg: SeedConfig,
    rng: random.Random,
    agents: list[str],
    tenants: list[str],
    *,
    drift_agent: str,
) -> tuple[list[EvalRecord], list[DemoEvent]]:
    """Generate EvalRecords with a fleet-wide improving pass-rate, except
    ``drift_agent`` which degrades over the window (the "one agent drifting"
    story) and emits a ``drift_detected`` event near the end.

    Evals are daily per agent (one dataset run/day) — far fewer rows than runs,
    which matches a real continuous-eval cadence (ADR 016 D2).
    """
    evals: list[EvalRecord] = []
    events: list[DemoEvent] = []
    window_start = cfg.now - timedelta(days=cfg.days)

    for agent in agents:
        agent_version = f"2026.{rng.randint(3, 5)}.{rng.randint(1, 28)}.{rng.randint(1, 3)}"
        # Most agents trend up from ~0.82 toward ~0.95.
        start_pass = rng.uniform(0.80, 0.86)
        for day in range(cfg.days):
            created = window_start + timedelta(days=day, hours=2)
            progress = day / max(cfg.days - 1, 1)
            if agent == drift_agent:
                # Degrade: 0.93 -> ~0.66 with noise; the headline drift story.
                pass_rate = 0.93 - 0.27 * progress + rng.uniform(-0.02, 0.02)
            else:
                pass_rate = start_pass + 0.12 * progress + rng.uniform(-0.03, 0.03)
            pass_rate = max(0.4, min(0.99, pass_rate))
            mean_score = max(0.4, min(0.99, pass_rate + rng.uniform(-0.05, 0.05)))
            sample_count = rng.randint(20, 40)

            evals.append(
                EvalRecord(
                    eval_id=_rng_id(rng),
                    tenant_id=tenants[0],  # evals run against the canonical tenant
                    agent=agent,
                    agent_version=agent_version,
                    dataset_hash=_stable_hash(DEMO_MARKER_KEY, agent, "dataset"),
                    judge_method=JudgeMethod.LLM_JUDGE,
                    judge_provider="openai/gpt-4o-2024-08-06",
                    runs_per_case=3,
                    gate_mode="mean",
                    threshold=0.70,
                    mean_score=round(mean_score, 4),
                    pass_rate=round(pass_rate, 4),
                    sample_count=sample_count,
                    total_cost_usd=round(rng.uniform(0.05, 0.40), 4),
                    dimension_means={
                        "faithfulness": round(min(0.99, mean_score + 0.02), 4),
                        "coverage": round(max(0.4, mean_score - 0.04), 4),
                        "safety": round(min(0.99, 0.95 + rng.uniform(-0.02, 0.03)), 4),
                    },
                    created_at=created,
                )
            )

    # Drift detection event ~3 days before the window end.
    events.append(
        DemoEvent(
            at=cfg.now - timedelta(days=3),
            kind="drift_detected",
            agent=drift_agent,
            tenant_id=tenants[0],
            detail=(
                f"{drift_agent} eval pass-rate fell below the 0.70 gate — "
                "quality regression across 3 consecutive runs"
            ),
        )
    )
    return evals, events


def _gen_failures(runs: list[RunRecord]) -> list[FailureRecord]:
    """One FailureRecord per failed run so the failures view isn't empty.

    Mirrors the runtime's behavior of persisting a FailureRecord alongside a
    failed run. Tenant + agent are copied off the run, so these inherit the
    same demo tagging.
    """
    failures: list[FailureRecord] = []
    for run in runs:
        if run.status in (JobStatus.ERROR, JobStatus.DEAD_LETTER) and run.error is not None:
            failures.append(
                FailureRecord(
                    failure_id=_stable_hash("failure", run.run_id),
                    run_id=run.run_id,
                    tenant_id=run.tenant_id,
                    agent=run.agent,
                    failure_type=run.error.type,
                    message=run.error.message,
                    retryable=run.error.retryable,
                    created_at=run.created_at,
                )
            )
    return failures


def generate_bundle(cfg: SeedConfig) -> DemoBundle:
    """Generate a full demo telemetry bundle for ``cfg``.

    Deterministic for a given ``(cfg.seed, cfg.now)``. The returned records are
    all demo-tagged (tenant prefix + input marker) and ready to batch-insert
    through the storage Protocol. See :class:`DemoBundle`.
    """
    rng = random.Random(cfg.seed)

    n_agents = max(1, min(cfg.agents, len(_AGENT_NAMES)))
    n_tenants = max(1, min(cfg.tenants, len(_TENANT_SLUGS)))
    agents = list(_AGENT_NAMES[:n_agents])
    tenants = [f"{DEMO_TENANT_PREFIX}{slug}" for slug in _TENANT_SLUGS[:n_tenants]]

    # Pick the protagonists of the two anomalies + the drift deterministically.
    cost_spike_agent = agents[rng.randrange(len(agents))]
    latency_regression_agent = agents[rng.randrange(len(agents))]
    drift_agent = agents[rng.randrange(len(agents))]
    # A deploy ~1/3 into the window (so there's "before" and "after").
    window_start = cfg.now - timedelta(days=cfg.days)
    deploy_at = window_start + timedelta(days=max(1, cfg.days // 3), hours=9)

    runs, run_events = _gen_runs(
        cfg,
        rng,
        agents,
        tenants,
        cost_spike_agent=cost_spike_agent,
        latency_regression_agent=latency_regression_agent,
        deploy_at=deploy_at,
    )
    evals, eval_events = _gen_evals(cfg, rng, agents, tenants, drift_agent=drift_agent)
    failures = _gen_failures(runs)

    # A couple of "good news" canary promotions so the exec "wins" panel has
    # something positive to show.
    canary_events = [
        DemoEvent(
            at=cfg.now - timedelta(days=5, hours=4),
            kind="canary_promotion",
            agent=agents[0],
            tenant_id=tenants[0],
            detail=f"{agents[0]} challenger promoted to champion — +6pt pass-rate",
            version=f"2026.{cfg.now.month}.{max(1, cfg.now.day - 5)}.1",
        ),
        DemoEvent(
            at=cfg.now - timedelta(days=1, hours=2),
            kind="canary_promotion",
            agent=agents[min(1, len(agents) - 1)],
            tenant_id=tenants[0],
            detail=f"{agents[min(1, len(agents) - 1)]} canary at 50% — error rate flat, healthy",
        ),
    ]

    events = sorted([*run_events, *eval_events, *canary_events], key=lambda e: e.at)

    total_cost = sum(r.metrics.cost_usd for r in runs)
    total_tokens = sum(r.metrics.tokens.input + r.metrics.tokens.output for r in runs)
    n_success = sum(1 for r in runs if r.status == JobStatus.SUCCESS)
    stats: dict[str, float | int] = {
        "runs": len(runs),
        "evals": len(evals),
        "failures": len(failures),
        "events": len(events),
        "tenants": len(tenants),
        "agents": len(agents),
        "total_cost_usd": round(total_cost, 2),
        "total_tokens": total_tokens,
        "success_rate_pct": round(100.0 * n_success / max(1, len(runs)), 1),
    }

    narrative = (
        f"Seeded {len(runs):,} runs + {len(evals):,} evals across "
        f"{len(agents)} agents x {len(tenants)} tenants over {cfg.days} days. "
        f"Storyline: cost spike on '{cost_spike_agent}' mid-window; latency "
        f"regression on '{latency_regression_agent}' after a deploy; eval "
        f"drift on '{drift_agent}'. Total synthetic spend ~"
        f"${stats['total_cost_usd']:,.2f}."
    )

    return DemoBundle(
        runs=runs,
        evals=evals,
        failures=failures,
        events=events,
        narrative=narrative,
        stats=stats,
    )
