"""Sprint N Day 6-7 — `mdk rollback` tests.

Two layers:

1. **Module** — :func:`rollback_to` restores files from the target
   snapshot, auto-captures a pre-rollback snapshot, returns a
   :class:`RollbackResult`. Errors on missing target / corrupted
   snapshot store.
2. **CLI** — `mdk rollback <hash>` defaults to dry-run (exit 1 without
   --force); --force performs the restore and prints the pre-rollback
   hash for round-trip recovery.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.snapshot import (
    create_snapshot,
    list_snapshots,
    rollback_to,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_project(root: Path, *, agents: dict[str, str] | None = None) -> Path:
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
# Module — rollback_to
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRollbackTo:
    def test_restores_files_from_target_snapshot(self, tmp_path: Path) -> None:
        """Rollback should rewrite captured files back to their
        original content (the state when the target snapshot was created)."""
        project = _scaffold_project(tmp_path / "p", agents={"a": "v1 — original"})
        target = create_snapshot(project_root=project, description="pinned")

        # Mutate state
        (project / "agents" / "a" / "prompt.md").write_text("v2 — drift")
        assert (project / "agents" / "a" / "prompt.md").read_text() == "v2 — drift"

        # Roll back
        target_short = target.hash.removeprefix("sha256:")[:8]
        result = rollback_to(project_root=project, target_hash=target_short)

        # File restored to v1
        assert (project / "agents" / "a" / "prompt.md").read_text() == "v1 — original"
        assert result.restored_count >= 1
        assert result.target.hash == target.hash

    def test_creates_pre_rollback_snapshot(self, tmp_path: Path) -> None:
        """Pre-rollback snapshot captures the state BEFORE the rollback
        so the operator can roll forward to undo."""
        project = _scaffold_project(tmp_path / "p", agents={"a": "v1"})
        target = create_snapshot(project_root=project, description="v1-baseline")
        (project / "agents" / "a" / "prompt.md").write_text("v2 — about to be undone")

        # Snapshot count before rollback
        before = len(list_snapshots(project))

        target_short = target.hash.removeprefix("sha256:")[:8]
        result = rollback_to(project_root=project, target_hash=target_short)

        # A new pre-rollback snapshot now exists
        after = len(list_snapshots(project))
        assert after == before + 1

        # Description encodes the rollback origin
        assert target_short in result.pre_snapshot.description
        assert "rolled back FROM" in result.pre_snapshot.description

        # Extras carry the full target hash
        assert result.pre_snapshot.extras.get("rollback_target") == target.hash

    def test_can_round_trip_via_pre_snapshot(self, tmp_path: Path) -> None:
        """Roll back, then roll forward to the pre-rollback snapshot —
        state should match what was there before the first rollback."""
        project = _scaffold_project(tmp_path / "p", agents={"a": "v1"})
        v1 = create_snapshot(project_root=project, description="v1")
        (project / "agents" / "a" / "prompt.md").write_text("v2 — to be revisited")

        v1_short = v1.hash.removeprefix("sha256:")[:8]
        rollback_result = rollback_to(project_root=project, target_hash=v1_short)
        assert (project / "agents" / "a" / "prompt.md").read_text() == "v1"

        # Now roll forward to the pre-rollback snapshot
        pre_short = rollback_result.pre_snapshot.hash.removeprefix("sha256:")[:8]
        rollback_to(project_root=project, target_hash=pre_short)
        assert (project / "agents" / "a" / "prompt.md").read_text() == "v2 — to be revisited"

    def test_unchanged_files_outside_snapshot_are_untouched(self, tmp_path: Path) -> None:
        """Files not in the target snapshot's manifest should NOT be
        affected — rollback is precise, not a wholesale dir wipe."""
        project = _scaffold_project(tmp_path / "p", agents={"a": "v1"})
        target = create_snapshot(project_root=project, description="t")

        # Add a NEW agent after the snapshot — it shouldn't be removed
        # by rollback (it's a file the snapshot doesn't know about).
        new_agent = project / "agents" / "b"
        new_agent.mkdir()
        (new_agent / "agent.yaml").write_text("name: b\n")

        target_short = target.hash.removeprefix("sha256:")[:8]
        rollback_to(project_root=project, target_hash=target_short)

        # The new agent's file is still there — rollback is non-destructive
        # against files outside the target snapshot's manifest.
        assert (new_agent / "agent.yaml").is_file()

    def test_unknown_target_raises_not_found(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p", agents={"a": "v1"})
        # Need at least one snapshot to populate the store
        create_snapshot(project_root=project, description="seed")

        from movate.snapshot import SnapshotNotFoundError  # noqa: PLC0415

        with pytest.raises(SnapshotNotFoundError):
            rollback_to(project_root=project, target_hash="ffffffff")


# ---------------------------------------------------------------------------
# CLI — mdk rollback
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_target_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    project = _scaffold_project(tmp_path / "p", agents={"a": "v1"})
    monkeypatch.chdir(project)
    target = create_snapshot(project_root=project, description="checkpoint")
    short = target.hash.removeprefix("sha256:")[:8]
    return project, short


@pytest.mark.unit
def test_cli_rollback_without_force_is_dry_run(
    project_with_target_snapshot: tuple[Path, str],
) -> None:
    """Default behavior: preview, exit 1, no file changes."""
    project, short = project_with_target_snapshot

    # Mutate state — rollback would change this back
    drifted = project / "agents" / "a" / "prompt.md"
    drifted.write_text("v2 — should NOT be undone in dry-run")

    result = runner.invoke(app, ["rollback", short])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "dry-run" in combined.lower() or "would roll back" in combined.lower()
    # File still drifted — dry-run is non-destructive
    assert drifted.read_text() == "v2 — should NOT be undone in dry-run"


@pytest.mark.unit
def test_cli_rollback_force_restores_and_announces_pre_snapshot(
    project_with_target_snapshot: tuple[Path, str],
) -> None:
    """--force performs the rollback + prints the pre-snapshot hash
    for round-trip recovery."""
    project, short = project_with_target_snapshot

    drifted = project / "agents" / "a" / "prompt.md"
    drifted.write_text("v2 — soon to be undone")

    result = runner.invoke(app, ["rollback", short, "--force"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "rolled back" in result.stdout.lower()
    # Restored
    assert drifted.read_text() == "v1"
    # Output mentions the pre-rollback hash for forward-recovery
    assert "roll forward" in result.stdout.lower() or "undo" in result.stdout.lower()


@pytest.mark.unit
def test_cli_rollback_unknown_hash_exits_one(
    project_with_target_snapshot: tuple[Path, str],
) -> None:
    """Bad hash → exit 1 + clear error message."""
    result = runner.invoke(app, ["rollback", "ffffffff", "--force"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "no snapshot" in combined.lower()


@pytest.mark.unit
def test_cli_rollback_dry_run_unknown_hash_also_exits_one(
    project_with_target_snapshot: tuple[Path, str],
) -> None:
    """Even the dry-run path resolves the target — bad hash still exits 1."""
    result = runner.invoke(app, ["rollback", "ffffffff"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "no snapshot" in combined.lower()
