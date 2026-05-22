"""Tests for parse_image — PNG / JPG / TIFF → OCR text (PR-JJ).

Coverage:
* parse_image: Pillow opens bytes, converts to RGB, routes to Tesseract (default)
* parse_image: MOVATE_OCR_BACKEND=easyocr routes to EasyOCR backend
* parse_image: always sets ocr_used=True on success
* parse_image: Pillow not installed → None (graceful)
* parse_image: Pillow.open raises (corrupt bytes) → None
* parse_image: OCR backend returns None → parse_image returns None
* parse_image: OCR returns blank string → None
* parse_image: whitespace normalisation applied (same rules as PDF OCR path)
* parse_image: MOVATE_OCR_LANG passed through to backend
* parse_document: routes .png / .jpg / .jpeg / .tiff / .tif to parse_image
* is_supported_extension: image extensions in SUPPORTED_EXTENSIONS
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_pil_image(label: str = "img") -> MagicMock:
    """Return a MagicMock that stands in for a PIL Image object."""
    img = MagicMock()
    img.__repr__ = lambda _: f"<FakePILImage {label}>"
    # convert() should return another image mock
    img.convert.return_value = img
    return img


def _make_pil_module(image: Any) -> MagicMock:
    """Return a mock PIL module whose Image.open() returns ``image``."""
    mock_pil = MagicMock()
    mock_pil.Image.open.return_value = image
    return mock_pil


# ---------------------------------------------------------------------------
# parse_image: Tesseract (default) backend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_image_basic_tesseract() -> None:
    """parse_image opens bytes via Pillow, converts to RGB, runs Tesseract."""
    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Scanned image text."

    with patch.dict(
        "sys.modules",
        {"PIL": mock_pil, "PIL.Image": mock_pil.Image, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"\x89PNG\r\n\x1a\n")

    assert result is not None
    assert result.text == "Scanned image text."
    assert result.ocr_used is True
    # Pillow must be called with the raw bytes wrapped in BytesIO
    mock_pil.Image.open.assert_called_once()
    # RGB conversion is mandatory
    img.convert.assert_called_once_with("RGB")
    mock_pytesseract.image_to_string.assert_called_once()


@pytest.mark.unit
def test_parse_image_ocr_used_always_true() -> None:
    """ocr_used is True whenever parse_image succeeds — image OCR is always OCR."""
    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Some text."

    with patch.dict(
        "sys.modules",
        {"PIL": mock_pil, "PIL.Image": mock_pil.Image, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"fakepng")

    assert result is not None
    assert result.ocr_used is True


@pytest.mark.unit
def test_parse_image_whitespace_normalised() -> None:
    """Whitespace normalisation applied: space/tab runs collapsed, excess newlines trimmed."""
    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = (
        "Hello   world.\t\tMore   text.\n\n\n\nNew paragraph."
    )

    with patch.dict(
        "sys.modules",
        {"PIL": mock_pil, "PIL.Image": mock_pil.Image, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"fakejpg")

    assert result is not None
    assert result.text == "Hello world. More text.\n\nNew paragraph."


@pytest.mark.unit
def test_parse_image_lang_env_var_passed_to_tesseract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MOVATE_OCR_LANG env var is forwarded to the OCR backend."""
    monkeypatch.setenv("MOVATE_OCR_LANG", "fra")

    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Bonjour."

    with patch.dict(
        "sys.modules",
        {"PIL": mock_pil, "PIL.Image": mock_pil.Image, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"fakepng")

    assert result is not None
    _, kwargs = mock_pytesseract.image_to_string.call_args
    assert kwargs["lang"] == "fra"


# ---------------------------------------------------------------------------
# parse_image: EasyOCR backend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_image_easyocr_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_OCR_BACKEND=easyocr routes to EasyOCR; pytesseract NOT called."""
    monkeypatch.setenv("MOVATE_OCR_BACKEND", "easyocr")

    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)

    mock_reader = MagicMock()
    mock_reader.readtext.return_value = ["EasyOCR line 1.", "EasyOCR line 2."]
    mock_easyocr = MagicMock()
    mock_easyocr.Reader.return_value = mock_reader

    mock_pytesseract = MagicMock()

    with patch.dict(
        "sys.modules",
        {
            "PIL": mock_pil,
            "PIL.Image": mock_pil.Image,
            "easyocr": mock_easyocr,
            "pytesseract": mock_pytesseract,
        },
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"fakepng")

    assert result is not None
    assert result.text == "EasyOCR line 1.\nEasyOCR line 2."
    assert result.ocr_used is True
    mock_easyocr.Reader.assert_called_once()
    mock_reader.readtext.assert_called_once()
    mock_pytesseract.image_to_string.assert_not_called()


# ---------------------------------------------------------------------------
# parse_image: failure / graceful-degradation cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_image_pillow_not_installed_returns_none() -> None:
    """Pillow not installed → parse_image returns None (graceful, logged at DEBUG)."""
    with patch.dict("sys.modules", {"PIL": None, "PIL.Image": None}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"\x89PNG\r\n\x1a\n")

    assert result is None


@pytest.mark.unit
def test_parse_image_corrupt_bytes_returns_none() -> None:
    """Pillow.open raises on corrupt bytes → None (logged at WARNING)."""
    mock_pil = MagicMock()
    mock_pil.Image.open.side_effect = OSError("cannot identify image file")

    with patch.dict("sys.modules", {"PIL": mock_pil, "PIL.Image": mock_pil.Image}):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"notanimage")

    assert result is None


@pytest.mark.unit
def test_parse_image_ocr_returns_none_gives_none() -> None:
    """OCR backend returns None → parse_image returns None."""
    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)
    mock_pytesseract = MagicMock()
    # The parser's `except pytesseract.TesseractNotFoundError` needs a real
    # exception class; a bare MagicMock attr would make `except` raise TypeError.
    mock_pytesseract.TesseractNotFoundError = type("TesseractNotFoundError", (Exception,), {})
    mock_pytesseract.image_to_string.side_effect = RuntimeError("tesseract not found")

    with patch.dict(
        "sys.modules",
        {"PIL": mock_pil, "PIL.Image": mock_pil.Image, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"fakepng")

    assert result is None


@pytest.mark.unit
def test_parse_image_blank_ocr_output_returns_none() -> None:
    """OCR returns only whitespace → None (not an empty ParseResult)."""
    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "   \n  \n  "

    with patch.dict(
        "sys.modules",
        {"PIL": mock_pil, "PIL.Image": mock_pil.Image, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_image(b"fakepng")

    assert result is None


# ---------------------------------------------------------------------------
# parse_document dispatch: image extensions
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("filename", ["photo.png", "scan.jpg", "scan.jpeg", "doc.tiff", "doc.tif"])
def test_parse_document_routes_image_extensions(filename: str) -> None:
    """parse_document routes all image extensions through parse_image."""
    img = _fake_pil_image()
    mock_pil = _make_pil_module(img)
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.return_value = "Image content."

    with patch.dict(
        "sys.modules",
        {"PIL": mock_pil, "PIL.Image": mock_pil.Image, "pytesseract": mock_pytesseract},
    ):
        from movate.kb import parsers as parsers_mod  # noqa: PLC0415

        importlib.reload(parsers_mod)
        result = parsers_mod.parse_document(filename, b"fakeimagebytes")

    assert result is not None
    assert result.ocr_used is True


# ---------------------------------------------------------------------------
# SUPPORTED_EXTENSIONS: image formats included
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".tiff", ".tif"])
def test_is_supported_extension_image_formats(ext: str) -> None:
    """Image extensions are in SUPPORTED_EXTENSIONS."""
    from movate.kb.parsers import is_supported_extension  # noqa: PLC0415

    assert is_supported_extension(f"scan{ext}")


@pytest.mark.unit
def test_is_supported_extension_unsupported_image_formats() -> None:
    """Uncommon image formats not in SUPPORTED_EXTENSIONS → False."""
    from movate.kb.parsers import is_supported_extension  # noqa: PLC0415

    assert not is_supported_extension("image.bmp")
    assert not is_supported_extension("image.webp")
    assert not is_supported_extension("image.gif")
