"""Sprint O Day 12-13 — `mdk promote` tests.

Three layers:

1. **Store** — :class:`PromotionsLog` round-trips through YAML;
   append-only semantics; rejection of malformed entries.
2. **Helper** — :func:`current_promotion` returns the most recent
   entry; cross-profile entries don't shadow each other.
3. **CLI** — `mdk promote <snap> --to <profile>` records and audits;
   `list` and `current` read back without exposing extra data;
   typo'd profile rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.profiles.store import Profile, ProfileRegistry, save_registry
from movate.promotions import (
    Promotion,
    PromotionsLog,
    PromotionsStoreError,
    current_promotion,
    load_log,
)
from movate.promotions.store import _log_path, record_promotion, save_log
from movate.snapshot import SnapshotManifest, create_snapshot

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~ so the profile registry writes to a temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Project with movate.yaml + one agent."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("api_version: movate/v1\n")
    agents = proj / "agents" / "triage"
    agents.mkdir(parents=True)
    (agents / "agent.yaml").write_text("name: triage\n")
    return proj


@pytest.fixture
def snap(project: Path) -> SnapshotManifest:
    return create_snapshot(project_root=project, description="baseline")


@pytest.fixture
def registered_profiles(isolated_home: Path) -> None:
    """Register dev, staging, prod profiles."""
    registry = ProfileRegistry()
    registry.add(Profile(name="dev"))
    registry.add(Profile(name="staging"))
    registry.add(Profile(name="prod"))
    save_registry(registry)


# ---------------------------------------------------------------------------
# Store: PromotionsLog
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPromotionsLog:
    def test_append_adds_to_promotions_list(self, project: Path) -> None:
        log = PromotionsLog(project_root=project)
        p = Promotion(
            profile="staging",
            snapshot_hash="sha256:abc",
            promoted_at="2026-05-15T10:00:00.000Z",
        )
        log.append(p)
        assert len(log.promotions) == 1
        assert log.promotions[0] == p

    def test_current_returns_most_recent(self, project: Path) -> None:
        log = PromotionsLog(project_root=project)
        log.append(Promotion(profile="prod", snapshot_hash="sha256:abc", promoted_at="1"))
        log.append(Promotion(profile="prod", snapshot_hash="sha256:def", promoted_at="2"))
        log.append(Promotion(profile="prod", snapshot_hash="sha256:ghi", promoted_at="3"))
        latest = log.current("prod")
        assert latest is not None
        assert latest.snapshot_hash == "sha256:ghi"

    def test_current_returns_none_when_no_matches(self, project: Path) -> None:
        log = PromotionsLog(project_root=project)
        log.append(Promotion(profile="prod", snapshot_hash="sha256:abc", promoted_at="1"))
        assert log.current("staging") is None

    def test_for_profile_filters(self, project: Path) -> None:
        log = PromotionsLog(project_root=project)
        log.append(Promotion(profile="prod", snapshot_hash="sha256:a", promoted_at="1"))
        log.append(Promotion(profile="staging", snapshot_hash="sha256:b", promoted_at="2"))
        log.append(Promotion(profile="prod", snapshot_hash="sha256:c", promoted_at="3"))
        prod_only = log.for_profile("prod")
        assert len(prod_only) == 2
        assert all(p.profile == "prod" for p in prod_only)

    def test_short_hash_strips_prefix(self) -> None:
        p = Promotion(
            profile="prod",
            snapshot_hash="sha256:abcdef1234567890",
            promoted_at="1",
        )
        assert p.short_hash == "abcdef12"


# ---------------------------------------------------------------------------
# Persistence: load / save round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPersistence:
    def test_load_missing_returns_empty_log(self, project: Path) -> None:
        log = load_log(project)
        assert log.promotions == []
        assert log.project_root == project

    def test_save_then_load_roundtrips(self, project: Path) -> None:
        log = PromotionsLog(project_root=project)
        log.append(
            Promotion(
                profile="prod",
                snapshot_hash="sha256:abc",
                promoted_at="2026-05-15T10:00:00.000Z",
                promoted_by="alice@laptop",
                description="release v0.7",
                eval_score=0.85,
            )
        )
        save_log(log)

        reloaded = load_log(project)
        assert len(reloaded.promotions) == 1
        p = reloaded.promotions[0]
        assert p.profile == "prod"
        assert p.snapshot_hash == "sha256:abc"
        assert p.description == "release v0.7"
        assert p.eval_score == 0.85

    def test_save_preserves_order(self, project: Path) -> None:
        log = PromotionsLog(project_root=project)
        for i in range(5):
            log.append(
                Promotion(
                    profile="prod",
                    snapshot_hash=f"sha256:{i:08x}",
                    promoted_at=f"2026-05-15T10:00:0{i}.000Z",
                )
            )
        save_log(log)
        reloaded = load_log(project)
        hashes = [p.snapshot_hash for p in reloaded.promotions]
        assert hashes == [f"sha256:{i:08x}" for i in range(5)]

    def test_record_promotion_writes_to_disk(self, project: Path) -> None:
        record_promotion(
            project_root=project,
            profile="staging",
            snapshot_hash="sha256:abc",
            description="ci pipeline test",
        )
        assert _log_path(project).is_file()
        reloaded = load_log(project)
        assert len(reloaded.promotions) == 1
        assert reloaded.promotions[0].promoted_at  # auto-stamped

    def test_load_malformed_yaml_raises(self, project: Path) -> None:
        path = _log_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not: : valid: : yaml:")
        with pytest.raises(PromotionsStoreError):
            load_log(project)

    def test_load_missing_required_field_raises(self, project: Path) -> None:
        path = _log_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "api_version": "movate/v1",
                    "kind": "Promotions",
                    "promotions": [{"profile": "prod"}],  # missing snapshot_hash, promoted_at
                }
            )
        )
        with pytest.raises(PromotionsStoreError, match="missing required field"):
            load_log(project)

    def test_load_bad_eval_score_raises(self, project: Path) -> None:
        path = _log_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "promotions": [
                        {
                            "profile": "prod",
                            "snapshot_hash": "sha256:a",
                            "promoted_at": "x",
                            "eval_score": "not a number",
                        }
                    ]
                }
            )
        )
        with pytest.raises(PromotionsStoreError, match="eval_score"):
            load_log(project)


# ---------------------------------------------------------------------------
# Helper: current_promotion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_current_promotion_returns_latest(project: Path) -> None:
    record_promotion(project_root=project, profile="prod", snapshot_hash="sha256:first")
    record_promotion(project_root=project, profile="prod", snapshot_hash="sha256:second")
    latest = current_promotion(project, "prod")
    assert latest is not None
    assert latest.snapshot_hash == "sha256:second"


@pytest.mark.unit
def test_current_promotion_returns_none_for_unknown_profile(project: Path) -> None:
    record_promotion(project_root=project, profile="prod", snapshot_hash="sha256:a")
    assert current_promotion(project, "ghost") is None


# ---------------------------------------------------------------------------
# CLI — promote (record)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_promote_records_to_disk(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "promote",
            short,
            "--to",
            "staging",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "promotion recorded" in result.stdout.lower()
    log = load_log(project)
    assert len(log.promotions) == 1
    assert log.promotions[0].profile == "staging"


@pytest.mark.unit
def test_cli_promote_unknown_profile_exits_2(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(app, ["promote", short, "--to", "ghost", "--project-root", str(project)])
    assert result.exit_code == 2
    # Nothing written
    log = load_log(project)
    assert log.promotions == []


@pytest.mark.unit
def test_cli_promote_unknown_snapshot_exits_1(project: Path, registered_profiles: None) -> None:
    result = runner.invoke(
        app, ["promote", "deadbeef", "--to", "staging", "--project-root", str(project)]
    )
    assert result.exit_code == 1


@pytest.mark.unit
def test_cli_promote_dry_run_does_not_write(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "promote",
            short,
            "--to",
            "prod",
            "--dry-run",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    # Log is still empty
    assert load_log(project).promotions == []


@pytest.mark.unit
def test_cli_promote_with_eval_pass_rate_records_score(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "promote",
            short,
            "--to",
            "prod",
            "--eval-pass-rate",
            "0.85",
            "--description",
            "release v0.7",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    log = load_log(project)
    assert log.promotions[0].eval_score == 0.85
    assert log.promotions[0].description == "release v0.7"


@pytest.mark.unit
def test_cli_promote_eval_pass_rate_out_of_range_exits_2(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "promote",
            short,
            "--to",
            "staging",
            "--eval-pass-rate",
            "1.5",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_promote_missing_snap_exits_2(project: Path, registered_profiles: None) -> None:
    result = runner.invoke(app, ["promote", "--to", "staging", "--project-root", str(project)])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_promote_missing_to_exits_2(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    result = runner.invoke(app, ["promote", short, "--project-root", str(project)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# CLI — promote --list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_promote_list_empty_prints_hint(project: Path, registered_profiles: None) -> None:
    result = runner.invoke(app, ["promote", "--list", "--project-root", str(project)])
    assert result.exit_code == 0
    assert "no promotions" in result.stdout.lower()


@pytest.mark.unit
def test_cli_promote_list_shows_all_entries(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    runner.invoke(app, ["promote", short, "--to", "staging", "--project-root", str(project)])
    runner.invoke(app, ["promote", short, "--to", "prod", "--project-root", str(project)])

    result = runner.invoke(app, ["promote", "--list", "--project-root", str(project)])
    assert result.exit_code == 0
    assert "staging" in result.stdout
    assert "prod" in result.stdout
    assert short in result.stdout


@pytest.mark.unit
def test_cli_promote_list_filtered_by_profile(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    runner.invoke(app, ["promote", short, "--to", "staging", "--project-root", str(project)])
    runner.invoke(app, ["promote", short, "--to", "prod", "--project-root", str(project)])

    result = runner.invoke(
        app,
        ["promote", "--list", "--profile", "prod", "--project-root", str(project)],
    )
    assert result.exit_code == 0
    # Header should reference prod specifically
    assert "prod" in result.stdout


# ---------------------------------------------------------------------------
# CLI — promote --current
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_promote_current_no_promotion_exits_1(project: Path, registered_profiles: None) -> None:
    result = runner.invoke(app, ["promote", "--current", "prod", "--project-root", str(project)])
    assert result.exit_code == 1


@pytest.mark.unit
def test_cli_promote_current_shows_latest(
    project: Path, snap: SnapshotManifest, registered_profiles: None
) -> None:
    short = snap.hash.removeprefix("sha256:")[:8]
    runner.invoke(
        app,
        [
            "promote",
            short,
            "--to",
            "prod",
            "--description",
            "v0.6",
            "--project-root",
            str(project),
        ],
    )
    runner.invoke(
        app,
        [
            "promote",
            short,
            "--to",
            "prod",
            "--description",
            "v0.7",
            "--project-root",
            str(project),
        ],
    )

    result = runner.invoke(app, ["promote", "--current", "prod", "--project-root", str(project)])
    assert result.exit_code == 0
    # Latest description visible
    assert "v0.7" in result.stdout
