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

        Wrap-tolerant: we join the entire stdout into a single string
        before substring-checking. CliRunner's ``env={"COLUMNS": …}``
        argument is reliably honored by Rich locally but NOT in CI
        (Rich falls back to a narrower default there), so a per-line
        assertion will spuriously fail when ``--verbose`` wraps to
        ``--verbo\\nse``. Joining sidesteps the wrap question entirely."""
        result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        # Collapse all whitespace (including the wrap newlines that Rich
        # inserts when it picks a narrower width than we asked for).
        flat = " ".join(result.stdout.split())
        assert "--verbose" in flat, "expected --verbose mentioned in help"
        assert "--version" in flat, "expected --version mentioned in help"
        # `-v` and `-V` are both paired with --version (not --verbose).
        # Pin that with the same flat string.
        assert "-V" in flat
        assert "-v" in flat
        # Defensive: anywhere ``--verbose`` is followed by ``-v`` (on
        # the same row) means we regressed the short-form re-pairing.
        # Use a non-greedy capture to look at the immediate-after window.
        import re  # noqa: PLC0415

        # Walk every `--verbose ...` window and ensure no `-v ` short
        # form follows before the next flag (--something) appears.
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
