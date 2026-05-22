"""KB-grounded test-case generation + source provenance.

When an agent has an ingested KB and a RAG-context input schema, eval-gen
samples real KB chunks as verbatim ``context``, asks the LLM for only a
``question`` answerable from them, and records each case's source document
+ page in a sibling ``source`` field. This is truthful provenance — the
context IS the stored chunks, so document/page are known exactly.

Tests use fakes for the runtime (storage + provider + executor) so the
deterministic plumbing is covered without a live LLM or embeddings.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import movate.cli.eval_gen_cmd as g
from movate.cli.eval import _format_rag_expected_cell

_RAG_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "context": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["question"],
}


class _Chunk:
    def __init__(self, text: str, source: str, page: int | None = None) -> None:
        self.text = text
        self.source = source
        self.metadata = {"page": page} if page is not None else {}


class _Resp:
    def __init__(self, text: str | None = None, data: dict[str, Any] | None = None) -> None:
        self.text = text
        self.data = data


class _Provider:
    async def complete(self, request: Any) -> _Resp:
        return _Resp(text='{"question": "What is the refund window?"}')


class _Executor:
    async def execute(self, bundle: Any, request: Any) -> _Resp:
        return _Resp(data={"answer": "ans", "citations": [1], "grounded": True, "confidence": 1.0})


class _Storage:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    async def list_kb_chunks(
        self, *, agent: str, tenant_id: str, limit: int = 1000
    ) -> list[_Chunk]:
        return self._chunks


class _Runtime:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self.storage = _Storage(chunks)
        self.provider = _Provider()
        self.executor = _Executor()
        self.tracer = None


def _bundle(schema: dict[str, Any]) -> Any:
    spec = types.SimpleNamespace(name="rag-qa", model=types.SimpleNamespace(provider="mock/mock"))
    return types.SimpleNamespace(input_schema=schema, spec=spec, skills=[])


@pytest.fixture
def patch_runtime(monkeypatch: pytest.MonkeyPatch):
    def _patch(chunks: list[_Chunk]) -> _Runtime:
        rt = _Runtime(chunks)

        async def _build(*_a: object, **_k: object) -> _Runtime:
            return rt

        async def _shutdown(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(g, "build_local_runtime", _build)
        monkeypatch.setattr(g, "shutdown_runtime", _shutdown)
        return rt

    return _patch


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_rag_context_schema() -> None:
    assert g._is_rag_context_schema(_bundle(_RAG_SCHEMA)) is True
    assert (
        g._is_rag_context_schema(
            _bundle({"type": "object", "properties": {"text": {"type": "string"}}})
        )
        is False
    )
    # question present but context not an array → not RAG-context shape.
    assert (
        g._is_rag_context_schema(
            _bundle({"type": "object", "properties": {"question": {"type": "string"}}})
        )
        is False
    )


@pytest.mark.unit
def test_sample_chunks_rotates_and_wraps() -> None:
    chunks = [_Chunk("a", "d"), _Chunk("b", "d"), _Chunk("c", "d")]
    assert [c.text for c in g._sample_chunks(chunks, k=2, seed=0)] == ["a", "b"]
    assert [c.text for c in g._sample_chunks(chunks, k=2, seed=1)] == ["c", "a"]  # wraps
    # k larger than the corpus is clamped.
    assert len(g._sample_chunks(chunks, k=10, seed=0)) == 3
    assert g._sample_chunks([], k=3, seed=0) == []


@pytest.mark.unit
def test_source_for_chunks_includes_page_when_present() -> None:
    out = g._source_for_chunks([_Chunk("a", "doc.pdf", 4), _Chunk("b", "notes.txt", None)])
    assert out == [
        {"document": "doc.pdf", "page": 4},
        {"document": "notes.txt", "page": None},
    ]


# ---------------------------------------------------------------------------
# Grounding loop + source plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grounded_inputs_use_real_chunks_and_carry_source(patch_runtime: Any) -> None:
    chunks = [
        _Chunk("Refund within 14 days.", "refund-policy.md", 1),
        _Chunk("Pro tier is $29/mo.", "pricing.md", None),
        _Chunk("SLA is 99.9%.", "sla.txt", 2),
    ]
    patch_runtime(chunks)
    inputs = await g._generate_inputs_only(
        _bundle(_RAG_SCHEMA), num=2, sample_input=None, mock=False
    )

    assert len(inputs) == 2
    chunk_texts = {c.text for c in chunks}
    for inp in inputs:
        assert inp["question"] == "What is the refund window?"  # LLM-generated
        assert inp["context"]  # non-empty
        assert set(inp["context"]).issubset(chunk_texts)  # verbatim real chunks
        src = inp[g._SOURCE_SIDECAR_KEY]
        assert len(src) == len(inp["context"])  # aligned 1:1
        assert all("document" in s and "page" in s for s in src)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_inputs_promotes_source_and_strips_sidecar(patch_runtime: Any) -> None:
    patch_runtime([_Chunk("Refund within 14 days.", "refund-policy.md", 4)])
    inputs = await g._generate_inputs_only(
        _bundle(_RAG_SCHEMA), num=1, sample_input=None, mock=False
    )
    entries = await g._execute_inputs(
        _bundle(_RAG_SCHEMA), inputs, mock=False, with_dimensions=False
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"][0] == {"document": "refund-policy.md", "page": 4}
    # The sidecar is stripped from the input the agent actually received.
    assert g._SOURCE_SIDECAR_KEY not in entry["input"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_kb_falls_back_to_synthesis(
    patch_runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime([])  # nothing ingested → grounding ineligible
    calls = {"n": 0}

    async def _fake_one(*_a: object, **_k: object) -> dict[str, Any]:
        calls["n"] += 1
        return {"question": "synth?", "context": ["synthesized context"]}

    monkeypatch.setattr(g, "_generate_one_input", _fake_one)
    inputs = await g._generate_inputs_only(
        _bundle(_RAG_SCHEMA), num=2, sample_input=None, mock=False
    )

    assert calls["n"] == 2  # fell back to LLM synthesis
    assert all(g._SOURCE_SIDECAR_KEY not in inp for inp in inputs)  # no provenance


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_rag_schema_skips_grounding(
    patch_runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime([_Chunk("x", "d", 1)])  # KB present but schema isn't RAG-context

    async def _fake_one(*_a: object, **_k: object) -> dict[str, Any]:
        return {"text": "hi"}

    monkeypatch.setattr(g, "_generate_one_input", _fake_one)
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    inputs = await g._generate_inputs_only(_bundle(schema), num=1, sample_input=None, mock=False)

    assert inputs == [{"text": "hi"}]  # synthesized, not grounded


# ---------------------------------------------------------------------------
# Preview rendering of source provenance (eval.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_expected_cell_annotates_citations_with_source() -> None:
    out = _format_rag_expected_cell(
        {"answer": "A", "citations": [1], "grounded": True, "confidence": 0.9},
        context=["Refund within 14 days."],
        source=[{"document": "refund-policy.md", "page": 4}],
    )
    # Cited passage carries its document + page; a Source summary line too.
    assert "refund-policy.md p.4" in out
    assert "Source" in out


@pytest.mark.unit
def test_expected_cell_without_source_is_unannotated() -> None:
    out = _format_rag_expected_cell(
        {"answer": "A", "citations": [1], "grounded": True, "confidence": 1.0},
        context=["passage one"],
    )
    assert "Cited sources" in out
    assert "—" not in out  # no provenance annotation when source is absent
    assert "Source" not in out
