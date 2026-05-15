"""Sprint O Day 1-3 — `mdk profiles` tests.

Two layers:

1. **Store** — :class:`ProfileRegistry` round-trips through
   ``~/.movate/profiles.yaml``; active-profile marker reads/writes;
   load is permissive (empty file = empty registry) but strict on
   malformed YAML.
2. **CLI** — `mdk profiles {list, show, use, create, delete}`
   commands render correctly, exit cleanly on errors, and ``@active``
   sugar resolves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.profiles import (
    Profile,
    ProfileNotFoundError,
    ProfileRegistry,
    ProfileStoreError,
    get_active_profile,
    load_registry,
    set_active_profile,
)
from movate.profiles.store import save_registry

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures — isolate $HOME so each test gets its own ~/.movate/
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ~ at a temp dir so each test gets a fresh ~/.movate/.

    Same pattern test_cli_list / test_cli_explain use — keeps the
    real user config untouched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Store: Profile + ProfileRegistry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProfile:
    def test_effective_tenant_id_falls_back_to_name(self) -> None:
        p = Profile(name="dev")
        assert p.effective_tenant_id == "dev"

    def test_effective_tenant_id_uses_explicit_value(self) -> None:
        p = Profile(name="prod", tenant_id="acme-prod")
        assert p.effective_tenant_id == "acme-prod"


@pytest.mark.unit
class TestProfileRegistry:
    def test_add_and_get(self) -> None:
        registry = ProfileRegistry()
        registry.add(Profile(name="dev"))
        assert registry.get("dev").name == "dev"

    def test_add_replaces_existing(self) -> None:
        registry = ProfileRegistry()
        registry.add(Profile(name="dev", description="v1"))
        registry.add(Profile(name="dev", description="v2"))
        assert registry.get("dev").description == "v2"
        assert len(registry.profiles) == 1

    def test_remove(self) -> None:
        registry = ProfileRegistry()
        registry.add(Profile(name="dev"))
        removed = registry.remove("dev")
        assert removed.name == "dev"
        assert "dev" not in registry.profiles

    def test_get_missing_raises(self) -> None:
        with pytest.raises(ProfileNotFoundError, match="not found"):
            ProfileRegistry().get("ghost")

    def test_list_names_is_sorted(self) -> None:
        registry = ProfileRegistry()
        for name in ("prod", "dev", "staging"):
            registry.add(Profile(name=name))
        assert registry.list_names() == ["dev", "prod", "staging"]


# ---------------------------------------------------------------------------
# Store: load / save / active marker
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadSave:
    def test_load_missing_file_returns_empty_registry(self, isolated_home: Path) -> None:
        """Permissive default — fresh installs don't need a config file."""
        registry = load_registry()
        assert registry.profiles == {}

    def test_save_then_load_roundtrips(self, isolated_home: Path) -> None:
        original = ProfileRegistry()
        original.add(
            Profile(
                name="dev",
                target="dev-runtime",
                tenant_id="movate-dev",
                description="Movate dev",
            )
        )
        save_registry(original)
        reloaded = load_registry()
        assert reloaded.get("dev").target == "dev-runtime"
        assert reloaded.get("dev").description == "Movate dev"

    def test_load_malformed_yaml_raises_store_error(self, isolated_home: Path) -> None:
        """Malformed YAML must fail loud — not silently treat as empty."""
        movate_dir = isolated_home / ".movate"
        movate_dir.mkdir()
        (movate_dir / "profiles.yaml").write_text("not: : valid: : yaml")
        with pytest.raises(ProfileStoreError):
            load_registry()

    def test_load_missing_name_raises(self, isolated_home: Path) -> None:
        """A profile entry without `name` is unusable; fail at load time."""
        movate_dir = isolated_home / ".movate"
        movate_dir.mkdir()
        (movate_dir / "profiles.yaml").write_text(
            yaml.safe_dump(
                {
                    "api_version": "movate/v1",
                    "kind": "Profiles",
                    "profiles": [{"target": "x"}],  # missing name
                }
            )
        )
        with pytest.raises(ProfileStoreError, match="missing required field 'name'"):
            load_registry()

    def test_save_uses_atomic_temp_rename(self, isolated_home: Path) -> None:
        """Confirm no leftover .tmp file after save (atomic rename worked)."""
        registry = ProfileRegistry()
        registry.add(Profile(name="dev"))
        save_registry(registry)
        movate_dir = isolated_home / ".movate"
        assert (movate_dir / "profiles.yaml").is_file()
        assert not (movate_dir / "profiles.yaml.tmp").exists()


@pytest.mark.unit
class TestActiveMarker:
    def test_no_active_returns_none(self, isolated_home: Path) -> None:
        assert get_active_profile() is None

    def test_set_then_get(self, isolated_home: Path) -> None:
        set_active_profile("dev")
        assert get_active_profile() == "dev"

    def test_clear_via_none(self, isolated_home: Path) -> None:
        set_active_profile("dev")
        set_active_profile(None)
        assert get_active_profile() is None

    def test_set_replaces_previous(self, isolated_home: Path) -> None:
        set_active_profile("dev")
        set_active_profile("prod")
        assert get_active_profile() == "prod"


# ---------------------------------------------------------------------------
# CLI — list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_list_empty_renders_hint(isolated_home: Path) -> None:
    result = runner.invoke(app, ["profiles", "list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no profiles" in result.stdout.lower()
    assert "mdk profiles create" in result.stdout


@pytest.mark.unit
def test_cli_list_renders_registered_profiles(isolated_home: Path) -> None:
    runner.invoke(app, ["profiles", "create", "dev", "-d", "Movate dev"])
    runner.invoke(app, ["profiles", "create", "prod", "-d", "Production"])
    result = runner.invoke(app, ["profiles", "list"])
    assert result.exit_code == 0
    assert "dev" in result.stdout
    assert "prod" in result.stdout
    assert "Movate dev" in result.stdout


@pytest.mark.unit
def test_cli_list_marks_active_profile(isolated_home: Path) -> None:
    runner.invoke(app, ["profiles", "create", "dev"])
    runner.invoke(app, ["profiles", "create", "prod"])
    runner.invoke(app, ["profiles", "use", "prod"])
    result = runner.invoke(app, ["profiles", "list"])
    # The active marker `*` should appear near the prod row
    assert "*" in result.stdout


# ---------------------------------------------------------------------------
# CLI — show
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_show_renders_full_profile(isolated_home: Path) -> None:
    runner.invoke(
        app,
        [
            "profiles",
            "create",
            "dev",
            "--target",
            "dev-runtime",
            "--tenant",
            "movate-dev",
            "-d",
            "Test profile",
        ],
    )
    result = runner.invoke(app, ["profiles", "show", "dev"])
    assert result.exit_code == 0
    assert "dev-runtime" in result.stdout
    assert "movate-dev" in result.stdout
    assert "Test profile" in result.stdout


@pytest.mark.unit
def test_cli_show_active_sugar(isolated_home: Path) -> None:
    """`@active` resolves to the currently-active profile name."""
    runner.invoke(app, ["profiles", "create", "dev", "--use"])
    result = runner.invoke(app, ["profiles", "show", "@active"])
    assert result.exit_code == 0
    assert "dev" in result.stdout


@pytest.mark.unit
def test_cli_show_active_with_no_active_exits_one(isolated_home: Path) -> None:
    result = runner.invoke(app, ["profiles", "show", "@active"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "no active profile" in combined.lower()


@pytest.mark.unit
def test_cli_show_unknown_profile_exits_one(isolated_home: Path) -> None:
    result = runner.invoke(app, ["profiles", "show", "ghost"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower()


# ---------------------------------------------------------------------------
# CLI — use
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_use_activates_profile(isolated_home: Path) -> None:
    runner.invoke(app, ["profiles", "create", "dev"])
    result = runner.invoke(app, ["profiles", "use", "dev"])
    assert result.exit_code == 0
    assert get_active_profile() == "dev"


@pytest.mark.unit
def test_cli_use_unknown_profile_exits_one(isolated_home: Path) -> None:
    result = runner.invoke(app, ["profiles", "use", "ghost"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower()


# ---------------------------------------------------------------------------
# CLI — create
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_create_minimal_profile(isolated_home: Path) -> None:
    result = runner.invoke(app, ["profiles", "create", "local"])
    assert result.exit_code == 0
    registry = load_registry()
    assert "local" in registry.profiles


@pytest.mark.unit
def test_cli_create_full_profile_with_use_flag(isolated_home: Path) -> None:
    """--use activates the new profile immediately."""
    result = runner.invoke(
        app,
        [
            "profiles",
            "create",
            "prod",
            "--target",
            "prod-runtime",
            "--tenant",
            "acme-prod",
            "-d",
            "Production",
            "--use",
        ],
    )
    assert result.exit_code == 0
    profile = load_registry().get("prod")
    assert profile.target == "prod-runtime"
    assert profile.tenant_id == "acme-prod"
    assert get_active_profile() == "prod"


@pytest.mark.unit
def test_cli_create_is_idempotent_update(isolated_home: Path) -> None:
    """Re-creating with same name updates the description (no duplicate)."""
    runner.invoke(app, ["profiles", "create", "dev", "-d", "v1"])
    result = runner.invoke(app, ["profiles", "create", "dev", "-d", "v2"])
    assert result.exit_code == 0
    assert "updated" in result.stdout.lower()
    assert load_registry().get("dev").description == "v2"


@pytest.mark.unit
def test_cli_create_invalid_name_exits_two(isolated_home: Path) -> None:
    """Uppercase / underscores / spaces rejected at parse time."""
    # Note: pattern matches existing agent-name convention (allows
    # numeric start), so "1-foo" is intentionally accepted.
    for bad in ("Dev", "dev_thing", "Web Search", ""):
        result = runner.invoke(app, ["profiles", "create", bad])
        assert result.exit_code == 2, f"unexpectedly accepted {bad!r}"


# ---------------------------------------------------------------------------
# CLI — delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_delete_without_force_is_dry_run(isolated_home: Path) -> None:
    runner.invoke(app, ["profiles", "create", "doomed"])
    result = runner.invoke(app, ["profiles", "delete", "doomed"])
    assert result.exit_code == 1
    assert "dry-run" in result.stdout.lower() or "would delete" in result.stdout.lower()
    # Still present
    assert "doomed" in load_registry().profiles


@pytest.mark.unit
def test_cli_delete_force_removes_profile(isolated_home: Path) -> None:
    runner.invoke(app, ["profiles", "create", "doomed"])
    result = runner.invoke(app, ["profiles", "delete", "doomed", "--force"])
    assert result.exit_code == 0
    assert "doomed" not in load_registry().profiles


@pytest.mark.unit
def test_cli_delete_active_profile_clears_active_marker(
    isolated_home: Path,
) -> None:
    """Deleting the active profile must clear the active marker —
    no dangling 'active=X' state pointing at a deleted entry."""
    runner.invoke(app, ["profiles", "create", "dev", "--use"])
    assert get_active_profile() == "dev"
    runner.invoke(app, ["profiles", "delete", "dev", "--force"])
    assert get_active_profile() is None


@pytest.mark.unit
def test_cli_delete_unknown_profile_exits_one(isolated_home: Path) -> None:
    result = runner.invoke(app, ["profiles", "delete", "ghost", "--force"])
    assert result.exit_code == 1
