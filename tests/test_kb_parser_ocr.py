"""Tests for OCR fallback in parse_pdf (PR-CC).

Coverage:
* text-based PDF → pypdf extraction, no OCR invoked
* scanned PDF + [ocr] extra present → OCR text returned
* scanned PDF + [ocr] extra missing → None (graceful)
* pdf2image raises → None (graceful)
* pytesseract raises on one page → that page skipped, others kept
* pytesseract raises on ALL pages → None
* OCR returns blank strings → None
* encrypted PDF → None (no OCR attempt)
* corrupt PDF bytes → None (no OCR attempt)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_pypdf(
    page_texts: list[str],
    is_encrypted: bool = False,
    raise_on_open: Exception | None = None,
) -> Any:
    """Build a mock `pypdf` module whose PdfReader returns controlled page text."""
    mock_page_list = []
    for text in page_texts:
        page = MagicMock()
        page.extract_text.return_value = text
        mock_page_list.append(page)

    reader = MagicMock()
    reader.is_encrypted = is_encrypted
    reader.pages = mock_page_list

    if raise_on_open is not None:
        reader_cls = MagicMock(side_effect=raise_on_open)
    else:
        reader_cls = MagicMock(return_value=reader)

    mock_pypdf = MagicMock()
    mock_pypdf.PdfReader = reader_cls
    return mock_pypdf


def _fake_image(label: str = "img") -> MagicMock:
    """Return a MagicMock that stands in for a PIL Image."""
    img = MagicMock()
    img.__repr__ = lambda _: f"<FakeImage {label}>"
    return img


# ---------------------------------------------------------------------------
# parse_pdf: text-based PDFs (OCR should NOT be invoked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_pdf_text_pdf_no_ocr() -> None:
    """Text-based PDF → pypdf extracts text, OCR never called."""
    fake_pypdf = _make_fake_pypdf(["Page one content.", "Page two content."])

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch("movate.kb.parsers._ocr_pdf") as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-fake-bytes")

    assert result == "Page one content.\n\nPage two content."
    mock_ocr.assert_not_called()


@pytest.mark.unit
def test_parse_pdf_partial_empty_pages() -> None:
    """Some pages empty, some not → extract non-empty, no OCR."""
    fake_pypdf = _make_fake_pypdf(["", "Good content here.", "  "])

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch("movate.kb.parsers._ocr_pdf") as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-fake-bytes")

    assert result == "Good content here."
    mock_ocr.assert_not_called()


# ---------------------------------------------------------------------------
# parse_pdf: encrypted / corrupt → None, no OCR
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_pdf_encrypted_no_ocr() -> None:
    """Encrypted PDF → None without OCR attempt."""
    fake_pypdf = _make_fake_pypdf([], is_encrypted=True)

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch("movate.kb.parsers._ocr_pdf") as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-fake-bytes")

    assert result is None
    mock_ocr.assert_not_called()


@pytest.mark.unit
def test_parse_pdf_corrupt_bytes_no_ocr() -> None:
    """Corrupt bytes → pypdf raises → None without OCR."""
    fake_pypdf = _make_fake_pypdf([], raise_on_open=ValueError("bad pdf"))

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch("movate.kb.parsers._ocr_pdf") as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"notapdf")

    assert result is None
    mock_ocr.assert_not_called()


# ---------------------------------------------------------------------------
# parse_pdf: scanned PDF → OCR dispatched
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_pdf_scanned_calls_ocr() -> None:
    """All pages empty → _ocr_pdf called with the original bytes."""
    fake_pypdf = _make_fake_pypdf(["", ""])  # all empty

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch("movate.kb.parsers._ocr_pdf", return_value="OCR'd text") as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-scanned")

    assert result == "OCR'd text"
    mock_ocr.assert_called_once_with(b"%PDF-scanned")


# ---------------------------------------------------------------------------
# _ocr_pdf: happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ocr_pdf_happy_path() -> None:
    """pdf2image + pytesseract mocked → returns joined page text."""
    img1, img2 = _fake_image("p1"), _fake_image("p2")

    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img1, img2]

    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.side_effect = [
        "First page OCR text.",
        "Second page OCR text.",
    ]

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract},
    ):
        import importlib  # noqa: PLC0415

        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"%PDF-scanned")

    assert result == "First page OCR text.\n\nSecond page OCR text."
    mock_pdf2image.convert_from_bytes.assert_called_once_with(b"%PDF-scanned", dpi=200)


@pytest.mark.unit
def test_ocr_pdf_limit_to_text_pages() -> None:
    """OCR: blank page interspersed → only non-blank pages in output."""
    img1, img2, img3 = _fake_image("p1"), _fake_image("p2"), _fake_image("p3")

    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img1, img2, img3]

    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.side_effect = [
        "Real text.",
        "   ",  # blank
        "More text.",
    ]

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract},
    ):
        import importlib  # noqa: PLC0415

        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf")

    assert result == "Real text.\n\nMore text."


# ---------------------------------------------------------------------------
# _ocr_pdf: fallback cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ocr_pdf_import_error_returns_none() -> None:
    """[ocr] extra not installed → _ocr_pdf returns None (graceful)."""
    with patch.dict("sys.modules", {"pdf2image": None, "pytesseract": None}):
        import importlib  # noqa: PLC0415

        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf")

    assert result is None


@pytest.mark.unit
def test_ocr_pdf_pdf2image_raises_returns_none() -> None:
    """pdf2image.convert_from_bytes raises → None."""
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.side_effect = RuntimeError("poppler not found")
    mock_pytesseract = MagicMock()

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract},
    ):
        import importlib  # noqa: PLC0415

        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf")

    assert result is None


@pytest.mark.unit
def test_ocr_pdf_tesseract_raises_on_all_pages() -> None:
    """pytesseract raises on every page → None."""
    img1, img2 = _fake_image("p1"), _fake_image("p2")

    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img1, img2]

    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.side_effect = RuntimeError("tesseract not found")

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract},
    ):
        import importlib  # noqa: PLC0415

        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf")

    assert result is None


@pytest.mark.unit
def test_ocr_pdf_tesseract_raises_on_one_page_keeps_others() -> None:
    """pytesseract raises on page 0 but succeeds on page 1 → page 1 text returned."""
    img1, img2 = _fake_image("p1"), _fake_image("p2")

    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img1, img2]

    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.side_effect = [
        RuntimeError("tesseract OOM"),
        "Good text from page 2.",
    ]

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract},
    ):
        import importlib  # noqa: PLC0415

        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf")

    assert result == "Good text from page 2."


@pytest.mark.unit
def test_ocr_pdf_all_blank_returns_none() -> None:
    """OCR succeeds but all pages are blank → None."""
    img = _fake_image()

    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]

    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "   \n  \n  "

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract},
    ):
        import importlib  # noqa: PLC0415

        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf")

    assert result is None
