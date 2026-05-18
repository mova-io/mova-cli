"""MVP bundle (May 2026): kb/ folder + project.yaml + per-agent contexts.

Three operator-facing changes to land by Monday:

1. `mdk init --project` scaffolds `kb/` (alongside agents/, skills/,
   contexts/) with a README explaining what goes in.
2. `project.yaml` is the canonical project-config filename. Loader
   accepts `policy.yaml` + `movate.yaml` as legacy with one-shot
   deprecation warnings. `mdk init --project` writes `project.yaml`
   going forward. All walk-up sites (mdk add / validate / snapshot /
   etc.) recognize all three.
3. Per-agent contexts at `<agent_dir>/contexts/<name>.md` override
   project-level contexts at `<project>/contexts/<name>.md` on name
   collision. Resolution: project base → agent-local overlay.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap a fresh project, return project root."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["init", "--project", "demo", "--skip-snapshot"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return tmp_path / "demo"


# ---------------------------------------------------------------------------
# kb/ folder in scaffold
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKbFolderInScaffold:
    def test_kb_dir_created(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _bootstrap(tmp_path, monkeypatch)
        assert (proj / "kb").is_dir()

    def test_kb_has_gitkeep(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _bootstrap(tmp_path, monkeypatch)
        assert (proj / "kb" / ".gitkeep").is_file()

    def test_kb_has_readme(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = _bootstrap(tmp_path, monkeypatch)
        readme = proj / "kb" / "README.md"
        assert readme.is_file()
        body = readme.read_text()
        # README explains the purpose + concrete file shapes.
        assert "knowledge" in body.lower()
        assert "kb-lookup" in body  # cross-references the demo skill
        # File-shape table mentions the supported asset types.
        assert ".json" in body
        assert ".md" in body
        assert ".pdf" in body

    def test_project_panel_lists_kb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "demo", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert "kb/" in result.stdout


# ---------------------------------------------------------------------------
# project.yaml as canonical name
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProjectYamlCanonical:
    def test_bootstrap_writes_project_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init --project` writes `project.yaml` (not `movate.yaml`)."""
        proj = _bootstrap(tmp_path, monkeypatch)
        assert (proj / "project.yaml").is_file()
        # And does NOT write the legacy name.
        assert not (proj / "movate.yaml").exists()
        assert not (proj / "policy.yaml").exists()

    def test_project_yaml_validates_as_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bootstrapped project.yaml still passes ProjectConfig."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        proj = _bootstrap(tmp_path, monkeypatch)
        data = yaml.safe_load((proj / "project.yaml").read_text())
        ProjectConfig.model_validate(data)  # must not raise

    def test_load_project_config_picks_up_project_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`load_project_config` reads `project.yaml` when present."""
        from movate.core.config import load_project_config  # noqa: PLC0415

        (tmp_path / "project.yaml").write_text("agents_dir: ./custom-agents\n")
        monkeypatch.chdir(tmp_path)
        cfg = load_project_config()
        assert cfg.agents_dir == "./custom-agents"

    def test_legacy_movate_yaml_still_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`movate.yaml` works for back-compat. (Deprecation warning
        fires once per process; we don't assert on it here — the
        warning singleton is exercised in test_canonical_config_split.)"""
        from movate.core.config import load_project_config  # noqa: PLC0415

        (tmp_path / "movate.yaml").write_text("agents_dir: ./legacy\n")
        monkeypatch.chdir(tmp_path)
        cfg = load_project_config()
        assert cfg.agents_dir == "./legacy"

    def test_legacy_policy_yaml_still_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`policy.yaml` (legacy v1.x) also works for back-compat."""
        from movate.core.config import load_project_config  # noqa: PLC0415

        (tmp_path / "policy.yaml").write_text("agents_dir: ./policy-loaded\n")
        monkeypatch.chdir(tmp_path)
        cfg = load_project_config()
        assert cfg.agents_dir == "./policy-loaded"

    def test_project_yaml_wins_over_legacy_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All three files present → project.yaml is read; legacy
        files are ignored."""
        from movate.core.config import load_project_config  # noqa: PLC0415

        (tmp_path / "project.yaml").write_text("agents_dir: ./from-project\n")
        (tmp_path / "policy.yaml").write_text("agents_dir: ./from-policy\n")
        (tmp_path / "movate.yaml").write_text("agents_dir: ./from-movate\n")
        monkeypatch.chdir(tmp_path)
        cfg = load_project_config()
        assert cfg.agents_dir == "./from-project"

    def test_in_place_bootstrap_refuses_existing_project_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bootstrapping `mdk init --project` (in-place) errors when
        any marker file already exists."""
        (tmp_path / "project.yaml").write_text("existing: true\n")
        monkeypatch.chdir(tmp_path)
        # CI runs with FORCE_COLOR=1 and a narrow default terminal,
        # so Rich both inserts ANSI escapes and wraps long lines.
        # Strip ANSI + flatten whitespace before substring-checking
        # so the assertion isn't tangled in either issue.
        import re  # noqa: PLC0415

        result = runner.invoke(app, ["init", "--project"], env={"COLUMNS": "200"})
        assert result.exit_code == 2
        raw = result.stdout + result.stderr
        plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw)
        flat = " ".join(plain.split())
        assert "project.yaml already exists" in flat

    def test_in_place_bootstrap_refuses_legacy_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same protection extends to the legacy names — we don't
        clobber a project just because someone named it the old way."""
        (tmp_path / "movate.yaml").write_text("existing: true\n")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--project"])
        assert result.exit_code == 2


@pytest.mark.unit
class TestWalkUpRecognizesAllNames:
    def test_is_project_root_recognizes_all_three(self, tmp_path: Path) -> None:
        """The shared `is_project_root` helper accepts any of the
        three marker filenames."""
        from movate.core.config import is_project_root  # noqa: PLC0415

        # No marker → False.
        assert is_project_root(tmp_path) is False
        # Each marker individually → True.
        for fname in ("project.yaml", "policy.yaml", "movate.yaml"):
            d = tmp_path / fname.split(".")[0]
            d.mkdir()
            (d / fname).write_text("agents_dir: ./agents\n")
            assert is_project_root(d) is True, f"{fname} not recognized"

    def test_mdk_add_finds_project_via_project_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project rooted at `project.yaml` (no legacy file) must
        be discoverable by `mdk add`."""
        # Manual minimal bootstrap (avoid going through `mdk init`).
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "project.yaml").write_text("agents_dir: ./agents\n")
        (proj / "agents").mkdir()
        monkeypatch.chdir(proj)

        result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (proj / "agents" / "faq" / "agent.yaml").is_file()


# ---------------------------------------------------------------------------
# Per-agent contexts (agent-local overrides project-level)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPerAgentContexts:
    def test_project_level_context_loads(self, tmp_path: Path) -> None:
        """Pre-MVP behavior preserved — project-level contexts load
        when agent declares them and no agent-local override exists."""
        from movate.core.context_loader import load_context_registry  # noqa: PLC0415

        (tmp_path / "contexts").mkdir()
        (tmp_path / "contexts" / "style.md").write_text("# Project style")

        registry = load_context_registry(tmp_path)
        assert "style" in registry
        assert "# Project style" in registry["style"]

    def test_agent_local_context_loads(self, tmp_path: Path) -> None:
        """Agent-local context (with no project-level counterpart)
        loads when `agent_dir` is passed."""
        from movate.core.context_loader import load_context_registry  # noqa: PLC0415

        agent_dir = tmp_path / "agents" / "rag-qa"
        agent_dir.mkdir(parents=True)
        (agent_dir / "contexts").mkdir()
        (agent_dir / "contexts" / "rag-specific.md").write_text("# Agent-local")

        registry = load_context_registry(tmp_path, agent_dir=agent_dir)
        assert "rag-specific" in registry
        assert "# Agent-local" in registry["rag-specific"]

    def test_agent_local_overrides_project_level(self, tmp_path: Path) -> None:
        """When same name exists in both tiers, agent-local wins."""
        from movate.core.context_loader import load_context_registry  # noqa: PLC0415

        (tmp_path / "contexts").mkdir()
        (tmp_path / "contexts" / "style.md").write_text("# Project version")

        agent_dir = tmp_path / "agents" / "rag-qa"
        agent_dir.mkdir(parents=True)
        (agent_dir / "contexts").mkdir()
        (agent_dir / "contexts" / "style.md").write_text("# Agent override")

        registry = load_context_registry(tmp_path, agent_dir=agent_dir)
        # Both names present; agent-local body wins.
        assert "# Agent override" in registry["style"]
        assert "# Project version" not in registry["style"]

    def test_no_agent_dir_falls_back_to_project_only(self, tmp_path: Path) -> None:
        """When `agent_dir` is None, only project-level is read
        (back-compat: matches pre-MVP behavior bit-for-bit)."""
        from movate.core.context_loader import load_context_registry  # noqa: PLC0415

        (tmp_path / "contexts").mkdir()
        (tmp_path / "contexts" / "style.md").write_text("# Project only")
        registry = load_context_registry(tmp_path)
        assert registry == {"style": "# Project only"}

    def test_load_agent_uses_agent_local_override(self, tmp_path: Path) -> None:
        """End-to-end via `load_agent`: agent-local context overrides
        project-level when both exist + agent declares the name."""
        from movate.core.loader import load_agent  # noqa: PLC0415

        # Minimal project scaffolding.
        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        # Project-level context.
        (tmp_path / "contexts").mkdir()
        (tmp_path / "contexts" / "style.md").write_text("PROJECT-LEVEL CONTEXT BODY")
        # Agent dir + agent-local override.
        agent_dir = tmp_path / "agents" / "demo"
        agent_dir.mkdir(parents=True)
        (agent_dir / "contexts").mkdir()
        (agent_dir / "contexts" / "style.md").write_text("AGENT-LOCAL OVERRIDE BODY")
        # Minimal agent.yaml + prompt.md.
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\n"
            "kind: Agent\n"
            "name: demo\n"
            "version: 0.1.0\n"
            "model:\n"
            "  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n"
            "  input:\n"
            "    q: string\n"
            "  output:\n"
            "    a: string\n"
            "contexts:\n"
            "  - style\n"
        )
        (agent_dir / "prompt.md").write_text("Q: {{ input.q }}")

        bundle = load_agent(agent_dir)
        # One context resolved.
        assert len(bundle.contexts) == 1
        name, body = bundle.contexts[0]
        assert name == "style"
        assert body == "AGENT-LOCAL OVERRIDE BODY"
        # And the rendered prompt embeds the override, not the project copy.
        rendered = bundle.render_prompt({"q": "test"})
        assert "AGENT-LOCAL OVERRIDE BODY" in rendered
        assert "PROJECT-LEVEL CONTEXT BODY" not in rendered

    def test_load_agent_falls_back_to_project_when_no_local(self, tmp_path: Path) -> None:
        """When agent declares context X and only project-level X.md
        exists, that's resolved (not an error)."""
        from movate.core.loader import load_agent  # noqa: PLC0415

        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        (tmp_path / "contexts").mkdir()
        (tmp_path / "contexts" / "style.md").write_text("PROJECT BODY")
        agent_dir = tmp_path / "agents" / "demo"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\n"
            "kind: Agent\n"
            "name: demo\n"
            "version: 0.1.0\n"
            "model:\n"
            "  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n"
            "  input:\n"
            "    q: string\n"
            "  output:\n"
            "    a: string\n"
            "contexts:\n"
            "  - style\n"
        )
        (agent_dir / "prompt.md").write_text("Q: {{ input.q }}")
        bundle = load_agent(agent_dir)
        assert bundle.contexts[0][1] == "PROJECT BODY"
