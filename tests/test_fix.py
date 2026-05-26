"""Sprint P — `mdk fix` tests.

Three layers:

1. **Individual fixes** — each `check` correctly detects the
   condition, each `apply` is idempotent + writes the expected file
   in apply mode (and only previews in dry-run).
2. **Dispatcher** — `diagnose_and_fix` honors --only / --skip,
   continues past a failed fix, and returns the right statuses.
3. **CLI** — `mdk fix` defaults to dry-run, `--apply` writes,
   `--list` shows the catalog, exit code reflects failures.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.credentials.store import CredentialsStore
from movate.fixes import FixStatus, available_fixes, diagnose_and_fix
from movate.fixes import registry as reg_mod

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A real movate project (movate.yaml present) — most fixes target this."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\nkind: Project\n")
    return tmp_path


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~ for tests that touch ~/.movate/secrets/."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Individual fixes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnsureMovateDir:
    def test_check_returns_true_when_missing(self, tmp_path: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-movate-dir")
        assert fix.check(tmp_path) is True

    def test_check_returns_false_when_present(self, tmp_path: Path) -> None:
        (tmp_path / ".movate").mkdir()
        fix = next(f for f in available_fixes() if f.id == "ensure-movate-dir")
        assert fix.check(tmp_path) is False

    def test_apply_creates_directory(self, tmp_path: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-movate-dir")
        result = fix.run(tmp_path, dry_run=False)
        assert result.status is FixStatus.APPLIED
        assert (tmp_path / ".mdk").is_dir()

    def test_dry_run_does_not_create(self, tmp_path: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-movate-dir")
        result = fix.run(tmp_path, dry_run=True)
        assert result.status is FixStatus.WOULD_APPLY
        assert not (tmp_path / ".mdk").exists()

    def test_idempotent_in_apply_mode(self, tmp_path: Path) -> None:
        """Running fix twice on a clean tree is a no-op."""
        fix = next(f for f in available_fixes() if f.id == "ensure-movate-dir")
        fix.run(tmp_path, dry_run=False)
        second = fix.run(tmp_path, dry_run=False)
        # Second run reports NOT_NEEDED, doesn't error
        assert second.status is FixStatus.NOT_NEEDED


@pytest.mark.unit
class TestEnsureGitignore:
    def test_creates_gitignore_with_movate_ignores(self, tmp_path: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-gitignore")
        result = fix.run(tmp_path, dry_run=False)
        assert result.status is FixStatus.APPLIED
        text = (tmp_path / ".gitignore").read_text()
        assert ".mdk/local.db" in text
        assert ".env" in text

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("operator's custom ignores\n")
        fix = next(f for f in available_fixes() if f.id == "ensure-gitignore")
        result = fix.run(tmp_path, dry_run=False)
        # Already exists → not needed → no write
        assert result.status is FixStatus.NOT_NEEDED
        assert (tmp_path / ".gitignore").read_text() == "operator's custom ignores\n"


@pytest.mark.unit
class TestEnsureEnvFromExample:
    def test_check_returns_false_without_env_example(self, tmp_path: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-env-from-example")
        # No .env.example present
        assert fix.check(tmp_path) is False

    def test_check_returns_true_when_example_but_no_env(self, tmp_path: Path) -> None:
        (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n")
        fix = next(f for f in available_fixes() if f.id == "ensure-env-from-example")
        assert fix.check(tmp_path) is True

    def test_apply_copies_example_to_env(self, tmp_path: Path) -> None:
        (tmp_path / ".env.example").write_text("FOO=\n")
        fix = next(f for f in available_fixes() if f.id == "ensure-env-from-example")
        result = fix.run(tmp_path, dry_run=False)
        assert result.status is FixStatus.APPLIED
        assert (tmp_path / ".env").read_text() == "FOO=\n"


@pytest.mark.unit
class TestEnsureAgentsDir:
    def test_check_returns_false_without_movate_yaml(self, tmp_path: Path) -> None:
        """Won't touch agents/ in non-movate directories."""
        fix = next(f for f in available_fixes() if f.id == "ensure-agents-dir")
        assert fix.check(tmp_path) is False

    def test_check_returns_true_in_movate_project_without_agents(self, project_root: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-agents-dir")
        assert fix.check(project_root) is True

    def test_apply_creates_agents_with_gitkeep(self, project_root: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-agents-dir")
        result = fix.run(project_root, dry_run=False)
        assert result.status is FixStatus.APPLIED
        assert (project_root / "agents").is_dir()
        assert (project_root / "agents" / ".gitkeep").is_file()


@pytest.mark.unit
class TestFixSecretsPermissions:
    def test_check_returns_false_without_secrets_dir(
        self, tmp_path: Path, isolated_home: Path
    ) -> None:
        fix = next(f for f in available_fixes() if f.id == "fix-secrets-permissions")
        assert fix.check(tmp_path) is False

    def test_check_returns_true_when_file_has_wrong_perms(
        self, tmp_path: Path, isolated_home: Path
    ) -> None:
        secrets_dir = isolated_home / ".movate" / "secrets"
        secrets_dir.mkdir(parents=True)
        secret_file = secrets_dir / "dev.yaml"
        secret_file.write_text("secrets: {}\n")
        os.chmod(secret_file, 0o644)  # group/world readable — bad

        fix = next(f for f in available_fixes() if f.id == "fix-secrets-permissions")
        assert fix.check(tmp_path) is True

    def test_apply_chmods_files_to_0600(self, tmp_path: Path, isolated_home: Path) -> None:
        secrets_dir = isolated_home / ".movate" / "secrets"
        secrets_dir.mkdir(parents=True)
        bad_file = secrets_dir / "dev.yaml"
        bad_file.write_text("secrets: {}\n")
        os.chmod(bad_file, 0o644)

        fix = next(f for f in available_fixes() if f.id == "fix-secrets-permissions")
        result = fix.run(tmp_path, dry_run=False)
        assert result.status is FixStatus.APPLIED
        # Mode tightened
        mode = bad_file.stat().st_mode & 0o777
        assert mode == 0o600
        # No group/world bits
        assert not (mode & stat.S_IRGRP)


@pytest.mark.unit
class TestUnshadowRuntimeKeys:
    """The shell-shadow auth fix: a stale `export <VAR>=...` in a shell
    profile shadows a freshly-saved key in ~/.movate/credentials."""

    FIX_ID = "unshadow-runtime-keys"

    @pytest.fixture
    def shadow_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, CredentialsStore]:
        """Isolate HOME (for profile discovery) + the credentials store.

        Returns ``(home, store)``. The store is pointed at a tempfile via
        ``MOVATE_CREDENTIALS_PATH`` so we never read the real
        ``~/.movate/credentials``; HOME points at a temp dir so profile
        discovery never reads real ~/.zshrc etc.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))
        # Make sure the file backend (not a stray keychain selection) is used.
        monkeypatch.delenv("MOVATE_CRED_BACKEND", raising=False)
        return home, CredentialsStore()

    def _fix(self) -> reg_mod.Fix:
        return next(f for f in available_fixes() if f.id == self.FIX_ID)

    def test_check_true_when_saved_cred_and_active_export(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        home, store = shadow_env
        store.set("MDK_DEV_KEY", "the-real-rotated-key")
        (home / ".zshrc").write_text("export MDK_DEV_KEY=stale-shadowing-value\n")
        assert self._fix().check(tmp_path) is True

    def test_check_false_with_only_saved_cred(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        """Saved cred but no profile export → no shadow."""
        _home, store = shadow_env
        store.set("MDK_DEV_KEY", "the-real-rotated-key")
        assert self._fix().check(tmp_path) is False

    def test_check_false_with_only_profile_export(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        """A lone profile export with no saved cred is benign — not flagged."""
        home, _store = shadow_env
        (home / ".zshrc").write_text("export MDK_DEV_KEY=some-value\n")
        assert self._fix().check(tmp_path) is False

    def test_check_false_when_export_already_commented(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        """Idempotency: a commented export is not an active shadow."""
        home, store = shadow_env
        store.set("MDK_DEV_KEY", "the-real-rotated-key")
        (home / ".zshrc").write_text("# export MDK_DEV_KEY=stale-shadowing-value\n")
        assert self._fix().check(tmp_path) is False

    def test_check_detects_provider_key(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        """Provider keys (OPENAI_API_KEY etc.) are tracked too."""
        home, store = shadow_env
        store.set("OPENAI_API_KEY", "sk-real")
        (home / ".bashrc").write_text("export OPENAI_API_KEY=sk-stale\n")
        assert self._fix().check(tmp_path) is True

    def test_dry_run_reports_would_apply_and_does_not_modify(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        home, store = shadow_env
        store.set("MDK_DEV_KEY", "the-real-rotated-key")
        profile = home / ".zshrc"
        original = "export MDK_DEV_KEY=stale-shadowing-value\n"
        profile.write_text(original)

        result = self._fix().run(tmp_path, dry_run=True)
        assert result.status is FixStatus.WOULD_APPLY
        # Names the var + profile, NEVER the secret value.
        assert "MDK_DEV_KEY" in result.message
        assert str(profile) in result.message
        assert "stale-shadowing-value" not in result.message
        assert "the-real-rotated-key" not in result.message
        # Profile untouched, no backup written.
        assert profile.read_text() == original
        assert not profile.with_name(profile.name + ".mdk-bak").exists()

    def test_apply_comments_line_writes_backup_and_is_idempotent(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        home, store = shadow_env
        store.set("MDK_DEV_KEY", "the-real-rotated-key")
        profile = home / ".zshrc"
        original = (
            "# my profile\n"
            "export PATH=$PATH:/usr/local/bin\n"
            "export MDK_DEV_KEY=stale-shadowing-value\n"
        )
        profile.write_text(original)

        result = self._fix().run(tmp_path, dry_run=False)
        assert result.status is FixStatus.APPLIED

        # The export is now commented (inert) with the self-documenting marker.
        text = profile.read_text()
        lines = text.splitlines()
        active = [ln for ln in lines if reg_mod._active_export_pattern("MDK_DEV_KEY").match(ln)]
        assert active == []  # no active export remains
        assert any("disabled by mdk fix" in ln and "MDK_DEV_KEY" in ln for ln in lines)
        # Unrelated lines preserved.
        assert "export PATH=$PATH:/usr/local/bin" in text

        # A one-time backup of the PRISTINE profile was written.
        backup = profile.with_name(profile.name + ".mdk-bak")
        assert backup.read_text() == original

        # Result names the var + the manual follow-up, never the secret.
        assert "MDK_DEV_KEY" in result.message
        assert "unset MDK_DEV_KEY" in result.message
        assert "stale-shadowing-value" not in result.message
        assert "the-real-rotated-key" not in result.message

        # Idempotent: re-running on the now-clean tree is a no-op.
        assert self._fix().check(tmp_path) is False
        second = self._fix().run(tmp_path, dry_run=False)
        assert second.status is FixStatus.NOT_NEEDED

    def test_apply_does_not_clobber_existing_backup(
        self, tmp_path: Path, shadow_env: tuple[Path, CredentialsStore]
    ) -> None:
        """A pre-existing .mdk-bak is never overwritten."""
        home, store = shadow_env
        store.set("MDK_DEV_KEY", "the-real-rotated-key")
        profile = home / ".zshrc"
        profile.write_text("export MDK_DEV_KEY=stale\n")
        backup = profile.with_name(profile.name + ".mdk-bak")
        backup.write_text("PRISTINE ORIGINAL FROM EARLIER RUN\n")

        self._fix().run(tmp_path, dry_run=False)
        assert backup.read_text() == "PRISTINE ORIGINAL FROM EARLIER RUN\n"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_diagnose_and_fix_returns_one_result_per_fix(project_root: Path) -> None:
    results = diagnose_and_fix(project_root, dry_run=True)
    assert len(results) == len(available_fixes())


@pytest.mark.unit
def test_diagnose_and_fix_only_filters(project_root: Path) -> None:
    results = diagnose_and_fix(project_root, dry_run=True, only=("ensure-gitignore",))
    assert len(results) == 1
    assert results[0].fix_id == "ensure-gitignore"


@pytest.mark.unit
def test_diagnose_and_fix_skip_filters(project_root: Path) -> None:
    """--skip removes the named fix from the dispatch list."""
    results = diagnose_and_fix(project_root, dry_run=True, skip=("ensure-gitignore",))
    fix_ids = {r.fix_id for r in results}
    assert "ensure-gitignore" not in fix_ids
    # Others still present
    assert len(results) == len(available_fixes()) - 1


@pytest.mark.unit
def test_diagnose_and_fix_continues_past_failure(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing fix shouldn't abort the rest of the dispatch."""
    # Force one specific fix to fail by patching its apply_fn.

    original = reg_mod._apply_movate_dir

    def boom(_root: Path, _dry_run: bool) -> reg_mod.FixResult:
        raise RuntimeError("simulated disk full")

    monkeypatch.setattr(reg_mod, "_apply_movate_dir", boom)
    # Also re-construct the registry with the patched apply_fn
    monkeypatch.setattr(
        reg_mod,
        "available_fixes",
        lambda: [
            reg_mod.Fix(
                id="ensure-movate-dir",
                label="x",
                description="x",
                check=reg_mod._check_movate_dir,
                apply_fn=boom,
            ),
            reg_mod.Fix(
                id="ensure-gitignore",
                label="x",
                description="x",
                check=reg_mod._check_gitignore,
                apply_fn=reg_mod._apply_gitignore,
            ),
        ],
    )

    results = diagnose_and_fix(project_root, dry_run=False)
    statuses = {r.fix_id: r.status for r in results}
    assert statuses["ensure-movate-dir"] is FixStatus.FAILED
    # Second fix still ran and succeeded
    assert statuses["ensure-gitignore"] is FixStatus.APPLIED

    _ = original  # silence unused-var lint


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_fix_default_is_dry_run(project_root: Path) -> None:
    result = runner.invoke(app, ["fix", "--project-root", str(project_root)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "dry-run" in result.stdout.lower() or "would apply" in result.stdout.lower()
    # Nothing actually written
    assert not (project_root / ".mdk").exists()


@pytest.mark.unit
def test_cli_fix_apply_writes(project_root: Path) -> None:
    result = runner.invoke(app, ["fix", "--apply", "--project-root", str(project_root)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # At minimum, .mdk/ was created
    assert (project_root / ".mdk").is_dir()


@pytest.mark.unit
def test_cli_fix_only_runs_named_fix(project_root: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fix",
            "--only",
            "ensure-gitignore",
            "--apply",
            "--project-root",
            str(project_root),
        ],
    )
    assert result.exit_code == 0
    # The named fix ran
    assert (project_root / ".gitignore").is_file()
    # Others did NOT run (e.g. .mdk/ wasn't created)
    assert not (project_root / ".mdk").exists()


@pytest.mark.unit
def test_cli_fix_list_shows_catalog(project_root: Path) -> None:
    result = runner.invoke(app, ["fix", "--list"])
    assert result.exit_code == 0
    # Every fix id appears in the catalog
    for f in available_fixes():
        assert f.id in result.stdout


@pytest.mark.unit
def test_cli_fix_clean_project_reports_nothing_to_fix(project_root: Path) -> None:
    """After running --apply once, a second run should report clean."""
    runner.invoke(app, ["fix", "--apply", "--project-root", str(project_root)])
    second = runner.invoke(app, ["fix", "--project-root", str(project_root)])
    assert second.exit_code == 0
    assert "nothing to fix" in second.stdout.lower()
