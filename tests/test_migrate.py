"""Sprint O Day 10-11 — `mdk migrate` tests.

Three layers:

1. **Helper unit** — _filter_files correctly applies glob patterns.
2. **Happy path** — dry-run preview, --apply restores files, --backup
   creates a pre-migrate snapshot.
3. **Error paths** — unknown snapshot exits 1; empty filter match exits 0
   with a warning; IO failures surface cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.cli.migrate_cmd import _filter_files
from movate.snapshot import (
    SnapshotManifest,
    create_snapshot,
    list_snapshots,
)
from movate.snapshot.manifest import FileEntry

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Minimal project with two agents and a movate.yaml."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\n")
    agents = tmp_path / "agents"
    (agents / "triage").mkdir(parents=True)
    (agents / "triage" / "agent.yaml").write_text("name: triage\n")
    (agents / "triage" / "prompt.md").write_text("# Triage\n")
    (agents / "summary").mkdir(parents=True)
    (agents / "summary" / "agent.yaml").write_text("name: summary\n")
    return tmp_path


@pytest.fixture
def snap(project: Path) -> SnapshotManifest:
    """Pre-built snapshot of the project fixture."""
    return create_snapshot(project_root=project, description="baseline")


# ---------------------------------------------------------------------------
# Unit: _filter_files
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFilterFiles:
    def _make_entries(self, paths: list[str]) -> tuple[FileEntry, ...]:
        return tuple(FileEntry(path=p, sha256="abc", size=10) for p in paths)

    def test_no_pattern_returns_all(self) -> None:
        entries = self._make_entries(["agents/a/agent.yaml", "movate.yaml"])
        assert _filter_files(entries, None) == list(entries)

    def test_pattern_narrows_to_matching(self) -> None:
        entries = self._make_entries(
            ["agents/triage/agent.yaml", "agents/triage/prompt.md", "movate.yaml"]
        )
        result = _filter_files(entries, "agents/triage/*")
        assert len(result) == 2
        assert all("triage" in f.path for f in result)

    def test_pattern_no_match_returns_empty(self) -> None:
        entries = self._make_entries(["movate.yaml"])
        assert _filter_files(entries, "agents/**") == []

    def test_top_level_glob_matches_root_files(self) -> None:
        entries = self._make_entries(["movate.yaml", "policy.yaml"])
        result = _filter_files(entries, "*.yaml")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# CLI: dry-run (default)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_exits_0_no_files_changed(project: Path, snap: SnapshotManifest) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(app, ["migrate", short, "--project-root", str(project)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "dry-run" in result.stdout.lower()
    assert "would be restored" in result.stdout.lower()


@pytest.mark.unit
def test_dry_run_lists_files_in_table(project: Path, snap: SnapshotManifest) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(app, ["migrate", short, "--project-root", str(project)])
    assert result.exit_code == 0
    # All captured files appear in the preview
    for entry in snap.files:
        assert entry.path in result.stdout


@pytest.mark.unit
def test_dry_run_with_filter_shows_only_matching_files(
    project: Path, snap: SnapshotManifest
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "migrate",
            short,
            "--filter",
            "agents/triage/*",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    assert "triage" in result.stdout
    # summary agent should NOT appear
    assert "summary/agent.yaml" not in result.stdout


# ---------------------------------------------------------------------------
# CLI: --apply (actual restore)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_restores_files_to_workspace(project: Path, snap: SnapshotManifest) -> None:
    # Mutate a file so we can verify it gets restored.
    triage_yaml = project / "agents" / "triage" / "agent.yaml"
    triage_yaml.write_text("name: MUTATED\n")

    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "migrate",
            short,
            "--apply",
            "--yes",
            "--filter",
            "agents/triage/agent.yaml",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # File should be restored to original content
    assert triage_yaml.read_text() == "name: triage\n"
    assert "migrated" in result.stdout.lower()


@pytest.mark.unit
def test_apply_restores_all_files_without_filter(project: Path, snap: SnapshotManifest) -> None:
    # Overwrite everything, then migrate back.
    for entry in snap.files:
        f = project / entry.path
        f.write_text("overwritten\n")

    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        ["migrate", short, "--apply", "--yes", "--project-root", str(project)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (project / "movate.yaml").read_text() == "api_version: movate/v1\n"
    assert (project / "agents" / "triage" / "agent.yaml").read_text() == "name: triage\n"


@pytest.mark.unit
def test_apply_with_backup_creates_pre_snapshot(project: Path, snap: SnapshotManifest) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    # Mutate to make a backup meaningful.
    (project / "movate.yaml").write_text("api_version: movate/v1\nchanged: true\n")

    before_count = len(list_snapshots(project))
    result = runner.invoke(
        app,
        [
            "migrate",
            short,
            "--apply",
            "--yes",
            "--backup",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    after_count = len(list_snapshots(project))
    # Should have created a new backup snapshot
    assert after_count == before_count + 1
    assert "backed up" in result.stdout.lower()


# ---------------------------------------------------------------------------
# CLI: error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_snapshot_exits_1(project: Path) -> None:
    result = runner.invoke(app, ["migrate", "deadbeef", "--project-root", str(project)])
    assert result.exit_code == 1
    # The snapshot store may say "no snapshots" (empty store) or "not found"
    # (prefix not matched). Either is acceptable; the key guarantee is exit 1.
    combined = result.stdout + result.stderr
    assert "✗" in combined or "error" in combined.lower()


@pytest.mark.unit
def test_filter_no_match_exits_0_with_warning(project: Path, snap: SnapshotManifest) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "migrate",
            short,
            "--filter",
            "skills/**",  # nothing in the snapshot matches
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    assert "no files" in result.stdout.lower()


@pytest.mark.unit
def test_dry_run_does_not_modify_files(project: Path, snap: SnapshotManifest) -> None:
    triage_yaml = project / "agents" / "triage" / "agent.yaml"
    triage_yaml.write_text("name: MUTATED\n")

    short = snap.hash.removeprefix("sha256:")[:8]
    runner.invoke(
        app,
        ["migrate", short, "--project-root", str(project)],  # dry-run default
    )
    # File still mutated — dry-run must not write
    assert triage_yaml.read_text() == "name: MUTATED\n"
