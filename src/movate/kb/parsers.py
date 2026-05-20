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
* **HTML** via readability-lxml + BeautifulSoup (PR-M) — runs
  Mozilla's Readability algorithm to extract just the main-article
  content, then strips tags. Right tier for Confluence / Notion /
  blog exports which carry heavy nav / sidebar / ad chrome.
* **Markdown / plain text** via UTF-8 decode (already supported
  by the runtime endpoint; included here for unified dispatch).

Out of scope (tracked in BACKLOG §10.6):

* **Legacy .doc** (binary Word format pre-2007) — python-docx
  rejects these; operators should convert to .docx first.
* **Scanned-image PDFs / EPUB / PPTX / scanned-image content** —
  each needs its own parser tier; tackle by demand.

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
        ".html",
        ".htm",
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


def parse_html(content: bytes) -> str | None:
    """Extract main-article text from HTML via Readability + BeautifulSoup.

    Two-stage pipeline:

    1. **Readability** — runs Mozilla's algorithm against the DOM,
       extracts the ``<article>`` / main-content block, throws away
       nav / sidebar / footer / ads. Same heuristic browsers' Reader
       View uses. This step is the difference between "useful for
       retrieval" and "garbage with menu items embedded in every
       chunk".
    2. **BeautifulSoup strip-tags** on the extracted block, with
       paragraph-break preservation between block-level elements
       (``<p>``, ``<h1>``-``<h6>``, ``<li>``, ``<br>``).

    Returns ``None`` if:

    * The bytes don't decode as text (rare — HTML is text-based, but
      arbitrary binary uploads with a ``.html`` extension hit this).
    * Readability can't find a main-content block AND the strip-tags
      fallback produces empty output (e.g. ``<html></html>``).

    Why readability + BeautifulSoup vs alternatives:

    * **Naive strip-all-tags**: keeps the whole page including nav /
      sidebar / footer junk. Useless for retrieval over real
      Confluence / Notion exports.
    * **trafilatura** (~600KB + deps): broader feature set
      (boilerplate detection, metadata extraction). Right tier for
      a content-pipeline product, overkill for KB ingest.
    * **html2text**: produces clean Markdown but ~100KB + slower
      to extract main content. We don't need Markdown round-trip —
      plain text feeds the chunker fine.
    """
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415 — lazy: only paid for HTML uploads
        from readability import Document  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "readability-lxml or beautifulsoup4 not installed — HTML parsing unavailable. "
            "Install via: uv pip install readability-lxml beautifulsoup4"
        )
        return None

    # Decode bytes. HTML in the wild uses many encodings (latin-1,
    # cp1252, gbk, ...); try utf-8 first, fall back to latin-1 (which
    # never fails). The lossy fallback is acceptable because we're
    # extracting for retrieval, not round-tripping the original.
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            return None

    # Stage 1: Readability picks the main-article block.
    main_html: str
    try:
        doc = Document(text)
        main_html = doc.summary(html_partial=True)
    except Exception as exc:
        logger.warning("readability failed: %s; falling back to raw strip", exc)
        main_html = text

    # Stage 2: BeautifulSoup strip-tags with block-element paragraph
    # breaks. Replace block-level tags with `\n\n` before text
    # extraction so paragraphs / headings / list items become natural
    # chunk boundaries.
    try:
        soup = BeautifulSoup(main_html, "html.parser")
    except Exception as exc:
        logger.warning("BeautifulSoup failed to parse HTML: %s", exc)
        return None

    # Drop script / style / noscript outright — even when readability
    # selects them, they're never useful retrieval content.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Insert paragraph breaks before block-level elements so the
    # chunker's \n\n splitter respects HTML's visual structure.
    for tag_name in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "div"):
        for tag in soup.find_all(tag_name):
            tag.insert_before("\n\n")

    raw = soup.get_text()
    # Collapse runs of whitespace within paragraphs; preserve the
    # \n\n paragraph boundaries between them.
    paragraphs: list[str] = []
    for para in raw.split("\n\n"):
        # Normalize internal whitespace to single spaces — preserves
        # readability without keeping HTML's stray tab/newline noise.
        cleaned = " ".join(para.split())
        if cleaned:
            paragraphs.append(cleaned)

    if not paragraphs:
        return None
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
    ".html": parse_html,
    ".htm": parse_html,
}
