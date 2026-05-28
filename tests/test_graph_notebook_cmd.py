"""Tests for ``mdk graph notebook`` (``movate.cli.graph_notebook_cmd``).

Covers:

* The command exists / is wired into the CLI.
* It generates a ``.ipynb`` at the requested path (and to stdout via ``-o -``).
* SECURITY: the generated notebook NEVER contains the API key — it reads
  it from ``os.environ["MOVATE_API_KEY"]`` at runtime.
* The generated notebook is valid JSON with a valid nbformat-v4 shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.graph_notebook_cmd import _API_KEY_ENV, build_notebook, graph_app
from movate.cli.main import app
from movate.core.user_config import TargetConfig

runner = CliRunner(mix_stderr=False)

# A sensitive-looking token we ensure never appears in the generated file.
_SECRET_KEY = "mvt_supersecret_abc123"


@pytest.fixture()
def _stub_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve any target to a fixed base URL without touching ~/.movate."""

    def fake_resolve(name: str | None = None) -> tuple[str, TargetConfig]:
        return (
            name or "dev",
            TargetConfig(url="https://runtime.example/api/v1", key_env=_API_KEY_ENV),
        )

    monkeypatch.setattr("movate.cli.graph_notebook_cmd.resolve_target", fake_resolve)


def _assert_valid_nbformat(nb: dict) -> None:
    assert nb["nbformat"] == 4
    assert "nbformat_minor" in nb
    assert isinstance(nb["cells"], list) and nb["cells"]
    for cell in nb["cells"]:
        assert cell["cell_type"] in {"code", "markdown"}
        assert isinstance(cell["source"], list)
        if cell["cell_type"] == "code":
            # nbformat v4 code cells require these keys.
            assert "outputs" in cell
            assert "execution_count" in cell


# ---------------------------------------------------------------------------
# build_notebook (pure)
# ---------------------------------------------------------------------------


class TestBuildNotebook:
    def test_returns_valid_nbformat_structure(self) -> None:
        nb = build_notebook(
            base_url="https://runtime.example/api/v1",
            project_id="my-kb",
            target="prod",
        )
        _assert_valid_nbformat(nb)
        # round-trips through JSON cleanly
        json.loads(json.dumps(nb))

    def test_key_value_never_embedded_only_env_name(self) -> None:
        nb = build_notebook(
            base_url="https://runtime.example/api/v1",
            project_id="my-kb",
            target="prod",
        )
        blob = json.dumps(nb)
        # The env-var NAME appears (the notebook reads from it)...
        assert _API_KEY_ENV in blob
        # ...and the code reads it via os.environ, not a literal value.
        assert "os.environ" in blob
        # No api_key argument is hardcoded in the notebook source.
        assert "api_key=API_KEY" in blob
        assert "API_KEY = os.environ" in blob

    def test_metadata_records_target_and_project(self) -> None:
        nb = build_notebook(
            base_url="https://runtime.example/api/v1",
            project_id="my-kb",
            target="prod",
        )
        mv = nb["metadata"]["movate"]
        assert mv["target"] == "prod"
        assert mv["project_id"] == "my-kb"
        assert mv["api_key_env"] == _API_KEY_ENV


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCommandExists:
    def test_graph_app_has_notebook_command(self) -> None:
        names = {cmd.name for cmd in graph_app.registered_commands}
        assert "notebook" in names

    def test_graph_registered_on_main_app(self) -> None:
        result = runner.invoke(app, ["graph", "--help"])
        assert result.exit_code == 0
        assert "notebook" in result.stdout

    def test_notebook_help(self) -> None:
        result = runner.invoke(app, ["graph", "notebook", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.stdout


# ---------------------------------------------------------------------------
# Generation behavior
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_writes_ipynb_file(self, tmp_path: Path, _stub_target: None) -> None:
        out = tmp_path / "explore.ipynb"
        result = runner.invoke(
            app,
            ["graph", "notebook", "--project", "my-kb", "-t", "prod", "-o", str(out)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert out.is_file()
        nb = json.loads(out.read_text())
        _assert_valid_nbformat(nb)

    def test_generated_file_does_not_contain_api_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _stub_target: None,
    ) -> None:
        # Even with the real key set in the environment, it must NOT be
        # written into the generated notebook file.
        monkeypatch.setenv(_API_KEY_ENV, _SECRET_KEY)
        out = tmp_path / "explore.ipynb"
        result = runner.invoke(
            app,
            ["graph", "notebook", "--project", "my-kb", "-o", str(out)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        contents = out.read_text()
        assert _SECRET_KEY not in contents
        # It still references the env var so the notebook can read it.
        assert _API_KEY_ENV in contents
        assert "os.environ" in contents

    def test_default_output_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_target: None
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["graph", "notebook", "--project", "my-kb"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / "explore-graph.ipynb").is_file()

    def test_stdout_snippet_mode(self, _stub_target: None) -> None:
        result = runner.invoke(app, ["graph", "notebook", "--project", "my-kb", "-o", "-"])
        assert result.exit_code == 0
        nb = json.loads(result.stdout)
        _assert_valid_nbformat(nb)
        assert _SECRET_KEY not in result.stdout
