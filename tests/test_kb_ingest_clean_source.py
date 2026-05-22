"""Tests for ``--clean-source`` flag and file-size guard in the ingest pipeline.

Coverage:

* ``clean_source=True`` deletes existing chunks before re-ingest so the
  chunk count doesn't grow on repeated ingest of the same file.
* Without ``clean_source``, a second ingest of the *same* file is a dedup
  no-op (chunks_saved == same count; no new rows).
* ``_MAX_FILE_MB`` guard: a file at exactly the limit is ingested; a file
  1 byte above is skipped (returns ``None`` from ``_ingest_one_file``) with
  a logged warning.
* ``MOVATE_MAX_FILE_MB`` env var (applied via ``monkeypatch.setattr`` on the
  module constant, since the constant is evaluated at import time) changes
  the threshold.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
) -> list[list[float]]:
    """Deterministic stub — no OpenAI traffic."""
    return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]


async def _do_ingest(
    storage: InMemoryStorage,
    path: Path,
    *,
    clean_source: bool = False,
) -> None:
    from movate.kb.ingest import ingest_path  # noqa: PLC0415

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        await ingest_path(
            storage=storage,
            path=path,
            agent="test-agent",
            tenant_id="t1",
            api_key="sk-stub",
            clean_source=clean_source,
        )


# ---------------------------------------------------------------------------
# --clean-source flag
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_clean_source_does_not_double_chunks(tmp_path: Path) -> None:
    """Re-ingesting with clean_source=True results in the same chunk
    count as the first ingest — old chunks are deleted before new ones
    are saved, so the total never grows."""
    (tmp_path / "doc.md").write_text(
        "First paragraph with enough text to qualify as a chunk.\n\n"
        "Second paragraph also qualifies for the minimum chunk size.\n",
        encoding="utf-8",
    )

    storage = InMemoryStorage()
    await storage.init()

    # First ingest.
    await _do_ingest(storage, tmp_path)
    first_count = len(await storage.list_kb_chunks(agent="test-agent", tenant_id="t1"))
    assert first_count > 0, "expected at least one chunk after first ingest"

    # Second ingest with --clean-source.
    await _do_ingest(storage, tmp_path, clean_source=True)
    second_count = len(await storage.list_kb_chunks(agent="test-agent", tenant_id="t1"))

    assert second_count == first_count, (
        f"clean_source=True should not grow chunks: before={first_count} after={second_count}"
    )


@pytest.mark.unit
async def test_clean_source_reports_chunks_removed(tmp_path: Path) -> None:
    """``IngestSummary.chunks_removed`` is non-zero when clean_source=True
    and there were pre-existing chunks for the source."""
    from movate.kb.ingest import ingest_path  # noqa: PLC0415

    (tmp_path / "doc.md").write_text(
        "A substantial paragraph with enough words to be kept.\n\n"
        "Another substantial paragraph that also meets the minimum.\n",
        encoding="utf-8",
    )

    storage = InMemoryStorage()
    await storage.init()

    # First ingest (no clean_source).
    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        summaries1, _ = await ingest_path(
            storage=storage,
            path=tmp_path,
            agent="test-agent",
            tenant_id="t1",
            api_key="sk-stub",
        )
    assert summaries1[0].chunks_removed == 0, "no removal on first ingest"

    # Second ingest with clean_source=True.
    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        summaries2, _ = await ingest_path(
            storage=storage,
            path=tmp_path,
            agent="test-agent",
            tenant_id="t1",
            api_key="sk-stub",
            clean_source=True,
        )
    assert summaries2[0].chunks_removed > 0, (
        "clean_source=True should report removed chunks on second ingest"
    )
    assert summaries2[0].chunks_removed == summaries1[0].chunks_saved, (
        "chunks_removed should equal the chunks that were saved in the first ingest"
    )


@pytest.mark.unit
async def test_without_clean_source_second_ingest_is_dedup_noop(tmp_path: Path) -> None:
    """Without clean_source, re-ingesting the same unchanged file is a
    no-op via the content_hash dedup key — the chunk count stays the same."""
    (tmp_path / "doc.md").write_text(
        "A paragraph with content for dedup test.\n\n"
        "Another paragraph for dedup verification purposes.\n",
        encoding="utf-8",
    )

    storage = InMemoryStorage()
    await storage.init()

    await _do_ingest(storage, tmp_path)
    count_after_first = len(await storage.list_kb_chunks(agent="test-agent", tenant_id="t1"))

    # Second ingest, same content, no clean_source.
    await _do_ingest(storage, tmp_path)
    count_after_second = len(await storage.list_kb_chunks(agent="test-agent", tenant_id="t1"))

    assert count_after_second == count_after_first, (
        "without clean_source, re-ingest of unchanged file should not add chunks"
    )


@pytest.mark.unit
async def test_clean_source_isolates_per_source(tmp_path: Path) -> None:
    """clean_source only removes chunks for the file being re-ingested —
    chunks from other files are untouched."""
    (tmp_path / "alpha.md").write_text(
        "Alpha document paragraph with substantial content here.\n\n"
        "Alpha second paragraph also substantial enough.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.md").write_text(
        "Beta document paragraph with substantial content here.\n\n"
        "Beta second paragraph also substantial enough.\n",
        encoding="utf-8",
    )

    storage = InMemoryStorage()
    await storage.init()

    # Ingest both files.
    await _do_ingest(storage, tmp_path)
    total_after_first = len(await storage.list_kb_chunks(agent="test-agent", tenant_id="t1"))

    # Re-ingest only alpha.md with clean_source.
    await _do_ingest(storage, tmp_path / "alpha.md", clean_source=True)

    # Beta's chunks must still be present; total = alpha_chunks + beta_chunks (unchanged).
    total_after_second = len(await storage.list_kb_chunks(agent="test-agent", tenant_id="t1"))
    assert total_after_second == total_after_first, (
        "clean_source for alpha.md should not remove beta.md chunks; "
        f"first={total_after_first} second={total_after_second}"
    )


# ---------------------------------------------------------------------------
# File-size guard — _MAX_FILE_MB
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_file_at_exact_limit_is_not_skipped(tmp_path: Path) -> None:
    """A file whose size equals _MAX_FILE_MB exactly must NOT be skipped."""
    import movate.kb.ingest as ingest_mod  # noqa: PLC0415

    limit_mb = 1.0  # Use 1 MB as a convenient test limit.
    # Write a file that is exactly limit_mb in size.
    content = b"x" * int(limit_mb * 1024 * 1024)
    file_path = tmp_path / "exactly.md"
    file_path.write_bytes(content)

    storage = InMemoryStorage()
    await storage.init()

    # parse_document is imported lazily inside _ingest_one_file from
    # movate.kb.parsers — patch it at the source module, not ingest.
    from movate.kb.parsers import ParseResult  # noqa: PLC0415

    with (
        mock.patch.object(ingest_mod, "_MAX_FILE_MB", limit_mb),
        mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed),
        mock.patch("movate.kb.parsers.parse_document") as mock_parse,
    ):
        mock_parse.return_value = ParseResult(
            text="A sufficient paragraph of text that should produce at least one chunk.\n\n"
            "A second paragraph to ensure multiple chunks are created here.",
            ocr_used=False,
        )
        result = await ingest_mod._ingest_one_file(
            storage=storage,
            file_path=file_path,
            agent="test-agent",
            tenant_id="t1",
            embedding_model="openai/text-embedding-3-small",
            api_key="sk-stub",
            clean_source=False,
        )

    assert result is not None, "file at exactly the limit should NOT be skipped"


@pytest.mark.unit
async def test_file_one_byte_over_limit_is_skipped(tmp_path: Path) -> None:
    """A file 1 byte above _MAX_FILE_MB must be skipped (returns None)
    and a warning must be logged."""
    import movate.kb.ingest as ingest_mod  # noqa: PLC0415

    limit_mb = 1.0
    # Write a file that is exactly 1 byte OVER the limit.
    content = b"x" * (int(limit_mb * 1024 * 1024) + 1)
    file_path = tmp_path / "oversized.md"
    file_path.write_bytes(content)

    storage = InMemoryStorage()
    await storage.init()

    with (
        mock.patch.object(ingest_mod, "_MAX_FILE_MB", limit_mb),
        mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed),
        mock.patch.object(ingest_mod.log, "warning") as mock_warn,
    ):
        result = await ingest_mod._ingest_one_file(
            storage=storage,
            file_path=file_path,
            agent="test-agent",
            tenant_id="t1",
            embedding_model="openai/text-embedding-3-small",
            api_key="sk-stub",
            clean_source=False,
        )

    assert result is None, "file 1 byte over the limit should be skipped (return None)"
    assert mock_warn.called, "a warning should be logged when a file is skipped"
    # The warning message should mention the file name.
    warned_args = mock_warn.call_args
    assert "oversized.md" in str(warned_args), (
        f"warning should mention the oversized filename; got: {warned_args}"
    )


@pytest.mark.unit
async def test_file_size_guard_uses_module_constant(tmp_path: Path) -> None:
    """Patching the module-level ``_MAX_FILE_MB`` constant changes which
    files get skipped — smaller limit skips what the default allows."""
    import movate.kb.ingest as ingest_mod  # noqa: PLC0415

    # A 2 MB file: passes default (50 MB), fails a patched 1 MB limit.
    content = b"x" * (2 * 1024 * 1024)
    file_path = tmp_path / "medium.md"
    file_path.write_bytes(content)

    storage = InMemoryStorage()
    await storage.init()

    # parse_document is imported lazily from movate.kb.parsers inside
    # _ingest_one_file — patch it at its definition site.
    from movate.kb.parsers import ParseResult  # noqa: PLC0415

    # With default limit (50 MB): parse is called (not skipped by size guard).
    with (
        mock.patch.object(ingest_mod, "_MAX_FILE_MB", 50.0),
        mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed),
        mock.patch("movate.kb.parsers.parse_document") as mock_parse,
    ):
        mock_parse.return_value = ParseResult(
            text="Enough text to produce chunks here.\n\nSecond paragraph too.",
            ocr_used=False,
        )
        result_ok = await ingest_mod._ingest_one_file(
            storage=storage,
            file_path=file_path,
            agent="test-agent",
            tenant_id="t1",
            embedding_model="openai/text-embedding-3-small",
            api_key="sk-stub",
        )

    assert result_ok is not None, "2 MB file should pass a 50 MB limit"

    # With a 1 MB patched limit: the same 2 MB file is skipped.
    with mock.patch.object(ingest_mod, "_MAX_FILE_MB", 1.0):
        result_skip = await ingest_mod._ingest_one_file(
            storage=storage,
            file_path=file_path,
            agent="test-agent",
            tenant_id="t1",
            embedding_model="openai/text-embedding-3-small",
            api_key="sk-stub",
        )

    assert result_skip is None, "2 MB file should be skipped under a 1 MB limit"


@pytest.mark.unit
async def test_file_size_guard_default_is_50mb() -> None:
    """The default ``_MAX_FILE_MB`` constant is 50 (when env var is absent)."""
    # We can't reload the module cleanly in a test, but we CAN assert the
    # default value that was baked in at import time (env var absent in CI).
    # If MOVATE_MAX_FILE_MB is unset, the value must be 50.
    import os  # noqa: PLC0415

    import movate.kb.ingest as ingest_mod  # noqa: PLC0415

    if "MOVATE_MAX_FILE_MB" not in os.environ:
        assert ingest_mod._MAX_FILE_MB == 50.0, (
            f"default _MAX_FILE_MB should be 50.0, got {ingest_mod._MAX_FILE_MB}"
        )
