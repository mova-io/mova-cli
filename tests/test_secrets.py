"""Sprint O Day 4-7 — `mdk secrets` tests.

Three layers:

1. **Store** — :class:`SecretsStore` round-trips through YAML;
   file mode is 0600 after save; load is permissive (missing file =
   empty store) but strict on malformed; set on existing name
   bumps last_rotated but preserves created_at.
2. **Profile resolution** — uses active profile by default; rejects
   when neither active nor --profile is set.
3. **CLI** — set / get / list / delete / export-shell with safety
   gates (`delete --force` dry-run; `list` never shows values).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.profiles.store import (
    Profile,
    ProfileRegistry,
    save_registry,
    set_active_profile,
)
from movate.secrets import (
    SecretNotFoundError,
    SecretsStore,
    SecretsStoreError,
    load_store,
)
from movate.secrets.store import _store_path, save_store

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~ to a temp dir so each test has clean ~/.movate state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def active_dev_profile(isolated_home: Path) -> Path:
    """Create a 'dev' profile + activate it. Most tests need a profile
    context to operate against."""
    registry = ProfileRegistry()
    registry.add(Profile(name="dev", description="test"))
    save_registry(registry)
    set_active_profile("dev")
    return isolated_home


# ---------------------------------------------------------------------------
# Store: SecretsStore
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSecretsStore:
    def test_set_creates_new_secret_with_timestamp(self) -> None:
        store = SecretsStore(profile="dev")
        secret = store.set("FOO", "bar", description="test")
        assert secret.name == "FOO"
        assert secret.value == "bar"
        assert secret.description == "test"
        assert secret.created_at != ""
        assert secret.last_rotated == ""  # never rotated yet

    def test_set_on_existing_preserves_created_at_bumps_rotated(self) -> None:
        store = SecretsStore(profile="dev")
        first = store.set("FOO", "v1")
        original_created = first.created_at

        # Reset with new value
        second = store.set("FOO", "v2")
        assert second.created_at == original_created
        assert second.last_rotated != ""

    def test_set_on_existing_preserves_description_when_blank(self) -> None:
        """Rotating without --description shouldn't wipe the existing one."""
        store = SecretsStore(profile="dev")
        store.set("FOO", "v1", description="initial desc")
        second = store.set("FOO", "v2")  # no description arg
        assert second.description == "initial desc"

    def test_get_raises_when_missing(self) -> None:
        store = SecretsStore(profile="dev")
        with pytest.raises(SecretNotFoundError, match="not found"):
            store.get("ghost")

    def test_delete_returns_removed(self) -> None:
        store = SecretsStore(profile="dev")
        store.set("FOO", "v")
        removed = store.delete("FOO")
        assert removed.name == "FOO"
        assert "FOO" not in store.secrets

    def test_names_sorted_no_values(self) -> None:
        store = SecretsStore(profile="dev")
        store.set("ZEBRA", "z")
        store.set("AARDVARK", "a")
        store.set("MONKEY", "m")
        # Plain list of names, no values exposed
        assert store.names() == ["AARDVARK", "MONKEY", "ZEBRA"]


# ---------------------------------------------------------------------------
# Store: load / save / file permissions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStorePersistence:
    def test_load_missing_returns_empty_store(self, isolated_home: Path) -> None:
        store = load_store("dev")
        assert store.profile == "dev"
        assert store.secrets == {}

    def test_save_then_load_roundtrips(self, isolated_home: Path) -> None:
        store = SecretsStore(profile="dev")
        store.set("FOO", "bar", description="test")
        save_store(store)

        reloaded = load_store("dev")
        assert reloaded.profile == "dev"
        assert reloaded.get("FOO").value == "bar"
        assert reloaded.get("FOO").description == "test"

    def test_saved_file_is_chmod_0600(self, isolated_home: Path) -> None:
        """The single most important safety gate at MVP — file perms
        are the only line of defense at rest."""
        store = SecretsStore(profile="dev")
        store.set("FOO", "supersecret")
        save_store(store)

        path = _store_path("dev")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_saved_directory_is_user_only(self, isolated_home: Path) -> None:
        """Even the directory should be tight — group/other shouldn't
        be able to list secret filenames."""
        store = SecretsStore(profile="dev")
        store.set("FOO", "v")
        save_store(store)

        secrets_dir = _store_path("dev").parent
        mode = secrets_dir.stat().st_mode & 0o777
        # Must not have group or world bits
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)

    def test_load_malformed_yaml_raises(self, isolated_home: Path) -> None:
        path = _store_path("dev")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not: : valid: yaml: :")
        with pytest.raises(SecretsStoreError):
            load_store("dev")

    def test_load_missing_value_field_raises(self, isolated_home: Path) -> None:
        """Each secret entry must have a value field."""
        path = _store_path("dev")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "api_version": "movate/v1",
                    "kind": "Secrets",
                    "profile": "dev",
                    "secrets": {"FOO": {"description": "no value!"}},
                }
            )
        )
        with pytest.raises(SecretsStoreError, match="missing required field 'value'"):
            load_store("dev")


# ---------------------------------------------------------------------------
# CLI — set
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_set_stores_value_in_active_profile(
    active_dev_profile: Path,
) -> None:
    result = runner.invoke(app, ["secrets", "set", "OPENAI_API_KEY", "--value", "sk-test"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "stored" in result.stdout.lower()
    # Warning printed on every set
    assert "unencrypted" in result.stdout.lower()
    # Verify on disk
    store = load_store("dev")
    assert store.get("OPENAI_API_KEY").value == "sk-test"


@pytest.mark.unit
def test_cli_set_on_existing_says_rotated(active_dev_profile: Path) -> None:
    runner.invoke(app, ["secrets", "set", "FOO", "--value", "v1"])
    result = runner.invoke(app, ["secrets", "set", "FOO", "--value", "v2"])
    assert result.exit_code == 0
    assert "rotated" in result.stdout.lower()
    assert load_store("dev").get("FOO").value == "v2"


@pytest.mark.unit
def test_cli_set_empty_value_exits_two(active_dev_profile: Path) -> None:
    """An empty --value is a user error — secret with no value is nonsense."""
    result = runner.invoke(app, ["secrets", "set", "FOO", "--value", ""])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_set_no_active_profile_exits_two(isolated_home: Path) -> None:
    """No active profile + no --profile = clean error, not a silent default."""
    result = runner.invoke(app, ["secrets", "set", "FOO", "--value", "x"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "no active profile" in combined.lower()


@pytest.mark.unit
def test_cli_set_profile_override_works(isolated_home: Path) -> None:
    """--profile overrides the active marker."""
    registry = ProfileRegistry()
    registry.add(Profile(name="prod"))
    save_registry(registry)
    # No active profile set
    result = runner.invoke(app, ["secrets", "set", "FOO", "--value", "v", "--profile", "prod"])
    assert result.exit_code == 0
    assert "prod" in result.stdout
    assert load_store("prod").get("FOO").value == "v"


@pytest.mark.unit
def test_cli_set_unknown_profile_exits_two(active_dev_profile: Path) -> None:
    """--profile pointing at an unregistered profile rejects loudly."""
    result = runner.invoke(app, ["secrets", "set", "FOO", "--value", "v", "--profile", "ghost"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# CLI — get
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_get_returns_raw_value_to_stdout(active_dev_profile: Path) -> None:
    """get's output must be plain (no Rich) — command substitution
    captures into $VAR cleanly."""
    runner.invoke(app, ["secrets", "set", "FOO", "--value", "sk-abcdef"])
    result = runner.invoke(app, ["secrets", "get", "FOO"])
    assert result.exit_code == 0
    # Trailing newline OK; otherwise the value should be the entire content
    assert result.stdout.strip() == "sk-abcdef"


@pytest.mark.unit
def test_cli_get_unknown_secret_exits_one(active_dev_profile: Path) -> None:
    result = runner.invoke(app, ["secrets", "get", "GHOST"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI — list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_list_empty_profile_prints_hint(active_dev_profile: Path) -> None:
    result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    assert "no secrets" in result.stdout.lower()


@pytest.mark.unit
def test_cli_list_shows_names_not_values(active_dev_profile: Path) -> None:
    """The most security-critical assertion: list never echoes a value."""
    runner.invoke(app, ["secrets", "set", "FOO", "--value", "supersecretvalue"])
    runner.invoke(app, ["secrets", "set", "BAR", "--value", "anothervalue", "-d", "my desc"])
    result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    # Names appear
    assert "FOO" in result.stdout
    assert "BAR" in result.stdout
    # Description appears
    assert "my desc" in result.stdout
    # Values DO NOT appear
    assert "supersecretvalue" not in result.stdout
    assert "anothervalue" not in result.stdout


# ---------------------------------------------------------------------------
# CLI — delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_delete_without_force_is_dry_run(active_dev_profile: Path) -> None:
    runner.invoke(app, ["secrets", "set", "FOO", "--value", "v"])
    result = runner.invoke(app, ["secrets", "delete", "FOO"])
    assert result.exit_code == 1
    assert "dry-run" in result.stdout.lower() or "would delete" in result.stdout.lower()
    # Still present
    assert "FOO" in load_store("dev").secrets


@pytest.mark.unit
def test_cli_delete_force_removes(active_dev_profile: Path) -> None:
    runner.invoke(app, ["secrets", "set", "FOO", "--value", "v"])
    result = runner.invoke(app, ["secrets", "delete", "FOO", "--force"])
    assert result.exit_code == 0
    assert "FOO" not in load_store("dev").secrets


@pytest.mark.unit
def test_cli_delete_unknown_exits_one(active_dev_profile: Path) -> None:
    result = runner.invoke(app, ["secrets", "delete", "GHOST", "--force"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI — export-shell
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_export_shell_emits_export_statements(active_dev_profile: Path) -> None:
    runner.invoke(app, ["secrets", "set", "FOO", "--value", "v1"])
    runner.invoke(app, ["secrets", "set", "BAR", "--value", "v2"])

    result = runner.invoke(app, ["secrets", "export-shell"])
    assert result.exit_code == 0
    assert "export FOO='v1'" in result.stdout
    assert "export BAR='v2'" in result.stdout


@pytest.mark.unit
def test_cli_export_shell_escapes_single_quotes(active_dev_profile: Path) -> None:
    """A value with single quotes shouldn't break the shell syntax."""
    runner.invoke(app, ["secrets", "set", "TRICKY", "--value", "ab'cd'ef"])
    result = runner.invoke(app, ["secrets", "export-shell"])
    assert result.exit_code == 0
    # Standard shell-quoting trick: '"'"' breaks out of single-quote,
    # quotes the literal quote, then re-enters single-quote.
    assert "'\"'\"'" in result.stdout


# ---------------------------------------------------------------------------
# CLI — where (diagnostic)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_where_shows_path_and_mode(active_dev_profile: Path) -> None:
    runner.invoke(app, ["secrets", "set", "FOO", "--value", "v"])
    result = runner.invoke(app, ["secrets", "where"])
    assert result.exit_code == 0
    assert "dev" in result.stdout
    assert "secrets" in result.stdout
    # Should report the correct mode
    assert "0o600" in result.stdout
