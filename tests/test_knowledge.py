"""RAG surface (Phase J-4) — module + CLI tests.

Coverage:
* Store: Document + Chunk shape; InMemoryStore add/get/list/all_chunks
* chunk_document: paragraph split; offsets correct; empty chunks dropped
* Loader: knowledge.yaml parse; path resolution; failure modes
* Retriever: substring bonus + word-overlap; top-k cap; empty query
* CLI: add (creates yaml + appends + replaces by id), list, query
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.knowledge import (
    Document,
    InMemoryStore,
    KnowledgeLoadError,
    load_knowledge,
    retrieve,
)
from movate.knowledge.store import chunk_document, make_document

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Module: store + chunking
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_document_computes_content_hash() -> None:
    doc = make_document(doc_id="a", body="hello")
    assert doc.content_hash != ""
    # Same body → same hash (content-addressed semantics).
    assert make_document(doc_id="b", body="hello").content_hash == doc.content_hash
    # Different body → different hash.
    assert make_document(doc_id="c", body="bye").content_hash != doc.content_hash


@pytest.mark.unit
def test_chunk_document_splits_on_blank_lines() -> None:
    doc = Document(id="d", body="Para one.\n\nPara two.\n\nPara three.")
    chunks = chunk_document(doc)
    assert len(chunks) == 3
    assert chunks[0].text == "Para one."
    assert chunks[1].text == "Para two."
    assert chunks[2].text == "Para three."
    # Chunk indices increment.
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    # Offsets are non-decreasing.
    offsets = [c.offset for c in chunks]
    assert offsets == sorted(offsets)


@pytest.mark.unit
def test_chunk_document_drops_empty_chunks() -> None:
    """Extra blank lines shouldn't produce empty chunks."""
    doc = Document(id="d", body="A\n\n\n\nB\n\n   \n\nC")
    chunks = chunk_document(doc)
    assert [c.text for c in chunks] == ["A", "B", "C"]


@pytest.mark.unit
def test_in_memory_store_add_get_list() -> None:
    store = InMemoryStore()
    doc = make_document(doc_id="d1", body="Hello world")
    store.add(doc)
    assert store.get("d1") is not None
    assert store.get("d1").body == "Hello world"  # type: ignore[union-attr]
    assert store.get("missing") is None
    docs = store.list_documents()
    assert len(docs) == 1


@pytest.mark.unit
def test_in_memory_store_add_replaces_same_id() -> None:
    store = InMemoryStore()
    store.add(make_document(doc_id="d1", body="v1"))
    store.add(make_document(doc_id="d1", body="v2"))
    # Same id → second add wins; only one entry.
    assert len(store.list_documents()) == 1
    assert store.get("d1").body == "v2"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Module: loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_knowledge_happy_path(tmp_path: Path) -> None:
    (tmp_path / "doc1.md").write_text("First doc")
    (tmp_path / "doc2.md").write_text("Second doc")
    (tmp_path / "knowledge.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Knowledge",
                "documents": [
                    {"id": "first", "path": "./doc1.md", "description": "first"},
                    {"id": "second", "path": "./doc2.md", "tags": ["a", "b"]},
                ],
            }
        )
    )
    store = load_knowledge(tmp_path / "knowledge.yaml")
    assert len(store.list_documents()) == 2
    first = store.get("first")
    assert first is not None
    assert first.body == "First doc"
    assert first.description == "first"
    second = store.get("second")
    assert second is not None
    assert second.tags == ("a", "b")


@pytest.mark.unit
def test_load_knowledge_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeLoadError, match="not found"):
        load_knowledge(tmp_path / "nope.yaml")


@pytest.mark.unit
def test_load_knowledge_unsupported_extension_errors(tmp_path: Path) -> None:
    (tmp_path / "doc.bin").write_text("binary")
    (tmp_path / "knowledge.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Knowledge",
                "documents": [{"id": "d", "path": "./doc.bin"}],
            }
        )
    )
    with pytest.raises(KnowledgeLoadError, match="unsupported extension"):
        load_knowledge(tmp_path / "knowledge.yaml")


@pytest.mark.unit
def test_load_knowledge_missing_doc_errors(tmp_path: Path) -> None:
    (tmp_path / "knowledge.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Knowledge",
                "documents": [{"id": "ghost", "path": "./does-not-exist.md"}],
            }
        )
    )
    with pytest.raises(KnowledgeLoadError, match="does not exist"):
        load_knowledge(tmp_path / "knowledge.yaml")


@pytest.mark.unit
def test_load_knowledge_unsupported_api_version_errors(tmp_path: Path) -> None:
    (tmp_path / "knowledge.yaml").write_text(
        yaml.safe_dump({"api_version": "movate/v99", "kind": "Knowledge"})
    )
    with pytest.raises(KnowledgeLoadError, match="unsupported api_version"):
        load_knowledge(tmp_path / "knowledge.yaml")


# ---------------------------------------------------------------------------
# Module: retriever
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retrieve_substring_bonus_ranks_exact_matches_higher() -> None:
    store = InMemoryStore()
    store.add(make_document(doc_id="d1", body="SQL injection is a security risk."))
    store.add(make_document(doc_id="d2", body="Database access patterns."))
    results = retrieve("SQL injection", store, top_k=2)
    assert len(results) >= 1
    # The substring match should rank first.
    assert results[0].chunk.doc_id == "d1"


@pytest.mark.unit
def test_retrieve_word_overlap_finds_keyword_matches() -> None:
    """Even without a substring hit, token overlap pulls relevant chunks."""
    store = InMemoryStore()
    store.add(make_document(doc_id="d1", body="injection is a security."))
    store.add(make_document(doc_id="d2", body="unrelated content here."))
    results = retrieve("injection security", store, top_k=2)
    assert len(results) >= 1
    assert results[0].chunk.doc_id == "d1"


@pytest.mark.unit
def test_retrieve_top_k_caps_results() -> None:
    store = InMemoryStore()
    for i in range(10):
        store.add(make_document(doc_id=f"d{i}", body=f"injection number {i}"))
    results = retrieve("injection", store, top_k=3)
    assert len(results) == 3


@pytest.mark.unit
def test_retrieve_empty_query_returns_no_results() -> None:
    store = InMemoryStore()
    store.add(make_document(doc_id="d1", body="any content"))
    assert retrieve("   ...   ", store) == []


@pytest.mark.unit
def test_retrieve_snippet_is_truncated_with_ellipsis() -> None:
    """Long chunks render with a trailing ellipsis in the snippet."""
    long_text = "injection " + ("padding " * 200)
    store = InMemoryStore()
    store.add(make_document(doc_id="d1", body=long_text))
    results = retrieve("injection", store, top_k=1, max_snippet_chars=50)
    assert results[0].snippet.endswith("...")
    assert len(results[0].snippet) <= len(long_text)


# ---------------------------------------------------------------------------
# CLI: mdk knowledge add
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from a fresh project dir so the CLI's default
    ./knowledge.yaml lands in tmp_path."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.unit
def test_knowledge_add_creates_yaml_with_new_entry(project_root: Path) -> None:
    """`add` against a fresh project creates knowledge.yaml + registers
    the document with the right shape."""
    (project_root / "doc.md").write_text("Hello world")
    result = runner.invoke(app, ["knowledge", "add", "doc.md", "--id", "hello"])
    assert result.exit_code == 0, result.stdout + result.stderr

    yaml_path = project_root / "knowledge.yaml"
    assert yaml_path.is_file()
    raw = yaml.safe_load(yaml_path.read_text())
    assert raw["api_version"] == "movate/v1"
    assert raw["kind"] == "Knowledge"
    assert len(raw["documents"]) == 1
    assert raw["documents"][0]["id"] == "hello"


@pytest.mark.unit
def test_knowledge_add_replaces_existing_entry(project_root: Path) -> None:
    """Re-adding with the same --id replaces the prior entry (operator
    decision — no silent duplication)."""
    (project_root / "doc.md").write_text("v1")
    runner.invoke(app, ["knowledge", "add", "doc.md", "--id", "x"])
    # Re-add with same id but a different description
    (project_root / "doc.md").write_text("v2")
    result = runner.invoke(
        app,
        ["knowledge", "add", "doc.md", "--id", "x", "--description", "updated"],
    )
    assert result.exit_code == 0
    raw = yaml.safe_load((project_root / "knowledge.yaml").read_text())
    # Single entry, with the new description.
    assert len(raw["documents"]) == 1
    assert raw["documents"][0]["description"] == "updated"
    assert "updated" in result.stdout.lower()


@pytest.mark.unit
def test_knowledge_add_default_id_is_filename_stem(project_root: Path) -> None:
    """Without --id, the document is registered as its file stem."""
    (project_root / "my-glossary.md").write_text("...")
    result = runner.invoke(app, ["knowledge", "add", "my-glossary.md"])
    assert result.exit_code == 0
    raw = yaml.safe_load((project_root / "knowledge.yaml").read_text())
    assert raw["documents"][0]["id"] == "my-glossary"


@pytest.mark.unit
def test_knowledge_add_nonexistent_path_exits_two(project_root: Path) -> None:
    result = runner.invoke(app, ["knowledge", "add", "ghost.md"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# CLI: mdk knowledge list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_knowledge_list_empty_prints_hint(project_root: Path) -> None:
    """No knowledge.yaml → friendly hint pointing at `knowledge add`."""
    result = runner.invoke(app, ["knowledge", "list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no" in result.stdout.lower() and "knowledge" in result.stdout.lower()
    assert "add" in result.stdout.lower()


@pytest.mark.unit
def test_knowledge_list_renders_registered_entries(project_root: Path) -> None:
    (project_root / "doc.md").write_text("...")
    runner.invoke(
        app,
        [
            "knowledge",
            "add",
            "doc.md",
            "--id",
            "my-doc",
            "--description",
            "test doc",
            "--tags",
            "a,b",
        ],
    )
    result = runner.invoke(app, ["knowledge", "list"])
    assert result.exit_code == 0
    assert "my-doc" in result.stdout
    assert "test doc" in result.stdout


@pytest.mark.unit
def test_knowledge_list_json_emits_parseable(project_root: Path) -> None:
    (project_root / "doc.md").write_text("...")
    runner.invoke(app, ["knowledge", "add", "doc.md", "--id", "d"])
    result = runner.invoke(app, ["knowledge", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["id"] == "d"


# ---------------------------------------------------------------------------
# CLI: mdk knowledge query
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_knowledge_query_renders_matches(project_root: Path) -> None:
    (project_root / "doc.md").write_text(
        "SQL injection is a class of vulnerability.\n\nAnother paragraph."
    )
    runner.invoke(app, ["knowledge", "add", "doc.md", "--id", "sec"])
    result = runner.invoke(app, ["knowledge", "query", "SQL injection"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "sec" in result.stdout
    # Snippet contains the matched substring.
    assert "injection" in result.stdout.lower()


@pytest.mark.unit
def test_knowledge_query_no_matches_prints_hint(project_root: Path) -> None:
    (project_root / "doc.md").write_text("unrelated content here.")
    runner.invoke(app, ["knowledge", "add", "doc.md", "--id", "u"])
    result = runner.invoke(app, ["knowledge", "query", "zzzzzzzz"])
    assert result.exit_code == 0
    assert "no matches" in result.stdout.lower()


@pytest.mark.unit
def test_knowledge_query_json_emits_parseable(project_root: Path) -> None:
    (project_root / "doc.md").write_text("injection here.")
    runner.invoke(app, ["knowledge", "add", "doc.md", "--id", "j"])
    result = runner.invoke(app, ["knowledge", "query", "injection", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["doc_id"] == "j"
    assert "score" in payload[0]
    assert "snippet" in payload[0]


@pytest.mark.unit
def test_knowledge_query_no_knowledge_yaml_exits_two(project_root: Path) -> None:
    """Without a knowledge.yaml, query exits 2 with a hint."""
    result = runner.invoke(app, ["knowledge", "query", "anything"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "knowledge add" in combined.lower()
