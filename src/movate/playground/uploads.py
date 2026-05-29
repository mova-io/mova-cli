"""Upload → conversation-context adapter (pure logic).

When an operator drops a file into the playground chat, we want its
text to become *conversation context* the agent can reference on
subsequent turns ("summarise the uploaded doc", "what does section 3
say?"). The extraction itself is **not** reimplemented here — it reuses
the exact same parser the KB-ingest endpoint uses
(:func:`movate.kb.parsers.parse_document`), so PDF / DOCX / HTML / MD /
TXT all extract identically to how they would if ingested into the KB.

This module is the thin adapter between that parser and the chat:

* classify a file as *text-extractable* vs *image* (images are held but
  multimodal/vision is deferred — see :class:`UploadOutcome`),
* run the shared extractor and package the result into an
  :class:`UploadedDocument` the conversation layer can splice into
  context,
* enforce a per-file size cap (the count cap is enforced by the UI's
  file picker; the size cap is re-checked here as defence in depth).

Pure logic, no Chainlit — unit-testable in isolation. The Chainlit app
reads bytes off the wire and calls :func:`adapt_upload`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from movate.kb.parsers import ParseResult, is_supported_extension, parse_document

# Extensions the shared parser supports via OCR (images). We hold these
# but do NOT feed them as text context in v1 — vision is a future
# capability (see module docstring + UploadOutcome.IMAGE_DEFERRED).
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".gif", ".webp", ".bmp"}
)


class UploadOutcome(StrEnum):
    """Why an upload did (not) become usable conversation context."""

    EXTRACTED = "extracted"
    """Text was extracted and is ready to splice into context."""

    EMPTY = "empty"
    """The file parsed but yielded no text (e.g. a blank doc)."""

    IMAGE_DEFERRED = "image_deferred"
    """An image was uploaded. Held, but vision/multimodal is deferred —
    its bytes are not turned into text context in v1."""

    UNSUPPORTED = "unsupported"
    """The extension has no registered parser — skipped."""

    TOO_LARGE = "too_large"
    """The file exceeded the per-file size cap; not read into context."""

    PARSE_FAILED = "parse_failed"
    """The parser ran but failed (corrupt / encrypted bytes)."""


@dataclass(frozen=True)
class UploadedDocument:
    """An uploaded file's distilled, context-ready form.

    Held in ``cl.user_session`` so subsequent turns can reference the
    document. Only :attr:`text` is fed to the agent as context; the
    other fields are for UI status + bookkeeping.
    """

    filename: str
    outcome: UploadOutcome
    text: str = ""
    """Extracted text (empty unless ``outcome == EXTRACTED``)."""

    size_bytes: int = 0
    note: str = ""
    """Human-readable status detail for the UI (e.g. why it was skipped)."""


def is_image(filename: str) -> bool:
    """True when ``filename``'s extension is an image format.

    Image uploads are held but deferred (no vision in v1) — kept as a
    separate predicate so the app can message the operator clearly.
    """
    idx = filename.rfind(".")
    if idx < 0:
        return False
    return filename[idx:].lower() in _IMAGE_EXTENSIONS


def adapt_upload(
    filename: str,
    content: bytes,
    *,
    max_size_mb: int,
) -> UploadedDocument:
    """Convert one uploaded file into an :class:`UploadedDocument`.

    Decision order (each step short-circuits):

    1. **size** — over ``max_size_mb`` → ``TOO_LARGE`` (defence in depth;
       the UI picker also caps this).
    2. **image** — vision deferred → ``IMAGE_DEFERRED`` (held, not text).
    3. **unsupported** — no parser for the extension → ``UNSUPPORTED``.
    4. **parse** — run the shared KB extractor:
       ``None`` → ``PARSE_FAILED``; empty text → ``EMPTY``;
       otherwise → ``EXTRACTED`` with the text.

    Never raises — every failure mode maps to an :class:`UploadOutcome`
    so a single bad file degrades to a status line, not an exception.
    """
    size_bytes = len(content)
    if size_bytes > max_size_mb * 1024 * 1024:
        return UploadedDocument(
            filename=filename,
            outcome=UploadOutcome.TOO_LARGE,
            size_bytes=size_bytes,
            note=f"{size_bytes / 1024 / 1024:.1f} MB exceeds the {max_size_mb} MB cap",
        )

    if is_image(filename):
        return UploadedDocument(
            filename=filename,
            outcome=UploadOutcome.IMAGE_DEFERRED,
            size_bytes=size_bytes,
            note="image held; multimodal/vision is a future capability (text-only in v1)",
        )

    if not is_supported_extension(filename):
        return UploadedDocument(
            filename=filename,
            outcome=UploadOutcome.UNSUPPORTED,
            size_bytes=size_bytes,
            note="no parser for this file type",
        )

    result: ParseResult | None = parse_document(filename, content)
    if result is None:
        return UploadedDocument(
            filename=filename,
            outcome=UploadOutcome.PARSE_FAILED,
            size_bytes=size_bytes,
            note="parser could not extract text (corrupt or encrypted?)",
        )

    text = result.text.strip()
    if not text:
        return UploadedDocument(
            filename=filename,
            outcome=UploadOutcome.EMPTY,
            size_bytes=size_bytes,
            note="parsed but no text content",
        )

    return UploadedDocument(
        filename=filename,
        outcome=UploadOutcome.EXTRACTED,
        text=text,
        size_bytes=size_bytes,
        note=f"{len(text)} chars extracted",
    )


@dataclass
class UploadStore:
    """In-session accumulator of extracted upload context.

    Lives in ``cl.user_session``. Only :class:`UploadedDocument` whose
    text was successfully extracted contribute to conversation context;
    deferred images / failures are tracked for UI status but excluded
    from what is sent to the agent.
    """

    documents: list[UploadedDocument] = field(default_factory=list)

    def add(self, doc: UploadedDocument) -> None:
        self.documents.append(doc)

    def context_documents(self) -> list[UploadedDocument]:
        """The subset whose extracted text should be fed as context."""
        return [d for d in self.documents if d.outcome == UploadOutcome.EXTRACTED]

    def has_context(self) -> bool:
        return any(d.outcome == UploadOutcome.EXTRACTED for d in self.documents)
