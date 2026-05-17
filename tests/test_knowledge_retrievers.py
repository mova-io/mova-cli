"""RAG surface v0.7 — store + retrievers + loader + CLI.

Covers BACKLOG #127 deliverables:

* :class:`InMemoryCorpus.from_path` — JSON-array on disk → memory.
* :class:`BM25Retriever` — IDF + length normalization + tag bonus.
* :class:`SubstringRetriever` — token-overlap baseline.
* :func:`load_knowledge_config` — knowledge.yaml → KnowledgeConfig.
* :func:`build_retriever` — config → retriever.
* ``mdk knowledge query`` — CLI wraps the retrievers.

Embeddings + reranking land in v0.8 — out of scope for these tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import KnowledgeConfig, KnowledgeRetrieverKind
from movate.knowledge import (
    BM25Retriever,
    InMemoryCorpus,
    KnowledgeLoadError,
    KnowledgeStoreError,
    SubstringRetriever,
    build_retriever,
    load_knowledge_config,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Sample corpora
# ---------------------------------------------------------------------------


_DOCS: list[dict[str, Any]] = [
    {
        "id": "KB-001",
        "title": "Login fails after password reset",
        "symptom": "user cannot log in even after resetting password",
        "resolution": (
            "Clear browser cookies and try again; if still failing, "
            "force password reset via admin console."
        ),
        "tags": ["login", "auth", "password"],
    },
    {
        "id": "KB-002",
        "title": "API responses are slow",
        "symptom": "dashboard takes more than 10 seconds to load",
        "resolution": "Check the rate-limit dashboard and confirm no upstream incidents.",
        "tags": ["api", "performance", "latency"],
    },
    {
        "id": "KB-003",
        "title": "Billing duplicate charge",
        "symptom": "customer charged twice on credit card",
        "resolution": "Refund duplicate via Stripe dashboard and email confirmation.",
        "tags": ["billing", "stripe", "refund"],
    },
]


def _write_corpus(tmp_path: Path, docs: list[dict[str, Any]]) -> Path:
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(docs))
    return p


# ---------------------------------------------------------------------------
# InMemoryCorpus
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInMemoryCorpus:
    def test_loads_json_array_from_disk(self, tmp_path: Path) -> None:
        p = _write_corpus(tmp_path, _DOCS)
        c = InMemoryCorpus.from_path(p)
        assert len(c) == 3
        assert c.entries[0]["id"] == "KB-001"

    def test_missing_file_raises_with_path_in_message(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(KnowledgeStoreError, match="not found"):
            InMemoryCorpus.from_path(tmp_path / "nope.json")

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(KnowledgeStoreError, match="not valid JSON"):
            InMemoryCorpus.from_path(p)

    def test_non_array_root_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "obj.json"
        p.write_text('{"a": 1}')
        with pytest.raises(KnowledgeStoreError, match="JSON array"):
            InMemoryCorpus.from_path(p)

    def test_non_dict_entry_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "mixed.json"
        p.write_text('[{"id":"a"}, "not a dict"]')
        with pytest.raises(KnowledgeStoreError, match=r"entry \[1\]"):
            InMemoryCorpus.from_path(p)

    def test_direct_constructor_works_for_fixtures(self) -> None:
        c = InMemoryCorpus(entries=_DOCS)
        assert len(c) == 3


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBM25Retriever:
    def test_finds_relevant_doc_for_simple_query(self) -> None:
        r = BM25Retriever(
            InMemoryCorpus(entries=_DOCS),
            body_fields=["title", "symptom", "resolution"],
        )
        hits = r.query("login password reset", top_k=2)
        assert len(hits) >= 1
        # KB-001 talks about login + password — best match.
        assert hits[0].doc_id == "KB-001"
        assert hits[0].score > 0

    def test_ranks_by_relevance_not_corpus_order(self) -> None:
        """Query that's strongly about billing should rank KB-003
        first even though it's the LAST doc in the corpus."""
        r = BM25Retriever(
            InMemoryCorpus(entries=_DOCS),
            body_fields=["title", "symptom", "resolution"],
        )
        hits = r.query("billing duplicate charge stripe", top_k=3)
        assert hits[0].doc_id == "KB-003"

    def test_tag_match_bonus_lifts_otherwise_lower_doc(self) -> None:
        """A query token matching an entry's tag should push that
        entry above docs that only have body-text overlap."""
        # Two docs with identical bodies; one has the tag.
        docs = [
            {"id": "A", "title": "the system is slow", "body": "very slow"},
            {
                "id": "B",
                "title": "the system is slow",
                "body": "very slow",
                "tags": ["performance"],
            },
        ]
        r = BM25Retriever(
            InMemoryCorpus(entries=docs), body_fields=["title", "body"]
        )
        hits = r.query("performance", top_k=2)
        assert hits[0].doc_id == "B"
        assert hits[0].score > (hits[1].score if len(hits) > 1 else 0)

    def test_empty_query_returns_no_hits(self) -> None:
        r = BM25Retriever(
            InMemoryCorpus(entries=_DOCS), body_fields=["title", "body"]
        )
        assert r.query("", top_k=5) == []
        assert r.query("   ", top_k=5) == []

    def test_top_k_caps_results(self) -> None:
        r = BM25Retriever(
            InMemoryCorpus(entries=_DOCS),
            body_fields=["title", "symptom", "resolution"],
        )
        # "the" + similar stop-ish tokens shouldn't generate hits,
        # but a broad query like "user" matches multiple docs.
        hits = r.query("dashboard customer log", top_k=1)
        assert len(hits) <= 1

    def test_unknown_query_terms_return_empty(self) -> None:
        r = BM25Retriever(
            InMemoryCorpus(entries=_DOCS),
            body_fields=["title", "symptom", "resolution"],
        )
        assert r.query("xyzzy quux nonsense", top_k=5) == []

    def test_id_field_defaults_to_index_when_absent(self) -> None:
        """A corpus where entries have no `id` field still produces
        deterministic doc_ids (stringified index)."""
        docs = [
            {"title": "first", "body": "alpha beta"},
            {"title": "second", "body": "gamma delta"},
        ]
        r = BM25Retriever(
            InMemoryCorpus(entries=docs), body_fields=["title", "body"]
        )
        hits = r.query("alpha", top_k=1)
        assert hits[0].doc_id == "0"


# ---------------------------------------------------------------------------
# Substring
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSubstringRetriever:
    def test_counts_token_overlap(self) -> None:
        r = SubstringRetriever(
            InMemoryCorpus(entries=_DOCS),
            body_fields=["title", "symptom", "resolution"],
        )
        hits = r.query("login password reset", top_k=3)
        assert hits[0].doc_id == "KB-001"
        # Score is integer overlap count + tag bonus.
        assert hits[0].score >= 1

    def test_empty_query_returns_no_hits(self) -> None:
        r = SubstringRetriever(
            InMemoryCorpus(entries=_DOCS), body_fields=["title", "body"]
        )
        assert r.query("", top_k=5) == []


# ---------------------------------------------------------------------------
# KnowledgeConfig + loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnowledgeConfig:
    def test_defaults_lock_the_v07_surface(self) -> None:
        """The defaults are the contract: existing agents that ship
        knowledge.yaml with just ``corpus:`` set get BM25 + sensible
        weights without touching anything else."""
        cfg = KnowledgeConfig(corpus="./kb/corpus.json")
        assert cfg.retriever == KnowledgeRetrieverKind.BM25
        assert cfg.top_k == 5
        assert cfg.body_fields == ["title", "body"]
        assert cfg.tag_field == "tags"
        assert cfg.id_field == "id"

    def test_empty_body_fields_rejected(self) -> None:
        with pytest.raises(
            ValueError, match="body_fields must contain at least one"
        ):
            KnowledgeConfig(corpus="./kb/corpus.json", body_fields=[])

    def test_top_k_outside_1_to_50_rejected(self) -> None:
        with pytest.raises(ValueError):
            KnowledgeConfig(corpus="./kb/corpus.json", top_k=0)
        with pytest.raises(ValueError):
            KnowledgeConfig(corpus="./kb/corpus.json", top_k=51)

    def test_extra_field_rejected(self) -> None:
        """``extra='forbid'`` so typos in the YAML surface as clean
        errors instead of silently being ignored."""
        with pytest.raises(ValueError):
            KnowledgeConfig.model_validate(
                {"corpus": "./kb/corpus.json", "bogus": True}
            )


@pytest.mark.unit
class TestLoadKnowledgeConfig:
    def test_parses_canonical_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "knowledge.yaml"
        p.write_text(
            "api_version: movate/v1\n"
            "kind: Knowledge\n"
            "retriever: bm25\n"
            "corpus: ./kb/corpus.json\n"
            "top_k: 3\n"
            "body_fields: [title, symptom, resolution]\n"
            "tag_field: tags\n"
        )
        cfg = load_knowledge_config(p)
        assert cfg.retriever == KnowledgeRetrieverKind.BM25
        assert cfg.top_k == 3
        assert cfg.body_fields == ["title", "symptom", "resolution"]

    def test_strips_api_version_and_kind_labels(self, tmp_path: Path) -> None:
        """``api_version`` + ``kind`` are informational labels in the
        YAML; the strict ``extra='forbid'`` model would reject them
        unless the loader strips them first."""
        p = tmp_path / "k.yaml"
        p.write_text(
            "api_version: movate/v1\nkind: Knowledge\ncorpus: ./x.json\n"
        )
        cfg = load_knowledge_config(p)
        assert cfg.corpus == "./x.json"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(KnowledgeLoadError, match="not found"):
            load_knowledge_config(tmp_path / "nope.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(": : : :\n")
        with pytest.raises(KnowledgeLoadError, match="invalid YAML"):
            load_knowledge_config(p)

    def test_non_mapping_root_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "list.yaml"
        p.write_text("- a\n- b\n")
        with pytest.raises(KnowledgeLoadError, match="must be a mapping"):
            load_knowledge_config(p)


@pytest.mark.unit
class TestBuildRetriever:
    def test_bm25_kind_returns_bm25_retriever(self, tmp_path: Path) -> None:
        corpus_path = _write_corpus(tmp_path, _DOCS)
        cfg = KnowledgeConfig(
            corpus=str(corpus_path),
            body_fields=["title", "symptom", "resolution"],
        )
        r = build_retriever(cfg)
        assert isinstance(r, BM25Retriever)
        # The built retriever actually works against the loaded corpus.
        hits = r.query("billing duplicate charge", top_k=1)
        assert hits[0].doc_id == "KB-003"

    def test_substring_kind_returns_substring_retriever(
        self, tmp_path: Path
    ) -> None:
        corpus_path = _write_corpus(tmp_path, _DOCS)
        cfg = KnowledgeConfig(
            corpus=str(corpus_path),
            retriever=KnowledgeRetrieverKind.SUBSTRING,
            body_fields=["title"],
        )
        r = build_retriever(cfg)
        assert isinstance(r, SubstringRetriever)

    def test_relative_corpus_resolved_against_base_dir(
        self, tmp_path: Path
    ) -> None:
        """When ``base_dir`` is given, a relative corpus path is
        resolved against it (typical: the agent directory holding
        knowledge.yaml)."""
        agent_dir = tmp_path / "agents" / "demo"
        agent_dir.mkdir(parents=True)
        kb_dir = agent_dir / "kb"
        kb_dir.mkdir()
        (kb_dir / "corpus.json").write_text(json.dumps(_DOCS))
        cfg = KnowledgeConfig(corpus="./kb/corpus.json")
        r = build_retriever(cfg, base_dir=agent_dir)
        hits = r.query("login", top_k=1)
        assert hits[0].doc_id == "KB-001"

    def test_missing_corpus_surfaces_as_load_error(
        self, tmp_path: Path
    ) -> None:
        cfg = KnowledgeConfig(corpus=str(tmp_path / "missing.json"))
        with pytest.raises(KnowledgeLoadError, match="not found"):
            build_retriever(cfg)


# ---------------------------------------------------------------------------
# CLI: mdk knowledge query
# ---------------------------------------------------------------------------


def _scaffold_project_with_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Init a project + drop the canonical kb-lookup corpus at
    ``kb/kb-lookup-corpus.json`` so the bare ``mdk knowledge query``
    invocation hits the default path."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.stdout + r.stderr
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    kb_dir = proj / "kb"
    kb_dir.mkdir(exist_ok=True)
    (kb_dir / "kb-lookup-corpus.json").write_text(json.dumps(_DOCS))
    return proj


@pytest.mark.unit
def test_cli_query_default_corpus_ranks_relevant_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scaffold_project_with_corpus(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["knowledge", "query", "billing duplicate"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "KB-003" in out
    # Header banner names the retriever and the query.
    assert "bm25" in out
    assert "billing duplicate" in out


@pytest.mark.unit
def test_cli_query_json_output_emits_doc_id_score_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` is for scripting + the v0.8 workflow retriever node —
    the shape must be stable + parseable."""
    _scaffold_project_with_corpus(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["knowledge", "query", "login password", "--json", "--top-k", "2"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["doc_id"] == "KB-001"
    assert isinstance(data[0]["score"], (int, float))
    assert data[0]["entry"]["title"] == "Login fails after password reset"


@pytest.mark.unit
def test_cli_query_substring_retriever_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scaffold_project_with_corpus(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "knowledge",
            "query",
            "billing duplicate",
            "--retriever",
            "substring",
            "--json",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data[0]["doc_id"] == "KB-003"


@pytest.mark.unit
def test_cli_query_invalid_retriever_kind_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scaffold_project_with_corpus(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["knowledge", "query", "x", "--retriever", "bogus"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "--retriever must be one of" in combined
    assert "bogus" in combined


@pytest.mark.unit
def test_cli_query_no_hits_reports_clean_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scaffold_project_with_corpus(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["knowledge", "query", "xyzzy quux nonsense"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0  # zero-hits isn't an error
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "no matches" in combined
