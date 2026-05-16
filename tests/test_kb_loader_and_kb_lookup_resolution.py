"""kb_loader helper + kb-lookup skill's two-tier corpus resolution.

The May-2026 MVP scaffolds a `kb/` folder at the project root.
This bundle wires the bundled `kb-lookup` skill to actually CONSUME
that folder when it carries a project-specific corpus file.

Resolution order (call-time):

1. `<project_root>/kb/kb-lookup-corpus.json` (operator override)
2. `<skill_dir>/corpus.json` (bundled demo, fallback)

The same `kb_loader.resolve_kb_file` helper is the canonical pattern
for any future skill that needs project-specific data files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# resolve_kb_file — pure helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveKbFile:
    def test_returns_none_outside_a_project(self, tmp_path: Path) -> None:
        """No project marker anywhere up the tree → None (caller
        decides whether to error or fall back)."""
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        # tmp_path has no project.yaml / policy.yaml / movate.yaml
        # in it or any parent of pytest's temp tree.
        assert resolve_kb_file("any.json", start=tmp_path) is None

    def test_returns_none_when_file_missing_in_kb(self, tmp_path: Path) -> None:
        """Project found but `kb/<name>` doesn't exist → None."""
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        (tmp_path / "kb").mkdir()
        # No `missing.json` in kb/.
        assert resolve_kb_file("missing.json", start=tmp_path) is None

    def test_returns_path_when_file_exists(self, tmp_path: Path) -> None:
        """Project + kb/<name> exists → returns the resolved Path."""
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        target = kb_dir / "data.json"
        target.write_text("[]")
        result = resolve_kb_file("data.json", start=tmp_path)
        assert result == target
        assert result.is_file()

    def test_walks_up_from_nested_start(self, tmp_path: Path) -> None:
        """Skills live at `<project>/skills/<name>/impl.py`. Starting
        the walk from that location must still find the project root
        three levels above + its `kb/`."""
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "corpus.json").write_text("[]")

        # Simulate the skill's location.
        skill_dir = tmp_path / "skills" / "kb-lookup"
        skill_dir.mkdir(parents=True)

        result = resolve_kb_file("corpus.json", start=skill_dir)
        assert result == kb_dir / "corpus.json"

    def test_recognizes_all_three_marker_filenames(self, tmp_path: Path) -> None:
        """The walk respects the same marker set as the rest of the
        CLI (project.yaml / policy.yaml / movate.yaml)."""
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        for fname in ("project.yaml", "policy.yaml", "movate.yaml"):
            d = tmp_path / fname.split(".")[0]
            d.mkdir()
            (d / fname).write_text("agents_dir: ./agents\n")
            (d / "kb").mkdir()
            (d / "kb" / "test.json").write_text("[]")
            assert resolve_kb_file("test.json", start=d) is not None, (
                f"{fname} not recognized as project marker"
            )

    def test_defaults_start_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting `start` walks from cwd (convenience for tests +
        ad-hoc callers)."""
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        (tmp_path / "kb").mkdir()
        (tmp_path / "kb" / "x.json").write_text("[]")
        monkeypatch.chdir(tmp_path)
        result = resolve_kb_file("x.json")
        assert result is not None and result.name == "x.json"

    def test_agent_local_tier_deployed_runtime(self, tmp_path: Path) -> None:
        """Simulates the deployed-runtime layout: agent bundle at
        <agents_path>/<agent_name>/ with skills/ and kb/ subdirs.

        When mdk deploy bundles kb/*.json into the agent dir, a skill
        running inside <agent_name>/skills/<skill_name>/ must resolve
        its corpus via the agent-local <agent_name>/kb/<name> tier —
        without any project marker file being present.
        """
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        # Simulate: /agents/ticket-triager/
        agent_dir = tmp_path / "agents" / "ticket-triager"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text("name: ticket-triager\n")

        # kb/ bundled alongside agent.yaml by mdk deploy
        (agent_dir / "kb").mkdir()
        corpus = agent_dir / "kb" / "kb-lookup-corpus.json"
        corpus.write_text("[]")

        # Skill lives at <agent_dir>/skills/<skill>/
        skill_dir = agent_dir / "skills" / "kb-lookup"
        skill_dir.mkdir(parents=True)

        result = resolve_kb_file("kb-lookup-corpus.json", start=skill_dir)
        assert result == corpus

    def test_agent_local_falls_through_to_project_when_kb_absent(
        self, tmp_path: Path
    ) -> None:
        """If the agent boundary is found but kb/<name> is missing,
        the walk continues and finds the project-level kb/ instead.

        This handles the case where an operator didn't add their corpus
        to the project but the skill is still running locally.
        """
        from movate.core.kb_loader import resolve_kb_file  # noqa: PLC0415

        # Project root with a real corpus.
        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        (tmp_path / "kb").mkdir()
        project_corpus = tmp_path / "kb" / "kb-lookup-corpus.json"
        project_corpus.write_text("[]")

        # Agent dir present but WITHOUT a local kb/ override.
        agent_dir = tmp_path / "agents" / "ticket-triager"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text("name: ticket-triager\n")
        # No agent_dir / "kb" created.

        skill_dir = agent_dir / "skills" / "kb-lookup"
        skill_dir.mkdir(parents=True)

        result = resolve_kb_file("kb-lookup-corpus.json", start=skill_dir)
        assert result == project_corpus


# ---------------------------------------------------------------------------
# kb-lookup skill: corpus path resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKbLookupCorpusResolution:
    def test_falls_back_to_bundled_when_no_project_kb(self, tmp_path: Path) -> None:
        """The bundled `corpus.json` ships next to `impl.py`. When
        the skill runs without a project KB override (e.g. in a
        tmpdir scaffold for `mdk skills test`), the corpus path
        resolves to the bundled file."""
        # Load the impl module and exercise `_resolve_corpus_path`
        # directly with __file__ pointing at the bundled location.
        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        impl_path = TEMPLATES_DIR / "skill_kb_lookup" / "impl.py"
        ns: dict[str, object] = {"__file__": str(impl_path)}
        exec(compile(impl_path.read_text(), str(impl_path), "exec"), ns)
        resolver = ns["_resolve_corpus_path"]
        assert callable(resolver)
        path = resolver()  # type: ignore[operator]
        assert isinstance(path, Path)
        # No project context → bundled corpus path returned.
        assert path.name == "corpus.json"
        assert "skill_kb_lookup" in str(path)
        assert path.is_file()

    def test_picks_up_project_kb_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When `<project>/kb/kb-lookup-corpus.json` exists AND the
        skill is invoked from inside the project, the resolver picks
        the project file (not the bundled one)."""
        # Build a fake project layout.
        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        override = kb_dir / "kb-lookup-corpus.json"
        # Tiny custom corpus matching the schema.
        override.write_text(
            json.dumps(
                [
                    {
                        "id": "CUSTOM-001",
                        "category": "billing",
                        "title": "Custom override entry",
                        "symptom": "test",
                        "resolution": "test resolution",
                        "tags": ["test"],
                    }
                ]
            )
        )

        # Simulate the skill's location inside this project.
        skill_dir = tmp_path / "skills" / "kb-lookup"
        skill_dir.mkdir(parents=True)
        # Copy impl.py + corpus.json to the simulated location so
        # __file__-relative paths work.
        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        src_impl = TEMPLATES_DIR / "skill_kb_lookup" / "impl.py"
        src_corpus = TEMPLATES_DIR / "skill_kb_lookup" / "corpus.json"
        (skill_dir / "impl.py").write_text(src_impl.read_text())
        (skill_dir / "corpus.json").write_text(src_corpus.read_text())

        # Exec impl.py with __file__ pointing at the simulated location.
        ns: dict[str, object] = {"__file__": str(skill_dir / "impl.py")}
        exec(
            compile(
                (skill_dir / "impl.py").read_text(),
                str(skill_dir / "impl.py"),
                "exec",
            ),
            ns,
        )
        resolver = ns["_resolve_corpus_path"]
        path = resolver()  # type: ignore[operator]
        # The OVERRIDE is picked up, not the bundled copy.
        assert path == override
        # And reading from it yields our custom entry.
        loaded = json.loads(path.read_text())
        assert loaded[0]["id"] == "CUSTOM-001"

    def test_end_to_end_run_uses_project_kb(self, tmp_path: Path) -> None:
        """End-to-end via the skill's `run()` function — the search
        result reflects the project's KB corpus, not the bundled
        demo data."""
        import asyncio  # noqa: PLC0415
        import sys  # noqa: PLC0415
        from importlib import util as _ilutil  # noqa: PLC0415

        from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

        # Same project + skill layout as the previous test.
        (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb-lookup-corpus.json").write_text(
            json.dumps(
                [
                    {
                        "id": "PROJ-001",
                        "category": "billing",
                        "title": "Project-specific billing case",
                        "symptom": "unique-marker-token",
                        "resolution": "specific to the operator's KB",
                        "tags": ["unique-marker-token"],
                    }
                ]
            )
        )
        skill_dir = tmp_path / "skills" / "kb-lookup"
        skill_dir.mkdir(parents=True)
        src_impl = TEMPLATES_DIR / "skill_kb_lookup" / "impl.py"
        src_corpus = TEMPLATES_DIR / "skill_kb_lookup" / "corpus.json"
        (skill_dir / "impl.py").write_text(src_impl.read_text())
        (skill_dir / "corpus.json").write_text(src_corpus.read_text())

        # Import the impl as a real module so async + Path(__file__) work.
        spec = _ilutil.spec_from_file_location("_test_kb_lookup_impl", skill_dir / "impl.py")
        assert spec and spec.loader
        module = _ilutil.module_from_spec(spec)
        sys.modules["_test_kb_lookup_impl"] = module
        try:
            spec.loader.exec_module(module)

            # Minimal SkillExecutionContext-shaped object (only need
            # the fields the skill reads; `del ctx` in this skill
            # means we can pass anything truthy).
            class _Ctx:
                call_ms_budget = None
                trace_id = "test"
                tenant_id = "test"
                run_id = "test"

            result = asyncio.run(module.run({"query": "unique-marker-token"}, _Ctx()))
            matches = result["matches"]
            assert matches, "expected at least one match"
            # The match's id MUST come from the project KB (PROJ-001),
            # NOT from the bundled corpus (KB-001…KB-010).
            assert matches[0]["id"] == "PROJ-001", (
                f"resolver picked the wrong corpus: {matches[0]['id']}"
            )
        finally:
            sys.modules.pop("_test_kb_lookup_impl", None)
