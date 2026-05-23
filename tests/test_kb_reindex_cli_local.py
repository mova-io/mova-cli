"""``mdk kb reindex <agent>`` — local reindex against the sqlite store.

Default path: rebuilds the index from existing vectors (a no-op on the
sqlite brute-force backend, but it must still report the chunk count and
never require an embedding key). ``--reembed`` path: re-runs the embedder
over every stored chunk and overwrites its vector before reindexing.

Drives the Typer app via ``CliRunner`` against a tmp sqlite DB (same
end-to-end storage roundtrip the inspect tests use). The embedder is
stubbed so no API traffic is required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "kb-reindex-tests.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)


def _seed(n: int = 2) -> None:
    """Seed ``n`` chunks for agent ``rag-qa`` via the storage layer."""
    from movate.core.models import KbChunk  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415

    async def _do() -> None:
        storage = build_storage()
        await storage.init()
        try:
            for i in range(n):
                await storage.save_kb_chunk(
                    KbChunk(
                        tenant_id="local",
                        agent="rag-qa",
                        source=f"/tmp/doc-{i}.md",
                        text=f"Sample chunk text number {i}",
                        embedding=[1.0, 0.0],
                        embedding_model="openai/text-embedding-3-small",
                        content_hash=f"hash-{i}",
                    )
                )
        finally:
            await storage.close()

    asyncio.new_event_loop().run_until_complete(_do())


def _read_embeddings() -> list[list[float]]:
    """Read back the stored embeddings for agent ``rag-qa`` (sorted by
    source for a stable order)."""
    from movate.storage import build_storage  # noqa: PLC0415

    async def _do() -> list[list[float]]:
        storage = build_storage()
        await storage.init()
        try:
            chunks = await storage.list_kb_chunks(agent="rag-qa", tenant_id="local")
        finally:
            await storage.close()
        return [c.embedding for c in sorted(chunks, key=lambda c: c.source)]

    return asyncio.new_event_loop().run_until_complete(_do())


# ---------------------------------------------------------------------------
# Default path (no --reembed): no embedding key required, reports count.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reindex_default_no_key_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default index-only path must NOT require an embedding key —
    it reuses the stored vectors. Pin this by clearing OPENAI_API_KEY."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _seed(2)
    r = runner.invoke(app, ["kb", "reindex", "rag-qa"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "no API key" not in out
    # sqlite has no real index → reports the no-op + chunk count.
    assert "sqlite" in out
    assert "2 chunk" in out


@pytest.mark.unit
def test_reindex_default_calls_storage_reindex(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local default path delegates to ``StorageProvider.reindex_kb``
    with the (agent, tenant) scope."""
    from movate.storage import sqlite as sqlite_mod  # noqa: PLC0415

    _seed(3)
    calls: list[tuple[str, str]] = []
    real = sqlite_mod.SqliteProvider.reindex_kb

    async def _spy(self: object, *, agent: str, tenant_id: str) -> int:
        calls.append((agent, tenant_id))
        return await real(self, agent=agent, tenant_id=tenant_id)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite_mod.SqliteProvider, "reindex_kb", _spy)
    r = runner.invoke(app, ["kb", "reindex", "rag-qa"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.stdout + r.stderr
    assert calls == [("rag-qa", "local")]


# ---------------------------------------------------------------------------
# --reembed path: re-runs the embedder + overwrites stored vectors.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reembed_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _seed(2)
    r = runner.invoke(
        app, ["kb", "reindex", "rag-qa", "--reembed", "--yes"], env={"COLUMNS": "200"}
    )
    assert r.exit_code == 2
    assert "OPENAI_API_KEY" in (r.stdout + r.stderr)


@pytest.mark.unit
def test_reembed_overwrites_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """--reembed re-runs the (stubbed) embedder over every chunk and
    overwrites the stored vector via save_kb_chunk's upsert."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(2)
    before = _read_embeddings()
    assert all(v == [1.0, 0.0] for v in before)

    calls: list[list[str]] = []

    async def _fake_embed(
        texts: list[str], *, model: str = "", api_key: str | None = None
    ) -> list[list[float]]:
        del model, api_key
        calls.append(texts)
        # Distinct, non-seed vector so we can prove the overwrite.
        return [[0.5, 0.5, 0.5] for _ in texts]

    monkeypatch.setattr("movate.kb.embed.embed_texts", _fake_embed)

    r = runner.invoke(
        app, ["kb", "reindex", "rag-qa", "--reembed", "--yes"], env={"COLUMNS": "200"}
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "re-embedded 2" in (r.stdout + r.stderr)
    # The embedder was called once with both chunks' text.
    assert len(calls) == 1
    assert len(calls[0]) == 2

    after = _read_embeddings()
    assert all(v == [0.5, 0.5, 0.5] for v in after), after


@pytest.mark.unit
def test_reembed_aborts_without_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """--reembed without --yes prompts; declining aborts before any
    embedder call."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(2)

    def _boom(*args: object, **kwargs: object) -> list[list[float]]:
        raise AssertionError("embedder must not be called when the user declines")

    monkeypatch.setattr("movate.kb.embed.embed_texts", _boom)

    r = runner.invoke(
        app, ["kb", "reindex", "rag-qa", "--reembed"], input="n\n", env={"COLUMNS": "200"}
    )
    assert r.exit_code == 0
    assert "aborted" in (r.stdout + r.stderr).lower()
