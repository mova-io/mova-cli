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
        assert (tmp_path / ".movate").is_dir()

    def test_dry_run_does_not_create(self, tmp_path: Path) -> None:
        fix = next(f for f in available_fixes() if f.id == "ensure-movate-dir")
        result = fix.run(tmp_path, dry_run=True)
        assert result.status is FixStatus.WOULD_APPLY
        assert not (tmp_path / ".movate").exists()

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
        assert ".movate/local.db" in text
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
    assert not (project_root / ".movate").exists()


@pytest.mark.unit
def test_cli_fix_apply_writes(project_root: Path) -> None:
    result = runner.invoke(app, ["fix", "--apply", "--project-root", str(project_root)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # At minimum, .movate/ was created
    assert (project_root / ".movate").is_dir()


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
    # Others did NOT run (e.g. .movate/ wasn't created)
    assert not (project_root / ".movate").exists()


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
