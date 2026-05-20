"""Tests for OCR fallback in parse_pdf (PR-CC, PR-HH, PR-II).

Coverage:
* text-based PDF → pypdf extraction, no OCR invoked
* mixed PDF (some pages have text, some scanned) → per-page OCR on empty pages
* all-scanned PDF → _ocr_pdf called once per page with correct page_num
* all-scanned PDF + OCR returns None → parse_pdf returns None
* encrypted PDF → None (no OCR attempt)
* corrupt PDF bytes → None (no OCR attempt)
* _ocr_pdf: 300 DPI, --oem 1 --psm 6, MOVATE_OCR_LANG env var
* _ocr_pdf: whitespace normalisation (collapse spaces/tabs, trim excess newlines)
* _ocr_pdf: [ocr] extra missing → None (graceful)
* _ocr_pdf: pdf2image raises → None (graceful)
* _ocr_pdf: pytesseract raises → None (graceful)
* _ocr_pdf: OCR returns blank string → None
* _ocr_pdf: MOVATE_OCR_BACKEND=easyocr → routes to EasyOCR, not Tesseract
* _ocr_easyocr: gpu=False by default; MOVATE_EASYOCR_GPU=1 → gpu=True
* _ocr_easyocr: readtext result lines joined with newline
* _ocr_easyocr: lang mapped from Tesseract code to EasyOCR code
* _ocr_easyocr: not installed → None (graceful)
* _ocr_easyocr: Reader raises → None (graceful)
* _tesseract_to_easyocr_langs: single code, compound +, unknown passthrough, Chinese
"""

from __future__ import annotations

import importlib
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

    assert result is not None
    assert result.text == "Page one content.\n\nPage two content."
    assert result.ocr_used is False
    mock_ocr.assert_not_called()


# ---------------------------------------------------------------------------
# parse_pdf: mixed PDFs — per-page OCR on empty pages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_pdf_mixed_pdf_ocr_called_for_empty_pages() -> None:
    """Mixed PDF: some pages have text, some are empty.
    _ocr_pdf is called for each empty page (not just when ALL are empty)."""
    fake_pypdf = _make_fake_pypdf(["", "Good content here.", "  "])

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch("movate.kb.parsers._ocr_pdf", return_value=None) as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-fake-bytes")

    assert result is not None
    assert result.text == "Good content here."
    assert result.ocr_used is False  # OCR returned None → no OCR text added
    # Called once for each empty page (page 0 = "", page 2 = "  ")
    assert mock_ocr.call_count == 2
    mock_ocr.assert_any_call(b"%PDF-fake-bytes", page_num=0)
    mock_ocr.assert_any_call(b"%PDF-fake-bytes", page_num=2)


@pytest.mark.unit
def test_parse_pdf_mixed_pdf_ocr_fills_scanned_pages() -> None:
    """Mixed PDF: OCR fills scanned pages; text pages come from pypdf.
    ocr_used=True when any page was OCR'd successfully."""
    fake_pypdf = _make_fake_pypdf(["", "Normal text.", ""])

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch(
            "movate.kb.parsers._ocr_pdf",
            side_effect=["OCR page 0.", None],  # page 0 OCR'd; page 2 blank scan
        ) as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-mixed")

    assert result is not None
    # Page order preserved: page 0 OCR → page 1 text → page 2 skipped (OCR=None)
    assert result.text == "OCR page 0.\n\nNormal text."
    assert result.ocr_used is True
    assert mock_ocr.call_count == 2
    mock_ocr.assert_any_call(b"%PDF-mixed", page_num=0)
    mock_ocr.assert_any_call(b"%PDF-mixed", page_num=2)


# ---------------------------------------------------------------------------
# parse_pdf: all-scanned PDF
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_pdf_scanned_calls_ocr_per_page() -> None:
    """All pages empty → _ocr_pdf called once per page with correct page_num."""
    fake_pypdf = _make_fake_pypdf(["", ""])  # two scanned pages

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch(
            "movate.kb.parsers._ocr_pdf",
            side_effect=["Page 0 OCR.", "Page 1 OCR."],
        ) as mock_ocr,
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-scanned")

    assert result is not None
    assert result.text == "Page 0 OCR.\n\nPage 1 OCR."
    assert result.ocr_used is True
    assert mock_ocr.call_count == 2
    mock_ocr.assert_any_call(b"%PDF-scanned", page_num=0)
    mock_ocr.assert_any_call(b"%PDF-scanned", page_num=1)


@pytest.mark.unit
def test_parse_pdf_all_scanned_ocr_returns_none_gives_none() -> None:
    """All pages scanned + every _ocr_pdf call returns None → parse_pdf returns None."""
    fake_pypdf = _make_fake_pypdf(["", ""])

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch("movate.kb.parsers._ocr_pdf", return_value=None),
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-scanned-no-text")

    assert result is None


@pytest.mark.unit
def test_parse_pdf_ocr_partial_fail_keeps_good_pages() -> None:
    """OCR fails on one page but succeeds on another — good page kept, failed page dropped."""
    fake_pypdf = _make_fake_pypdf(["", ""])

    with (
        patch.dict("sys.modules", {"pypdf": fake_pypdf}),
        patch(
            "movate.kb.parsers._ocr_pdf",
            side_effect=[None, "Good text from page 1."],
        ),
    ):
        from movate.kb.parsers import parse_pdf  # noqa: PLC0415

        result = parse_pdf(b"%PDF-scanned")

    assert result is not None
    assert result.text == "Good text from page 1."
    assert result.ocr_used is True


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
# _ocr_pdf: quality — DPI, config, lang, whitespace
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ocr_pdf_uses_300_dpi_and_psm_config() -> None:
    """_ocr_pdf rasterises at 300 DPI and passes --oem 1 --psm 6 to tesseract."""
    img = _fake_image("p0")
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Hello world."

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"%PDF-scan", page_num=0)

    assert result == "Hello world."
    mock_pdf2image.convert_from_bytes.assert_called_once_with(
        b"%PDF-scan", dpi=300, first_page=1, last_page=1
    )
    mock_pytesseract.image_to_string.assert_called_once_with(
        img, lang="eng", config="--oem 1 --psm 6"
    )


@pytest.mark.unit
def test_ocr_pdf_page_num_maps_to_first_last_page() -> None:
    """page_num=2 → first_page=3, last_page=3 (Poppler pages are 1-based)."""
    img = _fake_image("p2")
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Page 3 text."

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        parsers_mod._ocr_pdf(b"%PDF", page_num=2)

    mock_pdf2image.convert_from_bytes.assert_called_once_with(
        b"%PDF", dpi=300, first_page=3, last_page=3
    )


@pytest.mark.unit
def test_ocr_pdf_uses_lang_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_OCR_LANG env var is passed as lang= to pytesseract."""
    monkeypatch.setenv("MOVATE_OCR_LANG", "fra")

    img = _fake_image()
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Bonjour."

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"%PDF", page_num=0)

    assert result == "Bonjour."
    _, kwargs = mock_pytesseract.image_to_string.call_args
    assert kwargs["lang"] == "fra"


@pytest.mark.unit
def test_ocr_pdf_normalizes_whitespace() -> None:
    """Whitespace normalisation: collapse tab/space runs; trim triple+ newlines."""
    img = _fake_image()
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]
    mock_pytesseract = MagicMock()
    # Raw tesseract output with noise whitespace
    mock_pytesseract.image_to_string.return_value = (
        "Hello   world.\t\tMore   text.\n\n\n\nNew paragraph."
    )

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"%PDF", page_num=0)

    # Spaces + tabs collapsed; 3+ newlines reduced to 2
    assert result == "Hello world. More text.\n\nNew paragraph."


# ---------------------------------------------------------------------------
# _ocr_pdf: happy path (single-page call)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ocr_pdf_happy_path() -> None:
    """_ocr_pdf with a single page returns the OCR'd text for that page."""
    img = _fake_image("p0")
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "First page OCR text."

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"%PDF-scanned", page_num=0)

    assert result == "First page OCR text."


# ---------------------------------------------------------------------------
# _ocr_pdf: fallback cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ocr_pdf_import_error_returns_none() -> None:
    """[ocr] extra not installed → _ocr_pdf returns None (graceful)."""
    with patch.dict("sys.modules", {"pdf2image": None, "pytesseract": None}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf", page_num=0)

    assert result is None


@pytest.mark.unit
def test_ocr_pdf_pdf2image_raises_returns_none() -> None:
    """pdf2image.convert_from_bytes raises → None."""
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.side_effect = RuntimeError("poppler not found")
    mock_pytesseract = MagicMock()

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf", page_num=0)

    assert result is None


@pytest.mark.unit
def test_ocr_pdf_tesseract_raises_returns_none() -> None:
    """pytesseract raises → None (logged at WARNING)."""
    img = _fake_image()
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.side_effect = RuntimeError("tesseract not found")

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf", page_num=0)

    assert result is None


@pytest.mark.unit
def test_ocr_pdf_blank_result_returns_none() -> None:
    """OCR succeeds but returns only whitespace → None."""
    img = _fake_image()
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "   \n  \n  "

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf", page_num=0)

    assert result is None


@pytest.mark.unit
def test_ocr_pdf_empty_image_list_returns_none() -> None:
    """pdf2image returns empty list (no pages decoded) → None."""
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = []
    mock_pytesseract = MagicMock()

    with patch.dict("sys.modules", {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"pdf", page_num=0)

    assert result is None
    mock_pytesseract.image_to_string.assert_not_called()


# ---------------------------------------------------------------------------
# _ocr_pdf: backend routing — MOVATE_OCR_BACKEND=easyocr
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ocr_pdf_backend_easyocr_routes_to_easyocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MOVATE_OCR_BACKEND=easyocr → EasyOCR called; pytesseract NOT called."""
    monkeypatch.setenv("MOVATE_OCR_BACKEND", "easyocr")

    img = _fake_image("p0")
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]

    mock_reader = MagicMock()
    mock_reader.readtext.return_value = ["EasyOCR result."]
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.return_value = mock_reader

    mock_pytesseract = MagicMock()

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "easyocr": mock_easyocr, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"%PDF-scan", page_num=0)

    assert result == "EasyOCR result."
    mock_easyocr.Reader.assert_called_once()
    mock_reader.readtext.assert_called_once_with(img, detail=0)
    # Tesseract must NOT be called
    mock_pytesseract.image_to_string.assert_not_called()


@pytest.mark.unit
def test_ocr_pdf_backend_default_routes_to_tesseract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default backend (no env var) → Tesseract called; EasyOCR NOT called."""
    monkeypatch.delenv("MOVATE_OCR_BACKEND", raising=False)

    img = _fake_image("p0")
    mock_pdf2image = MagicMock()
    mock_pdf2image.convert_from_bytes.return_value = [img]

    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Tesseract result."

    mock_easyocr = MagicMock()

    with patch.dict(
        "sys.modules",
        {"pdf2image": mock_pdf2image, "pytesseract": mock_pytesseract, "easyocr": mock_easyocr},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_pdf(b"%PDF-scan", page_num=0)

    assert result == "Tesseract result."
    mock_pytesseract.image_to_string.assert_called_once()
    # EasyOCR must NOT be called
    mock_easyocr.Reader.assert_not_called()


# ---------------------------------------------------------------------------
# _ocr_easyocr: direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ocr_easyocr_returns_joined_lines() -> None:
    """EasyOCR readtext returns a list of text blocks; they are joined with newlines."""
    img = _fake_image()
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = ["First line.", "Second line.", "Third line."]
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.return_value = mock_reader

    with patch.dict("sys.modules", {"easyocr": mock_easyocr}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_easyocr(img, "eng")

    assert result == "First line.\nSecond line.\nThird line."
    mock_reader.readtext.assert_called_once_with(img, detail=0)


@pytest.mark.unit
def test_ocr_easyocr_gpu_false_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """EasyOCR Reader initialised with gpu=False by default."""
    monkeypatch.delenv("MOVATE_EASYOCR_GPU", raising=False)

    img = _fake_image()
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = ["text"]
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.return_value = mock_reader

    with patch.dict("sys.modules", {"easyocr": mock_easyocr}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        parsers_mod._ocr_easyocr(img, "eng")

    _, kwargs = mock_easyocr.Reader.call_args
    assert kwargs.get("gpu") is False


@pytest.mark.unit
def test_ocr_easyocr_gpu_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_EASYOCR_GPU=1 → Reader initialised with gpu=True."""
    monkeypatch.setenv("MOVATE_EASYOCR_GPU", "1")

    img = _fake_image()
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = ["text"]
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.return_value = mock_reader

    with patch.dict("sys.modules", {"easyocr": mock_easyocr}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        parsers_mod._ocr_easyocr(img, "eng")

    _, kwargs = mock_easyocr.Reader.call_args
    assert kwargs.get("gpu") is True


@pytest.mark.unit
def test_ocr_easyocr_maps_tesseract_lang_to_easyocr_code() -> None:
    """lang='fra' is mapped to 'fr' before being passed to easyocr.Reader."""
    img = _fake_image()
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = ["Bonjour."]
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.return_value = mock_reader

    with patch.dict("sys.modules", {"easyocr": mock_easyocr}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_easyocr(img, "fra")

    assert result == "Bonjour."
    # First positional arg to Reader should be the mapped lang list
    lang_list = mock_easyocr.Reader.call_args[0][0]
    assert lang_list == ["fr"]


@pytest.mark.unit
def test_ocr_easyocr_not_installed_returns_none() -> None:
    """[easyocr] extra not installed → _ocr_easyocr returns None (graceful)."""
    img = _fake_image()
    with patch.dict("sys.modules", {"easyocr": None}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_easyocr(img, "eng")

    assert result is None


@pytest.mark.unit
def test_ocr_easyocr_reader_raises_returns_none() -> None:
    """easyocr.Reader raises (e.g. bad lang code) → None."""
    img = _fake_image()
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.side_effect = RuntimeError("lang not found")

    with patch.dict("sys.modules", {"easyocr": mock_easyocr}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_easyocr(img, "eng")

    assert result is None


@pytest.mark.unit
def test_ocr_easyocr_readtext_raises_returns_none() -> None:
    """easyocr reader.readtext raises → None."""
    img = _fake_image()
    mock_reader = MagicMock()
    mock_reader.readtext.side_effect = RuntimeError("CUDA OOM")
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.return_value = mock_reader

    with patch.dict("sys.modules", {"easyocr": mock_easyocr}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod._ocr_easyocr(img, "eng")

    assert result is None


# ---------------------------------------------------------------------------
# _tesseract_to_easyocr_langs: language code mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tesseract_to_easyocr_langs_single_known() -> None:
    """Single known code is mapped correctly."""
    from movate.kb.parsers import _tesseract_to_easyocr_langs  # noqa: PLC0415

    assert _tesseract_to_easyocr_langs("eng") == ["en"]
    assert _tesseract_to_easyocr_langs("fra") == ["fr"]
    assert _tesseract_to_easyocr_langs("deu") == ["de"]
    assert _tesseract_to_easyocr_langs("jpn") == ["ja"]


@pytest.mark.unit
def test_tesseract_to_easyocr_langs_compound() -> None:
    """Compound '+' syntax maps each part independently."""
    from movate.kb.parsers import _tesseract_to_easyocr_langs  # noqa: PLC0415

    assert _tesseract_to_easyocr_langs("eng+fra") == ["en", "fr"]
    assert _tesseract_to_easyocr_langs("eng+fra+deu") == ["en", "fr", "de"]


@pytest.mark.unit
def test_tesseract_to_easyocr_langs_unknown_passthrough() -> None:
    """Unknown codes pass through unchanged (EasyOCR may accept them or silently ignore)."""
    from movate.kb.parsers import _tesseract_to_easyocr_langs  # noqa: PLC0415

    assert _tesseract_to_easyocr_langs("xyz") == ["xyz"]
    assert _tesseract_to_easyocr_langs("xyz+eng") == ["xyz", "en"]


@pytest.mark.unit
def test_tesseract_to_easyocr_langs_chinese_scripts() -> None:
    """Chinese simplified/traditional use EasyOCR's extended codes ch_sim / ch_tra."""
    from movate.kb.parsers import _tesseract_to_easyocr_langs  # noqa: PLC0415

    assert _tesseract_to_easyocr_langs("chi_sim") == ["ch_sim"]
    assert _tesseract_to_easyocr_langs("chi_tra") == ["ch_tra"]
