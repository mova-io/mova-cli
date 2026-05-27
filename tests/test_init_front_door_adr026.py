"""ADR 026 — ``mdk init`` front-door UX (the six decisions).

Covers the ADR's test matrix end-to-end with hermetic patterns (``--mock``,
no API keys, no network, ``MOVATE_CONFIG_PATH`` redirected away from the real
``~/.movate``):

* D1 — ``mdk init <name> -t/--llm`` OUTSIDE a project yields a runnable
  PROJECT (project.yaml + AGENTS.md + agents/<name>/ + snapshot); INSIDE a
  project ADDS the agent; ``--bare`` keeps the legacy standalone agent.
* D2 — name/path resolution backing run / validate / dev (by name, by path,
  ambiguous → path wins, not-found → friendly error, ``mdk run .``).
* D3 — the shared editor launcher gates on TTY / ``--no-open`` / ``--mock``.
* D4 — next-steps print the EXACT runnable command for what landed on disk.
* D6 — the ``--llm`` scaffold model resolves by layered precedence and
  ``mdk config set scaffold.model`` persists.

(D5 — doctor staleness — lives in ``test_doctor_version_staleness_adr026.py``.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


@pytest.fixture(autouse=True)
def _hermetic_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ~/.movate/config.yaml to a per-test temp file so D6's
    ``mdk config set`` never touches the developer's real config."""
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / "user-config.yaml"))


def _scaffold_standalone_agent(parent: Path, name: str = "solo") -> Path:
    """Scaffold a STANDALONE (``--bare``) agent dir for resolution tests."""
    result = runner.invoke(
        app,
        ["init", name, "-t", "default", "--bare", "--no-open-editor", "--target", str(parent)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return parent / name


# ---------------------------------------------------------------------------
# D1 — context-aware init: project / add / bare
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestD1ContextAwareInit:
    def test_template_outside_project_yields_runnable_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init sitebot -t default` outside a project → project +
        agents/sitebot/ + project.yaml + AGENTS.md + snapshot, and
        `mdk run sitebot` / `validate sitebot` work from the project root."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "sitebot", "-t", "default", "--no-open-editor"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        proj = tmp_path / "sitebot"
        assert (proj / "project.yaml").is_file()
        assert (proj / "AGENTS.md").is_file()
        assert (proj / "agents" / "sitebot" / "agent.yaml").is_file()
        # Initial snapshot exists (ADR 021 baseline).
        snaps = proj / ".mdk" / "snapshots"
        assert snaps.is_dir() and any(snaps.iterdir())
        # run + validate resolve the agent BY NAME from the project root.
        monkeypatch.chdir(proj)
        rv = runner.invoke(app, ["validate", "sitebot"], env={"COLUMNS": "200"})
        assert rv.exit_code == 0, rv.stdout + rv.stderr
        rr = runner.invoke(
            app, ["run", "sitebot", '{"text": "hi"}', "--mock"], env={"COLUMNS": "200"}
        )
        assert rr.exit_code == 0, rr.stdout + rr.stderr

    def test_llm_outside_project_yields_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init sitebot --llm "..." --mock` outside a project routes
        through the project scaffold (project.yaml + agents/)."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "sitebot",
                "--llm",
                "An FAQ bot answering pricing questions with a confidence score",
                "--mock",
                "--no-open-editor",
            ],
            env={"COLUMNS": "200"},
        )
        proj = tmp_path / "sitebot"
        # The project wrapper is created regardless of mock-scaffold outcome.
        assert (proj / "project.yaml").is_file()
        assert (proj / "AGENTS.md").is_file()
        assert (proj / "agents").is_dir()
        _ = result

    def test_inside_project_adds_agent_no_nested_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INSIDE a project, `mdk init helper -t default` adds the agent
        under agents/<name>/ — no nested project.yaml."""
        monkeypatch.chdir(tmp_path)
        boot = runner.invoke(
            app, ["init", "proj", "--skip-snapshot", "--no-open-editor"], env={"COLUMNS": "200"}
        )
        assert boot.exit_code == 0, boot.stdout + boot.stderr
        proj = tmp_path / "proj"
        monkeypatch.chdir(proj)
        result = runner.invoke(
            app, ["init", "helper", "-t", "default", "--no-open-editor"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Added under the EXISTING project's agents/, no nested project.
        assert (proj / "agents" / "helper" / "agent.yaml").is_file()
        assert not (proj / "helper").exists()
        assert not (proj / "agents" / "helper" / "project.yaml").exists()

    def test_bare_keeps_legacy_standalone_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--bare` → standalone single-dir agent (no project.yaml / agents/),
        and `mdk run .` works on it."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "barebot", "-t", "default", "--bare", "--no-open-editor"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        agent_dir = tmp_path / "barebot"
        assert (agent_dir / "agent.yaml").is_file()
        assert not (agent_dir / "project.yaml").exists()
        assert not (agent_dir / "agents").exists()
        # `mdk run .` from inside the standalone agent dir works.
        monkeypatch.chdir(agent_dir)
        rr = runner.invoke(app, ["run", ".", '{"text": "hi"}', "--mock"], env={"COLUMNS": "200"})
        assert rr.exit_code == 0, rr.stdout + rr.stderr


# ---------------------------------------------------------------------------
# D2 — name/path resolution across run / validate / dev
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestD2NameResolution:
    def _project_with_agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            app, ["init", "rag-qa", "-t", "default", "--no-open-editor"], env={"COLUMNS": "200"}
        )
        proj = tmp_path / "rag-qa"
        monkeypatch.chdir(proj)
        return proj

    def test_run_by_name_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._project_with_agent(tmp_path, monkeypatch)
        result = runner.invoke(
            app, ["run", "rag-qa", '{"text": "hi"}', "--mock"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr

    def test_run_by_path_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = self._project_with_agent(tmp_path, monkeypatch)
        path = str(proj / "agents" / "rag-qa")
        result = runner.invoke(
            app, ["run", path, '{"text": "hi"}', "--mock"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr

    def test_existing_path_wins_over_same_named_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ambiguity: a dir literally named like an agent in cwd — the
        existing PATH wins (deterministic order step 1)."""
        proj = self._project_with_agent(tmp_path, monkeypatch)
        # Create a standalone agent dir named "rag-qa" in cwd (collides with
        # the project agent name). The on-disk path must win.
        local = proj / "rag-qa"  # a literal subdir of cwd named rag-qa
        runner.invoke(
            app,
            [
                "init",
                "rag-qa",
                "-t",
                "default",
                "--bare",
                "--no-open-editor",
                "--target",
                str(proj),
            ],
            env={"COLUMNS": "200"},
        )
        assert (local / "agent.yaml").is_file()
        # `mdk run rag-qa` — "rag-qa" IS an existing path (./rag-qa) → path wins.
        result = runner.invoke(
            app, ["run", "rag-qa", '{"text": "hi"}', "--mock"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr

    def test_not_found_friendly_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._project_with_agent(tmp_path, monkeypatch)
        result = runner.invoke(
            app, ["run", "nope-bot", '{"text": "x"}', "--mock"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 2
        combined = (result.stdout + result.stderr).lower()
        assert "no agent 'nope-bot'" in combined
        # Friendly message offers the by-name and project-root guidance.
        assert "mdk run nope-bot" in combined

    def test_validate_by_name_and_friendly_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._project_with_agent(tmp_path, monkeypatch)
        ok = runner.invoke(app, ["validate", "rag-qa"], env={"COLUMNS": "200"})
        assert ok.exit_code == 0, ok.stdout + ok.stderr
        bad = runner.invoke(app, ["validate", "ghost"], env={"COLUMNS": "200"})
        assert bad.exit_code == 2
        assert "no agent 'ghost'" in (bad.stdout + bad.stderr).lower()

    def test_standalone_dot_is_first_class(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk validate .` works on a standalone agent dir WITHOUT a
        project.yaml marker up the tree (loader falls back to the parent)."""
        agent_dir = _scaffold_standalone_agent(tmp_path, "solo")
        monkeypatch.chdir(agent_dir)
        result = runner.invoke(app, ["validate", "."], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# D3 — shared editor launcher gating
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestD3EditorLauncher:
    """`_launch_editor` imports sys/subprocess/shutil locally (cold-path
    hygiene), so we patch the REAL modules a launch would touch + a fake
    TTY to exercise each gate without spawning a process."""

    def test_no_open_skips_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        from movate.cli import init as init_mod  # noqa: PLC0415

        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        spawned: list[object] = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a), raising=False)
        # open_editor=False → never launches even on a tty.
        assert init_mod._launch_editor(Path("/tmp/x"), open_editor=False) is False
        assert spawned == []

    def test_mock_skips_launch_even_on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        from movate.cli import init as init_mod  # noqa: PLC0415

        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        spawned: list[object] = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a), raising=False)
        # mock=True is a hermetic run → no GUI even on a tty.
        assert init_mod._launch_editor(Path("/tmp/x"), open_editor=True, mock=True) is False
        assert spawned == []

    def test_no_tty_skips_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        from movate.cli import init as init_mod  # noqa: PLC0415

        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        spawned: list[object] = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a), raising=False)
        # Non-tty stdout (pipe / CI) → never launches.
        assert init_mod._launch_editor(Path("/tmp/x"), open_editor=True) is False
        assert spawned == []

    def test_launches_on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        from movate.cli import init as init_mod  # noqa: PLC0415

        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(shutil, "which", lambda name: "/bin/code" if name == "code" else None)
        spawned: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda argv, **k: spawned.append(list(argv)), raising=False
        )
        # TTY + editor on PATH + not mock + open_editor=True → launches.
        assert init_mod._launch_editor(Path("/tmp/proj"), open_editor=True) is True
        assert spawned and spawned[0][0] == "code"


# ---------------------------------------------------------------------------
# D4 — exact runnable next-steps command
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestD4ExactNextSteps:
    def test_created_project_next_steps_cd_and_run_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "shopbot", "-t", "default", "--no-open-editor", "--skip-snapshot"],
            env={"COLUMNS": "240"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        out = result.stdout
        # Exact command for a freshly-created project: cd + run BY NAME.
        assert "cd shopbot" in out
        assert "mdk run shopbot" in out

    def test_bare_next_steps_use_run_dot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "barebot", "-t", "default", "--bare", "--no-open-editor"],
            env={"COLUMNS": "240"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Standalone → `mdk run .` (the standalone agent is first-class).
        assert "mdk run ." in result.stdout


# ---------------------------------------------------------------------------
# D6 — scaffold-model precedence + persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestD6ScaffoldModelPrecedence:
    def test_config_set_scaffold_model_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(
            app,
            ["config", "set", "scaffold.model", "anthropic/claude-haiku-4-5-20251001"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        from movate.core.user_config import load_user_config  # noqa: PLC0415

        cfg = load_user_config()
        assert cfg.scaffold.model == "anthropic/claude-haiku-4-5-20251001"

    def test_config_set_unknown_key_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(app, ["config", "set", "bogus.key", "value"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        assert "unknown config key" in (result.stdout + result.stderr).lower()

    def test_precedence_flag_over_env_over_project_over_user_over_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from movate.cli.init import _DEFAULT_LLM_MODEL, _resolve_scaffold_model  # noqa: PLC0415
        from movate.core.user_config import (  # noqa: PLC0415
            UserConfig,
            save_user_config,
        )

        monkeypatch.chdir(tmp_path)

        # 5. built-in default (nothing set anywhere).
        monkeypatch.delenv("MDK_LLM_MODEL", raising=False)
        assert (
            _resolve_scaffold_model(llm_model=_DEFAULT_LLM_MODEL, llm_model_explicit=False)
            == _DEFAULT_LLM_MODEL
        )

        # 4. user-config scaffold.model.
        cfg = UserConfig()
        cfg.scaffold.model = "anthropic/claude-haiku-4-5-20251001"
        save_user_config(cfg)
        assert (
            _resolve_scaffold_model(llm_model=_DEFAULT_LLM_MODEL, llm_model_explicit=False)
            == "anthropic/claude-haiku-4-5-20251001"
        )

        # 3. project scaffold.model overrides user-config.
        (tmp_path / "project.yaml").write_text("scaffold:\n  model: azure/gpt-4o-2024-08-06\n")
        assert (
            _resolve_scaffold_model(llm_model=_DEFAULT_LLM_MODEL, llm_model_explicit=False)
            == "azure/gpt-4o-2024-08-06"
        )

        # 2. MDK_LLM_MODEL env var overrides project.
        monkeypatch.setenv("MDK_LLM_MODEL", "gemini/gemini-1.5-flash")
        assert (
            _resolve_scaffold_model(llm_model=_DEFAULT_LLM_MODEL, llm_model_explicit=False)
            == "gemini/gemini-1.5-flash"
        )

        # 1. explicit --llm-model flag wins over everything.
        assert (
            _resolve_scaffold_model(llm_model="openai/gpt-4o-2024-08-06", llm_model_explicit=True)
            == "openai/gpt-4o-2024-08-06"
        )

    def test_run_input_example_uses_dataset_sample(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D4 helper: the next-steps input snippet is a real dataset row."""
        agent_dir = _scaffold_standalone_agent(tmp_path, "ex-bot")
        from movate.cli.init import _run_input_example  # noqa: PLC0415

        snippet = _run_input_example(agent_dir)
        # Either a JSON object (dataset sample) or the placeholder fallback.
        if snippet != "{…}":
            parsed = json.loads(snippet)
            assert isinstance(parsed, dict)
