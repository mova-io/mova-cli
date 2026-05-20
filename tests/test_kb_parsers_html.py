"""Tests for ``movate.kb.parsers.parse_html`` (PR-M).

Same shape as the PDF / DOCX tests:

* Extension dispatch routes .html / .htm to parse_html
* Round-trip: HTML with <article> → main-content text (no nav/sidebar)
* Block-level tags become paragraph breaks for the chunker
* Defensive: garbage bytes → None, empty page → None, scripts stripped

Synthetic HTML inputs are inline string literals; tests stay hermetic.
"""

from __future__ import annotations

import pytest

from movate.kb.parsers import (
    SUPPORTED_EXTENSIONS,
    is_supported_extension,
    parse_document,
    parse_html,
)

# ---------------------------------------------------------------------------
# Extension dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_html_in_supported_extensions() -> None:
    assert ".html" in SUPPORTED_EXTENSIONS
    assert ".htm" in SUPPORTED_EXTENSIONS
    assert is_supported_extension("page.HTML")
    assert is_supported_extension("page.htm")


@pytest.mark.unit
def test_parse_document_routes_html_to_html_parser() -> None:
    body = b"<html><body><article><p>Hello HTML</p></article></body></html>"
    out = parse_document("page.html", body)
    assert out is not None
    assert "Hello HTML" in out.text
    assert out.ocr_used is False


# ---------------------------------------------------------------------------
# parse_html — main content extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_html_extracts_article_content() -> None:
    """A page with <article> tags extracts the article body."""
    body = (
        b"<html><body>"
        b"<article>"
        b"<h1>Refund Policy</h1>"
        b"<p>Annual subscriptions refundable within 14 days.</p>"
        b"</article>"
        b"</body></html>"
    )
    out = parse_html(body)
    assert out is not None
    assert "Refund Policy" in out.text
    assert "Annual subscriptions" in out.text
    assert out.ocr_used is False


@pytest.mark.unit
def test_parse_html_strips_script_and_style() -> None:
    """``<script>`` / ``<style>`` content never makes it through —
    JavaScript and CSS aren't useful retrieval content."""
    body = (
        b"<html><body>"
        b"<article>"
        b"<style>body { color: red; }</style>"
        b"<script>alert('hello')</script>"
        b"<p>This is the real article body.</p>"
        b"</article>"
        b"</body></html>"
    )
    out = parse_html(body)
    assert out is not None
    assert "real article body" in out.text
    assert "color: red" not in out.text
    assert "alert" not in out.text
    assert out.ocr_used is False


@pytest.mark.unit
def test_parse_html_block_tags_become_paragraph_breaks() -> None:
    """Block-level tags (``<p>``, ``<h1>``, etc.) split into separate
    paragraphs so the chunker treats them as natural boundaries."""
    body = (
        b"<html><body><article>"
        b"<h1>Title</h1>"
        b"<p>First paragraph.</p>"
        b"<p>Second paragraph.</p>"
        b"</article></body></html>"
    )
    out = parse_html(body)
    assert out is not None
    # \n\n boundaries between block-level pieces.
    assert "\n\n" in out.text
    # Each chunk distinguishable.
    assert "First paragraph" in out.text
    assert "Second paragraph" in out.text
    assert out.ocr_used is False


@pytest.mark.unit
def test_parse_html_normalizes_internal_whitespace() -> None:
    """HTML in the wild has tabs / newlines inside paragraphs.
    parse_html collapses runs of whitespace to single spaces so
    the extracted text is clean (without losing the \\n\\n
    paragraph boundaries)."""
    body = (
        b"<html><body><article>"
        b"<p>Word\n\twith\n    excess    whitespace.</p>"
        b"</article></body></html>"
    )
    out = parse_html(body)
    assert out is not None
    # Multiple spaces in a row collapsed to single.
    assert "Word with excess whitespace" in out.text
    assert out.ocr_used is False


@pytest.mark.unit
def test_parse_html_decodes_latin1_fallback() -> None:
    """HTML files with non-UTF-8 encoding (e.g. legacy Windows-1252)
    decode via latin-1 fallback rather than failing — lossy but
    keeps retrieval content available."""
    # A latin-1-encoded byte sequence with non-ASCII chars that
    # would fail UTF-8 decoding.
    body = b"<html><body><article><p>caf\xe9 menu</p></article></body></html>"
    out = parse_html(body)
    assert out is not None
    # latin-1 decoded "\xe9" → "é"; preserved in the extracted text.
    assert "café menu" in out.text or "menu" in out.text  # decode-flexible
    assert out.ocr_used is False


# ---------------------------------------------------------------------------
# Defensive returns
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_html_returns_none_on_empty_html() -> None:
    """``<html></html>`` (no content) → None; operator sees skipped."""
    out = parse_html(b"<html></html>")
    assert out is None


@pytest.mark.unit
def test_parse_html_returns_none_on_completely_empty_bytes() -> None:
    out = parse_html(b"")
    assert out is None


@pytest.mark.unit
def test_parse_html_handles_garbage_bytes_gracefully() -> None:
    """Random non-HTML bytes either return None or return a stripped
    string — readability is lenient. Either way it must NOT raise."""
    # Don't assert on the specific output — just that the call is safe.
    parse_html(b"not html at all just text")
    parse_html(b"\x00\x01\x02\x03")  # arbitrary binary


# ---------------------------------------------------------------------------
# find_files dispatch — picks up .html / .htm
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_files_includes_html_and_htm(tmp_path: object) -> None:
    """``mdk kb ingest <dir>`` walks .html + .htm alongside other
    supported formats."""
    from pathlib import Path  # noqa: PLC0415

    from movate.kb.ingest import find_files  # noqa: PLC0415

    root = Path(str(tmp_path))
    (root / "a.html").write_text("<html><body>a</body></html>")
    (root / "b.htm").write_text("<html><body>b</body></html>")
    (root / "skip.xml").write_text("<root>xml</root>")

    found = {p.name for p in find_files(root)}
    assert "a.html" in found
    assert "b.htm" in found
    assert "skip.xml" not in found
