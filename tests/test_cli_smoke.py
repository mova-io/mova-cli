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
@pytest.mark.parametrize(
    ("command", "expected_example"),
    [
        # Each tuple is (subcommand, a substring that ONLY appears in
        # that command's docstring Examples block). If someone wraps
        # the command with `help="..."` in main.py the docstring stops
        # rendering — and these substrings disappear from `--help`.
        # That's exactly the regression we're guarding against.
        ("run", "movate run ./faq-agent"),
        ("bench", "movate bench ./faq-agent"),
        ("submit", "movate submit faq-agent"),
        ("watch", "movate watch ./agents/faq-agent"),
    ],
)
def test_subcommand_help_renders_docstring_examples(command: str, expected_example: str) -> None:
    """Every command's --help should surface the Examples block from
    its docstring.

    Background: Typer/Click only uses the function's docstring when
    `app.command(...)` is called WITHOUT a `help=` override. Passing
    `help="short one-liner"` silently replaces the entire help with
    that string, dropping the carefully-written Examples blocks. We
    hit this in v0.5 — `movate submit --help` was missing its 12 lines
    of examples for weeks because `main.py` overrode `help=`. This
    test fails loudly if anyone re-introduces an override that strips
    a docstring."""
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert expected_example in result.stdout, (
        f"`movate {command} --help` is missing its docstring examples — "
        f"someone likely re-added a `help=` override in main.py that's "
        f"shadowing the function's docstring."
    )


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
        # `serve` (v0.5 stage 3b) and `worker` (v0.5 stage 4) both used
        # to be stubs; both have been replaced with real loops that
        # block forever, so neither can appear here. Their coverage
        # lives in tests/test_runtime_*.py + manual real-binary smoke.
        ["deploy", "dev"],
    ],
)
def test_phase2plus_stub_commands_exit_nonzero(command: list[str]) -> None:
    """Commands not yet implemented exit with code 2 + a clear message."""
    result = runner.invoke(app, command)
    assert result.exit_code == STUB_EXIT_CODE
