"""Sprint N Day 4-5 — `mdk diff <a> <b>` tests.

Layered coverage:

1. **Module** — :func:`diff_snapshots` is a pure function: same inputs
   always produce the same diff; categorises into added/removed/
   modified correctly; computes agent + workflow deltas; identifies
   identical snapshots cleanly.
2. **CLI** — renders the diff as a Rich table, supports --json,
   exits 1 on drift / 0 on identical; honors unknown / ambiguous
   hash paths from the snapshot store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.snapshot import (
    FileEntry,
    SnapshotManifest,
    create_snapshot,
    diff_snapshots,
)
from movate.snapshot.manifest import now_iso8601

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_project(root: Path, *, agents: dict[str, str] | None = None) -> Path:
    """Tiny scaffold reused from snapshot tests."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("# test\n")
    agents_dir = root / "agents"
    agents_dir.mkdir(exist_ok=True)
    for name, body in (agents or {}).items():
        agent_dir = agents_dir / name
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(f"name: {name}\n")
        (agent_dir / "prompt.md").write_text(body)
    return root


def _make_manifest(
    *,
    hash_suffix: str,
    files: tuple[FileEntry, ...],
    description: str = "",
    agent_count: int = 1,
    workflow_count: int = 0,
) -> SnapshotManifest:
    """Build an in-memory SnapshotManifest for module-level diff tests
    (no disk I/O — diff is a pure function)."""
    return SnapshotManifest(
        api_version="movate/v1",
        kind="Snapshot",
        hash=f"sha256:{hash_suffix}",
        created_at=now_iso8601(),
        description=description,
        project_root="/tmp/p",
        files=files,
        agent_count=agent_count,
        workflow_count=workflow_count,
    )


# ---------------------------------------------------------------------------
# Module — diff_snapshots
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiffSnapshots:
    def test_identical_snapshots_yield_no_changes(self) -> None:
        files = (
            FileEntry(path="a", sha256="sha256:11", size=1),
            FileEntry(path="b", sha256="sha256:22", size=2),
        )
        m = _make_manifest(hash_suffix="same", files=files)
        diff = diff_snapshots(m, m)
        assert diff.is_identical
        assert diff.total_changes == 0
        assert not diff.files_added
        assert not diff.files_removed
        assert not diff.files_modified

    def test_detects_added_files(self) -> None:
        before = _make_manifest(
            hash_suffix="b",
            files=(FileEntry(path="a", sha256="sha256:1", size=1),),
        )
        after = _make_manifest(
            hash_suffix="a",
            files=(
                FileEntry(path="a", sha256="sha256:1", size=1),
                FileEntry(path="new", sha256="sha256:2", size=20),
            ),
        )
        diff = diff_snapshots(before, after)
        assert len(diff.files_added) == 1
        assert diff.files_added[0].path == "new"
        assert diff.files_added[0].kind == "added"
        assert diff.files_added[0].after is not None
        assert diff.files_added[0].before is None

    def test_detects_removed_files(self) -> None:
        before = _make_manifest(
            hash_suffix="b",
            files=(
                FileEntry(path="a", sha256="sha256:1", size=1),
                FileEntry(path="gone", sha256="sha256:9", size=9),
            ),
        )
        after = _make_manifest(
            hash_suffix="a",
            files=(FileEntry(path="a", sha256="sha256:1", size=1),),
        )
        diff = diff_snapshots(before, after)
        assert len(diff.files_removed) == 1
        assert diff.files_removed[0].path == "gone"
        assert diff.files_removed[0].kind == "removed"
        assert diff.files_removed[0].before is not None
        assert diff.files_removed[0].after is None

    def test_detects_modified_files_via_sha_change(self) -> None:
        """Same path, different sha256 → modified."""
        before = _make_manifest(
            hash_suffix="b",
            files=(FileEntry(path="changed", sha256="sha256:old", size=10),),
        )
        after = _make_manifest(
            hash_suffix="a",
            files=(FileEntry(path="changed", sha256="sha256:new", size=15),),
        )
        diff = diff_snapshots(before, after)
        assert len(diff.files_modified) == 1
        mod = diff.files_modified[0]
        assert mod.kind == "modified"
        assert mod.size_delta == 5

    def test_size_delta_is_signed(self) -> None:
        """File shrinks → negative size_delta."""
        before = _make_manifest(
            hash_suffix="b",
            files=(FileEntry(path="x", sha256="sha256:a", size=100),),
        )
        after = _make_manifest(
            hash_suffix="a",
            files=(FileEntry(path="x", sha256="sha256:b", size=30),),
        )
        diff = diff_snapshots(before, after)
        assert diff.files_modified[0].size_delta == -70

    def test_agent_and_workflow_count_deltas(self) -> None:
        before = _make_manifest(
            hash_suffix="b",
            files=(),
            agent_count=2,
            workflow_count=1,
        )
        after = _make_manifest(
            hash_suffix="a",
            files=(),
            agent_count=4,
            workflow_count=0,
        )
        diff = diff_snapshots(before, after)
        assert diff.agent_count_delta == 2
        assert diff.workflow_count_delta == -1

    def test_mixed_changes_partition_correctly(self) -> None:
        """Same path can't appear in more than one of added/removed/modified."""
        before = _make_manifest(
            hash_suffix="b",
            files=(
                FileEntry(path="kept-same", sha256="sha256:k", size=5),
                FileEntry(path="changed", sha256="sha256:old", size=10),
                FileEntry(path="gone", sha256="sha256:g", size=7),
            ),
        )
        after = _make_manifest(
            hash_suffix="a",
            files=(
                FileEntry(path="kept-same", sha256="sha256:k", size=5),
                FileEntry(path="changed", sha256="sha256:new", size=10),
                FileEntry(path="new-file", sha256="sha256:n", size=3),
            ),
        )
        diff = diff_snapshots(before, after)

        added = {c.path for c in diff.files_added}
        removed = {c.path for c in diff.files_removed}
        modified = {c.path for c in diff.files_modified}

        assert added == {"new-file"}
        assert removed == {"gone"}
        assert modified == {"changed"}
        # Partition — no overlap
        assert not (added & removed)
        assert not (added & modified)
        assert not (removed & modified)
        # kept-same appears in none of the change sets
        assert "kept-same" not in added | removed | modified


# ---------------------------------------------------------------------------
# CLI — `mdk diff`
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_two_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, str]:
    """Build a project with two distinct snapshots; return (project, before, after)."""
    project = _scaffold_project(tmp_path / "p", agents={"triage": "v1"})
    monkeypatch.chdir(project)
    first = create_snapshot(project_root=project, description="alpha")
    # Mutate state so the second snapshot is genuinely different
    (project / "agents" / "triage" / "prompt.md").write_text("v2 — drift introduced")
    (project / "policy.yaml").write_text("policy: {}\n")  # also adds a file
    second = create_snapshot(project_root=project, description="beta")
    before_short = first.hash.removeprefix("sha256:")[:8]
    after_short = second.hash.removeprefix("sha256:")[:8]
    return project, before_short, after_short


@pytest.mark.unit
def test_cli_diff_renders_changes(
    project_with_two_snapshots: tuple[Path, str, str],
) -> None:
    _, before, after = project_with_two_snapshots
    result = runner.invoke(app, ["diff", before, after])
    # Drift → exit 1 (CI-gate friendly)
    assert result.exit_code == 1, result.stdout + result.stderr
    out = result.stdout
    # Header + descriptions render
    assert before in out
    assert after in out
    assert "alpha" in out
    assert "beta" in out
    # File changes — policy.yaml was added, prompt.md was modified
    assert "policy.yaml" in out
    assert "prompt.md" in out


@pytest.mark.unit
def test_cli_diff_identical_returns_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diffing the same snapshot against itself = exit 0 + 'no changes'."""
    project = _scaffold_project(tmp_path / "p", agents={"a": "x"})
    monkeypatch.chdir(project)
    only = create_snapshot(project_root=project, description="solo")
    short = only.hash.removeprefix("sha256:")[:8]

    result = runner.invoke(app, ["diff", short, short])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no changes" in result.stdout.lower()


@pytest.mark.unit
def test_cli_diff_json_output_is_parseable(
    project_with_two_snapshots: tuple[Path, str, str],
) -> None:
    _, before, after = project_with_two_snapshots
    result = runner.invoke(app, ["diff", before, after, "--json"])
    assert result.exit_code == 1  # drift
    payload = json.loads(result.stdout)
    assert payload["before_hash"].endswith(before) or before in payload["before_hash"]
    assert "files_added" in payload
    assert "files_modified" in payload
    assert payload["total_changes"] > 0
    assert payload["is_identical"] is False


@pytest.mark.unit
def test_cli_diff_unknown_hash_exits_one(
    project_with_two_snapshots: tuple[Path, str, str],
) -> None:
    _, before, _after = project_with_two_snapshots
    result = runner.invoke(app, ["diff", before, "ffffffff"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "no snapshot" in combined.lower()


@pytest.mark.unit
def test_cli_diff_no_snapshots_at_all_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project with no snapshots can't diff anything."""
    project = _scaffold_project(tmp_path / "p")
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["diff", "aaaaaaaa", "bbbbbbbb"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "no snapshot" in combined.lower()
