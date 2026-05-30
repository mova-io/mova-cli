"""Monday-demo polish — three blockers found by the Saturday smoke test.

Three small fixes bundled into one PR (feat/demo-polish-blockers):

1. ``mdk serve``/``mdk serve --dev`` prints a friendly ``[runtime]``-extras
   install hint when uvicorn/fastapi aren't importable, rather than a raw
   ``ModuleNotFoundError`` traceback. See :mod:`movate.cli._runtime_extras`.
2. ``mdk doctor`` adds a ``mdk-binary-staleness`` check that compares the
   installed ``__version__`` against ``project.yaml`` ``mdk_version_min:``
   (or the ``MDK_VERSION_MIN`` env override). Silent skip when neither is set.
3. ``mdk init`` (the with-agents Project Panel + the Workspace Panel) emits
   ``cd <project> && <cmd>`` lines with a sensible ``--mock`` payload — the
   smoke test found ``mdk run <agent>`` from one directory up produced a
   confusing ``agent.yaml not found`` error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fix 1 — friendly extras-check on `mdk serve --dev` (and `mdk serve`)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuntimeExtrasHint:
    def test_helper_exits_with_friendly_message_when_uvicorn_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The shared ``ensure_runtime_extras`` helper exits 2 with a
        helpful install hint when the [runtime] extra isn't importable.

        Mocks ``importlib.util.find_spec`` so the test doesn't actually
        need an env without uvicorn.
        """
        import importlib.util as _iu  # noqa: PLC0415

        import typer  # noqa: PLC0415

        from movate.cli import _runtime_extras  # noqa: PLC0415

        def fake_find_spec(name: str) -> Any:
            if name in {"uvicorn", "fastapi"}:
                return None
            return _iu.find_spec(name)

        monkeypatch.setattr(_runtime_extras.importlib.util, "find_spec", fake_find_spec)

        with pytest.raises(typer.Exit) as exc_info:
            _runtime_extras.ensure_runtime_extras()

        assert exc_info.value.exit_code == 2
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # The message names the extra + the install command operators
        # can copy-paste verbatim.
        assert "mdk serve requires" in combined
        assert "[runtime]" in combined
        assert "uv tool install --editable" in combined
        assert "playground" in combined
        # voice + otel are mentioned as recommended extras.
        assert "voice" in combined and "otel" in combined

    def test_helper_is_noop_when_runtime_extra_installed(self) -> None:
        """When uvicorn + fastapi import successfully (the standard CI
        env), the helper returns silently — never exits."""
        from movate.cli._runtime_extras import ensure_runtime_extras  # noqa: PLC0415

        # Must not raise — uvicorn + fastapi are in the test env's deps.
        ensure_runtime_extras()

    def test_serve_dev_emits_friendly_hint_when_runtime_extra_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: ``mdk serve --dev`` invocation with the [runtime]
        extra mocked out emits the friendly hint + exits 2 — never the
        raw ``ModuleNotFoundError`` traceback the smoke test caught."""
        import importlib.util as _iu  # noqa: PLC0415

        from movate.cli import _runtime_extras  # noqa: PLC0415
        from movate.cli.main import app  # noqa: PLC0415

        def fake_find_spec(name: str) -> Any:
            if name in {"uvicorn", "fastapi"}:
                return None
            return _iu.find_spec(name)

        monkeypatch.setattr(_runtime_extras.importlib.util, "find_spec", fake_find_spec)

        result = runner.invoke(
            app,
            ["serve", "--dev", "--host", "127.0.0.1"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "[runtime]" in combined
        assert "uv tool install --editable" in combined


# ---------------------------------------------------------------------------
# Fix 2 — `mdk doctor` mdk-binary-staleness check
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMdkBinaryStalenessCheck:
    def test_silent_skip_when_no_pin_anywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env override AND no project.yaml in cwd → return None, the
        caller skips the row entirely (the doctor table stays quiet for
        projects that don't opt in)."""
        from movate.cli import doctor as doctor_mod  # noqa: PLC0415

        monkeypatch.delenv("MDK_VERSION_MIN", raising=False)
        # cwd isn't a project root — load_project_config will raise and
        # the helper degrades to None.
        monkeypatch.chdir(Path.cwd())
        assert doctor_mod._check_mdk_binary_staleness() is None

    def test_env_override_wins_and_warns_when_installed_is_older(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``MDK_VERSION_MIN`` set to a CalVer NEWER than the installed
        ``__version__`` → warning row with reinstall command."""
        from movate.cli import doctor as doctor_mod  # noqa: PLC0415

        monkeypatch.setattr(doctor_mod, "__version__", "2026.5.27.1")
        monkeypatch.setenv("MDK_VERSION_MIN", "2026.6.1.0")

        out = doctor_mod._check_mdk_binary_staleness()
        assert out is not None
        result, _purpose = out
        assert "stale" in result.lower()
        assert "installed = 2026.5.27.1" in result
        assert "expects >= 2026.6.1.0" in result
        assert "uv tool install --editable" in result

    def test_env_override_passes_when_installed_is_newer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Installed >= pinned → green ok row, no warning."""
        from movate.cli import doctor as doctor_mod  # noqa: PLC0415

        monkeypatch.setattr(doctor_mod, "__version__", "2026.6.1.0")
        monkeypatch.setenv("MDK_VERSION_MIN", "2026.5.27.1")

        out = doctor_mod._check_mdk_binary_staleness()
        assert out is not None
        result, _purpose = out
        assert "stale" not in result.lower()
        # _ok markup renders green and includes the version comparison.
        assert "green" in result.lower()
        assert "2026.6.1.0" in result

    def test_project_yaml_pin_picked_up_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``mdk_version_min:`` in project.yaml is honored when no env
        override is present — the project's recorded floor wins."""
        from movate.cli import doctor as doctor_mod  # noqa: PLC0415

        monkeypatch.delenv("MDK_VERSION_MIN", raising=False)
        # Minimal project.yaml that load_project_config can ingest.
        (tmp_path / "project.yaml").write_text("mdk_version_min: '2030.1.1.0'\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(doctor_mod, "__version__", "2026.5.27.1")

        out = doctor_mod._check_mdk_binary_staleness()
        assert out is not None
        result, _purpose = out
        assert "stale" in result.lower()
        assert "2030.1.1.0" in result

    def test_non_calver_pin_degrades_to_neutral_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pin that isn't CalVer-shaped (legacy SemVer / typo) doesn't
        cry wolf — the row reports 'skipped' instead of false-warning."""
        from movate.cli import doctor as doctor_mod  # noqa: PLC0415

        monkeypatch.setattr(doctor_mod, "__version__", "2026.5.27.1")
        monkeypatch.setenv("MDK_VERSION_MIN", "v0.5.1")

        out = doctor_mod._check_mdk_binary_staleness()
        assert out is not None
        result, _purpose = out
        assert "skipped" in result.lower()


@pytest.mark.unit
class TestProjectConfigSchemaAcceptsMdkVersionMin:
    def test_mdk_version_min_is_an_optional_field(self) -> None:
        """The new field is optional (defaults to ``None``) and parses as
        a string when set — schema additions stay backward-compatible."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        # Default — old project.yaml files still parse.
        cfg = ProjectConfig()
        assert cfg.mdk_version_min is None

        # Explicit value.
        cfg2 = ProjectConfig(mdk_version_min="2026.5.27.1")
        assert cfg2.mdk_version_min == "2026.5.27.1"


# ---------------------------------------------------------------------------
# Fix 3 — `mdk init` next-steps wording (cd <project> && ...)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitNextStepsCdPrefix:
    def test_workspace_panel_chains_cd_with_single_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Workspace Panel (rendered by every ``mdk init ...
        --with-agents X`` invocation, single or multi) emits
        ``cd <project> && <cmd>`` lines so the operator's copy-paste
        works from anywhere. Before this fix, the smoke test caught
        operators running ``mdk run <agent>`` one directory up and
        getting a confusing ``agent.yaml not found`` error.
        """
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            _import_app(),
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa",
                "--no-open-editor",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        out = result.stdout
        # The Workspace Panel renders for any --with-agents invocation.
        assert "Workspace ready" in out
        # Each suggested cmd is now prefixed with `cd <project> &&`.
        assert "cd support-bot && mdk validate --all" in out
        assert "cd support-bot && mdk run rag-qa" in out
        assert "cd support-bot && mdk ci eval --mock" in out
        # A `--mock` payload is included so the first run is offline.
        assert "--mock '" in out

    def test_workspace_panel_chains_cd_for_multi_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-agent ``--with-agents`` invocation still chains ``cd``
        on every suggested next step + carries a ``--mock`` payload for
        the first agent."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            _import_app(),
            [
                "init",
                "--project",
                "support-bot",
                "--skip-snapshot",
                "--with-agents",
                "rag-qa,ticket-triager",
                "--no-open-editor",
            ],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        out = result.stdout
        assert "cd support-bot && mdk validate --all" in out
        assert "cd support-bot && mdk run rag-qa" in out
        assert "cd support-bot && mdk ci eval --mock" in out
        assert "--mock '" in out

    def test_example_mock_payload_falls_back_when_no_schema(self, tmp_path: Path) -> None:
        """The ``_example_mock_payload`` helper returns the documented
        default when the agent's input schema is missing/unreadable."""
        from movate.cli.init import _example_mock_payload  # noqa: PLC0415

        # No agent dir → fallback.
        assert _example_mock_payload(tmp_path, "nonexistent") == '{"message":"hello"}'

    def test_example_mock_payload_extracts_input_yaml_examples(self, tmp_path: Path) -> None:
        """When ``agents/<name>/schema/input.yaml`` declares fields with
        ``example:`` values, the payload helper builds a single-line
        JSON object from them — so the post-init hint command is
        runnable first try."""
        from movate.cli.init import _example_mock_payload  # noqa: PLC0415

        schema_dir = tmp_path / "agents" / "lookup" / "schema"
        schema_dir.mkdir(parents=True)
        (schema_dir / "input.yaml").write_text(
            "fields:\n"
            "  user_id:\n"
            "    type: integer\n"
            "    example: 42\n"
            "  question:\n"
            "    type: string\n"
            "    example: 'What is their email?'\n"
        )
        payload = _example_mock_payload(tmp_path, "lookup")
        # Single-line JSON, no internal whitespace — copy-pasteable.
        assert payload == '{"user_id":42,"question":"What is their email?"}'


def _import_app() -> Any:
    """Late import — keeps test collection cheap when only one class runs."""
    from movate.cli.main import app  # noqa: PLC0415

    return app
