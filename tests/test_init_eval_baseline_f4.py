"""F4' (#138) — post-scaffold ``--mock`` eval BASELINE.

F4 (#113) did a single post-scaffold ``--mock`` smoke RUN. F4' upgrades that
into an eval BASELINE: after a template (or ``--llm``) scaffold, run the new
agent's eval dataset (``evals/dataset.jsonl``) under the deterministic mock
provider, capture a baseline pass-rate, and surface it (e.g.
``baseline: 5/5 cases pass under --mock``). This gives the operator a
known-good starting point AND catches a template whose evals don't even run.

Behavior under test (all hermetic — MockProvider, tmp sqlite via
``MOVATE_DB``, isolated ``HOME``; no API keys, no network):

* **Per-shape guard:** ``mdk init <name> -t <template> --mock`` scaffolds the
  agent AND runs its ``--mock`` eval baseline, reporting a sensible pass-rate
  (>0 cases, no crash), for EVERY shipped agent template — the core
  "for every shape" assertion.
* The baseline pass-rate line is surfaced in output.
* A template whose eval errors under ``--mock`` (e.g. an active LLM judge that
  the mock can't satisfy) → init STILL succeeds with a warning (graceful), not
  a hard failure.
* ``--no-baseline`` opts out; ``--mock`` is required (a real-provider scaffold
  skips the baseline with a hint rather than spending tokens on every init).

The baseline reuses the SHIPPED eval engine + mock path (it mirrors
``mdk eval --mock``), so a passing baseline here means the operator's first
``mdk eval --mock`` will score the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import list_templates

runner = CliRunner(mix_stderr=False)

# The task's explicit "for every shape" list — these MUST produce a passing
# baseline under --mock. (hr-policy ships an active LLM ``judge.yaml`` the
# mock provider can't satisfy, so it degrades gracefully instead — covered by
# TestBaselineGracefulDegradation rather than asserted to pass here.)
_SHAPES_THAT_PASS = sorted(set(list_templates()) - {"hr-policy"})

_BASELINE_SUMMARY_RE = re.compile(
    r"mdk_baseline_summary: agent=\S+ cases=(\d+) passing=(\d+) "
    r"pass_rate=([\d.]+) mock=true"
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route storage + home to per-test tmp dirs so the baseline's
    ``build_local_runtime`` never touches the developer's ``~/.movate``.

    Also ``chdir`` into ``tmp_path`` so ``mdk init`` doesn't detect the
    repo's own ``project.yaml`` up the tree (which would add the agent to the
    repo instead of bootstrapping a fresh project under ``tmp_path``).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "local.db"))
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.chdir(tmp_path)


def _init_template(tmp_path: Path, name: str, template: str, *extra: str) -> object:
    """``mdk init <name> -t <template> --mock`` into a fresh project under
    ``tmp_path``. ``--no-open-editor`` keeps it headless; ``--skip-snapshot``
    keeps the project bootstrap cheap.
    """
    return runner.invoke(
        app,
        [
            "init",
            name,
            "-t",
            template,
            "--mock",
            "--no-open-editor",
            "--skip-snapshot",
            "--target",
            str(tmp_path),
            *extra,
        ],
        env={"COLUMNS": "200"},
    )


# ---------------------------------------------------------------------------
# Core "for every shape" guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaselineForEveryShape:
    @pytest.mark.parametrize("template", _SHAPES_THAT_PASS)
    def test_every_template_scaffolds_and_baselines(self, template: str, tmp_path: Path) -> None:
        """Every shipped agent template: ``mdk init -t <tpl> --mock`` scaffolds
        the agent AND its ``--mock`` eval baseline runs, reporting a sensible
        pass-rate (>0 cases, no crash, exit 0)."""
        name = "agent-under-test"
        result = _init_template(tmp_path, name, template)

        assert result.exit_code == 0, f"{template}: init failed\n{result.stdout}\n{result.stderr}"
        # The agent landed on disk in the project layout.
        agent_dir = tmp_path / name / "agents" / name
        assert (agent_dir / "agent.yaml").is_file(), f"{template}: agent.yaml missing"
        assert (agent_dir / "evals" / "dataset.jsonl").is_file(), (
            f"{template}: dataset.jsonl missing"
        )

        # The greppable baseline summary line fired with a real measurement.
        match = _BASELINE_SUMMARY_RE.search(result.stdout)
        assert match, f"{template}: no baseline summary line in output\n{result.stdout}"
        cases, passing, rate = int(match.group(1)), int(match.group(2)), float(match.group(3))
        assert cases > 0, f"{template}: baseline ran with zero cases"
        assert 0 <= passing <= cases
        assert 0.0 <= rate <= 1.0
        # Shipped templates pass under the dataset-aware mock — a regression
        # in a template (or in the mock wiring) would drop this below 1.0.
        assert rate == pytest.approx(1.0), (
            f"{template}: baseline pass_rate {rate} < 1.0 ({passing}/{cases})"
        )


# ---------------------------------------------------------------------------
# The pass-rate line is surfaced
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaselineSurfaced:
    def test_baseline_passrate_line_appears(self, tmp_path: Path) -> None:
        """The human-readable baseline line (``baseline: N/M cases pass under
        --mock``) is printed to stdout — not just the greppable summary."""
        result = _init_template(tmp_path, "faqbot", "faq")
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "baseline:" in result.stdout
        assert "cases pass under" in result.stdout
        assert "--mock" in result.stdout
        # And the count matches the faq dataset (15 cases).
        match = re.search(r"baseline:\s+(\d+)/(\d+)\s+cases pass", result.stdout)
        assert match, result.stdout
        assert int(match.group(1)) == int(match.group(2))  # all pass
        assert int(match.group(2)) > 0


# ---------------------------------------------------------------------------
# Graceful degradation — an eval that errors never fails init
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaselineGracefulDegradation:
    def test_llm_judge_template_degrades_to_warning(self, tmp_path: Path) -> None:
        """hr-policy ships an active LLM ``judge.yaml`` the mock can't satisfy
        under ``--mock``. The baseline must degrade to a WARNING — init STILL
        succeeds (exit 0), the scaffold is intact, no baseline summary fires."""
        result = _init_template(tmp_path, "hp", "hr-policy")
        assert result.exit_code == 0, result.stdout + result.stderr
        # Scaffold intact.
        agent_dir = tmp_path / "hp" / "agents" / "hp"
        assert (agent_dir / "agent.yaml").is_file()
        # Degraded to a warning (stderr), not a passing baseline summary.
        assert "eval baseline skipped" in result.stderr
        assert _BASELINE_SUMMARY_RE.search(result.stdout) is None
        # The warning points the operator at the manual command.
        assert "mdk eval --mock" in result.stderr


# ---------------------------------------------------------------------------
# Gating — opt-out + --mock-only
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaselineGating:
    def test_no_baseline_opts_out(self, tmp_path: Path) -> None:
        """``--no-baseline`` skips the step entirely (no run, no summary line),
        with a one-line opt-out note; init still succeeds."""
        result = _init_template(tmp_path, "faqbot", "faq", "--no-baseline")
        assert result.exit_code == 0, result.stdout + result.stderr
        assert _BASELINE_SUMMARY_RE.search(result.stdout) is None
        # Opt-out note surfaced (stderr hint).
        combined = result.stdout + result.stderr
        assert "--no-baseline" in combined and "skipped" in combined

    def test_non_mock_skips_with_hint(self, tmp_path: Path) -> None:
        """Without ``--mock`` the baseline is skipped (it's a hermetic
        zero-cost step) with a hint pointing at ``mdk eval --mock`` — never a
        silent real-provider eval on every init."""
        result = runner.invoke(
            app,
            [
                "init",
                "faqbot",
                "-t",
                "faq",
                "--no-open-editor",
                "--skip-snapshot",
                "--target",
                str(tmp_path),
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert _BASELINE_SUMMARY_RE.search(result.stdout) is None
        combined = result.stdout + result.stderr
        assert "mdk eval --mock" in combined
