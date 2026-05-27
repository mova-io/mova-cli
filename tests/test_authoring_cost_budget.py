"""D7e (#136) — copilot/autopilot cost budgeting tests.

Hermetic + offline: a tiny fake :class:`BaseLLMProvider` returns scripted
planner JSON + configurable token usage, so we drive the real
:class:`LLMPlanner` cost path (token usage → canonical pricing table → session
accumulator) with no keys / no network. We assert the brief's contract:

* a session with a low cap STOPS before exceeding it — the planner refuses the
  next call (raising before the model call), so no further LLM call happens and
  the message is clear;
* with no cap, behavior is unchanged (calls accumulate but are never refused);
* the autopilot stops cleanly (``budget_exceeded``) when the cap is hit, keeping
  whatever already applied (no half-applied action);
* budget config resolves via flag → ~/.movate/config.yaml → no-cap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from movate.authoring import (
    AuthoringContext,
    AuthoringDriver,
    Autopilot,
    BudgetExceededError,
    CostBudget,
    EvalSnapshot,
    FailingCase,
    MockEvalRunner,
    SessionCostTracker,
)
from movate.authoring.budget import cost_of_tokens
from movate.authoring.planner import LLMPlanner
from movate.core.models import TokenUsage
from movate.providers.base import CompletionRequest, CompletionResponse
from movate.providers.pricing import load_pricing

_MODEL = "openai/gpt-4o-mini-2024-07-18"

_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: greeter
version: 0.1.0
description: A test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
  params:
    temperature: 0.0
prompt: ./prompt.md
schema:
  input:
    text: string
  output:
    message: string
evals:
  dataset: ./evals/dataset.jsonl
"""

_PLAN_JSON = json.dumps(
    {"actions": [{"name": "add-context", "args": {"agent": "greeter", "name": "c"}}]}
)


def _make_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("agents_dir: ./agents\n")
    agent_dir = root / "agents" / "greeter"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML)
    (agent_dir / "prompt.md").write_text("You are a greeter.\n")
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "hello"}}\n'
    )
    return root


class _FakeProvider:
    """A scripted provider: returns fixed planner JSON + fixed token usage.

    Counts ``complete`` calls so a test can assert the budget gate refuses the
    NEXT call (no further provider hit) once the cap is spent.
    """

    name = "fake"
    version = "0"

    def __init__(self, *, tokens: TokenUsage) -> None:
        self._tokens = tokens
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        return CompletionResponse(text=_PLAN_JSON, tokens=self._tokens)


def _planner(root: Path, provider: _FakeProvider, tracker: SessionCostTracker) -> LLMPlanner:
    return LLMPlanner(
        provider,  # type: ignore[arg-type]
        project=root,
        model=_MODEL,
        tracker=tracker,
        pricing=load_pricing(),
    )


# A token count whose openai/gpt-4o-mini price is comfortably > $0.01 so a low
# cap is crossed by a single call (kept large to be robust to price tweaks).
_BIG_TOKENS = TokenUsage(input=2_000_000, output=2_000_000)


# ---------------------------------------------------------------------------
# cost_of_tokens — derives cost from the canonical pricing table (ADR 024)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cost_of_tokens_uses_pricing_table() -> None:
    pricing = load_pricing()
    cost = cost_of_tokens(pricing, provider=_MODEL, tokens=TokenUsage(input=1000, output=1000))
    assert cost > 0
    # Matches the canonical surface exactly (no separate code path).
    assert cost == pricing.cost_for(provider=_MODEL, tokens=TokenUsage(input=1000, output=1000))


@pytest.mark.unit
def test_cost_of_tokens_unknown_model_is_zero_not_fatal() -> None:
    """An unpriced model degrades to 0.0 — it never crashes the copilot."""
    assert cost_of_tokens(load_pricing(), provider="no/such-model", tokens=_BIG_TOKENS) == 0.0


# ---------------------------------------------------------------------------
# SessionCostTracker — the accumulator + gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tracker_no_cap_never_refuses() -> None:
    t = SessionCostTracker()  # no cap
    t.record(100.0)
    t.check_before_call()  # must not raise
    assert t.total_usd == 100.0
    assert t.remaining_usd() is None
    assert not t.would_exceed()


@pytest.mark.unit
def test_tracker_refuses_next_call_once_cap_spent() -> None:
    t = SessionCostTracker(budget=CostBudget(cap_usd=0.10))
    t.check_before_call()  # fine — nothing spent
    t.record(0.10)  # now at the cap
    assert t.would_exceed()
    with pytest.raises(BudgetExceededError, match="budget exhausted"):
        t.check_before_call()


@pytest.mark.unit
def test_tracker_approaching_cap_is_one_shot() -> None:
    t = SessionCostTracker(budget=CostBudget(cap_usd=1.0, warn_fraction=0.8))
    t.record(0.85)
    assert t.approaching_cap()  # crossed 80%
    assert not t.approaching_cap()  # one-shot — already warned
    assert t.remaining_usd() == pytest.approx(0.15)


@pytest.mark.unit
def test_cost_budget_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="cap_usd"):
        CostBudget(cap_usd=-1.0)
    with pytest.raises(ValueError, match="warn_fraction"):
        CostBudget(warn_fraction=0.0)


# ---------------------------------------------------------------------------
# LLMPlanner — wires the gate before the call + records cost after
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_planner_records_call_cost(tmp_path: Path) -> None:
    """A planner call accumulates its derived cost into the session tracker."""
    root = _make_project(tmp_path / "proj")
    provider = _FakeProvider(tokens=TokenUsage(input=1000, output=1000))
    tracker = SessionCostTracker()  # no cap
    outcome = _planner(root, provider, tracker).plan("add a context", agent="greeter")

    assert outcome.actions[0].name == "add-context"
    assert provider.calls == 1
    assert tracker.calls == 1
    assert tracker.total_usd == pytest.approx(
        load_pricing().cost_for(provider=_MODEL, tokens=TokenUsage(input=1000, output=1000))
    )


@pytest.mark.unit
def test_planner_no_cap_is_unchanged(tmp_path: Path) -> None:
    """With no cap the planner never refuses — repeated calls all go through."""
    root = _make_project(tmp_path / "proj")
    provider = _FakeProvider(tokens=_BIG_TOKENS)
    tracker = SessionCostTracker()  # no cap
    planner = _planner(root, provider, tracker)
    for _ in range(3):
        planner.plan("add a context", agent="greeter")
    assert provider.calls == 3  # all three calls happened


@pytest.mark.unit
def test_planner_stops_before_exceeding_cap(tmp_path: Path) -> None:
    """A low cap: the FIRST call goes through, the next is refused BEFORE calling."""
    root = _make_project(tmp_path / "proj")
    provider = _FakeProvider(tokens=_BIG_TOKENS)  # one call blows a small cap
    tracker = SessionCostTracker(budget=CostBudget(cap_usd=0.01))
    planner = _planner(root, provider, tracker)

    # First call is allowed (budget not yet spent) and records a big cost.
    planner.plan("add a context", agent="greeter")
    assert provider.calls == 1
    assert tracker.would_exceed()

    # The NEXT call is refused before the provider is hit (no half-work).
    with pytest.raises(BudgetExceededError):
        planner.plan("add another context", agent="greeter")
    assert provider.calls == 1  # provider was NOT called again


# ---------------------------------------------------------------------------
# Autopilot — stops cleanly on budget, keeping what already applied
# ---------------------------------------------------------------------------


def _failing() -> EvalSnapshot:
    return EvalSnapshot(
        total_cases=2,
        passed_cases=0,
        mean_score=0.0,
        failures=[
            FailingCase(
                input={"text": "q"},
                expected={"message": "a"},
                actual={"message": "?"},
                score=0.0,
            )
        ],
    )


@pytest.mark.unit
def test_autopilot_stops_when_budget_exhausted(tmp_path: Path) -> None:
    """A spent budget makes the autopilot end cleanly — no crash, no half-apply."""
    root = _make_project(tmp_path / "proj")
    provider = _FakeProvider(tokens=_BIG_TOKENS)
    tracker = SessionCostTracker(budget=CostBudget(cap_usd=0.0001))
    # Pre-spend the budget so the autopilot's FIRST proposal call is refused.
    tracker.record(1.0)

    driver = AuthoringDriver(AuthoringContext(project=root))
    autopilot = Autopilot(
        eval_runner=MockEvalRunner([_failing(), _failing()]),
        planner=_planner(root, provider, tracker),
        driver=driver,
        max_iterations=3,
    )
    result = autopilot.run("greeter", fast_mode=True)

    assert result.budget_exceeded
    assert result.total_applied == 0
    assert provider.calls == 0  # the gate fired before any model call
    # Nothing was half-applied — the project has no audit-applied records.
    assert [r.outcome.value for r in driver.audit_records()] == []


# ---------------------------------------------------------------------------
# Budget config resolution: --budget flag → ~/.movate/config.yaml → no cap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_tracker_flag_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import movate.cli.dev_cmd as dc  # noqa: PLC0415

    # Even with a config value present, the explicit flag wins.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("copilot:\n  budget_usd: 5.0\n")
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
    tracker = dc._build_cost_tracker(0.25)
    assert tracker.budget.cap_usd == 0.25


@pytest.mark.unit
def test_build_tracker_falls_back_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import movate.cli.dev_cmd as dc  # noqa: PLC0415

    cfg = tmp_path / "config.yaml"
    cfg.write_text("copilot:\n  budget_usd: 0.75\n")
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
    tracker = dc._build_cost_tracker(None)
    assert tracker.budget.cap_usd == 0.75


@pytest.mark.unit
def test_build_tracker_no_cap_when_nothing_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import movate.cli.dev_cmd as dc  # noqa: PLC0415

    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    tracker = dc._build_cost_tracker(None)
    assert tracker.budget.cap_usd is None
    assert not tracker.would_exceed()


@pytest.mark.unit
def test_build_tracker_bad_config_degrades_to_no_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed user config must not crash dev — it degrades to no-cap."""
    import movate.cli.dev_cmd as dc  # noqa: PLC0415

    cfg = tmp_path / "config.yaml"
    cfg.write_text("copilot:\n  budget_usd: not-a-number\n")
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
    tracker = dc._build_cost_tracker(None)  # must not raise
    assert tracker.budget.cap_usd is None


@pytest.mark.unit
def test_cli_config_set_copilot_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk config set copilot.budget_usd` persists a numeric cap to the user config."""
    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app  # noqa: PLC0415
    from movate.core.user_config import load_user_config  # noqa: PLC0415

    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg))
    result = CliRunner().invoke(app, ["config", "set", "copilot.budget_usd", "0.50"])
    assert result.exit_code == 0
    assert load_user_config().copilot.budget_usd == 0.50


@pytest.mark.unit
def test_cli_config_set_copilot_budget_rejects_nonnumeric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app  # noqa: PLC0415

    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "config.yaml"))
    result = CliRunner().invoke(app, ["config", "set", "copilot.budget_usd", "lots"])
    assert result.exit_code == 2
