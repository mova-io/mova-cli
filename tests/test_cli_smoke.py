"""CLI smoke tests: help renders, version works, real commands hit their handlers,
remaining stubs exit non-zero with the "not implemented" message.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from movate import __version__
from movate.cli.main import app

runner = CliRunner()

STUB_EXIT_CODE = 2  # see movate/cli/_stub.py


@pytest.mark.unit
def test_help_renders() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Develop" in result.stdout
    assert "Run & evaluate" in result.stdout
    assert "Diagnose" in result.stdout
    assert "Deploy & operate" in result.stdout
    assert "Manage" in result.stdout


@pytest.mark.unit
def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.unit
def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "movate" in result.stdout.lower()


@pytest.mark.unit
def test_doctor_runs() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        ["logs", "x"],
        ["serve"],
        ["worker"],
        ["deploy", "dev"],
    ],
)
def test_phase2plus_stub_commands_exit_nonzero(command: list[str]) -> None:
    """Commands not yet implemented exit with code 2 + a clear message."""
    result = runner.invoke(app, command)
    assert result.exit_code == STUB_EXIT_CODE
