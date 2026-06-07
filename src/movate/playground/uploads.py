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
  file picker; the size cap is re-checked here as defence in depth),
* enforce a MIME/type allowlist so only known-safe file types are
  accepted (#218 upload hardening).

Pure logic, no Chainlit — unit-testable in isolation. The Chainlit app
reads bytes off the wire and calls :func:`adapt_upload`.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from enum import StrEnum

from movate.kb.parsers import ParseResult, is_supported_extension, parse_document

# Register MIME types that Python's ``mimetypes`` may not know.
# These are common document extensions the playground's KB parser handles.
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".markdown")
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"
)

# Extensions the shared parser supports via OCR (images). We hold these
# but do NOT feed them as text context in v1 — vision is a future
# capability (see module docstring + UploadOutcome.IMAGE_DEFERRED).
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".gif", ".webp", ".bmp"}
)

# ---------------------------------------------------------------------------
# Upload hardening (#218) — MIME/type allowlist + configurable size limit
# ---------------------------------------------------------------------------

#: Default MIME type prefixes/types that the playground will accept.
#: Configurable via ``MDK_PLAYGROUND_UPLOAD_MIME_ALLOWLIST`` (comma-separated).
DEFAULT_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "text/*",
        "application/pdf",
        "application/json",
        "image/*",
    }
)

#: Default per-file upload ceiling in MB.  Configurable via
#: ``MDK_PLAYGROUND_MAX_UPLOAD_MB`` env var.
DEFAULT_PLAYGROUND_MAX_UPLOAD_MB: int = 10


def configured_max_upload_mb() -> int:
    """Read the per-file upload ceiling from the env (or the default).

    The env var ``MDK_PLAYGROUND_MAX_UPLOAD_MB`` overrides the default
    when set to a positive integer.  Non-numeric / non-positive values
    fall back to the default silently.
    """
    raw = os.environ.get("MDK_PLAYGROUND_MAX_UPLOAD_MB", "")
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_PLAYGROUND_MAX_UPLOAD_MB


def configured_mime_allowlist() -> frozenset[str]:
    """Read the MIME allowlist from the env (or the default).

    ``MDK_PLAYGROUND_UPLOAD_MIME_ALLOWLIST`` is a comma-separated list of
    MIME types or ``prefix/*`` patterns (e.g. ``text/*,application/pdf``).
    """
    raw = os.environ.get("MDK_PLAYGROUND_UPLOAD_MIME_ALLOWLIST", "")
    if raw.strip():
        return frozenset(entry.strip() for entry in raw.split(",") if entry.strip())
    return DEFAULT_MIME_ALLOWLIST


def _mime_matches_allowlist(mime: str, allowlist: frozenset[str]) -> bool:
    """Check whether ``mime`` is permitted by the allowlist.

    Supports exact matches (``application/pdf``) and prefix wildcards
    (``text/*``).  A ``None`` / empty mime is rejected.
    """
    if not mime:
        return False
    mime_lower = mime.lower().split(";")[0].strip()  # strip params like charset
    if mime_lower in allowlist:
        return True
    prefix = mime_lower.split("/")[0] + "/*"
    return prefix in allowlist


def check_mime_allowed(filename: str, allowlist: frozenset[str] | None = None) -> str | None:
    """Return ``None`` if the file's MIME type is allowed, else an error message.

    Uses Python's :mod:`mimetypes` to guess from the extension.  Unknown
    extensions are rejected by default (the allowlist is opt-in).
    """
    if allowlist is None:
        allowlist = configured_mime_allowlist()
    mime, _ = mimetypes.guess_type(filename, strict=False)
    if mime is None:
        # Unknown extension — check if it's a known image or text extension
        # that mimetypes might miss (defense in depth).
        idx = filename.rfind(".")
        ext = filename[idx:].lower() if idx >= 0 else ""
        if ext in _IMAGE_EXTENSIONS:
            return None  # images are always allowed (held as deferred)
        return f"Unsupported file type: {ext or '(no extension)'}"
    if _mime_matches_allowlist(mime, allowlist):
        return None
    return f"Unsupported file type: {os.path.splitext(filename)[1]} ({mime})"


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

    MIME_REJECTED = "mime_rejected"
    """The file's MIME type is not in the configured allowlist (#218)."""

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
    mime_allowlist: frozenset[str] | None = None,
) -> UploadedDocument:
    """Convert one uploaded file into an :class:`UploadedDocument`.

    Decision order (each step short-circuits):

    1. **MIME check** (#218) — reject files whose MIME type is not in the
       configured allowlist → ``MIME_REJECTED``.
    2. **size** — over ``max_size_mb`` → ``TOO_LARGE`` (defence in depth;
       the UI picker also caps this).
    3. **image** — vision deferred → ``IMAGE_DEFERRED`` (held, not text).
    4. **unsupported** — no parser for the extension → ``UNSUPPORTED``.
    5. **parse** — run the shared KB extractor:
       ``None`` → ``PARSE_FAILED``; empty text → ``EMPTY``;
       otherwise → ``EXTRACTED`` with the text.

    Never raises — every failure mode maps to an :class:`UploadOutcome`
    so a single bad file degrades to a status line, not an exception.

    Parameters
    ----------
    mime_allowlist:
        Explicit allowlist to use.  ``None`` → MIME validation is
        **skipped** (backward compatibility with callers that predate
        #218).  Pass a ``frozenset`` (e.g. :data:`DEFAULT_MIME_ALLOWLIST`
        or :func:`configured_mime_allowlist`) to enforce.
    """
    size_bytes = len(content)

    # Step 1: MIME-type allowlist (#218).
    # Only enforced when the caller passes an explicit allowlist — old
    # callers that do not pass one get the pre-#218 behavior (no check).
    if mime_allowlist is not None:
        mime_err = check_mime_allowed(filename, allowlist=mime_allowlist)
    else:
        mime_err = None
    if mime_err is not None:
        return UploadedDocument(
            filename=filename,
            outcome=UploadOutcome.MIME_REJECTED,
            size_bytes=size_bytes,
            note=mime_err,
        )

    # Step 2: per-file size ceiling.
    if size_bytes > max_size_mb * 1024 * 1024:
        return UploadedDocument(
            filename=filename,
            outcome=UploadOutcome.TOO_LARGE,
            size_bytes=size_bytes,
            note=f"File too large (max {max_size_mb}MB)",
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
