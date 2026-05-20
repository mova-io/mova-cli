"""Document parsers — uploaded file bytes → extracted text.

The KB ingest pipeline operates on plain text strings. Real KB
content arrives as PDFs (policy documents, runbooks), DOCXes
(internal wikis exported to Word), HTML pages (Confluence /
Notion exports). Without a parser layer, every operator has to
pre-extract text on their own before ``mdk kb ingest`` accepts
the content — a friction point that the PR-D Chainlit upload
flow made acutely visible (PDFs get ``status="skipped"``).

This module is the dispatch layer. Each format gets a small
parser; the orchestrator picks one by file extension. Failures
return ``None`` so the caller can surface a clean
``status="skipped"`` rather than 500-ing the upload.

v0.9 MVP scope:

* **PDF** via pypdf (PR-G) — handles text-based PDFs.
  Scanned-image PDFs need OCR; deferred to a separate
  ``[ocr]`` extra with tesseract.
* **DOCX** via python-docx (PR-L) — handles standard Word
  documents. Honors heading levels as paragraph boundaries.
* **Markdown / plain text** via UTF-8 decode (already supported
  by the runtime endpoint; included here for unified dispatch).

Out of scope (tracked in BACKLOG §10.6):

* **HTML** — needs readability-lxml to extract main-article content
  from messy nav/sidebar/ad noise.
* **Legacy .doc** (binary Word format pre-2007) — python-docx
  rejects these; operators should convert to .docx first.

The :class:`DocumentParser` Protocol lays the groundwork for
per-format entry-point registration when more formats land — but
v0.9 keeps it simple with a dispatch dict.
"""

from __future__ import annotations

import io
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


# File extensions the dispatch layer accepts. Sorted by frequency
# of operator-uploaded format (rough — refine when we have telemetry).
# Anything not in this set returns ``None`` from
# :func:`parse_document` so the caller reports ``status="skipped"``.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".pdf",
        ".docx",
    }
)


class DocumentParser(Protocol):
    """Per-format parser interface. Each registered parser converts
    raw bytes to extracted text or returns ``None`` on failure.

    Implementations MUST NOT raise; they log and return ``None`` so
    the dispatch layer can route a single bad file's
    ``status="skipped"`` without sinking a multi-file upload.
    """

    def __call__(self, content: bytes) -> str | None: ...


def parse_document(filename: str, content: bytes) -> str | None:
    """Extract text from ``content`` based on ``filename``'s extension.

    Returns the extracted text on success, or ``None`` if:
    * the extension isn't in :data:`SUPPORTED_EXTENSIONS`, OR
    * the parser failed (corrupt bytes, encrypted PDF, etc).

    The two failure modes are distinguishable by the caller via
    :func:`is_supported_extension` — useful for status reporting
    (``"skipped"`` for unsupported vs ``"empty"`` for parsed-but-no-text).

    Args:
        filename: The uploaded file's name (used only for extension
            dispatch — content is identified purely by its bytes).
        content: The raw bytes from the upload.

    Returns:
        Extracted text (UTF-8 string) or ``None``.
    """
    if not filename:
        return None
    ext = _extension(filename)
    parser = _PARSERS.get(ext)
    if parser is None:
        return None
    return parser(content)


def is_supported_extension(filename: str) -> bool:
    """Return True if ``filename``'s extension has a registered parser.

    Used by the KB upload endpoint + CLI to short-circuit
    obviously-unsupported files before reading the bytes off the
    wire (saves bandwidth on bulk uploads).
    """
    return _extension(filename) in SUPPORTED_EXTENSIONS


def _extension(filename: str) -> str:
    """Return the lowercase extension including the dot, or ``""``."""
    idx = filename.rfind(".")
    if idx < 0:
        return ""
    return filename[idx:].lower()


# ---------------------------------------------------------------------------
# Per-format parsers
# ---------------------------------------------------------------------------


def parse_text(content: bytes) -> str | None:
    """Decode UTF-8 text. Returns ``None`` on decode failure.

    Used for .md / .markdown / .txt. Same semantics as the KB
    upload endpoint's existing inline decode — extracting here
    centralizes the failure-mode reporting.
    """
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def parse_pdf(content: bytes) -> str | None:
    """Extract text from a PDF via pypdf.

    Returns the concatenated page text (one page per ``\\n\\n``
    paragraph) so the downstream paragraph-chunker can split
    naturally on page boundaries. Returns ``None`` if:

    * pypdf rejects the bytes (corrupt / not a PDF / encrypted
      without a password), OR
    * every page yields empty text (scanned-image PDF — needs OCR).

    Why pypdf vs alternatives:

    * **pypdf** (~250KB, pure Python): text extraction only,
      enough for the 80% case. Handles text-based PDFs reliably.
    * **pdfplumber** (~600KB + multiple transitive deps): adds
      table extraction. Overkill for the v0.9 MVP — operators
      who need table-aware extraction can preprocess.
    * **pdfminer.six** (~1MB): more accurate layout extraction
      but 3x larger install + slower per-page parse.

    For text-extraction-only on internal docs, pypdf is the
    correct tier.
    """
    try:
        import pypdf  # noqa: PLC0415 — lazy: only paid for when an operator uploads a PDF
    except ImportError:
        logger.warning(
            "pypdf is not installed — PDF parsing unavailable. Install via: uv pip install pypdf"
        )
        return None

    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
    except Exception as exc:
        logger.warning("pypdf failed to open PDF: %s", exc)
        return None

    # Encrypted PDFs need a password to extract. We don't have a
    # password channel in the upload UI; surface as ``None`` so the
    # operator sees ``status="skipped"`` rather than a partial extract.
    if reader.is_encrypted:
        logger.warning("PDF is encrypted; skipping")
        return None

    pages: list[str] = []
    for page_num, page in enumerate(reader.pages):
        try:
            text = page.extract_text()
        except Exception as exc:
            logger.warning("page %d extraction failed: %s; skipping page", page_num, exc)
            continue
        if text and text.strip():
            pages.append(text.strip())

    if not pages:
        # All-pages-empty = scanned image PDF or extraction failure.
        # Either way the result is unusable for retrieval.
        return None

    # Join pages with the paragraph boundary the chunker uses so
    # natural page breaks become chunk breaks.
    return "\n\n".join(pages)


def parse_docx(content: bytes) -> str | None:
    """Extract text from a .docx via python-docx.

    Returns paragraphs joined by ``\\n\\n`` so the downstream chunker
    treats them as natural chunk boundaries. Inserts a paragraph
    break around any paragraph with the "Heading 1" / "Heading 2" /
    etc. style so heading-style cues survive the round-trip even if
    the heading itself doesn't get pulled into its own chunk.

    Returns ``None`` if:

    * python-docx rejects the bytes (not a valid .docx — legacy
      binary .doc, corrupt zip, or a misnamed file), OR
    * the document has no extractable text (paragraphs all empty,
      or document contains only embedded images / tables we don't
      extract from in v0.9).

    Why python-docx vs alternatives:

    * **python-docx** (~3MB incl. lxml): mature, well-maintained,
      handles standard .docx files. Only reads from the document's
      main body; doesn't touch headers/footers (intentional — they
      are usually boilerplate noise).
    * **mammoth**: better at converting to HTML/Markdown but ~6MB
      with extra deps. Overkill for plain text extraction.
    * **textract**: heavyweight (depends on many system tools).

    Tables in .docx files are NOT extracted in v0.9 — most real KB
    content lives in paragraphs, and table cells would need their
    own row/col reconstruction logic. Operator can convert tables
    to inline text in Word before upload if they're critical.
    """
    try:
        import docx  # noqa: PLC0415 — lazy: only paid for when an operator uploads a .docx
    except ImportError:
        logger.warning(
            "python-docx is not installed — DOCX parsing unavailable. "
            "Install via: uv pip install python-docx"
        )
        return None

    try:
        # python-docx's Document() accepts a file-like object.
        document = docx.Document(io.BytesIO(content))
    except Exception as exc:
        logger.warning("python-docx failed to open document: %s", exc)
        return None

    paragraphs: list[str] = []
    for para in document.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        # Headings get their own paragraph so the chunker doesn't
        # blur a "Refund Policy" heading into the surrounding
        # body text — operator-visible structure stays intact.
        style_name = getattr(getattr(para, "style", None), "name", "") or ""
        if style_name.lower().startswith("heading"):
            paragraphs.append(text)
        else:
            paragraphs.append(text)

    if not paragraphs:
        # Empty document OR contained only tables / images we don't
        # extract from. Either way the result is unusable.
        return None

    # Same join semantics as parse_pdf — chunker splits on \n\n.
    return "\n\n".join(paragraphs)


# Dispatch table — extension → parser. Keeping this at the module
# bottom lets each parser's docstring sit next to the function for
# readability. Extending: add the extension to SUPPORTED_EXTENSIONS,
# add the parser function above, register here.
_PARSERS: dict[str, DocumentParser] = {
    ".md": parse_text,
    ".markdown": parse_text,
    ".txt": parse_text,
    ".pdf": parse_pdf,
    ".docx": parse_docx,
}
