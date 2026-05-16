"""Sprint N (polish sweep) — missing-kb-corpus and large-context scanner tests.

Covers the two scanners added in the polish sweep:

* missing-kb-corpus — kb-lookup skill declared but no corpus JSON present
* large-context     — declared context file exceeds 4 096 B advisory limit
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from movate.audit.scanners import (
    SCANNERS,
    scan_large_context,
    scan_missing_kb_corpus,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_agent(
    tmp_path: Path,
    *,
    name: str = "demo",
    skills: list[str] | None = None,
    contexts: list[str] | None = None,
) -> Path:
    """Build a minimal agent dir under tmp_path/agents/<name>/."""
    agent_dir = tmp_path / "agents" / name
    agent_dir.mkdir(parents=True)

    lines = [
        "api_version: movate/v1",
        "kind: Agent",
        f"name: {name}",
        "model:",
        "  provider: openai/gpt-4o-mini-2024-07-18",
        "prompt: ./prompt.md",
    ]
    if skills:
        lines.append("skills:")
        for s in skills:
            lines.append(f"  - {s}")
    if contexts:
        lines.append("contexts:")
        for c in contexts:
            lines.append(f"  - {c}")

    (agent_dir / "agent.yaml").write_text("\n".join(lines) + "\n")
    (agent_dir / "prompt.md").write_text("answer the question\n")
    return agent_dir


# ---------------------------------------------------------------------------
# missing-kb-corpus
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMissingKbCorpus:
    def test_no_kb_skill_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, skills=["summarize"])
        findings = scan_missing_kb_corpus(agent_dir, "demo")
        assert findings == []

    def test_kb_skill_no_corpus_flagged(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, skills=["kb-lookup"])
        findings = scan_missing_kb_corpus(agent_dir, "demo")
        assert len(findings) == 1
        f = findings[0]
        assert f.category == "missing-kb-corpus"
        assert f.severity.value == "warning"
        assert "kb-lookup-corpus.json" in f.message

    def test_agent_local_kb_satisfies(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, skills=["kb-lookup"])
        kb_dir = agent_dir / "kb"
        kb_dir.mkdir()
        corpus = kb_dir / "kb-lookup-corpus.json"
        corpus.write_text(json.dumps([{"id": "KB-1", "title": "T", "resolution": "R"}]))
        findings = scan_missing_kb_corpus(agent_dir, "demo")
        assert findings == []

    def test_project_level_kb_satisfies(self, tmp_path: Path) -> None:
        # project root = tmp_path; agents live at tmp_path/agents/<name>/
        # so project-level kb/ is at tmp_path/kb/ (agent_dir.parent.parent / "kb")
        agent_dir = _make_agent(tmp_path, skills=["kb-lookup"])
        project_kb = tmp_path / "kb"
        project_kb.mkdir()
        (project_kb / "kb-lookup-corpus.json").write_text(
            json.dumps([{"id": "KB-1", "title": "T", "resolution": "R"}])
        )
        findings = scan_missing_kb_corpus(agent_dir, "demo")
        assert findings == []

    def test_no_agent_yaml_no_finding(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "empty"
        agent_dir.mkdir(parents=True)
        findings = scan_missing_kb_corpus(agent_dir, "empty")
        assert findings == []

    def test_hint_mentions_mdk_add_kb(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, skills=["kb-lookup"])
        findings = scan_missing_kb_corpus(agent_dir, "demo")
        assert findings and "mdk add kb" in (findings[0].hint or "")


# ---------------------------------------------------------------------------
# large-context
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLargeContext:
    def test_no_contexts_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        findings = scan_large_context(agent_dir, "demo")
        assert findings == []

    def test_small_context_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, contexts=["style"])
        ctx_dir = tmp_path / "contexts"
        ctx_dir.mkdir()
        (ctx_dir / "style.md").write_text("# Style\n\nBe concise.\n")
        findings = scan_large_context(agent_dir, "demo")
        assert findings == []

    def test_large_project_context_flagged(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, contexts=["big"])
        ctx_dir = tmp_path / "contexts"
        ctx_dir.mkdir()
        (ctx_dir / "big.md").write_text("x" * 5_000)
        findings = scan_large_context(agent_dir, "demo")
        assert len(findings) == 1
        f = findings[0]
        assert f.category == "large-context"
        assert f.severity.value == "warning"
        assert "big" in f.message
        assert "5,000" in f.message

    def test_agent_local_context_beats_project_level(self, tmp_path: Path) -> None:
        # Agent-local small context file shadows the large project-level one.
        agent_dir = _make_agent(tmp_path, contexts=["rubric"])
        # Large project-level file
        (tmp_path / "contexts").mkdir()
        (tmp_path / "contexts" / "rubric.md").write_text("x" * 5_000)
        # Small agent-local file
        local_ctx = agent_dir / "contexts"
        local_ctx.mkdir()
        (local_ctx / "rubric.md").write_text("small local override\n")
        findings = scan_large_context(agent_dir, "demo")
        assert findings == []

    def test_missing_context_file_skipped(self, tmp_path: Path) -> None:
        # Declared but absent context — large-context scanner ignores it
        # (validate covers missing files separately).
        agent_dir = _make_agent(tmp_path, contexts=["ghost"])
        findings = scan_large_context(agent_dir, "demo")
        assert findings == []

    def test_multiple_contexts_all_checked(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, contexts=["a", "b"])
        ctx_dir = tmp_path / "contexts"
        ctx_dir.mkdir()
        (ctx_dir / "a.md").write_text("x" * 5_000)
        (ctx_dir / "b.md").write_text("x" * 6_000)
        findings = scan_large_context(agent_dir, "demo")
        assert len(findings) == 2
        cats = {f.category for f in findings}
        assert cats == {"large-context"}

    def test_no_agent_yaml_no_finding(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agents" / "empty"
        agent_dir.mkdir(parents=True)
        findings = scan_large_context(agent_dir, "empty")
        assert findings == []


# ---------------------------------------------------------------------------
# Integration: new scanners registered in SCANNERS map
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_v3_scanners_registered() -> None:
    """Both polish-sweep scanners must appear in the SCANNERS registry."""
    assert "missing-kb-corpus" in SCANNERS
    assert "large-context" in SCANNERS
