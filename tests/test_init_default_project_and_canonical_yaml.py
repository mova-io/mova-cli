"""`mdk init` defaults to project mode + project.yaml is canonical.

Two behavior changes in this bundle:

1. **Bare `mdk init <name>` scaffolds a PROJECT** (was: a single
   agent). The dispatch is "template OR llm present → agent mode,
   else → project mode." Operators reach agent mode by passing
   `-t <template>` or `--llm`.

2. **`project.yaml` is canonical, self-documenting**. Every
   layered-config block (defaults / policy / runtime / skills / eval /
   bench) ships either uncommented (active) or commented (as
   enable-by-uncomment examples) with one-line annotations. The
   file is meant to be readable top-to-bottom as the reference
   for project-level configuration — no docs round-trip required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Default behavior: `mdk init <name>` → project mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitDefaultsToProject:
    def test_bare_init_name_creates_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init my-proj` (no `-t`, no `--llm`) creates a project."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "my-proj", "--skip-snapshot"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        # Project markers present.
        proj = tmp_path / "my-proj"
        assert (proj / "project.yaml").is_file()
        assert (proj / "agents").is_dir()
        assert (proj / "skills").is_dir()
        assert (proj / "contexts").is_dir()
        assert (proj / "kb").is_dir()
        # And NOT an agent (no agent.yaml at the root).
        assert not (proj / "agent.yaml").exists()

    def test_init_with_template_still_scaffolds_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init my-agent -t faq` keeps the agent-scaffold behavior."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "my-agent", "-t", "default", "--target", str(tmp_path)],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Agent markers present.
        agent_dir = tmp_path / "my-agent"
        assert (agent_dir / "agent.yaml").is_file()
        assert (agent_dir / "prompt.md").is_file()
        # And NOT a project (no project.yaml at the agent dir level).
        assert not (agent_dir / "project.yaml").exists()

    def test_init_with_llm_still_scaffolds_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init my-bot --llm "..." --mock` runs the LLM-scaffold
        path (agent mode, not project mode)."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "my-bot",
                "--llm",
                "A simple chatbot agent",
                "--mock",
                "--target",
                str(tmp_path),
            ],
            env={"COLUMNS": "200"},
        )
        # MockProvider may or may not produce a valid LLM scaffold —
        # what we care about is the dispatch went to AGENT mode, not
        # project mode. So: no project.yaml at the dir level.
        agent_dir = tmp_path / "my-bot"
        # The agent dir might not have been finalized if mock scaffold
        # validation failed, but the dispatch can't have produced a
        # project.yaml here.
        assert not (agent_dir / "project.yaml").exists()
        # And `agents/` definitely shouldn't exist (would mean project mode).
        assert not (agent_dir / "agents").is_dir()
        _ = result  # exit code varies on mock-LLM JSON content

    def test_positional_description_still_routes_to_agent_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The positional-description shorthand (`mdk init <name>
        "<desc>"`) treats the second positional as `--llm` and routes
        to agent mode, not project mode."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "my-bot",
                "A simple agent for testing",
                "--mock",
                "--target",
                str(tmp_path),
            ],
            env={"COLUMNS": "200"},
        )
        # Same as above — no project.yaml at the dir level.
        assert not (tmp_path / "my-bot" / "project.yaml").exists()
        assert not (tmp_path / "my-bot" / "agents").is_dir()
        _ = result

    def test_init_inside_existing_project_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a default-mode init would nest a project inside an
        existing one, surface a warning pointing operators at
        `mdk add` (which is what they probably meant)."""
        # Bootstrap a parent project.
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            app,
            ["init", "parent-proj", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        # Then cd into it and run `mdk init` again (which would nest).
        monkeypatch.chdir(tmp_path / "parent-proj")
        result = runner.invoke(
            app,
            ["init", "nested-proj", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        # Succeeds (nesting is allowed; the operator might genuinely
        # want it), but stderr carries the heads-up.
        assert result.exit_code == 0, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "nested" in combined.lower()
        assert "mdk add" in combined  # the suggested alternative


# ---------------------------------------------------------------------------
# Canonical project.yaml content
# ---------------------------------------------------------------------------


# Each layered-config block must appear in the canonical template
# (active or commented-out) so operators see the full surface area
# without leaving the file. Tests check for the marker prefix that
# disambiguates "this block exists in the file" from "the word
# happens to appear in prose."
_REQUIRED_CANONICAL_BLOCKS = (
    "agents_dir:",  # active
    "workflows_dir:",  # active
    "skills_dir:",  # active
    "contexts_dir:",  # active
    "defaults:",  # active
    "# policy:",  # commented example
    "# runtime:",  # commented example
    "# skills:",  # commented example
    "# eval:",  # commented example
    "# bench:",  # commented example
)


@pytest.mark.unit
class TestCanonicalProjectYaml:
    def test_every_layered_config_block_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The canonical project.yaml documents every project-level
        configuration block in the file body. Operators get a
        complete-by-construction reference without doc lookups."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", "demo", "--skip-snapshot"], env={"COLUMNS": "200"})
        body = (tmp_path / "demo" / "project.yaml").read_text()
        missing = [marker for marker in _REQUIRED_CANONICAL_BLOCKS if marker not in body]
        assert not missing, f"project.yaml missing canonical blocks: {missing}"

    def test_kb_convention_documented(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kb/ folder convention is documented in the canonical
        file — operators see WHERE to drop knowledge files."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", "demo", "--skip-snapshot"])
        body = (tmp_path / "demo" / "project.yaml").read_text()
        assert "kb/" in body
        assert "kb_loader" in body or "resolve_kb_file" in body

    def test_legacy_filenames_documented(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The filename history (project.yaml → policy.yaml → movate.yaml)
        is called out so operators see the back-compat lineage in-file."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", "demo", "--skip-snapshot"])
        body = (tmp_path / "demo" / "project.yaml").read_text()
        for name in ("project.yaml", "policy.yaml", "movate.yaml"):
            assert name in body, f"filename {name!r} not documented"

    def test_snapshot_primitive_documented(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `.movate/snapshots/` story (immutable, content-addressed,
        operated on by `mdk diff` / `mdk rollback` / etc.) appears in
        the canonical file."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", "demo", "--skip-snapshot"])
        body = (tmp_path / "demo" / "project.yaml").read_text()
        assert "snapshot" in body.lower()
        assert "mdk diff" in body
        assert "mdk rollback" in body
        assert "content-addressed" in body.lower()

    def test_file_is_substantially_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Floor on file size. Pre-bundle: ~20 lines minimal. Canonical
        form: 150+ lines. The floor catches accidental truncation
        regressions; the actual file is well above."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", "demo", "--skip-snapshot"])
        body = (tmp_path / "demo" / "project.yaml").read_text()
        line_count = len(body.splitlines())
        assert line_count >= 100, (
            f"project.yaml is {line_count} lines; expected ≥100 for "
            f"the canonical self-documenting template"
        )

    def test_canonical_yaml_still_validates_as_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Despite the extensive comments + commented-out blocks, the
        YAML body MUST still parse cleanly into ProjectConfig."""
        from movate.core.config import ProjectConfig  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", "demo", "--skip-snapshot"])
        body = (tmp_path / "demo" / "project.yaml").read_text()
        data = yaml.safe_load(body)
        cfg = ProjectConfig.model_validate(data)
        # Defaults survive the canonical render.
        assert cfg.agents_dir == "./agents"
        assert cfg.skills_dir == "./skills"
        assert cfg.contexts_dir == "./contexts"
