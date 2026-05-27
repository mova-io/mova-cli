"""ADR 025 PR3 — conversational ``mdk dev`` copilot tests (S1, D6).

Hermetic + offline: the whole copilot is driven by the deterministic
:class:`MockPlanner` (no API keys, no network) over tmp_path projects, exactly
how CI runs it. We assert the contract the brief calls out:

* a scripted NL intent → the mock planner picks the right catalog action → the
  driver applies it → verify passes (e.g. "add a context named X" → add-context
  → context attached + validate green);
* an ambiguous intent → the planner returns a single clarifying question and
  **nothing** is mutated (D6);
* a destructive / networked / cost action proposed → confirmation is required
  (it is NOT auto-applied);
* the menu wiring — the new ``a`` key routes to the copilot path, and the
  planner's prompt is generated from the catalog + grounded in the project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import movate.cli.dev_cmd as dc
from movate.authoring import (
    AuthoringContext,
    AuthoringDriver,
    ConfirmationRequiredError,
    MockPlanner,
    PlannerOutcome,
    ProposedAction,
)
from movate.authoring.planner import (
    PlannerError,
    build_messages,
    build_static_prefix,
    parse_planner_response,
    project_state_summary,
)
from movate.testing import scaffold_agent

# ---------------------------------------------------------------------------
# A tiny loadable project (mirrors test_authoring_catalog conventions).
# ---------------------------------------------------------------------------

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

_PROMPT = "You are a greeter. Reply with a greeting.\n"
_DATASET = '{"input": {"text": "hi"}, "expected": {"message": "hello"}}\n'


def _make_project(root: Path, *, agent: str = "greeter") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("agents_dir: ./agents\n")
    agent_dir = root / "agents" / agent
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML.replace("name: greeter", f"name: {agent}"))
    (agent_dir / "prompt.md").write_text(_PROMPT)
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text(_DATASET)
    return root


def _driver(root: Path) -> AuthoringDriver:
    return AuthoringDriver(AuthoringContext(project=root))


def _snapshot_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".mdk" not in p.parts and ".movate" not in p.parts:
            out[str(p.relative_to(root))] = p.read_text(encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# D6 — system prompt is generated from the catalog (no hand-maintained list)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_static_prefix_is_generated_from_catalog() -> None:
    """The cacheable prefix embeds every catalog action's self-description."""
    prefix = build_static_prefix()
    # The catalog manifest is embedded verbatim → every shipped action name
    # appears, so the prompt can never drift from the registry (D6).
    for name in ("add-context", "edit-instructions", "ingest-kb", "add-skill", "set-model"):
        assert name in prefix
    assert "needs_clarification" in prefix  # the clarify protocol is documented


@pytest.mark.unit
def test_messages_put_state_and_request_after_cacheable_prefix() -> None:
    """Static catalog/instructions first (cacheable, #109); per-turn state last."""
    msgs = build_messages("add a context", project_state={"agent": "greeter"})
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == build_static_prefix()  # identical prefix → cacheable
    assert msgs[1]["role"] == "user"
    assert "greeter" in msgs[1]["content"]  # grounded in THIS project
    assert "add a context" in msgs[1]["content"]


@pytest.mark.unit
def test_project_state_summary_is_grounded(tmp_path: Path) -> None:
    """The grounding snapshot reflects the actual project tree (D6)."""
    root = _make_project(tmp_path / "proj")
    # Attach a context on disk so the summary surfaces it.
    _driver(root).apply("add-context", {"agent": "greeter", "name": "tone"}, fast_mode=True)
    state = project_state_summary(root, agent="greeter")
    assert state["agent"] == "greeter"
    assert state["model"] == "openai/gpt-4o-mini-2024-07-18"
    assert "tone" in state["context_files"]
    assert "greeter" in state["project_agents"]


# ---------------------------------------------------------------------------
# D6 — response parsing (actions / clarification / errors)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_actions_response() -> None:
    raw = json.dumps({"actions": [{"name": "add-context", "args": {"agent": "g", "name": "x"}}]})
    outcome = parse_planner_response(raw)
    assert not outcome.is_clarification
    assert outcome.actions[0].name == "add-context"
    assert outcome.actions[0].args == {"agent": "g", "name": "x"}


@pytest.mark.unit
def test_parse_clarification_response() -> None:
    outcome = parse_planner_response('{"needs_clarification": "which agent?"}')
    assert outcome.is_clarification
    assert outcome.needs_clarification == "which agent?"


@pytest.mark.unit
def test_parse_strips_code_fence() -> None:
    raw = '```json\n{"needs_clarification": "huh?"}\n```'
    assert parse_planner_response(raw).needs_clarification == "huh?"


@pytest.mark.unit
def test_parse_rejects_unknown_action() -> None:
    with pytest.raises(PlannerError):
        parse_planner_response('{"actions": [{"name": "rm-rf-everything", "args": {}}]}')


@pytest.mark.unit
def test_parse_rejects_non_json() -> None:
    with pytest.raises(PlannerError):
        parse_planner_response("sorry, I can't do that")


# ---------------------------------------------------------------------------
# Brief scenario 1 — NL intent → mock planner → right action → apply → verify
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mock_planner_maps_add_context_and_driver_applies(tmp_path: Path) -> None:
    """'add a context named X' → add-context → attached + validate green."""
    root = _make_project(tmp_path / "proj")
    planner = MockPlanner()

    outcome = planner.plan('add a context named "returns-policy"', agent="greeter")
    assert not outcome.is_clarification
    assert len(outcome.actions) == 1
    chosen = outcome.actions[0]
    assert chosen.name == "add-context"
    assert chosen.args["agent"] == "greeter"
    assert chosen.args["name"] == "returns-policy"

    # Drive the chosen action through the existing catalog driver.
    applied = _driver(root).apply(chosen.name, chosen.args, fast_mode=True)
    # Context file written + wired into agent.yaml (the shipped primitive).
    assert (root / "agents" / "greeter" / "contexts" / "returns-policy.md").is_file()
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert "returns-policy" in data["contexts"]
    # Verify ran and passed (validate + mock-run).
    assert applied.verify is not None
    assert applied.verify.ok
    assert applied.verify.validated


@pytest.mark.unit
def test_mock_planner_maps_edit_instructions(tmp_path: Path) -> None:
    """'make the tone formal' → edit-instructions → prompt.md rewritten."""
    root = _make_project(tmp_path / "proj")
    outcome = MockPlanner().plan("make the tone more formal", agent="greeter")
    chosen = outcome.actions[0]
    assert chosen.name == "edit-instructions"
    _driver(root).apply(chosen.name, chosen.args, fast_mode=True)
    body = (root / "agents" / "greeter" / "prompt.md").read_text()
    assert "formal" in body.lower()


@pytest.mark.unit
def test_mock_planner_maps_add_skill(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    outcome = MockPlanner().plan("add a calculator skill", agent="greeter")
    chosen = outcome.actions[0]
    assert chosen.name == "add-skill"
    _driver(root).apply(chosen.name, chosen.args, fast_mode=True)
    assert (root / "skills" / chosen.args["name"] / "skill.yaml").is_file()


# ---------------------------------------------------------------------------
# Brief scenario 2 — ambiguous intent → clarifying question, NO mutation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ambiguous_intent_returns_clarification_and_does_not_mutate(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    outcome = MockPlanner().plan("do the thing with the stuff", agent="greeter")
    assert outcome.is_clarification
    assert outcome.needs_clarification  # a non-empty question
    assert outcome.actions == []
    # A clarifying question never touches the project.
    assert _snapshot_tree(root) == before


# ---------------------------------------------------------------------------
# Brief scenario 3 — destructive/networked/cost action requires confirmation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_networked_action_requires_confirmation(tmp_path: Path) -> None:
    """'ingest …' → ingest-kb (networked + cost) → not auto-applied (D2)."""
    root = _make_project(tmp_path / "proj")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("some knowledge " * 20)

    outcome = MockPlanner().plan(f"ingest {docs / 'a.txt'}", agent="greeter")
    chosen = outcome.actions[0]
    assert chosen.name == "ingest-kb"

    # The plan declares networked + cost → confirmation required.
    plan = _driver(root).plan(chosen.name, chosen.args)
    assert plan.requires_confirmation is True

    # And the driver refuses to apply it without an explicit yes — even fast_mode
    # must not bypass a confirmation-gated action.
    with pytest.raises(ConfirmationRequiredError):
        _driver(root).apply(chosen.name, chosen.args, fast_mode=True)


@pytest.mark.unit
def test_set_model_proposal_requires_confirmation(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    outcome = MockPlanner().plan("set the model to anthropic/claude-sonnet-4-6", agent="greeter")
    chosen = outcome.actions[0]
    assert chosen.name == "set-model"
    assert chosen.args["provider"] == "anthropic/claude-sonnet-4-6"
    plan = _driver(root).plan(chosen.name, chosen.args)
    assert plan.requires_confirmation is True
    with pytest.raises(ConfirmationRequiredError):
        _driver(root).apply(chosen.name, chosen.args, fast_mode=True)


# ---------------------------------------------------------------------------
# Scripted planner — drive an exact intent→outcome sequence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scripted_mock_planner_returns_in_order(tmp_path: Path) -> None:
    planner = MockPlanner(
        script=[
            PlannerOutcome(needs_clarification="which one?"),
            PlannerOutcome(
                actions=[ProposedAction(name="add-eval-case", args={"agent": "greeter"})]
            ),
        ]
    )
    first = planner.plan("anything", agent="greeter")
    assert first.is_clarification
    second = planner.plan("anything", agent="greeter")
    assert second.actions[0].name == "add-eval-case"


# ---------------------------------------------------------------------------
# Menu wiring — the `a` key routes to the copilot path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_actions_menu_maps_a_to_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking 'a' in the menu resolves to the 'ask' (copilot) action."""
    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: "a"))
    assert dc._actions_menu() == "ask"


def _scaffold_in_project(tmp_path: Path, *, name: str = "demo") -> Path:
    """Scaffold an agent in the canonical ``<project>/agents/<name>`` layout.

    Mirrors how a real ``mdk dev`` session resolves an agent (the authoring
    driver resolves ``<project>/agents/<name>``), so the copilot's project-root
    derivation finds the right tree.
    """
    project = tmp_path / "proj"
    (project / "agents").mkdir(parents=True)
    (project / "movate.yaml").write_text("agents_dir: ./agents\n")
    return scaffold_agent(project / "agents" / name, name=name)


@pytest.mark.unit
def test_copilot_action_drives_planner_then_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the CLI handler: NL → MockPlanner → driver.apply.

    Exercises the real ``_copilot_action`` glue: a request is read from the
    prompt, the (mock) planner maps it, the plan is previewed, the user
    confirms, and the catalog driver applies it — proving the menu action and
    the planner+driver compose.
    """
    agent_dir = _scaffold_in_project(tmp_path)

    # First Prompt.ask → the NL request; subsequent confirms say yes.
    monkeypatch.setattr(
        dc.Prompt, "ask", staticmethod(lambda *a, **k: 'add a context named "tone"')
    )
    monkeypatch.setattr(dc.typer, "confirm", lambda *a, **k: True)

    dc._copilot_action(agent_dir, mock=True)

    ctx_file = agent_dir / "contexts" / "tone.md"
    assert ctx_file.is_file()
    data = yaml.safe_load((agent_dir / "agent.yaml").read_text())
    assert "tone" in (data.get("contexts") or [])


@pytest.mark.unit
def test_copilot_action_clarification_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous request through the handler asks a question, changes nothing."""
    agent_dir = _scaffold_in_project(tmp_path)
    project = agent_dir.parent.parent
    before = _snapshot_tree(project)

    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: "do the thing"))
    # confirm must never be reached; make it explode if it is.
    monkeypatch.setattr(
        dc.typer,
        "confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not confirm")),
    )

    dc._copilot_action(agent_dir, mock=True)
    assert _snapshot_tree(project) == before


@pytest.mark.unit
def test_copilot_action_skips_when_user_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User declines the confirm → the action is NOT applied (project unchanged)."""
    agent_dir = _scaffold_in_project(tmp_path)
    project = agent_dir.parent.parent
    before = _snapshot_tree(project)

    monkeypatch.setattr(
        dc.Prompt, "ask", staticmethod(lambda *a, **k: 'add a context named "tone"')
    )
    monkeypatch.setattr(dc.typer, "confirm", lambda *a, **k: False)

    dc._copilot_action(agent_dir, mock=True)
    assert _snapshot_tree(project) == before
    assert not (agent_dir / "contexts" / "tone.md").is_file()


@pytest.mark.unit
def test_build_planner_mock_returns_mock_planner(tmp_path: Path) -> None:
    """--mock wires the deterministic MockPlanner (no keys, hermetic)."""
    agent_dir = _scaffold_in_project(tmp_path)
    planner = dc._build_planner(agent_dir, agent_dir.parent.parent, mock=True)
    assert isinstance(planner, MockPlanner)


@pytest.mark.unit
def test_project_root_for_canonical_agent(tmp_path: Path) -> None:
    """An agent under ``agents/`` resolves the project root two levels up."""
    agent_dir = _scaffold_in_project(tmp_path)
    assert dc._project_root_for_agent(agent_dir) == (tmp_path / "proj").resolve()
