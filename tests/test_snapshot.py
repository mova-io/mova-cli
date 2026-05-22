"""Sprint N Day 1-3 — `mdk snapshot` tests.

Layered coverage:

1. **Manifest** — :class:`SnapshotManifest` round-trips through YAML;
   :func:`compute_snapshot_hash` is deterministic + content-addressed.
2. **Store** — :func:`create_snapshot` captures the right files;
   idempotent re-runs return the same hash; :func:`resolve_snapshot`
   handles full / short / prefix lookup with proper errors.
3. **CLI** — create / list / show / delete (with --force gate) all
   render correctly + return appropriate exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.snapshot import (
    FileEntry,
    SnapshotManifest,
    SnapshotManifestError,
    SnapshotNotFoundError,
    SnapshotStoreError,
    create_snapshot,
    delete_snapshot,
    list_snapshots,
    load_manifest,
)
from movate.snapshot.manifest import compute_snapshot_hash, now_iso8601
from movate.snapshot.store import resolve_snapshot

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers — minimal project scaffolds
# ---------------------------------------------------------------------------


def _scaffold_project(root: Path, *, agents: dict[str, str] | None = None) -> Path:
    """Build a tiny project: movate.yaml + N agents under agents/<name>/.

    Each agent gets a minimal agent.yaml + prompt.md. Used by tests
    that need a snapshot to operate on without spinning up the
    full role-template machinery.
    """
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


# ---------------------------------------------------------------------------
# Manifest — round-trip + hash determinism
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestManifestRoundtrip:
    def _build_manifest(self) -> SnapshotManifest:
        return SnapshotManifest(
            api_version="movate/v1",
            kind="Snapshot",
            hash="sha256:abc123",
            created_at=now_iso8601(),
            description="test",
            project_root="/tmp/proj",
            files=(
                FileEntry(path="agents/a/agent.yaml", sha256="sha256:aa", size=10),
                FileEntry(path="movate.yaml", sha256="sha256:bb", size=20),
            ),
            agent_count=1,
        )

    def test_to_yaml_roundtrips_through_load_manifest(self, tmp_path: Path) -> None:
        manifest = self._build_manifest()
        path = tmp_path / "manifest.yaml"
        path.write_text(manifest.to_yaml())
        reloaded = load_manifest(path)
        assert reloaded.hash == manifest.hash
        assert reloaded.description == manifest.description
        assert len(reloaded.files) == 2
        assert reloaded.files[0].path == manifest.files[0].path

    def test_load_manifest_missing_file_errors(self, tmp_path: Path) -> None:
        with pytest.raises(SnapshotManifestError, match="not found"):
            load_manifest(tmp_path / "nope.yaml")

    def test_load_manifest_malformed_yaml_errors(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: : valid: : yaml")
        with pytest.raises(SnapshotManifestError):
            load_manifest(bad)

    def test_load_manifest_missing_required_field_errors(self, tmp_path: Path) -> None:
        partial = tmp_path / "partial.yaml"
        partial.write_text(yaml.safe_dump({"api_version": "movate/v1", "kind": "Snapshot"}))
        with pytest.raises(SnapshotManifestError, match="missing required field"):
            load_manifest(partial)

    def test_load_manifest_wrong_kind_errors(self, tmp_path: Path) -> None:
        wrong = tmp_path / "wrong.yaml"
        wrong.write_text(
            yaml.safe_dump(
                {
                    "api_version": "movate/v1",
                    "kind": "NotSnapshot",
                    "hash": "sha256:x",
                    "created_at": "t",
                    "description": "d",
                    "project_root": "/r",
                    "files": [],
                    "agent_count": 0,
                }
            )
        )
        with pytest.raises(SnapshotManifestError, match="must be 'Snapshot'"):
            load_manifest(wrong)


@pytest.mark.unit
class TestSnapshotHash:
    def _files(self) -> tuple[FileEntry, ...]:
        return (
            FileEntry(path="a", sha256="sha256:1", size=1),
            FileEntry(path="b", sha256="sha256:2", size=2),
        )

    def test_same_inputs_produce_same_hash(self) -> None:
        """Content-addressed semantics: identical inputs always
        produce identical hashes. Free deduplication on disk."""
        h1 = compute_snapshot_hash(
            description="x",
            project_root="/r",
            files=self._files(),
            agent_count=2,
            workflow_count=0,
            extras={},
        )
        h2 = compute_snapshot_hash(
            description="x",
            project_root="/r",
            files=self._files(),
            agent_count=2,
            workflow_count=0,
            extras={},
        )
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_description_change_changes_hash(self) -> None:
        h1 = compute_snapshot_hash(
            description="x",
            project_root="/r",
            files=self._files(),
            agent_count=2,
            workflow_count=0,
            extras={},
        )
        h2 = compute_snapshot_hash(
            description="DIFFERENT",
            project_root="/r",
            files=self._files(),
            agent_count=2,
            workflow_count=0,
            extras={},
        )
        assert h1 != h2

    def test_file_change_changes_hash(self) -> None:
        h1 = compute_snapshot_hash(
            description="x",
            project_root="/r",
            files=self._files(),
            agent_count=2,
            workflow_count=0,
            extras={},
        )
        h2 = compute_snapshot_hash(
            description="x",
            project_root="/r",
            files=(
                FileEntry(path="a", sha256="sha256:DIFFERENT", size=1),
                FileEntry(path="b", sha256="sha256:2", size=2),
            ),
            agent_count=2,
            workflow_count=0,
            extras={},
        )
        assert h1 != h2


# ---------------------------------------------------------------------------
# Store — create / list / resolve / delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateSnapshot:
    def test_captures_movate_yaml_and_agent_files(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p", agents={"triage": "Body 1"})
        manifest = create_snapshot(project_root=project, description="first")
        captured = {entry.path for entry in manifest.files}
        assert "movate.yaml" in captured
        assert "agents/triage/agent.yaml" in captured
        assert "agents/triage/prompt.md" in captured
        assert manifest.agent_count == 1
        assert manifest.description == "first"

    def test_idempotent_returns_same_hash_for_identical_state(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p", agents={"a": "x"})
        first = create_snapshot(project_root=project, description="d")
        second = create_snapshot(project_root=project, description="d")
        assert first.hash == second.hash
        # Only one directory on disk.
        snapshot_dir = project / ".mdk" / "snapshots"
        children = list(snapshot_dir.iterdir())
        assert len(children) == 1

    def test_writes_files_subdir_with_captured_contents(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p", agents={"a": "specific content"})
        manifest = create_snapshot(project_root=project, description="")
        short = manifest.hash.removeprefix("sha256:")[:8]
        files_dir = project / ".mdk" / "snapshots" / short / "files"
        assert files_dir.is_dir()
        assert (files_dir / "movate.yaml").is_file()
        # The captured prompt body matches the original
        assert "specific content" in (files_dir / "agents" / "a" / "prompt.md").read_text()

    def test_skips_junk_directories(self, tmp_path: Path) -> None:
        """__pycache__ and similar dirs must NOT end up in the snapshot."""
        project = _scaffold_project(tmp_path / "p", agents={"a": "x"})
        # Plant a __pycache__ dir under an agent
        pyc_dir = project / "agents" / "a" / "__pycache__"
        pyc_dir.mkdir()
        (pyc_dir / "x.pyc").write_bytes(b"binary")

        manifest = create_snapshot(project_root=project, description="")
        captured = {f.path for f in manifest.files}
        for path in captured:
            assert "__pycache__" not in path

    def test_rejects_nonexistent_project_root(self, tmp_path: Path) -> None:
        with pytest.raises(SnapshotStoreError, match="not a directory"):
            create_snapshot(project_root=tmp_path / "ghost", description="")


@pytest.mark.unit
class TestListSnapshots:
    def test_empty_when_no_snapshots(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p")
        assert list_snapshots(project) == []

    def test_returns_newest_first(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p", agents={"a": "v1"})
        first = create_snapshot(project_root=project, description="first")
        # Mutate state so the second snapshot is genuinely different
        (project / "agents" / "a" / "prompt.md").write_text("v2")
        second = create_snapshot(project_root=project, description="second")

        listed = list_snapshots(project)
        assert len(listed) == 2
        # Newest first: 'second' has a later created_at
        assert listed[0].description == "second"
        assert listed[0].hash == second.hash
        assert listed[1].hash == first.hash


@pytest.mark.unit
class TestResolveSnapshot:
    def _setup(self, tmp_path: Path) -> tuple[Path, SnapshotManifest]:
        project = _scaffold_project(tmp_path / "p", agents={"a": "x"})
        manifest = create_snapshot(project_root=project, description="solo")
        return project, manifest

    def test_full_hash_lookup(self, tmp_path: Path) -> None:
        project, manifest = self._setup(tmp_path)
        resolved = resolve_snapshot(project, manifest.hash)
        assert resolved.hash == manifest.hash

    def test_short_hash_lookup(self, tmp_path: Path) -> None:
        project, manifest = self._setup(tmp_path)
        short = manifest.hash.removeprefix("sha256:")[:8]
        resolved = resolve_snapshot(project, short)
        assert resolved.hash == manifest.hash

    def test_prefix_lookup(self, tmp_path: Path) -> None:
        project, manifest = self._setup(tmp_path)
        short = manifest.hash.removeprefix("sha256:")[:8]
        resolved = resolve_snapshot(project, short[:5])
        assert resolved.hash == manifest.hash

    def test_too_short_prefix_errors(self, tmp_path: Path) -> None:
        project, _ = self._setup(tmp_path)
        with pytest.raises(SnapshotNotFoundError, match="too short"):
            resolve_snapshot(project, "ab")

    def test_unknown_hash_errors(self, tmp_path: Path) -> None:
        project, _ = self._setup(tmp_path)
        with pytest.raises(SnapshotNotFoundError):
            resolve_snapshot(project, "deadbeef")

    def test_no_snapshots_at_all_errors(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p")
        with pytest.raises(SnapshotNotFoundError, match="no snapshots"):
            resolve_snapshot(project, "abcdef")


@pytest.mark.unit
class TestDeleteSnapshot:
    def test_removes_directory_and_returns_manifest(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p", agents={"a": "x"})
        created = create_snapshot(project_root=project, description="goodbye")
        short = created.hash.removeprefix("sha256:")[:8]
        target = project / ".mdk" / "snapshots" / short
        assert target.is_dir()

        returned = delete_snapshot(project, short)
        assert returned.hash == created.hash
        assert not target.exists()


# ---------------------------------------------------------------------------
# CLI — mdk snapshot {create, list, show, delete}
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal project + cd into it so the walk-up resolution finds it."""
    project = _scaffold_project(tmp_path / "proj", agents={"triage": "test body"})
    monkeypatch.chdir(project)
    return project


@pytest.mark.unit
def test_cli_create_writes_snapshot(project_root: Path) -> None:
    result = runner.invoke(app, ["snapshot", "create", "-d", "first"])
    assert result.exit_code == 0, result.stdout + result.stderr
    snapshots = list((project_root / ".mdk" / "snapshots").iterdir())
    assert len(snapshots) == 1
    # Header rendered
    assert "✓ Created" in result.stdout or "snapshot" in result.stdout.lower()


@pytest.mark.unit
def test_cli_list_empty_renders_hint(project_root: Path) -> None:
    result = runner.invoke(app, ["snapshot", "list"])
    assert result.exit_code == 0
    assert "no snapshots" in result.stdout.lower()
    assert "snapshot create" in result.stdout.lower()


@pytest.mark.unit
def test_cli_list_renders_recent_snapshots(project_root: Path) -> None:
    runner.invoke(app, ["snapshot", "create", "-d", "alpha"])
    (project_root / "agents" / "triage" / "prompt.md").write_text("changed")
    runner.invoke(app, ["snapshot", "create", "-d", "beta"])

    result = runner.invoke(app, ["snapshot", "list"])
    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "beta" in result.stdout
    # Beta is newer; should appear first in the rendered output
    pos_beta = result.stdout.index("beta")
    pos_alpha = result.stdout.index("alpha")
    assert pos_beta < pos_alpha


@pytest.mark.unit
def test_cli_list_json_output(project_root: Path) -> None:
    runner.invoke(app, ["snapshot", "create", "-d", "json-test"])
    result = runner.invoke(app, ["snapshot", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["description"] == "json-test"


@pytest.mark.unit
def test_cli_show_renders_manifest(project_root: Path) -> None:
    create_result = runner.invoke(app, ["snapshot", "create", "-d", "specific"])
    assert create_result.exit_code == 0
    # The CLI prints the short hash in the success panel; capture it
    # by listing.
    manifests = list_snapshots(project_root)
    short = manifests[0].hash.removeprefix("sha256:")[:8]

    result = runner.invoke(app, ["snapshot", "show", short])
    assert result.exit_code == 0
    assert "specific" in result.stdout
    assert short in result.stdout


@pytest.mark.unit
def test_cli_show_with_files_includes_file_table(project_root: Path) -> None:
    runner.invoke(app, ["snapshot", "create", "-d", "x"])
    short = list_snapshots(project_root)[0].hash.removeprefix("sha256:")[:8]
    result = runner.invoke(app, ["snapshot", "show", short, "--files"])
    assert result.exit_code == 0
    # File table includes the agent.yaml path
    assert "agent.yaml" in result.stdout


@pytest.mark.unit
def test_cli_show_unknown_hash_exits_one(project_root: Path) -> None:
    result = runner.invoke(app, ["snapshot", "show", "ffffffff"])
    assert result.exit_code == 1


@pytest.mark.unit
def test_cli_delete_without_force_is_dry_run(project_root: Path) -> None:
    runner.invoke(app, ["snapshot", "create", "-d", "doomed"])
    short = list_snapshots(project_root)[0].hash.removeprefix("sha256:")[:8]

    # Without --force: exit 1, snapshot NOT deleted
    result = runner.invoke(app, ["snapshot", "delete", short])
    assert result.exit_code == 1
    assert "dry-run" in result.stdout.lower() or "would delete" in result.stdout.lower()
    assert (project_root / ".mdk" / "snapshots" / short).is_dir()


@pytest.mark.unit
def test_cli_delete_with_force_removes_snapshot(project_root: Path) -> None:
    runner.invoke(app, ["snapshot", "create", "-d", "doomed"])
    short = list_snapshots(project_root)[0].hash.removeprefix("sha256:")[:8]

    result = runner.invoke(app, ["snapshot", "delete", short, "--force"])
    assert result.exit_code == 0
    assert not (project_root / ".mdk" / "snapshots" / short).exists()
