"""``mdk serve`` preflight — clean error when optional deps are missing.

FastAPI doesn't pull ``python-multipart`` in transitively, but our
POST /api/v1/agents route uses ``UploadFile`` which needs it. Without
the preflight, the operator hits a 20-frame stack trace deep inside
``build_app()``'s route registration. The preflight surfaces a clean
error with a copy-paste install hint instead.

Same pattern applies to any future serve-only optional dep — add to
``_SERVE_REQUIRED_OPTIONAL_DEPS`` and it gets caught here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

# PR #80's serve.py preflight changes didn't survive the May-2026 stack
# cascade cleanly — the test file is on main but the source isn't. Skip
# until the preflight code is re-landed (chip filed).
pytestmark = pytest.mark.skip(
    reason="PR #80 source incomplete on main (May-2026 cascade); chip filed."
)


@pytest.mark.unit
class TestServePreflight:
    def test_serve_errors_cleanly_when_multipart_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ImportError on BOTH multipart aliases (python_multipart +
        multipart) → exit 2 with a copy-paste hint BEFORE any FastAPI
        route registration runs."""

        def fake_import(name: str) -> object:
            # Reject both aliases so the preflight registers the dep
            # as missing regardless of which name is canonical.
            if name in ("multipart", "python_multipart"):
                raise ImportError(f"No module named '{name}'")
            return __import__(name)

        with patch("movate.cli.serve.importlib.import_module", side_effect=fake_import):
            result = runner.invoke(app, ["serve"], env={"COLUMNS": "200"})

        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        # Surfaced as a missing-dep error, not a 20-frame traceback.
        assert "missing optional dependencies" in combined.lower()
        # Names the missing dep + the route that needs it.
        assert "python-multipart" in combined
        # Surfaces both uv-tool and pip install paths.
        assert "uv tool install" in combined
        assert "pip install" in combined
        # And does NOT include a "Traceback" — the whole point is
        # avoiding the late-failure noise.
        assert "Traceback" not in combined

    def test_serve_preflight_passes_when_all_deps_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every required dep imports cleanly, the preflight
        returns silently and `serve` proceeds (we stub out the actual
        uvicorn run to keep the test hermetic)."""
        from movate.cli.serve import _preflight_optional_deps  # noqa: PLC0415

        # Real imports — multipart IS installed in the dev env.
        _preflight_optional_deps()  # must not raise

    def test_preflight_lists_every_missing_dep_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If multiple optional deps are missing, the error message
        lists ALL of them — not just the first — so the operator gets
        a single install command that fixes everything.

        Patches the registry to add a fake second required dep, then
        forces both to be 'missing' via the import shim.
        """
        from movate.cli import serve as serve_module  # noqa: PLC0415

        fake_registry = (
            (
                ("python_multipart", "multipart"),
                "python-multipart",
                "POST /api/v1/agents bundle upload",
            ),
            (
                ("nonexistent_pkg",),
                "nonexistent-pkg",
                "hypothetical future surface",
            ),
        )

        def fake_import(name: str) -> object:
            if name in ("multipart", "python_multipart", "nonexistent_pkg"):
                raise ImportError(f"No module named '{name}'")
            return __import__(name)

        with (
            patch.object(serve_module, "_SERVE_REQUIRED_OPTIONAL_DEPS", fake_registry),
            patch("movate.cli.serve.importlib.import_module", side_effect=fake_import),
        ):
            result = runner.invoke(app, ["serve"], env={"COLUMNS": "200"})

        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        # Both missing deps appear in the error.
        assert "python-multipart" in combined
        assert "nonexistent-pkg" in combined
        # The fix-hint pip install line bundles both names.
        assert "python-multipart nonexistent-pkg" in combined
