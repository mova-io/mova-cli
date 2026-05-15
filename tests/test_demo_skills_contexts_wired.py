"""Demo-flow skills + contexts wired into the three role agents.

Layers REAL content on top of the foundation PR's mechanisms:

- Three skill templates (`web-search`, `lint-runner`, `kb-lookup`)
  with working impls registered in `SKILL_TEMPLATES` — auto-scaffolded
  by name when an agent declares them.
- Hand-written context Markdown shipped inside each demo agent
  template's `contexts/` subdir — copied into the project's contexts/
  on `mdk add` via the new `_maybe_copy_template_contexts` helper.
- `agent.yaml` for `rag-qa`, `ticket-triager`, `code-reviewer` declares
  the appropriate skills + contexts so the wiring fires on `mdk add`.

The customer-demo path now lights up:

    mdk init --project support-bot --with-agents rag-qa,ticket-triager,code-reviewer

produces a project where each agent has skills (with working impls)
and contexts (with curated content) already in place. Operators
can `mdk run <agent>` without writing any glue.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Skill templates ship + register
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkillTemplatesShip:
    @pytest.mark.parametrize(
        "skill_name,dirname",
        [
            ("web-search", "skill_web_search"),
            ("lint-runner", "skill_lint_runner"),
            ("kb-lookup", "skill_kb_lookup"),
        ],
    )
    def test_skill_template_dir_exists_with_required_files(
        self, skill_name: str, dirname: str
    ) -> None:
        """Each curated skill template ships skill.yaml + impl.py +
        README.md."""
        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        template_dir = TEMPLATES_DIR / dirname
        assert template_dir.is_dir(), f"missing template dir: {template_dir}"
        for required in ("skill.yaml", "impl.py", "README.md"):
            assert (template_dir / required).is_file(), f"{skill_name} missing {required}"

    @pytest.mark.parametrize("skill_name", ["web-search", "lint-runner", "kb-lookup"])
    def test_skill_template_registered(self, skill_name: str) -> None:
        """The SKILL_TEMPLATES registry maps each curated name."""
        from movate.templates import SKILL_TEMPLATES  # noqa: PLC0415

        assert skill_name in SKILL_TEMPLATES, (
            f"{skill_name} not in SKILL_TEMPLATES; auto-scaffold would "
            f"fall back to the default echo template"
        )


# ---------------------------------------------------------------------------
# Per-name skill scaffold dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPerNameSkillDispatch:
    def test_named_skill_uses_curated_template(self, tmp_path: Path) -> None:
        """`_scaffold_one_skill(name='web-search', ...)` should copy
        skill_web_search/, not skill_init/."""
        from movate.cli.add_cmd import _scaffold_one_skill  # noqa: PLC0415

        project_root = tmp_path / "proj"
        project_root.mkdir()
        _scaffold_one_skill(name="web-search", project_root=project_root)

        skill_yaml = project_root / "skills" / "web-search" / "skill.yaml"
        assert skill_yaml.is_file()
        # The curated web-search template's description mentions
        # DuckDuckGo; default echo template wouldn't.
        assert "DuckDuckGo" in skill_yaml.read_text()

    def test_unnamed_skill_falls_back_to_default(self, tmp_path: Path) -> None:
        """A skill name not in SKILL_TEMPLATES uses the default echo
        template — still gets a working stub."""
        from movate.cli.add_cmd import _scaffold_one_skill  # noqa: PLC0415

        project_root = tmp_path / "proj"
        project_root.mkdir()
        _scaffold_one_skill(name="some-ad-hoc-skill", project_root=project_root)

        skill_yaml = project_root / "skills" / "some-ad-hoc-skill" / "skill.yaml"
        assert skill_yaml.is_file()
        # Default echo template is what landed.
        body = skill_yaml.read_text()
        assert "some-ad-hoc-skill" in body  # name stamped in

    def test_name_substitution_walks_all_files(self, tmp_path: Path) -> None:
        """The new walk-and-substitute path (not just skill.yaml)
        should stamp the skill name across impl.py + README.md too."""
        from movate.cli.add_cmd import _scaffold_one_skill  # noqa: PLC0415

        project_root = tmp_path / "proj"
        project_root.mkdir()
        _scaffold_one_skill(name="my-search", project_root=project_root)

        skill_dir = project_root / "skills" / "my-search"
        # No __SKILL_NAME__ leftovers anywhere in the scaffolded dir.
        for path in skill_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            assert "__SKILL_NAME__" not in text, f"unsubstituted placeholder in {path}"


# ---------------------------------------------------------------------------
# Template contexts ship + are copied on `mdk add`
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTemplateContextsShip:
    @pytest.mark.parametrize(
        "agent,context_files",
        [
            ("rag-qa", ["grounded-qa-rubric.md"]),
            ("ticket-triager", ["triage-rubric.md", "support-tone.md"]),
            ("code-reviewer", ["review-rubric.md"]),
        ],
    )
    def test_context_files_ship_with_template(self, agent: str, context_files: list[str]) -> None:
        """Each demo agent's template directory contains a `contexts/`
        subdir with the expected curated Markdown files."""
        from movate.templates import get_template_path  # noqa: PLC0415

        template_dir = get_template_path(agent)
        contexts_dir = template_dir / "contexts"
        assert contexts_dir.is_dir(), f"{agent}: missing contexts/ subdir"
        for ctx_file in context_files:
            f = contexts_dir / ctx_file
            assert f.is_file(), f"{agent}: missing {ctx_file}"
            # The content is non-trivial (≥30 lines) — these are
            # hand-written rubrics, not stubs.
            assert len(f.read_text().splitlines()) >= 30, (
                f"{agent}/contexts/{ctx_file} looks truncated"
            )


@pytest.mark.unit
class TestContextCopyHelper:
    def test_copies_template_contexts_into_project(self, tmp_path: Path) -> None:
        """`_maybe_copy_template_contexts(template_dir=, project_root=)`
        copies `.md` files from template_dir/contexts/ to
        project_root/contexts/."""
        from movate.cli.add_cmd import _maybe_copy_template_contexts  # noqa: PLC0415
        from movate.templates import get_template_path  # noqa: PLC0415

        template_dir = get_template_path("rag-qa")
        project_root = tmp_path / "proj"
        project_root.mkdir()

        copied = _maybe_copy_template_contexts(template_dir=template_dir, project_root=project_root)
        assert "grounded-qa-rubric" in copied
        landed = project_root / "contexts" / "grounded-qa-rubric.md"
        assert landed.is_file()
        # Content survives the copy.
        assert "Grounded Q&A Rubric" in landed.read_text()

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        """If a context with the same name already exists in the
        project, the template version is NOT copied over it."""
        from movate.cli.add_cmd import _maybe_copy_template_contexts  # noqa: PLC0415
        from movate.templates import get_template_path  # noqa: PLC0415

        template_dir = get_template_path("rag-qa")
        project_root = tmp_path / "proj"
        contexts_dir = project_root / "contexts"
        contexts_dir.mkdir(parents=True)
        existing = contexts_dir / "grounded-qa-rubric.md"
        existing.write_text("# Custom version — don't clobber")

        copied = _maybe_copy_template_contexts(template_dir=template_dir, project_root=project_root)
        # Nothing copied; existing file preserved.
        assert copied == []
        assert existing.read_text() == "# Custom version — don't clobber"

    def test_returns_empty_for_template_without_contexts(self, tmp_path: Path) -> None:
        """Templates that DON'T ship a `contexts/` subdir produce
        an empty list (not an error)."""
        from movate.cli.add_cmd import _maybe_copy_template_contexts  # noqa: PLC0415
        from movate.templates import get_template_path  # noqa: PLC0415

        # `default` (agent_init) has no contexts/ subdir.
        template_dir = get_template_path("default")
        project_root = tmp_path / "proj"
        project_root.mkdir()
        assert (
            _maybe_copy_template_contexts(template_dir=template_dir, project_root=project_root)
            == []
        )


# ---------------------------------------------------------------------------
# Demo agents declare skills + contexts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDemoAgentsDeclareSkillsAndContexts:
    @pytest.mark.parametrize(
        "agent,expected_skills,expected_contexts",
        [
            ("rag-qa", ["web-search"], ["grounded-qa-rubric"]),
            (
                "ticket-triager",
                ["kb-lookup"],
                ["triage-rubric", "support-tone"],
            ),
            ("code-reviewer", ["lint-runner"], ["review-rubric"]),
        ],
    )
    def test_agent_yaml_declares_expected_skills_and_contexts(
        self,
        agent: str,
        expected_skills: list[str],
        expected_contexts: list[str],
    ) -> None:
        """Each demo agent.yaml has the right `skills:` + `contexts:`
        wired so `mdk add <agent>` triggers the right scaffolding."""
        from movate.templates import get_template_path  # noqa: PLC0415

        agent_yaml = get_template_path(agent) / "agent.yaml"
        data = yaml.safe_load(agent_yaml.read_text())
        assert sorted(data.get("skills", [])) == sorted(expected_skills), (
            f"{agent}: skills mismatch"
        )
        assert sorted(data.get("contexts", [])) == sorted(expected_contexts), (
            f"{agent}: contexts mismatch"
        )


# ---------------------------------------------------------------------------
# End-to-end: `mdk add rag-qa` in a fresh project wires everything
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEndToEndAddRagQa:
    def test_mdk_add_rag_qa_creates_skill_and_context_in_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mdk init --project foo && cd foo && mdk add rag-qa` should:

        - Create the agent at `agents/rag-qa/`.
        - Copy `contexts/grounded-qa-rubric.md` from the template.
        - Auto-scaffold `skills/web-search/` with the curated impl.
        """
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "support-bot", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        project = tmp_path / "support-bot"
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["add", "rag-qa"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr

        # Agent landed.
        assert (project / "agents" / "rag-qa" / "agent.yaml").is_file()

        # Context copied from the template (curated content, not a stub).
        ctx = project / "contexts" / "grounded-qa-rubric.md"
        assert ctx.is_file()
        assert "Grounded Q&A Rubric" in ctx.read_text()

        # Skill auto-scaffolded with the curated DuckDuckGo impl
        # (not the default echo).
        skill_yaml = project / "skills" / "web-search" / "skill.yaml"
        assert skill_yaml.is_file()
        assert "DuckDuckGo" in skill_yaml.read_text()
        # impl.py uses httpx (proves the curated template, not echo).
        impl = project / "skills" / "web-search" / "impl.py"
        assert "httpx" in impl.read_text()

    def test_mdk_add_code_reviewer_includes_corpus_for_kb_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin behavior of the kb-lookup skill specifically — its
        `corpus.json` MUST land in the scaffolded skill dir alongside
        impl.py, otherwise the impl raises FileNotFoundError on first
        call."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            app,
            ["init", "--project", "x", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        project = tmp_path / "x"
        monkeypatch.chdir(project)
        result = runner.invoke(app, ["add", "ticket-triager"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr

        corpus = project / "skills" / "kb-lookup" / "corpus.json"
        assert corpus.is_file(), (
            "kb-lookup skill scaffold lost corpus.json — impl.py would "
            "raise FileNotFoundError on first call"
        )
        # Corpus is the 10-entry mock data we shipped.
        import json  # noqa: PLC0415

        entries = json.loads(corpus.read_text())
        assert len(entries) >= 10, "mock corpus shrunk unexpectedly"


# ---------------------------------------------------------------------------
# Curated skill impls actually load (basic smoke)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkillImplsImportable:
    def test_web_search_impl_imports(self) -> None:
        """The shipped web-search impl.py at least imports cleanly."""
        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        impl_path = TEMPLATES_DIR / "skill_web_search" / "impl.py"
        # exec in a clean namespace; check for the `run` function.
        # Pre-populate __file__ so impls that compute paths from it
        # (e.g. kb-lookup loading corpus.json) don't NameError.
        ns: dict[str, object] = {"__file__": str(impl_path)}
        exec(
            compile(impl_path.read_text(), str(impl_path), "exec"),
            ns,
        )
        assert "run" in ns, "web-search impl missing run()"

    def test_lint_runner_impl_imports(self) -> None:
        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        impl_path = TEMPLATES_DIR / "skill_lint_runner" / "impl.py"
        ns: dict[str, object] = {"__file__": str(impl_path)}
        exec(
            compile(impl_path.read_text(), str(impl_path), "exec"),
            ns,
        )
        assert "run" in ns

    def test_kb_lookup_impl_imports(self) -> None:
        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        impl_path = TEMPLATES_DIR / "skill_kb_lookup" / "impl.py"
        ns: dict[str, object] = {"__file__": str(impl_path)}
        exec(
            compile(impl_path.read_text(), str(impl_path), "exec"),
            ns,
        )
        assert "run" in ns
