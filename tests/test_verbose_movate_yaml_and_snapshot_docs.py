"""Verbose `movate.yaml` + `.movate/README.md` for new projects.

Two operator-facing improvements to `mdk init --project`:

1. The bootstrapped `movate.yaml` now ships with COMMENTED-OUT
   sections documenting every layered-config block (defaults / policy
   / runtime / skills / eval / bench), so prospects opening the file
   immediately see WHAT can be configured, not just a minimal skeleton.

2. A `.movate/README.md` lands at project root, explaining the
   runtime-state directory (`.movate/`) and snapshots as the central
   operational primitive. Operators who poke into `.movate/snapshots/`
   wondering "what is this?" find the answer in-place.

Tests lock in the structural markers; wording can evolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap a fresh project, chdir in, return project root."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["init", "--project", "demo", "--skip-snapshot"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return tmp_path / "demo"


# ---------------------------------------------------------------------------
# Verbose movate.yaml
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerboseMovateYaml:
    def test_yaml_still_validates_as_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verbose template MUST still pass ProjectConfig validation —
        the commented-out blocks don't count toward the schema, so
        adding them shouldn't break anything."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        proj = _bootstrap(tmp_path, monkeypatch)
        data = yaml.safe_load((proj / "project.yaml").read_text())
        cfg = ProjectConfig.model_validate(data)
        # Headline fields still present.
        assert cfg.agents_dir == "./agents"
        assert cfg.workflows_dir == "./workflows"

    def test_yaml_documents_every_layered_config_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each layered-config block (defaults/policy/runtime/skills/
        eval/bench) appears in the file — uncommented in headers, or
        commented-out as a 'how to enable' example. Operators get a
        survey of the surface area without leaving the file."""
        proj = _bootstrap(tmp_path, monkeypatch)
        body = (proj / "project.yaml").read_text()
        for marker in (
            "defaults:",  # active
            "# policy:",  # commented-out example
            "# runtime:",  # commented-out example
            "# skills:",  # commented-out example
            "# eval:",  # commented-out example
            "# bench:",  # commented-out example
        ):
            assert marker in body, f"movate.yaml missing documentation block: {marker!r}"

    def test_yaml_documents_snapshot_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `.movate/` + snapshot story appears in the YAML
        comments so operators understand what gets created when they
        start running commands."""
        proj = _bootstrap(tmp_path, monkeypatch)
        body = (proj / "project.yaml").read_text()
        # The doc block mentions the directory.
        assert ".movate/" in body
        assert "snapshots/" in body
        # And the commands that operate on snapshots.
        assert "mdk diff" in body
        assert "mdk rollback" in body

    def test_yaml_is_substantially_more_verbose_than_pre_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-bundle the bootstrapped movate.yaml was ~20 lines.
        Verbose form is 100+. Floor at 80 to catch accidental
        truncation regressions while leaving wording flexibility."""
        proj = _bootstrap(tmp_path, monkeypatch)
        body = (proj / "project.yaml").read_text()
        line_count = len(body.splitlines())
        assert line_count >= 80, (
            f"movate.yaml is {line_count} lines; expected ≥80 for the verbose template"
        )


# ---------------------------------------------------------------------------
# .movate/README.md
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMovateStateReadme:
    def test_readme_lands_at_dot_movate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _bootstrap(tmp_path, monkeypatch)
        readme = proj / ".movate" / "README.md"
        assert readme.is_file(), f"expected {readme} to be created"

    def test_readme_explains_snapshots_concretely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The README must explain WHAT snapshots are + WHICH commands
        operate on them — operators landing here should leave with a
        concrete next step, not just abstract context."""
        proj = _bootstrap(tmp_path, monkeypatch)
        body = (proj / ".movate" / "README.md").read_text()
        # Concept marker.
        assert "snapshot" in body.lower()
        # Concrete commands.
        for cmd in ("mdk diff", "mdk rollback", "mdk audit"):
            assert cmd in body, f"README missing command reference: {cmd}"
        # Content-addressed + immutable — the two properties that
        # explain WHY snapshots are useful.
        body_l = body.lower()
        assert "content-addressed" in body_l
        assert "immutable" in body_l

    def test_readme_explains_git_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`.gitignore` policy for snapshots vs. local.db is a common
        operator question — README answers it."""
        proj = _bootstrap(tmp_path, monkeypatch)
        body = (proj / ".movate" / "README.md").read_text()
        assert ".gitignore" in body
        assert "local.db" in body
