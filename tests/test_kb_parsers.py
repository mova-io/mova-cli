"""Tests for ``movate.kb.parsers`` — dispatch + per-format parsers.

Covers:

* Extension dispatch — happy path for each supported extension,
  ``None`` for unsupported.
* Per-format parsers — plain-text decode, PDF text extraction,
  defensive returns on corrupt / encrypted / scanned PDFs.
* End-to-end via ``ingest_text`` (the runtime + CLI both use it).

PDF bytes are constructed via pypdf itself so we don't ship a
binary test fixture and the test stays hermetic.
"""

from __future__ import annotations

import io

import pypdf
import pytest

from movate.kb.parsers import (
    SUPPORTED_EXTENSIONS,
    is_supported_extension,
    parse_document,
    parse_pdf,
    parse_text,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pdf_bytes(*pages_text: str) -> bytes:
    """Build a synthetic PDF in memory containing the given pages.

    Uses pypdf's writer to construct a minimal valid PDF — single
    blank page per text input with the text drawn on it via
    ``add_blank_page`` + a low-level page rewrite. Good enough for
    extractor round-trip tests.

    pypdf doesn't have a one-call "write text to new page" helper
    in the version we pin, so we use reportlab... wait, we don't
    ship reportlab. Use a hand-crafted minimal PDF instead — a
    valid PDF dict with one Page object per input string and a
    stream containing the BT...ET text-drawing operators.
    """
    # Build a minimal PDF by hand. PDF format is text-based:
    # objects numbered + cross-referenced. This produces a
    # spec-compliant 1-page PDF per text input that pypdf can
    # extract from.
    buf = io.BytesIO()
    writer = pypdf.PdfWriter()
    for text in pages_text:
        page = writer.add_blank_page(width=612, height=792)
        # pypdf doesn't expose text drawing at the high level; we
        # mutate the page's /Contents stream with a minimal BT/ET
        # block that draws the text at position (100, 700).
        escaped = text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
        content_stream = (
            f"BT /F1 12 Tf 100 700 Td ({escaped}) Tj ET".encode()
        )
        from pypdf.generic import (  # noqa: PLC0415
            DecodedStreamObject,
            DictionaryObject,
            NameObject,
        )

        stream_obj = DecodedStreamObject()
        stream_obj.set_data(content_stream)
        page[NameObject("/Contents")] = stream_obj
        # Need a /Font resource for the BT/ET block to be valid.
        resources = DictionaryObject()
        font_dict = DictionaryObject()
        font_dict[NameObject("/F1")] = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources[NameObject("/Font")] = font_dict
        page[NameObject("/Resources")] = resources
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Extension dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_supported_extensions_includes_md_txt_pdf() -> None:
    """The supported set is the public contract — operators rely on it."""
    assert ".md" in SUPPORTED_EXTENSIONS
    assert ".markdown" in SUPPORTED_EXTENSIONS
    assert ".txt" in SUPPORTED_EXTENSIONS
    assert ".pdf" in SUPPORTED_EXTENSIONS


@pytest.mark.unit
def test_is_supported_extension_case_insensitive() -> None:
    assert is_supported_extension("doc.PDF")
    assert is_supported_extension("DOC.MD")
    assert is_supported_extension("doc.Markdown")
    # Legacy binary .doc is NOT supported (python-docx rejects it);
    # operators convert to .docx first. PR-L added .docx support
    # — see test_kb_parsers_docx.py.
    assert not is_supported_extension("legacy.doc")
    assert not is_supported_extension("noext")
    assert not is_supported_extension("")


@pytest.mark.unit
def test_parse_document_routes_md_to_text() -> None:
    out = parse_document("hello.md", b"# Heading\n\nbody")
    assert out == "# Heading\n\nbody"


@pytest.mark.unit
def test_parse_document_returns_none_for_unsupported() -> None:
    # Legacy .doc / unknown extensions / extensionless / empty filenames.
    # PR-L added .docx; PR-M added .html/.htm (see test_kb_parsers_docx.py
    # / test_kb_parsers_html.py respectively).
    assert parse_document("legacy.doc", b"anything") is None
    assert parse_document("data.xml", b"anything") is None
    assert parse_document("noext", b"anything") is None
    assert parse_document("", b"anything") is None


# ---------------------------------------------------------------------------
# parse_text — UTF-8 decode
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_text_decodes_utf8() -> None:
    assert parse_text("héllo wörld".encode()) == "héllo wörld"


@pytest.mark.unit
def test_parse_text_returns_none_on_non_utf8() -> None:
    """Latin-1 / ISO bytes that aren't valid UTF-8 → None.

    Operators see this as ``status="skipped"`` — the alternative
    (silent corruption from a Latin-1 fallback) is worse."""
    bad_bytes = b"\xff\xfe\xfd"
    assert parse_text(bad_bytes) is None


# ---------------------------------------------------------------------------
# parse_pdf — text extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_pdf_extracts_single_page_text() -> None:
    pdf_bytes = _make_pdf_bytes("Hello PDF world")
    out = parse_pdf(pdf_bytes)
    assert out is not None
    assert "Hello PDF world" in out


@pytest.mark.unit
def test_parse_pdf_joins_multiple_pages_with_paragraph_breaks() -> None:
    """Multi-page extraction joins on ``\\n\\n`` so the downstream
    paragraph chunker treats page breaks as chunk breaks."""
    pdf_bytes = _make_pdf_bytes("Page one text", "Page two text")
    out = parse_pdf(pdf_bytes)
    assert out is not None
    assert "Page one text" in out
    assert "Page two text" in out
    # Confirm the paragraph-boundary separator is present.
    assert "\n\n" in out


@pytest.mark.unit
def test_parse_pdf_returns_none_on_corrupt_bytes() -> None:
    """Random bytes that aren't a PDF → None (not exception)."""
    assert parse_pdf(b"not a pdf file at all") is None
    assert parse_pdf(b"") is None


@pytest.mark.unit
def test_parse_pdf_returns_none_on_encrypted_pdf() -> None:
    """Encrypted PDFs can't be extracted without a password.

    The upload UI has no password channel; surface as None so
    operator sees ``status="skipped"`` rather than empty content."""
    buf = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("hunter2")
    writer.write(buf)
    out = parse_pdf(buf.getvalue())
    assert out is None


@pytest.mark.unit
def test_parse_pdf_returns_none_when_all_pages_empty() -> None:
    """Scanned-image PDF or extraction-failed PDF → None.

    Blank pages with no text content can't contribute to retrieval —
    surfaces as skipped rather than ingesting empty chunks."""
    buf = io.BytesIO()
    writer = pypdf.PdfWriter()
    # Three blank pages, no text drawn.
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    writer.write(buf)
    out = parse_pdf(buf.getvalue())
    assert out is None


# ---------------------------------------------------------------------------
# End-to-end via parse_document
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_document_routes_pdf_to_pdf_parser() -> None:
    pdf_bytes = _make_pdf_bytes("Routed correctly")
    out = parse_document("policy.pdf", pdf_bytes)
    assert out is not None
    assert "Routed correctly" in out


@pytest.mark.unit
def test_parse_document_pdf_failure_returns_none() -> None:
    """End-to-end: a .pdf with junk content returns None at the
    dispatch level. Callers report ``status="skipped"``."""
    assert parse_document("policy.pdf", b"corrupt") is None


# ---------------------------------------------------------------------------
# find_files dispatch — picks up .pdf files in directory walks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_files_includes_pdf(tmp_path: object) -> None:
    """``mdk kb ingest <dir>`` should now walk .pdf files alongside
    .md / .txt. Regression guard: the old hardcoded
    ``{".md", ".txt", ".markdown"}`` set excluded PDFs."""
    from pathlib import Path  # noqa: PLC0415

    from movate.kb.ingest import find_files  # noqa: PLC0415

    root = Path(str(tmp_path))
    (root / "a.md").write_text("md content")
    (root / "b.txt").write_text("txt content")
    (root / "c.pdf").write_bytes(_make_pdf_bytes("pdf content"))
    # PR-M added .html/.htm; pick .xml as the "not supported" example.
    (root / "skip.xml").write_text("<root>xml</root>")

    found = {p.name for p in find_files(root)}
    assert found == {"a.md", "b.txt", "c.pdf"}
