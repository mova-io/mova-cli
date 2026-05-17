"""Agent → knowledge wiring: `spec.knowledge` resolves to a built retriever on the bundle.

PR #160 shipped the RAG surface (KnowledgeConfig + retrievers + the
`mdk knowledge query` CLI) but left the surface unwired from the
agent ecosystem — agents had no way to declare a knowledge source in
their `agent.yaml`. This polish-bundle adds the missing piece:

* `AgentSpec.knowledge: str | None` — path to a `knowledge.yaml`
  relative to the agent directory.
* `AgentBundle.retriever: Any` — populated when `spec.knowledge` is
  set, None otherwise.
* `load_agent()` resolves the knowledge config + builds the
  retriever (lazy import — agents without knowledge pay no cost).

Tests pin:
* default behavior (no knowledge → no retriever) — backward compat.
* happy path (declared knowledge → built retriever that queries).
* malformed knowledge.yaml → clean `AgentLoadError`.
* corpus missing → clean `AgentLoadError`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from movate.core.loader import AgentLoadError, load_agent
from movate.knowledge import BM25Retriever, SubstringRetriever

_TEMPLATE = (
    Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"
)


def _scaffold_agent(dst: Path, name: str = "test-agent") -> Path:
    """Copy the canonical agent template into ``dst`` and stamp the
    name — mirrors the helper in test_loader.py so the loader sees a
    real agent.yaml + prompt + schemas."""
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


def _write_corpus(path: Path, docs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(docs))


def _append_knowledge_field(agent_dir: Path, knowledge_path: str) -> None:
    """Append a ``knowledge:`` field to the scaffolded agent.yaml."""
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(
        yaml_path.read_text() + f"\nknowledge: {knowledge_path}\n"
    )


# ---------------------------------------------------------------------------
# Default: no knowledge declared → bundle.retriever is None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_without_knowledge_has_no_retriever(tmp_path: Path) -> None:
    """Backwards compatibility: every existing agent has no
    ``spec.knowledge`` and must continue to load with
    ``bundle.retriever is None``. No KnowledgeConfig import should
    fire for these agents (lazy)."""
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    assert bundle.spec.knowledge is None
    assert bundle.retriever is None


# ---------------------------------------------------------------------------
# Happy path: knowledge.yaml declared + present + corpus exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_with_knowledge_yaml_builds_bm25_retriever(tmp_path: Path) -> None:
    """End-to-end: agent.yaml declares ``knowledge: ./knowledge.yaml``,
    that file picks BM25, the loader resolves it, and the resulting
    bundle.retriever actually answers queries against the local
    corpus."""
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    _write_corpus(
        agent_dir / "kb" / "corpus.json",
        [
            {
                "id": "A",
                "title": "Login fails after password reset",
                "body": "User cannot log in even after resetting the password.",
                "tags": ["login", "auth"],
            },
            {
                "id": "B",
                "title": "Billing duplicate charge",
                "body": "Customer was charged twice for the same invoice.",
                "tags": ["billing", "stripe"],
            },
        ],
    )
    (agent_dir / "knowledge.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Knowledge\n"
        "retriever: bm25\n"
        "corpus: ./kb/corpus.json\n"
        "body_fields: [title, body]\n"
    )
    _append_knowledge_field(agent_dir, "./knowledge.yaml")

    bundle = load_agent(agent_dir)
    assert bundle.spec.knowledge == "./knowledge.yaml"
    assert isinstance(bundle.retriever, BM25Retriever)
    # The built retriever answers queries against the agent's corpus.
    hits = bundle.retriever.query("login password reset", top_k=1)
    assert hits[0].doc_id == "A"


@pytest.mark.unit
def test_agent_can_select_substring_retriever_via_knowledge_yaml(
    tmp_path: Path,
) -> None:
    """``retriever: substring`` in knowledge.yaml picks the cheap
    baseline — useful for tiny corpora where BM25 IDF is noisy."""
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    _write_corpus(
        agent_dir / "kb" / "corpus.json",
        [{"id": "A", "title": "first", "body": "alpha beta"}],
    )
    (agent_dir / "knowledge.yaml").write_text(
        "retriever: substring\n"
        "corpus: ./kb/corpus.json\n"
        "body_fields: [title, body]\n"
    )
    _append_knowledge_field(agent_dir, "./knowledge.yaml")

    bundle = load_agent(agent_dir)
    assert isinstance(bundle.retriever, SubstringRetriever)


@pytest.mark.unit
def test_knowledge_yaml_in_nested_subdir_resolves_against_agent_dir(
    tmp_path: Path,
) -> None:
    """``spec.knowledge: ./rag/knowledge.yaml`` — the loader resolves
    against the agent dir; the corpus path inside the YAML resolves
    against the knowledge.yaml's location."""
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    rag_dir = agent_dir / "rag"
    rag_dir.mkdir()
    _write_corpus(
        rag_dir / "corpus.json",
        [{"id": "A", "title": "ok", "body": "alpha"}],
    )
    (rag_dir / "knowledge.yaml").write_text(
        "retriever: bm25\ncorpus: ./corpus.json\nbody_fields: [title, body]\n"
    )
    _append_knowledge_field(agent_dir, "./rag/knowledge.yaml")

    bundle = load_agent(agent_dir)
    assert isinstance(bundle.retriever, BM25Retriever)
    assert bundle.retriever.query("alpha", top_k=1)[0].doc_id == "A"


# ---------------------------------------------------------------------------
# Error paths: clean AgentLoadError, not a raw exception
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_knowledge_yaml_surfaces_as_agent_load_error(
    tmp_path: Path,
) -> None:
    """If ``spec.knowledge`` points at a file that doesn't exist, the
    error surfaces as an :class:`AgentLoadError` (the loader's
    canonical taxonomy) — not a bare KnowledgeLoadError. Operators
    see "knowledge resolution failed: knowledge config not found: …"
    not a confusing import-from-deep-module trace."""
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    _append_knowledge_field(agent_dir, "./missing.yaml")

    with pytest.raises(AgentLoadError, match="knowledge resolution failed"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_malformed_knowledge_yaml_surfaces_as_agent_load_error(
    tmp_path: Path,
) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "knowledge.yaml").write_text(": : : :\n")
    _append_knowledge_field(agent_dir, "./knowledge.yaml")

    with pytest.raises(AgentLoadError, match="knowledge resolution failed"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_corpus_missing_surfaces_as_agent_load_error(tmp_path: Path) -> None:
    """The knowledge.yaml parses, but its ``corpus:`` path doesn't
    exist. Same single error class so operators have one thing to
    catch."""
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "knowledge.yaml").write_text(
        "retriever: bm25\ncorpus: ./kb/missing.json\nbody_fields: [title, body]\n"
    )
    _append_knowledge_field(agent_dir, "./knowledge.yaml")

    with pytest.raises(AgentLoadError, match="knowledge resolution failed"):
        load_agent(agent_dir)
