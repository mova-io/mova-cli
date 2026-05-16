"""PR #109 — `mdk eval-gen --all` project sweep.

Closes the gap between PR #108's per-agent guided wizard and CI use.
Operators (and CI workflows) get one command that generates eval cases
for every agent in the project, with a greppable summary line so
``mdk_eval_gen_all_summary:`` can be branched on the same way as
``mdk_eval_all_summary:``.

Tested here:

1. Happy path — two agents, both generated, each gets its
   ``evals/<agent>/dataset.generated.jsonl`` with N rows.
2. Idempotent skip — agent with an existing generated dataset is
   skipped, not overwritten, unless ``--force`` is passed.
3. ``--force`` regenerates everything.
4. Empty project (no agents) emits a vacuous-pass summary (ok=true,
   exit 0) so CI scripts can branch on agents_total=0 cleanly.
5. Not-in-project → clean error with exit 2.
6. Mutex: ``--all`` + AGENT, ``--all`` + ``--guided``.
7. Summary line shape — ``mdk_eval_gen_all_summary: agents_total=N
   generated=N skipped=N failed=N ok=true|false``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project_with_two_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init project + add two agents (faq + summarizer). Returns root.

    Two agents are enough to exercise the per-agent state machine —
    one agent passes through every state in the sweep, so two agents
    means we see the loop work + the summary line aggregate."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    for template in ("faq", "summarizer"):
        result = runner.invoke(app, ["add", template], env={"COLUMNS": "200"})
        assert result.exit_code == 0, f"add {template}: {result.stdout + result.stderr}"
    return proj


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_generates_for_every_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk eval-gen --all --mock --num 3`` should walk both agents
    and write a 3-row JSONL each. The summary line aggregates the
    counts so CI scrapers can branch."""
    proj = _bootstrap_project_with_two_agents(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval-gen", "--all", "--mock", "--num", "3"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Both agents got their generated file.
    faq_out = proj / "evals" / "faq" / "dataset.generated.jsonl"
    sum_out = proj / "evals" / "summarizer" / "dataset.generated.jsonl"
    assert faq_out.is_file()
    assert sum_out.is_file()
    assert len([ln for ln in faq_out.read_text().splitlines() if ln.strip()]) == 3
    assert len([ln for ln in sum_out.read_text().splitlines() if ln.strip()]) == 3
    # Summary line on stdout (Rich console → captured by CliRunner).
    combined = result.stdout + result.stderr
    assert "mdk_eval_gen_all_summary:" in combined
    assert "agents_total=2" in combined
    assert "generated=2" in combined
    assert "skipped=0" in combined
    assert "failed=0" in combined
    assert "ok=true" in combined


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_skips_existing_generated_datasets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second sweep is idempotent: agents that already have a
    generated dataset are skipped, not overwritten. Lets operators
    chain ``mdk eval-gen --all`` in CI without paying for tokens on
    every push."""
    proj = _bootstrap_project_with_two_agents(tmp_path, monkeypatch)
    # First sweep — generates both.
    runner.invoke(app, ["eval-gen", "--all", "--mock", "--num", "2"], env={"COLUMNS": "200"})
    faq_out = proj / "evals" / "faq" / "dataset.generated.jsonl"
    sentinel = "first-run-content"
    faq_out.write_text(sentinel + "\n")  # tamper to detect overwrite
    # Second sweep — should skip both because the files exist.
    result = runner.invoke(
        app, ["eval-gen", "--all", "--mock", "--num", "2"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Tampered file is intact — no overwrite happened.
    assert faq_out.read_text().strip() == sentinel
    # Summary reports the skips.
    combined = result.stdout + result.stderr
    assert "skipped=2" in combined
    assert "generated=0" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_all_force_regenerates_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--force`` overrides the idempotent skip — every agent
    regenerates from scratch."""
    proj = _bootstrap_project_with_two_agents(tmp_path, monkeypatch)
    runner.invoke(app, ["eval-gen", "--all", "--mock", "--num", "2"], env={"COLUMNS": "200"})
    faq_out = proj / "evals" / "faq" / "dataset.generated.jsonl"
    sentinel = "first-run-tampered\n"
    faq_out.write_text(sentinel)
    # --force replaces.
    result = runner.invoke(
        app,
        ["eval-gen", "--all", "--mock", "--num", "2", "--force"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # File was rewritten to real JSONL — sentinel gone, valid JSON rows.
    content = faq_out.read_text()
    assert sentinel.strip() not in content
    rows = [json.loads(ln) for ln in content.splitlines() if ln.strip()]
    assert len(rows) == 2
    assert all(r.get("generated") is True for r in rows)
    # Summary: 2 generated, 0 skipped.
    combined = result.stdout + result.stderr
    assert "generated=2" in combined
    assert "skipped=0" in combined


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_empty_project_emits_vacuous_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project with no agents → exit 0 + agents_total=0 summary so
    CI workflows don't choke on freshly-init'd projects."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    # No `mdk add` — bare project.
    result = runner.invoke(
        app, ["eval-gen", "--all", "--mock", "--num", "5"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "agents_total=0" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_all_outside_project_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``mdk eval-gen --all`` outside any project → clean exit 2
    with the project-required hint. CI scripts get a deterministic
    failure rather than a confusing wall of nothing-to-do output."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["eval-gen", "--all", "--mock"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    assert "not inside a movate project" in result.stderr


# ---------------------------------------------------------------------------
# Mutex
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_with_agent_is_mutex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk eval-gen --all faq`` is ambiguous — refuse with exit 2."""
    _bootstrap_project_with_two_agents(tmp_path, monkeypatch)
    result = runner.invoke(app, ["eval-gen", "faq", "--all", "--mock"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


@pytest.mark.unit
def test_all_with_guided_is_mutex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk eval-gen --all --guided`` — guided picks ONE agent, --all
    sweeps every agent; combining them is incoherent."""
    _bootstrap_project_with_two_agents(tmp_path, monkeypatch)
    result = runner.invoke(app, ["eval-gen", "--all", "--guided", "--mock"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_zero_num_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--num 0`` rejected — same preflight the per-agent path uses."""
    _bootstrap_project_with_two_agents(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["eval-gen", "--all", "--num", "0", "--mock"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    assert "--num must be ≥ 1" in result.stderr


@pytest.mark.unit
def test_all_bad_sample_input_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad --sample-input is rejected before any agent loads."""
    _bootstrap_project_with_two_agents(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["eval-gen", "--all", "--mock", "--sample-input", "not-valid-json"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    assert "not valid JSON" in result.stderr
