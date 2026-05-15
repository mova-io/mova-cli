"""``mdk config set-project-dir`` — pin a default location for new projects.

Stops operators from typing ``--at ~/projects`` on every
``mdk init --project`` invocation. Stored in ``~/.movate/config.yaml``
alongside deployment targets (semantically a preference, not a secret —
so it lives in config.yaml, not the credentials file).

The resolution order is:

1. Explicit ``--at`` / ``--target`` on the invocation (always wins).
2. ``default_project_dir`` from ``~/.movate/config.yaml`` (this PR).
3. Current working directory (the historical default).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.user_config import (
    UserConfig,
    resolve_default_project_dir,
)

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point MOVATE_CONFIG_PATH at a tmp file so each test starts clean."""
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(path))
    return path


# ---------------------------------------------------------------------------
# resolve_default_project_dir() — pure helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveDefaultProjectDir:
    def test_returns_none_when_unset(self, isolated_user_config: Path) -> None:
        """Empty config → None (NOT cwd) so the caller can distinguish."""
        assert resolve_default_project_dir() is None

    def test_expands_tilde_and_envvars(
        self,
        isolated_user_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``~`` and ``$VAR`` are resolved at READ time, not write time."""
        # Point $HOME at tmp_path so ~ is predictable.
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        # Stash a config with a tilde-path.
        isolated_user_config.write_text(yaml.safe_dump({"default_project_dir": "~/projects"}))
        result = resolve_default_project_dir()
        assert result == (home_dir / "projects").resolve()

    def test_absolute_path_passes_through(self, isolated_user_config: Path, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit"
        isolated_user_config.write_text(yaml.safe_dump({"default_project_dir": str(explicit)}))
        result = resolve_default_project_dir()
        assert result == explicit.resolve()

    def test_malformed_config_returns_none_not_raise(self, isolated_user_config: Path) -> None:
        """Malformed config shouldn't break ``mdk init``. The error
        surfaces via ``mdk config show``; init silently falls back."""
        isolated_user_config.write_text(": :: bogus YAML :::")
        assert resolve_default_project_dir() is None


# ---------------------------------------------------------------------------
# mdk config set-project-dir / get-project-dir
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigSetProjectDir:
    def test_set_writes_to_config_yaml(self, isolated_user_config: Path, tmp_path: Path) -> None:
        target = tmp_path / "my-workspace"
        result = runner.invoke(
            app,
            ["config", "set-project-dir", str(target)],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Config file written with the raw path.
        data = yaml.safe_load(isolated_user_config.read_text())
        assert data["default_project_dir"] == str(target)

    def test_clear_removes_the_field(self, isolated_user_config: Path, tmp_path: Path) -> None:
        target = tmp_path / "my-workspace"
        runner.invoke(app, ["config", "set-project-dir", str(target)])
        # Now clear it.
        result = runner.invoke(app, ["config", "set-project-dir", "ignored", "--clear"])
        assert result.exit_code == 0
        data = yaml.safe_load(isolated_user_config.read_text()) or {}
        assert data.get("default_project_dir") is None

    def test_get_reports_unset(self, isolated_user_config: Path) -> None:
        result = runner.invoke(app, ["config", "get-project-dir"])
        assert result.exit_code == 0
        assert "unset" in result.stdout.lower()

    def test_get_reports_resolved_path(self, isolated_user_config: Path, tmp_path: Path) -> None:
        target = tmp_path / "my-workspace"
        runner.invoke(app, ["config", "set-project-dir", str(target)])
        # COLUMNS=300 keeps Rich from wrapping long tmp-paths across
        # lines (tmp_path on macOS is /private/var/folders/...).
        result = runner.invoke(app, ["config", "get-project-dir"], env={"COLUMNS": "300"})
        assert result.exit_code == 0
        # Output mentions both the raw and the resolved form.
        assert str(target) in result.stdout

    def test_round_trip_via_usermodel(self, isolated_user_config: Path, tmp_path: Path) -> None:
        """The new field round-trips through UserConfig serialization."""
        cfg = UserConfig(default_project_dir=str(tmp_path / "x"))
        dumped = cfg.model_dump(exclude_none=True)
        # `targets: {}` survives `exclude_none` (it's an empty dict, not
        # None) — assert on the field we care about, not the whole shape.
        assert dumped.get("default_project_dir") == str(tmp_path / "x")
        # And re-parses.
        cfg2 = UserConfig.model_validate(dumped)
        assert cfg2.default_project_dir == str(tmp_path / "x")


# ---------------------------------------------------------------------------
# mdk init --project uses the configured dir
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitUsesDefaultDir:
    def test_init_project_uses_configured_dir_when_no_at_flag(
        self,
        isolated_user_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Set a default dir, then `mdk init --project foo` (no --at)
        should land foo/ under that dir, not under cwd."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        runner.invoke(app, ["config", "set-project-dir", str(workspace)])

        # cwd is somewhere ELSE — not the workspace.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = runner.invoke(
            app,
            ["init", "--project", "foo", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Project landed in the configured dir, NOT in cwd.
        assert (workspace / "foo" / "movate.yaml").is_file()
        assert not (elsewhere / "foo").exists()

    def test_explicit_at_overrides_configured_dir(
        self,
        isolated_user_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--at`` always wins, even when default_project_dir is set."""
        configured = tmp_path / "configured"
        explicit = tmp_path / "explicit"
        configured.mkdir()
        explicit.mkdir()
        runner.invoke(app, ["config", "set-project-dir", str(configured)])
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "init",
                "--project",
                "foo",
                "--skip-snapshot",
                "--at",
                str(explicit),
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # Project landed under --at, NOT under the configured dir.
        assert (explicit / "foo" / "movate.yaml").is_file()
        assert not (configured / "foo").exists()

    def test_no_configured_dir_falls_back_to_cwd(
        self,
        isolated_user_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a configured dir, behavior is unchanged: project
        lands in cwd."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--project", "foo", "--skip-snapshot"])
        assert result.exit_code == 0
        assert (tmp_path / "foo" / "movate.yaml").is_file()

    def test_resolution_note_appears_in_output(
        self,
        isolated_user_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the configured dir kicks in, the operator gets a dim
        stderr note explaining where the project landed and why."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        runner.invoke(app, ["config", "set-project-dir", str(workspace)])
        monkeypatch.chdir(tmp_path / "elsewhere" if (tmp_path / "elsewhere").is_dir() else tmp_path)

        result = runner.invoke(
            app,
            ["init", "--project", "foo", "--skip-snapshot"],
            env={"COLUMNS": "400"},  # wide enough that the dim note doesn't wrap
        )
        assert result.exit_code == 0
        combined = result.stdout + result.stderr
        # The dim note mentions both the resolved dir AND the override mechanism.
        assert "configured default project dir" in combined
        assert "mdk config set-project-dir" in combined
