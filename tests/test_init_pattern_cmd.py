"""``mdk init --pattern <name>`` + ``mdk patterns list`` — CLI tests (ADR 038).

Covers:
* ``mdk init <name> --pattern <each>`` scaffolds the right shape (single-agent
  for chatbot; a workflow bundle under ``workflows/<name>/`` for the rest),
  ALWAYS leaving a runnable project (ADR 026).
* a ``--mock`` smoke per pattern: each scaffolded pattern runs end-to-end
  against the deterministic mock provider (``mdk run --mock``).
* back-compat: ``--pattern`` is additive — plain ``mdk init`` and
  ``mdk init -t <template>`` are unchanged, and ``--pattern`` + ``-t`` is a
  hard error (mutually exclusive).
* ``mdk patterns list`` output lists all five patterns + their topology.

Hermetic: ``--mock`` (no API keys / network), ``--no-open-editor``,
``MOVATE_CONFIG_PATH`` redirected, cwd a fresh tmp_path (so init bootstraps a
fresh project rather than nesting into the repo).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

WORKFLOW_PATTERNS = [
    "task-oriented",
    "goal-oriented",
    "monitor",
    "simulation",
    "expense-approval",
    "itsm-request",
    "purchase-order",
    "approval-timeout",
    "human-escalation",
]
ALL_PATTERNS = ["chatbot", *WORKFLOW_PATTERNS]

# A unified mock response: every node's required output key + a router `label`.
# Each pattern node's output schema is additionalProperties:true with a single
# required key, so ONE response satisfies every agent node AND every
# intent-router classifier (mdk run --mock dispatches routers via the provider).
_UNIFIED_MOCK = json.dumps(
    {
        "reply": "ok",
        "plan": "p",
        "task_a_result": "a",
        "task_b_result": "b",
        "answer": "done",
        "attempt": "draft",
        "result": "final",
        "metric": "error_rate=0.12",
        "action_taken": "open-incident: breach",
        "status": "ok",
        "transcript": "A.. B..",
        "outcome": "resolved",
        "label": "continue",
    }
)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "user-config.yaml"))


# ---------------------------------------------------------------------------
# patterns list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_patterns_list_shows_all_five() -> None:
    result = runner.invoke(app, ["patterns", "list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    for name in ALL_PATTERNS:
        assert name in out
    # Topology + the ADR reference are surfaced.
    assert "ADR 038" in out
    assert "SUPERVISOR" in out


# ---------------------------------------------------------------------------
# init --pattern: chatbot (single agent)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_pattern_chatbot_scaffolds_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "mybot", "--pattern", "chatbot", "--no-open-editor", "--skip-snapshot"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "mybot"
    assert (proj / "project.yaml").is_file()
    agent_yaml = proj / "agents" / "mybot" / "agent.yaml"
    assert agent_yaml.is_file()
    # name substituted (no placeholder left behind).
    assert "__AGENT_NAME__" not in agent_yaml.read_text()
    assert "max_cost_usd_per_run" in agent_yaml.read_text()


@pytest.mark.unit
def test_init_pattern_chatbot_runs_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rv = runner.invoke(
        app,
        ["init", "mybot", "--pattern", "chatbot", "--no-open-editor", "--skip-snapshot"],
        env={"COLUMNS": "200"},
    )
    assert rv.exit_code == 0, rv.stdout + rv.stderr
    monkeypatch.chdir(tmp_path / "mybot")
    rr = runner.invoke(
        app,
        ["run", "mybot", '{"message": "hello"}', "--mock", "-o", "json"],
        env={"COLUMNS": "200", "MOVATE_MOCK_RESPONSE": '{"reply": "hi there"}'},
    )
    assert rr.exit_code == 0, rr.stdout + rr.stderr
    payload = json.loads(rr.stdout)
    assert payload["status"] == "success"
    assert payload["data"]["reply"] == "hi there"


# ---------------------------------------------------------------------------
# init --pattern: workflow patterns
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("pattern", WORKFLOW_PATTERNS)
def test_init_pattern_workflow_scaffolds_bundle(
    pattern: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    name = f"demo-{pattern}"
    result = runner.invoke(
        app,
        ["init", name, "--pattern", pattern, "--no-open-editor", "--skip-snapshot"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / name
    assert (proj / "project.yaml").is_file()
    bundle = proj / "workflows" / name
    wf_yaml = bundle / "workflow.yaml"
    assert wf_yaml.is_file()
    assert (bundle / "state.json").is_file()
    assert (bundle / "agents").is_dir()
    assert (bundle / "GOVERNANCE.md").is_file()
    # workflow name set to the operator-provided name.
    assert f"name: {name}\n" in wf_yaml.read_text()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "initial", "label"),
    [
        ("task-oriented", '{"request": "compare two things"}', "continue"),
        ("goal-oriented", '{"goal": "write a tagline"}', "continue"),
        ("monitor", '{"signal": "120/1000 5xx"}', "breach"),
        ("simulation", '{"scenario": "a dispute"}', "continue"),
    ],
)
def test_init_pattern_workflow_runs_mock(
    pattern: str,
    initial: str,
    label: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The --mock smoke: each workflow pattern runs end-to-end against the mock
    provider (status success, all nodes executed)."""
    monkeypatch.chdir(tmp_path)
    name = f"demo-{pattern}"
    rv = runner.invoke(
        app,
        ["init", name, "--pattern", pattern, "--no-open-editor", "--skip-snapshot"],
        env={"COLUMNS": "200"},
    )
    assert rv.exit_code == 0, rv.stdout + rv.stderr

    # Per-pattern label drives the gate route (breach for monitor, continue
    # elsewhere). The unified response satisfies every node's output schema.
    resp = json.loads(_UNIFIED_MOCK)
    resp["label"] = label
    monkeypatch.chdir(tmp_path / name)
    rr = runner.invoke(
        app,
        ["run", str(Path("workflows") / name), initial, "--mock", "-o", "json"],
        env={"COLUMNS": "200", "MOVATE_MOCK_RESPONSE": json.dumps(resp)},
    )
    assert rr.exit_code == 0, rr.stdout + rr.stderr
    payload = json.loads(rr.stdout)
    assert payload["status"] == "success", payload
    assert len(payload["nodes"]) >= 3


# ---------------------------------------------------------------------------
# bare standalone workflow bundle
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_pattern_workflow_bare_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "loner-sim",
            "--pattern",
            "simulation",
            "--bare",
            "--no-open-editor",
            "--target",
            str(tmp_path),
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    bundle = tmp_path / "loner-sim"
    # --bare → standalone bundle, NO project wrapper.
    assert (bundle / "workflow.yaml").is_file()
    assert not (bundle / "project.yaml").exists()
    assert not (bundle.parent / "project.yaml").exists()


# ---------------------------------------------------------------------------
# back-compat + mutual exclusion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plain_init_unchanged_by_pattern_addition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --pattern → plain project bootstrap, exactly as before."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "plainproj", "--no-open-editor"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "plainproj"
    assert (proj / "project.yaml").is_file()
    # empty agents/ (no pattern scaffolded).
    assert (proj / "agents").is_dir()
    assert not (proj / "workflows").exists()


@pytest.mark.unit
def test_init_dash_t_template_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """-t <template> is unaffected by the --pattern addition."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "faqproj", "-t", "faq", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "faqproj" / "agents" / "faqproj" / "agent.yaml").is_file()


@pytest.mark.unit
def test_pattern_plus_template_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "x", "--pattern", "chatbot", "-t", "faq", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_unknown_pattern_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "x", "--pattern", "nope", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    assert "unknown pattern" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_pattern_scaffold_name_sanitized_from_path_target(tmp_path) -> None:
    """A path-y init target must yield a VALID workflow name (basename,
    sanitized to the spec rule) — the raw target used to be written verbatim
    into ``name:``, producing a scaffold that failed its own validate."""
    from movate.cli.init import _workflow_name_from_target  # noqa: PLC0415
    from movate.core.workflow.spec import load_workflow_spec  # noqa: PLC0415

    assert _workflow_name_from_target(str(tmp_path / "exp")) == "exp"
    assert _workflow_name_from_target("./demos/My_Exp.Test") == "my-exp-test"
    assert _workflow_name_from_target("expense-approval") == "expense-approval"
    assert _workflow_name_from_target("___") is None  # unsalvageable → keep template name

    result = runner.invoke(
        app, ["init", str(tmp_path / "Path_Target.v2"), "--pattern", "expense-approval"]
    )
    assert result.exit_code == 0, result.output
    wf = tmp_path / "Path_Target.v2" / "workflow.yaml"
    spec, _ = load_workflow_spec(wf)  # parses ⇒ the name passed the spec rule
    assert spec.name == "path-target-v2"
