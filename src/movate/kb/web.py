"""Single-page web ingest: URL → fetched HTML → readable text.

F5 (#114): lets ``mdk kb ingest <agent> <url>`` pull a single web
page into the KB through the *same* chunk → embed → store pipeline a
local file uses (:func:`movate.kb.ingest.ingest_text`). Only the
"get bytes + strip to text" front-end is new; everything downstream
(chunking, embedding, dedup, storage) is unchanged.

Design constraints (CLAUDE.md §8 — minimal dependencies):

* **Fetch** with ``httpx`` (already a shipped dependency — it is the
  client the ``--target`` remote paths use).
* **Extract** with the **standard library** only — :class:`html.parser.HTMLParser`
  strips tags, drops the content of non-prose elements
  (``<script>`` / ``<style>`` / ``<nav>`` / ``<footer>`` / ``<head>`` /
  ``<noscript>`` / ``<template>``), and collapses whitespace into
  paragraph-separated text the chunker's ``\\n\\n`` splitter respects.

  This is deliberately a *low-fidelity* extractor — it keeps zero new
  shipped deps. A higher-quality reader (Readability + BeautifulSoup,
  already used by :func:`movate.kb.parsers.parse_html` for ``.html``
  *file* uploads, or trafilatura) can be wired in later behind an
  opt-in ``pyproject.toml`` extra without changing this module's
  public surface.

Scope: single page only. Multi-page crawl (F6) and ``--llm``
auto-ingest wiring (F7) are deferred.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# A source argument is treated as a URL (rather than a filesystem path)
# iff it starts with an ``http://`` or ``https://`` scheme. Everything
# else stays on the unchanged local-file path — keeps backward compat.
_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)

# Default GET timeout (seconds). Generous enough for slow origins but
# bounded so a hung server surfaces a clean typed error rather than
# blocking the CLI indefinitely.
DEFAULT_FETCH_TIMEOUT_S = 30.0

# Below this many characters of extracted prose we treat the page as
# having "nothing ingestible" — a login wall, a JS-only shell, or an
# empty document. 40 chars comfortably clears the chunker's 20-char
# MIN_CHUNK_CHARS floor while still rejecting near-empty pages.
_MIN_INGESTIBLE_CHARS = 40

# Elements whose *content* is never useful retrieval prose. We drop
# everything between the open and close tag (not just the tag itself).
# <nav>/<footer> are page chrome; the rest are non-prose machinery.
_SKIP_CONTENT_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "head",
        "nav",
        "footer",
    }
)

# Block-level elements that introduce a paragraph boundary. Inserting a
# ``\n\n`` around these makes the chunker's blank-line splitter line up
# with the page's visual structure instead of mashing headings, list
# items, and paragraphs into one blob.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)

# 2xx success range is [_HTTP_OK, _HTTP_REDIRECT) — 200..299 inclusive.
_HTTP_OK = 200
_HTTP_REDIRECT = 300


class WebFetchError(Exception):
    """Raised when a URL can't be fetched or has no ingestible content.

    Carries a human-readable reason that already names the URL, so the
    CLI can print it verbatim as a clean one-line error (exit non-zero)
    instead of leaking an ``httpx`` stack trace. Covers:

    * non-2xx HTTP status,
    * timeout / connection / transport error,
    * empty or too-short extracted text ("nothing ingestible").
    """


def is_url(arg: str) -> bool:
    """Return ``True`` iff ``arg`` should be treated as a web URL.

    The single source-detection rule for ``mdk kb ingest``: a string
    matching ``^https?://`` is a URL; anything else is a filesystem
    path (unchanged behavior). Case-insensitive on the scheme.
    """
    return bool(_URL_SCHEME_RE.match(arg))


class _TextExtractor(HTMLParser):
    """Stdlib HTML → text stripper.

    Accumulates text data, skipping the *content* of non-prose elements
    (:data:`_SKIP_CONTENT_TAGS`) and inserting paragraph breaks around
    block-level elements (:data:`_BLOCK_TAGS`). The result is a single
    string with ``\\n\\n`` between visual blocks, ready for the
    paragraph chunker. Whitespace collapsing happens in
    :func:`extract_text` after parsing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        # Depth counter per skip-tag so nested <script> etc. unwind
        # correctly; >0 means "inside skipped content, drop data".
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        tag = tag.lower()
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n\n")

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        # Self-closing block (e.g. <br/>) still introduces a break.
        if tag.lower() in _BLOCK_TAGS:
            self._parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_CONTENT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def extract_text(html: str) -> str:
    """Strip ``html`` to readable, paragraph-separated plain text.

    Uses the stdlib :class:`html.parser.HTMLParser` (zero new deps):
    drops ``<script>`` / ``<style>`` / ``<nav>`` / ``<footer>`` (and
    other non-prose) content, treats block-level tags as paragraph
    boundaries, then collapses each paragraph's internal whitespace to
    single spaces while preserving the ``\\n\\n`` boundaries between
    them — exactly the shape :func:`movate.kb.chunk.split_paragraphs`
    expects.

    Returns the empty string for input that yields no prose.
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTMLParser is lenient, but malformed input can still raise.
        # Fall back to whatever was accumulated before the failure.
        pass

    raw = unescape(parser.get_text())

    paragraphs: list[str] = []
    for para in raw.split("\n\n"):
        # Collapse runs of whitespace (incl. stray tabs/newlines from
        # source formatting) to single spaces within each paragraph.
        cleaned = " ".join(para.split())
        if cleaned:
            paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)


def fetch_url(url: str, *, timeout_s: float = DEFAULT_FETCH_TIMEOUT_S) -> str:
    """GET ``url`` and return the response body as text.

    Uses ``httpx`` (already a shipped dependency) with redirects
    followed. Translates every failure mode into a :class:`WebFetchError`
    whose message already names the URL + reason, so the CLI never leaks
    a stack trace:

    * **non-2xx status** → ``WebFetchError`` ("HTTP <code>").
    * **timeout / connection / transport error** → ``WebFetchError``
      (the underlying reason, no traceback).

    Returns the raw response text (HTML). Extraction is a separate step
    (:func:`extract_text`) so callers can test fetch + parse in isolation.
    """
    # Lazy import: keeps httpx off the import path for KB code that never
    # ingests a URL, mirroring the rest of the CLI's lazy-httpx pattern.
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_s),
            headers={"User-Agent": "movate-cli/kb-ingest"},
        )
    except httpx.HTTPError as exc:
        raise WebFetchError(f"could not fetch {url}: {exc}") from exc

    if not (_HTTP_OK <= resp.status_code < _HTTP_REDIRECT):
        raise WebFetchError(
            f"could not fetch {url}: HTTP {resp.status_code} {resp.reason_phrase}".rstrip()
        )

    return resp.text


def fetch_and_extract(url: str, *, timeout_s: float = DEFAULT_FETCH_TIMEOUT_S) -> str:
    """Fetch ``url`` and return its extracted, ingestible prose.

    Convenience wrapper over :func:`fetch_url` + :func:`extract_text`
    that also enforces the "nothing ingestible" guard: if the extracted
    text is shorter than :data:`_MIN_INGESTIBLE_CHARS` (login wall,
    JS-only shell, empty document), it raises :class:`WebFetchError`
    with a clear message rather than silently "succeeding" with no
    chunks. The returned text is what the caller hands to
    :func:`movate.kb.ingest.ingest_text`.
    """
    html = fetch_url(url, timeout_s=timeout_s)
    text = extract_text(html)
    if len(text) < _MIN_INGESTIBLE_CHARS:
        raise WebFetchError(
            f"nothing ingestible at {url} — the page produced "
            f"{len(text)} char(s) of extractable text "
            "(empty page, login wall, or JavaScript-rendered content)."
        )
    return text
