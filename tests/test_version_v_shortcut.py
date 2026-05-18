"""Convention swap: `-v` is now an alias for `--version` (was --verbose).

Before this PR:
* `mdk -v` errored with "Missing command" (verbose needs a subcommand)
* `mdk -V` showed version

After:
* `mdk -v` shows version + exits (matches docker, npm, node)
* `mdk -V` shows version + exits (unchanged)
* `mdk --verbose <subcmd>` still works (long form preserved)
* Subcommand-level `-v` (e.g. `mdk trace replay -v <id>`) is unaffected
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


@pytest.mark.unit
class TestVersionShortcut:
    def test_lowercase_v_shows_version(self) -> None:
        """The convention swap: `mdk -v` now shows version + exits."""
        result = runner.invoke(app, ["-v"])
        assert result.exit_code == 0
        # The version-callback echoes the binary name + version.
        assert "mdk" in result.stdout or "movate" in result.stdout
        # No "Missing command" error.
        assert "Missing command" not in result.stderr

    def test_capital_v_still_shows_version(self) -> None:
        """`mdk -V` was the canonical version flag before; it still is."""
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        assert "mdk" in result.stdout or "movate" in result.stdout

    def test_long_form_still_shows_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "mdk" in result.stdout or "movate" in result.stdout


@pytest.mark.unit
class TestVerbosePreserved:
    def test_long_form_verbose_still_works(self) -> None:
        """`--verbose <subcmd>` must continue to work — only the short
        form was reassigned."""
        result = runner.invoke(app, ["--verbose", "doctor", "--no-fix-prompt"])
        # Doctor itself returns 0 in a clean environment; the verbose
        # flag's only effect is setting DEBUG-level logging.
        assert result.exit_code in (0, 2), result.stdout + result.stderr

    def test_lowercase_v_no_longer_triggers_verbose_for_subcommand(self) -> None:
        """`mdk -v doctor` now shows version + exits before doctor
        runs. Previous behavior: ran doctor with DEBUG logging."""
        result = runner.invoke(app, ["-v", "doctor"])
        # Version eager callback fires + exits. Doctor doesn't run.
        assert result.exit_code == 0
        # No doctor table in stdout (version is just one line).
        assert "movate doctor" not in result.stdout

    def test_help_shows_v_as_version_alias(self) -> None:
        """The help table should show `-v` paired with `--version`,
        NOT with `--verbose` anymore.

        Two CI-specific gotchas the test has to defeat:

        1. **ANSI escapes inside option names**: CI runs with
           ``FORCE_COLOR=1``, so Rich styles ``--`` and ``verbose`` as
           separate spans. A raw substring search for ``--verbose``
           misses them entirely. Strip ANSI before checking.

        2. **Narrow-terminal line wrap**: ``env={"COLUMNS": …}`` isn't
           reliably honored in CI, so option/short-form pairs can land
           on separate lines. Flatten to a single string before
           checking — sidesteps the wrap question entirely."""
        import re  # noqa: PLC0415

        result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # Strip ANSI escapes first (CI sets FORCE_COLOR=1).
        plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", result.stdout)
        # Then collapse all whitespace (wrap newlines + multi-space
        # padding from Rich's table layout).
        flat = " ".join(plain.split())
        assert "--verbose" in flat, "expected --verbose mentioned in help"
        assert "--version" in flat, "expected --version mentioned in help"
        # `-v` and `-V` are both paired with --version (not --verbose).
        assert "-V" in flat
        assert "-v" in flat
        # Defensive: anywhere ``--verbose`` is followed by ``-v`` before
        # the next ``--`` flag would mean we regressed the short-form
        # re-pairing.
        for match in re.finditer(r"--verbose(.*?)(?=--|\Z)", flat):
            window = match.group(1)
            assert " -v " not in window, f"--verbose row still claims -v: window={window!r}"


@pytest.mark.unit
def test_subcommand_v_short_unaffected() -> None:
    """The `mdk trace replay -v <run-id>` subcommand-level `-v` is a
    different scope and shouldn't have been touched. We confirm the
    trace subcommand still accepts -v as a flag (will error on the
    missing run-id, but it parses)."""
    # `mdk trace replay -v nonexistent-id` parses -v as the verbose
    # flag for the replay subcommand, then errors because the run-id
    # isn't found. Exit code 1 or 2 is fine; we just need to confirm
    # parsing didn't fail with "no such option -v".
    result = runner.invoke(app, ["trace", "replay", "-v", "fake-id"])
    assert result.exit_code in (1, 2)
    # No "no such option" parse error.
    assert "no such option" not in result.stderr.lower()
    assert "invalid option" not in result.stderr.lower()
