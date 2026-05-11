"""``movate run --dry-run`` — render-only path with no provider call.

Contracts verified here:

* Renders the prompt + cost estimate, exits 0.
* No SQLite row written, no provider construction (test asserts on the
  empty SQLite file from `~/.movate/local.db` isolation in tmp_path).
* Input schema validation fires — bad input still fails fast.
* Jinja template errors surface as exit 2.
* `--dry-run` is mutually exclusive with `--replay` and `--stream`.
* Workflow paths are rejected with a pointer to `movate show`.
* `-o json` payload is valid JSON with the expected keys.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ~/.movate at an empty tmp_path so we can assert dry-run
    doesn't touch SQLite."""
    movate_home = tmp_path / "movate_home"
    movate_home.mkdir()
    monkeypatch.setenv("MOVATE_HOME", str(movate_home))
    return movate_home


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    return scaffold_agent(tmp_path / "demo", name="demo")


# ---------------------------------------------------------------------------
# Happy path — JSON output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_json_renders_messages_and_estimate(agent_dir: Path) -> None:
    r = runner.invoke(
        cli_app,
        ["run", str(agent_dir), '{"text": "What is movate?"}', "--dry-run"],
    )
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["dry_run"] is True
    assert payload["agent"] == "demo"
    assert payload["model"].startswith("openai/")
    assert isinstance(payload["messages"], list) and payload["messages"]
    # Estimate has the expected shape.
    est = payload["estimate"]
    assert est["input_chars"] > 0
    assert est["input_tokens"] > 0
    assert est["output_tokens_budget"] > 0
    # cost_per_call_usd may be None when the model isn't priced; for the
    # default scaffold's openai/gpt-4o-mini it should resolve.
    assert est["cost_per_call_usd"] is None or est["cost_per_call_usd"] > 0


@pytest.mark.unit
def test_dry_run_text_format_includes_estimate_block(agent_dir: Path) -> None:
    r = runner.invoke(
        cli_app,
        ["run", str(agent_dir), '{"text": "hi"}', "--dry-run", "-o", "text"],
    )
    assert r.exit_code == 0
    # Text format renders via a fresh Console() targeting stdout.
    out = r.stdout
    assert "dry-run" in out
    assert "demo" in out
    assert "estimate" in out
    assert "no provider calls were made" in out


# ---------------------------------------------------------------------------
# Side-effect freedom — no SQLite, no run row
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_does_not_write_to_sqlite(
    agent_dir: Path,
    isolated_db: Path,
) -> None:
    r = runner.invoke(
        cli_app,
        ["run", str(agent_dir), '{"text": "hi"}', "--dry-run"],
    )
    assert r.exit_code == 0

    # Either the local.db doesn't exist (dry-run never opened storage),
    # or it exists but has zero rows in the runs table.
    db_path = isolated_db / "local.db"
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        )
        if cur.fetchone() is None:
            return  # no runs table created — nothing to assert
        cur = conn.execute("SELECT COUNT(*) FROM runs")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation paths — schema mismatch, missing input, bad Jinja
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_rejects_missing_required_field(agent_dir: Path) -> None:
    r = runner.invoke(
        cli_app,
        ["run", str(agent_dir), "{}", "--dry-run"],
    )
    # Schema says `text` is required. Should fail with exit 2.
    assert r.exit_code == 2
    # Errors render via the module-level stderr Console.
    err = r.stderr.lower()
    assert "schema" in err or "text" in err


@pytest.mark.unit
def test_dry_run_without_input_exits_2(agent_dir: Path) -> None:
    r = runner.invoke(cli_app, ["run", str(agent_dir), "--dry-run"])
    assert r.exit_code == 2
    assert "input" in r.stderr.lower()


@pytest.mark.unit
def test_dry_run_jinja_error_exits_2(tmp_path: Path) -> None:
    """A Jinja reference to a field NOT in the input schema would crash
    the loader at validate; if it slips through, dry-run still catches
    the render failure."""
    agent = scaffold_agent(tmp_path / "demo", name="demo")
    # Inject a prompt template that references a field not in the schema.
    (agent / "prompt.md").write_text("Hello, {{ input.does_not_exist }}!")

    r = runner.invoke(
        cli_app,
        ["run", str(agent), '{"text": "ok"}', "--dry-run"],
    )
    assert r.exit_code == 2
    # Loader's prompt linter rejects undeclared-input refs first (the
    # prompt fails to even load), so the failure surfaces as either a
    # load error or a render error. Either is correct.
    err = r.stderr.lower()
    assert "load failed" in err or "render failed" in err


# ---------------------------------------------------------------------------
# Mutual exclusion + workflow rejection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_and_replay_are_mutually_exclusive(agent_dir: Path) -> None:
    r = runner.invoke(
        cli_app,
        ["run", str(agent_dir), "--dry-run", "--replay", "fake-run-id"],
    )
    assert r.exit_code == 2
    assert "mutually exclusive" in r.stderr.lower()


@pytest.mark.unit
def test_dry_run_and_stream_are_mutually_exclusive(agent_dir: Path) -> None:
    r = runner.invoke(
        cli_app,
        ["run", str(agent_dir), '{"text":"hi"}', "--dry-run", "--stream"],
    )
    assert r.exit_code == 2
    assert "mutually exclusive" in r.stderr.lower()


@pytest.mark.unit
def test_dry_run_rejects_workflow_path(tmp_path: Path) -> None:
    """A directory containing workflow.yaml should be rejected with a
    pointer to `movate show`."""
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "workflow.yaml").write_text(
        "api_version: movate/v1\nkind: Workflow\nname: wf\nversion: 0.1.0\n"
    )

    r = runner.invoke(cli_app, ["run", str(wf), "{}", "--dry-run"])
    assert r.exit_code == 2
    err = r.stderr.lower()
    assert "agents only" in err or "movate show" in err


# ---------------------------------------------------------------------------
# CLI --help surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_flag_appears_in_help() -> None:
    r = runner.invoke(cli_app, ["run", "--help"])
    assert r.exit_code == 0
    # ANSI codes can interleave inside the flag text; strip whitespace
    # + ANSI for the substring assertion (mirrors the pattern in
    # tests/test_watch.py).
    plain = r.stdout.replace("\n", "").replace(" ", "")
    assert "--dry-run" in plain
