"""ADR 026 D5 — ``mdk doctor`` version-staleness check.

``mdk doctor`` compares the INSTALLED ``mdk`` against its source of truth:
when run from/alongside an editable repo checkout, the repo's version; else
"last-updated N days ago" from the installed CalVer. Warns when behind, with
the reinstall command. The unit tests drive the pure helpers directly so they
don't depend on the test environment's actual install age.
"""

from __future__ import annotations

import datetime as dt

import pytest

from movate.cli import doctor as doctor_mod


@pytest.mark.unit
class TestParseCalverDate:
    def test_parses_calver(self) -> None:
        assert doctor_mod._parse_calver_date("2026.5.27.9") == dt.date(2026, 5, 27)

    def test_non_calver_returns_none(self) -> None:
        # Too few segments / non-numeric / invalid date → None (never raises).
        assert doctor_mod._parse_calver_date("v0") is None
        assert doctor_mod._parse_calver_date("garbage") is None
        assert doctor_mod._parse_calver_date("0.5.1") is None  # year 0 is invalid
        assert doctor_mod._parse_calver_date("v0.5.1") is None


@pytest.mark.unit
class TestStalenessFromCalverDate:
    def test_fresh_build_is_current(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = dt.date.today()
        version = f"{today.year}.{today.month}.{today.day}.1"
        monkeypatch.setattr(doctor_mod, "__version__", version)
        # No newer editable repo (force the repo-version probe to None).
        monkeypatch.setattr(doctor_mod, "_editable_repo_version", lambda: None)
        result, _purpose = doctor_mod._check_version_staleness()
        assert "current" in result.lower()
        assert "behind" not in result.lower()

    def test_old_build_warns_with_upgrade_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        old = dt.date.today() - dt.timedelta(days=60)
        version = f"{old.year}.{old.month}.{old.day}.1"
        monkeypatch.setattr(doctor_mod, "__version__", version)
        monkeypatch.setattr(doctor_mod, "_editable_repo_version", lambda: None)
        result, _purpose = doctor_mod._check_version_staleness()
        assert "last updated" in result.lower()
        # Warning markup + actionable upgrade command.
        assert "yellow" in result.lower()
        assert "uv tool install --force" in result or "uv tool install" in result


@pytest.mark.unit
class TestStalenessFromEditableRepo:
    def test_repo_newer_than_installed_warns_reinstall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Repo source-of-truth reports a NEWER version than what's installed.
        monkeypatch.setattr(doctor_mod, "__version__", "2026.5.27.1")
        monkeypatch.setattr(doctor_mod, "_editable_repo_version", lambda: "2026.5.27.9")
        result, _purpose = doctor_mod._check_version_staleness()
        assert "behind repo" in result.lower()
        assert "2026.5.27.9" in result
        assert "uv tool install --force ." in result


@pytest.mark.unit
class TestDoctorTableSurfacesStaleness:
    def test_doctor_renders_up_to_date_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: `mdk doctor` renders the staleness row."""
        from typer.testing import CliRunner  # noqa: PLC0415

        from movate.cli.main import app  # noqa: PLC0415

        cli_runner = CliRunner(mix_stderr=False)
        result = cli_runner.invoke(app, ["doctor"], env={"COLUMNS": "220"})
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "up-to-date" in result.stdout
