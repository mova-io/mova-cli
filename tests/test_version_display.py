"""``mdk --version`` shows version + release date + Python info.

Pre-bundle output:
    mdk 0.7.0

Verbose form (this bundle):
    mdk 0.7.1
      released:  2026-05-15
      python:    3.11.15

Three lines makes the "which build is this and when was it cut?"
question answerable in one CLI call. The first line stays identical
shape (brand + space + semver) so any script doing
``mdk --version | awk '{print $2}'`` keeps working.

Also locks in the per-PR version-bump practice: ``__version__`` and
``__release_date__`` MUST round-trip through ``mdk --version``, and
both MUST stay in sync with pyproject.toml.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate import __release_date__, __version__
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


@pytest.mark.unit
class TestVersionDisplay:
    def test_first_line_is_brand_space_semver(self) -> None:
        """Back-compat: line 1 is `<brand> <semver>` exactly. Any script
        grepping `awk '{print $2}'` on this line keeps working."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        line1 = result.stdout.splitlines()[0]
        # `mdk 0.7.1` or `movate 0.7.1` — brand is one word, version is
        # semver-shaped. (Tolerate the brand label change between runs.)
        assert re.match(r"^(mdk|movate) \d+\.\d+\.\d+\b", line1), (
            f"line 1 must be `<brand> <semver>`, got: {line1!r}"
        )

    def test_release_date_line_appears(self) -> None:
        """`released: YYYY-MM-DD` appears so operators see WHEN this
        build was cut, not just which number."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        # The release date constant from movate/__init__.py is rendered.
        assert __release_date__ in result.stdout
        assert "released:" in result.stdout

    def test_python_version_line_appears(self) -> None:
        """`python: X.Y.Z` helps debug version-mismatch issues
        (operator runs mdk under one Python, asks 'what changed?'
        — the answer is sometimes in this line)."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        # The line carries a semver-shaped Python version.
        py_lines = [ln for ln in result.stdout.splitlines() if ln.strip().startswith("python:")]
        assert py_lines, "expected a `python:` line in --version output"
        assert re.search(r"\d+\.\d+\.\d+", py_lines[0])

    def test_short_v_shows_full_display(self) -> None:
        """`-v` (the convention swap) shows the same full output."""
        result = runner.invoke(app, ["-v"])
        assert result.exit_code == 0
        assert __release_date__ in result.stdout
        assert "released:" in result.stdout
        assert "python:" in result.stdout

    def test_capital_v_shows_full_display(self) -> None:
        """`-V` (canonical short) shows the same full output."""
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        assert __release_date__ in result.stdout

    def test_version_constants_are_sane(self) -> None:
        """Sanity: the constants exist and are well-formed."""
        assert re.match(r"^\d+\.\d+\.\d+$", __version__), (
            f"__version__ must be semver: {__version__!r}"
        )
        # Release date is ISO-8601 (YYYY-MM-DD).
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", __release_date__), (
            f"__release_date__ must be YYYY-MM-DD: {__release_date__!r}"
        )


@pytest.mark.unit
class TestVersionSyncWithPyproject:
    def test_pyproject_version_matches_init_version(self) -> None:
        """`pyproject.toml: version` and `src/movate/__init__.py:
        __version__` MUST agree — they're the two places `uv build` /
        `pip install` / `import movate; movate.__version__` read from,
        and drift between them is impossible to debug from a stack
        trace alone.

        This test is the cheap forcing function for the per-PR
        bump-version discipline: whoever bumps one MUST bump the other,
        or this test goes red on the next CI run.
        """
        # Walk up from this test file to find the project root + pyproject.
        repo_root = Path(__file__).resolve().parent.parent
        pyproject = repo_root / "pyproject.toml"
        assert pyproject.is_file(), f"pyproject.toml not found at {pyproject}"
        text = pyproject.read_text()
        # Grep for `version = "..."` in the top [project] block (lazy
        # match — `[project]` is the FIRST table in this pyproject so
        # the first `version =` is ours).
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
        assert match is not None, "pyproject.toml has no version field"
        pyproject_version = match.group(1)
        assert pyproject_version == __version__, (
            f"version drift: pyproject.toml says {pyproject_version!r}, "
            f"src/movate/__init__.py says {__version__!r}. Bump both."
        )
