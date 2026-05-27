"""ADR 025 PR1 — authoring action catalog + plan→apply→verify→undo tests.

Coverage (hermetic; uses tmp_path projects + ``InMemoryStorage`` + the
deterministic mock-run path — no API keys, no ``~/.movate`` writes):

* **Catalog/registry** — every action is listed + self-describes (D1).
* **plan = no writes** — each action's ``plan`` produces a correct diff and
  touches nothing on disk (D2).
* **apply via primitive + passes validate** — ``apply`` mutates through the
  shipped primitive and the result loads cleanly (D2/D3).
* **verify reverts on validate failure** — an injected broken edit triggers a
  revert to the pre-apply checkpoint (D3/D4).
* **undo restores the prior checkpoint exactly** (D4).
* **cost/networked/destructive ⇒ requires_confirmation** (D2).

Representative action subset per the brief: add-context, edit-instructions,
ingest-kb, add-skill, set-retrieval, add-eval-case (+ set-model for the confirm
gate, + describe-agent for the rename gate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from movate.authoring import (
    AuthoringContext,
    AuthoringDriver,
    ConfirmationRequiredError,
    SideEffect,
    action_names,
    describe_catalog,
    get_action,
)
from movate.authoring.base import AuthoringActionError
from movate.authoring.catalog import UnknownActionError

# Minimal but loadable agent.yaml — string in → string out, no skills.
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
    """Build a tiny loadable project: movate.yaml + one agent under agents/."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("# test project\n")
    agent_dir = root / "agents" / agent
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML.replace("name: greeter", f"name: {agent}"))
    (agent_dir / "prompt.md").write_text(_PROMPT)
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text(_DATASET)
    return root


def _driver(root: Path) -> AuthoringDriver:
    return AuthoringDriver(AuthoringContext(project=root))


# ---------------------------------------------------------------------------
# D1 — catalog / registry
# ---------------------------------------------------------------------------


def test_catalog_lists_initial_actions() -> None:
    """Every action declared in ADR 025 D1 is registered."""
    names = set(action_names())
    expected = {
        "add-context",
        "edit-context",
        "remove-context",
        "edit-instructions",
        "set-model",
        "add-fallback",
        "set-retrieval",
        "describe-agent",
        "add-eval-case",
        "add-skill",
        "add-agent",
        "compose-workflow",
        "ingest-kb",
    }
    assert expected <= names, f"missing actions: {expected - names}"


def test_every_action_self_describes() -> None:
    """Each action exposes name + description + side effects + an arg schema."""
    for entry in describe_catalog():
        assert entry["name"]
        assert entry["description"]
        assert isinstance(entry["side_effects"], list)
        assert isinstance(entry["reversible"], bool)
        # The arg schema is a JSON Schema object with properties.
        assert entry["args_schema"]["type"] == "object"
        assert "properties" in entry["args_schema"]


def test_action_names_are_unique_and_sorted() -> None:
    names = action_names()
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_get_unknown_action_raises() -> None:
    with pytest.raises(UnknownActionError):
        get_action("no-such-action")


def test_describe_catalog_is_json_serializable() -> None:
    """The self-describing manifest (PR3/PR4 consume it) round-trips through JSON."""
    payload = describe_catalog()
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# D2 — plan produces a diff with NO writes
# ---------------------------------------------------------------------------


def _snapshot_tree(root: Path) -> dict[str, str]:
    """Map every file under ``root`` (excluding state dirs) to its content."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".mdk" not in p.parts and ".movate" not in p.parts:
            out[str(p.relative_to(root))] = p.read_text(encoding="utf-8")
    return out


def test_plan_add_context_no_writes(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    plan = _driver(root).plan(
        "add-context", {"agent": "greeter", "name": "tone", "body": "# Tone\nBe warm.\n"}
    )
    assert plan.diff  # a unified diff was produced
    assert "+# Tone" in plan.diff
    assert plan.requires_confirmation is False  # additive + reversible + free
    assert _snapshot_tree(root) == before  # nothing written


def test_plan_edit_instructions_diff(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    plan = _driver(root).plan(
        "edit-instructions", {"agent": "greeter", "body": "Totally new instructions.\n"}
    )
    assert "Totally new instructions." in plan.diff
    assert "greeter" in plan.summary
    assert _snapshot_tree(root) == before


def test_plan_set_retrieval_validates_and_diffs(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    plan = _driver(root).plan("set-retrieval", {"agent": "greeter", "auto_into": "context"})
    assert "auto_into" in plan.diff
    assert SideEffect.COST in plan.side_effects
    assert plan.requires_confirmation is True


def test_plan_add_eval_case_diff(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    plan = _driver(root).plan(
        "add-eval-case",
        {"agent": "greeter", "input": {"text": "yo"}, "expected": {"message": "hey"}},
    )
    assert '"yo"' in plan.diff
    assert plan.requires_confirmation is False  # additive
    assert _snapshot_tree(root) == before


# ---------------------------------------------------------------------------
# D2/D3 — apply mutates via the primitive + the result passes validate
# ---------------------------------------------------------------------------


def test_apply_add_context_mutates_and_validates(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    outcome = _driver(root).apply(
        "add-context",
        {"agent": "greeter", "name": "tone", "body": "# Tone\nBe warm.\n"},
        fast_mode=True,
    )
    # File written + agent.yaml wired (the shipped primitive's effect).
    ctx_file = root / "agents" / "greeter" / "contexts" / "tone.md"
    assert ctx_file.is_file()
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert "tone" in data["contexts"]
    # Verify ran + passed (validate + mock-run).
    assert outcome.verify is not None
    assert outcome.verify.ok
    assert outcome.verify.validated
    assert outcome.verify.mock_ran


def test_apply_edit_instructions_writes_prompt(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    new_body = "Be extremely formal at all times.\n"
    _driver(root).apply("edit-instructions", {"agent": "greeter", "body": new_body}, fast_mode=True)
    assert (root / "agents" / "greeter" / "prompt.md").read_text() == new_body


def test_apply_set_model_uses_canonical_round_trip(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    _driver(root).apply(
        "set-model",
        {"agent": "greeter", "provider": "anthropic/claude-sonnet-4-6"},
        confirmed=True,
    )
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert data["model"]["provider"] == "anthropic/claude-sonnet-4-6"


def test_apply_set_retrieval_enables_auto_into(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    _driver(root).apply(
        "set-retrieval", {"agent": "greeter", "auto_into": "context"}, confirmed=True
    )
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert data["retrieval"]["auto_into"] == "context"


def test_apply_add_eval_case_appends(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    _driver(root).apply(
        "add-eval-case",
        {"agent": "greeter", "input": {"text": "yo"}, "expected": {"message": "hey"}},
        fast_mode=True,
    )
    lines = [
        ln
        for ln in (root / "agents" / "greeter" / "evals" / "dataset.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert len(lines) == 2  # original + appended
    assert json.loads(lines[1])["input"] == {"text": "yo"}


def test_apply_add_skill_scaffolds_and_wires(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    _driver(root).apply("add-skill", {"name": "echo-skill", "agent": "greeter"}, fast_mode=True)
    assert (root / "skills" / "echo-skill" / "skill.yaml").is_file()
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert "echo-skill" in data["skills"]


# ---------------------------------------------------------------------------
# D2 — confirmation gate (cost / networked / destructive)
# ---------------------------------------------------------------------------


def test_set_model_requires_confirmation(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    with pytest.raises(ConfirmationRequiredError):
        _driver(root).apply(
            "set-model",
            {"agent": "greeter", "provider": "anthropic/claude-sonnet-4-6"},
            fast_mode=True,  # fast mode must NOT bypass a confirmation-gated action
        )


def test_ingest_kb_plan_requires_confirmation_and_is_networked(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("some knowledge base content " * 20)
    plan = _driver(root).plan("ingest-kb", {"agent": "greeter", "path": str(docs)})
    assert plan.requires_confirmation is True
    assert SideEffect.NETWORK in plan.side_effects
    assert SideEffect.COST in plan.side_effects
    assert plan.reversible is False
    assert plan.estimated_cost_usd is not None and plan.estimated_cost_usd >= 0


def test_remove_context_is_confirm_gated(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    # Attach first (additive), then a removal must be confirm-gated.
    _driver(root).apply("add-context", {"agent": "greeter", "name": "tone"}, fast_mode=True)
    plan = _driver(root).plan("remove-context", {"agent": "greeter", "name": "tone"})
    assert plan.requires_confirmation is True


def test_describe_agent_rename_is_confirm_gated(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    rename_plan = _driver(root).plan("describe-agent", {"agent": "greeter", "new_name": "welcomer"})
    assert rename_plan.requires_confirmation is True
    desc_plan = _driver(root).plan(
        "describe-agent", {"agent": "greeter", "description": "A friendly greeter."}
    )
    assert desc_plan.requires_confirmation is False  # description-only is additive


def test_apply_without_confirmation_refuses(tmp_path: Path) -> None:
    """The library never silently applies — needs confirmed or fast_mode."""
    root = _make_project(tmp_path / "proj")
    with pytest.raises(ConfirmationRequiredError):
        _driver(root).apply(
            "add-context", {"agent": "greeter", "name": "tone"}
        )  # no confirmed, no fast_mode


# ---------------------------------------------------------------------------
# D4 — undo restores the prior checkpoint exactly
# ---------------------------------------------------------------------------


def test_undo_restores_prior_state_exactly(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    driver = _driver(root)
    driver.apply(
        "add-context",
        {"agent": "greeter", "name": "tone", "body": "# Tone\nBe warm.\n"},
        fast_mode=True,
    )
    # State changed (new file + wired agent.yaml).
    assert _snapshot_tree(root) != before
    undone = driver.undo()
    assert undone is not None
    assert undone.action == "add-context"
    # The created file is gone and agent.yaml is byte-identical to before.
    assert not (root / "agents" / "greeter" / "contexts" / "tone.md").is_file()
    after = _snapshot_tree(root)
    assert after == before


def test_undo_marks_entry_and_is_idempotent_at_empty(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    driver = _driver(root)
    driver.apply("add-context", {"agent": "greeter", "name": "tone"}, fast_mode=True)
    assert driver.undo() is not None
    # The (now-undone) entry stays in history, flagged undone.
    hist = driver.history()
    assert len(hist) == 1
    assert hist[0].undone is True
    # No more not-undone entries → undo is a no-op.
    assert driver.undo() is None


def test_history_records_applied_actions(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    driver = _driver(root)
    driver.apply("add-context", {"agent": "greeter", "name": "a"}, fast_mode=True)
    driver.apply("add-context", {"agent": "greeter", "name": "b"}, fast_mode=True)
    hist = driver.history()
    assert [e.action for e in hist] == ["add-context", "add-context"]
    assert [e.args["name"] for e in hist] == ["a", "b"]
    # Each entry carries the pre-apply checkpoint hash (the undo target).
    assert all(e.checkpoint_hash.startswith("sha256:") for e in hist)


# ---------------------------------------------------------------------------
# D3/D4 — verify reverts on an injected validate failure
# ---------------------------------------------------------------------------


def test_verify_reverts_on_injected_validate_failure(tmp_path: Path, monkeypatch) -> None:
    """An apply that produces an invalid tree is reverted to the checkpoint (D3→D4).

    We inject the failure by monkeypatching the action's ``apply`` to write a
    broken prompt reference, so ``validate`` (load_agent) fails in the verify
    loop. The driver must roll the project back to the pre-apply checkpoint and
    NOT record the entry.
    """
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    driver = _driver(root)

    action = get_action("edit-instructions")
    real_apply = action.apply

    def _broken_apply(ctx, args):
        # Run the real edit, then corrupt agent.yaml so validate fails.
        result = real_apply(ctx, args)
        ay = ctx.agent_yaml(args.agent)
        data = yaml.safe_load(ay.read_text())
        data["prompt"] = "./does-not-exist.md"  # prompt file missing → load_agent raises
        ay.write_text(yaml.safe_dump(data, sort_keys=False))
        return result

    monkeypatch.setattr(action, "apply", _broken_apply)

    outcome = driver.apply(
        "edit-instructions", {"agent": "greeter", "body": "new body\n"}, fast_mode=True
    )
    assert outcome.verify is not None
    assert outcome.verify.ok is False
    assert outcome.verify.reverted is True
    assert outcome.verify.error  # the structured load error is surfaced
    assert outcome.log_entry is None  # a reverted apply is not recorded
    # The project was rolled back to the pre-apply checkpoint.
    assert _snapshot_tree(root) == before
    # And nothing landed in the action log.
    assert driver.history() == []


# ---------------------------------------------------------------------------
# D1 — args schema validation rejects bad input before any write
# ---------------------------------------------------------------------------


def test_bad_args_rejected_before_write(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    before = _snapshot_tree(root)
    with pytest.raises(ValidationError):
        _driver(root).plan("add-context", {"agent": "greeter"})  # missing required `name`
    assert _snapshot_tree(root) == before


def test_set_model_rejects_floating_tag(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    with pytest.raises(AuthoringActionError):
        _driver(root).plan("set-model", {"agent": "greeter", "provider": "openai/gpt-4o-latest"})
