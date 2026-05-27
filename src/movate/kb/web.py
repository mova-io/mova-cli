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

F6 (#115) adds an opt-in, bounded, same-domain crawl on top of this
single-page front-end: :func:`crawl_site` does a breadth-first walk
from a start URL, following ``<a href>`` links to other pages on the
**same registrable domain**, all reusing :func:`fetch_url` +
:func:`extract_text` per page and feeding the unchanged
``ingest_text`` pipeline (each page's source = its own URL). It adds
zero new shipped dependencies — link discovery is a second stdlib
:class:`html.parser.HTMLParser` subclass.

Scope: single page (F5) + bounded crawl (F6). ``--llm``
auto-ingest wiring (F7) is deferred.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

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

# ---------------------------------------------------------------------------
# F6 — bounded same-domain crawl
# ---------------------------------------------------------------------------

# Safe defaults for the bounded crawl (CLAUDE.md §10 — think in failure
# modes: a crawl that wanders or runs unbounded is the dangerous case).
# Both are *hard* caps, surfaced as CLI knobs (``--max-pages`` /
# ``--max-depth``); these constants are just the defaults.
DEFAULT_MAX_PAGES = 25
DEFAULT_MAX_DEPTH = 2

# Per-page politeness delay (seconds) between sequential fetches. Small
# enough not to make a 25-page crawl feel slow, large enough to avoid
# hammering an origin. Sequential by design — no aggressive concurrency.
DEFAULT_CRAWL_DELAY_S = 0.0

# URL schemes that are never crawlable page links. ``mailto:`` / ``tel:``
# aren't pages; ``javascript:`` is an inline action; ``data:`` is inline
# content. Anything not http(s) after normalization is skipped anyway,
# but naming these keeps the intent explicit.
_NON_PAGE_SCHEMES = frozenset({"mailto", "tel", "javascript", "data", "ftp", "file"})

# File extensions that are not HTML pages — skip by extension before we
# even fetch, so a crawl doesn't try to extract prose from a PDF/zip/img
# linked inline. (PDFs etc. are ingested via the *file* path, not crawl.)
_NON_HTML_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".rar",
        ".7z",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".ico",
        ".bmp",
        ".tiff",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".ogg",
        ".webm",
        ".css",
        ".js",
        ".json",
        ".xml",
        ".rss",
        ".atom",
        ".csv",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".dmg",
        ".exe",
        ".bin",
    }
)

# Content-Type prefixes we accept as crawlable HTML. A page whose
# response advertises e.g. ``application/pdf`` is skipped even if its URL
# had no telltale extension.
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml")


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


# ---------------------------------------------------------------------------
# F6 — link extraction + bounded same-domain BFS crawl
# ---------------------------------------------------------------------------


class _LinkExtractor(HTMLParser):
    """Stdlib HTML → list of raw ``href`` values from ``<a>`` tags.

    Deliberately minimal: it collects the raw ``href`` attribute of every
    anchor (in document order). Resolution to absolute URLs, same-domain
    filtering, scheme/extension skipping, and dedup all happen in
    :func:`extract_links` so this class stays a dumb token collector
    (mirrors how :class:`_TextExtractor` defers whitespace cleanup).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value)
                break


def _registrable_host(host: str) -> str:
    """Normalize a hostname for same-site comparison.

    Lowercases and strips a leading ``www.`` so ``www.example.com`` and
    ``example.com`` are treated as the same site. This is a deliberately
    simple, dependency-free heuristic — it compares the host as-is rather
    than computing the true registrable domain via the public-suffix list
    (which would need a new shipped dep, see CLAUDE.md §8). It is the
    *conservative* choice: it never treats two genuinely different hosts
    as the same site, so the crawl can't wander off to an unrelated
    domain. A subdomain like ``docs.example.com`` is treated as a
    distinct host and is NOT followed — by design, "same host" is the
    hard boundary.
    """
    host = host.lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    return host


def _same_site(start: str, candidate: str) -> bool:
    """Return ``True`` iff ``candidate`` is on the same site as ``start``.

    Compares the normalized host (:func:`_registrable_host`). Both must
    be http(s). A scheme upgrade (http→https) on the same host counts as
    same-site so a site that links to its own ``https://`` pages from an
    ``http://`` start URL still crawls.
    """
    s = urlsplit(start)
    c = urlsplit(candidate)
    if c.scheme not in ("http", "https") or s.scheme not in ("http", "https"):
        return False
    if not c.hostname:
        return False
    return _registrable_host(c.hostname) == _registrable_host(s.hostname or "")


def _normalize_url(url: str) -> str:
    """Canonical form for visited-set dedup: drop the fragment, keep the
    rest verbatim. (Trailing-slash and query normalization are
    intentionally left alone — two URLs differing only by query are
    genuinely different pages.)"""
    return urldefrag(url).url


def _looks_like_non_html(url: str) -> bool:
    """Cheap pre-fetch filter: ``True`` if the URL path ends in a known
    non-HTML file extension (PDF, image, archive, asset). Lets us skip a
    fetch we know would yield no prose. Query string is ignored for the
    extension check."""
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:] in _NON_HTML_EXTENSIONS


def extract_links(html: str, *, base_url: str) -> list[str]:
    """Return the absolute, deduped, crawlable links found in ``html``.

    Parses ``<a href>`` values with the stdlib :class:`_LinkExtractor`
    (zero new deps), then for each raw href:

    * resolves it against ``base_url`` (so relative links work),
    * strips the fragment,
    * skips non-page schemes (``mailto:`` / ``tel:`` / ``javascript:`` /
      ``data:`` …) and anything that isn't http(s),
    * skips obvious non-HTML files by extension,
    * dedups while preserving first-seen order.

    Same-*domain* filtering is intentionally NOT done here — that's the
    crawler's job (:func:`crawl_site`), so this helper stays reusable and
    independently testable. Returns links in document order.
    """
    parser = _LinkExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTMLParser is lenient but malformed input can still raise; use
        # whatever anchors were collected before the failure.
        pass

    out: list[str] = []
    seen: set[str] = set()
    for raw in parser.hrefs:
        href = raw.strip()
        if not href:
            continue
        # Reject non-page schemes up front (before urljoin, which would
        # otherwise pass a "mailto:" through unchanged).
        scheme = urlsplit(href).scheme.lower()
        if scheme in _NON_PAGE_SCHEMES:
            continue
        absolute = _normalize_url(urljoin(base_url, href))
        abs_scheme = urlsplit(absolute).scheme.lower()
        if abs_scheme not in ("http", "https"):
            continue
        if _looks_like_non_html(absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out


@dataclass
class CrawlPage:
    """One successfully-fetched page from a crawl: its URL + extracted prose."""

    url: str
    text: str


@dataclass
class CrawlResult:
    """Outcome of a :func:`crawl_site` run.

    ``pages`` are the successfully fetched + extractable pages (each with
    its own URL as the source). ``skipped`` is a list of
    ``(url, reason)`` for pages that 404'd / timed out / had no prose /
    were off-limits — surfaced so the CLI can warn without aborting the
    crawl. Counts make the CLI summary trivial.
    """

    pages: list[CrawlPage] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def fetched_count(self) -> int:
        return len(self.pages)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _is_html_response(content_type: str | None) -> bool:
    """``True`` if a response Content-Type advertises HTML (or is absent).

    A missing/blank Content-Type is treated as crawlable (be lenient —
    many origins omit it); a non-HTML type (``application/pdf``,
    ``image/png``) is skipped."""
    if not content_type:
        return True
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return True
    return any(ct.startswith(prefix) for prefix in _HTML_CONTENT_TYPES)


def _fetch_page_for_crawl(url: str, *, timeout_s: float) -> tuple[str, list[str]]:
    """Fetch one page during a crawl and return ``(extracted_text, links)``.

    Unlike :func:`fetch_url` (which returns the raw body), this does the
    full crawl-page job in one request: it checks the response is HTML by
    Content-Type, extracts prose AND discovers links from the same body
    (one fetch, not two). Raises :class:`WebFetchError` on any failure
    mode (non-2xx, transport error, non-HTML content-type, no ingestible
    prose) so the crawler's per-page ``try`` can isolate it.
    """
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_s),
            headers={"User-Agent": "movate-cli/kb-crawl"},
        )
    except httpx.HTTPError as exc:
        raise WebFetchError(f"could not fetch {url}: {exc}") from exc

    if not (_HTTP_OK <= resp.status_code < _HTTP_REDIRECT):
        raise WebFetchError(
            f"could not fetch {url}: HTTP {resp.status_code} {resp.reason_phrase}".rstrip()
        )

    if not _is_html_response(resp.headers.get("content-type")):
        raise WebFetchError(
            f"skipping {url}: non-HTML content-type ({resp.headers.get('content-type', '?')})"
        )

    html = resp.text
    # The page we landed on may differ from the requested URL after
    # redirects; resolve links against the *final* URL.
    final_url = str(resp.url) if resp.url else url
    text = extract_text(html)
    if len(text) < _MIN_INGESTIBLE_CHARS:
        raise WebFetchError(
            f"nothing ingestible at {url} — {len(text)} char(s) of extractable text."
        )
    links = extract_links(html, base_url=final_url)
    return text, links


def crawl_site(
    start_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    delay_s: float = DEFAULT_CRAWL_DELAY_S,
    on_page: Callable[[str, int, int], None] | None = None,
    on_skip: Callable[[str, str], None] | None = None,
    _fetch: Callable[[str], tuple[str, list[str]]] | None = None,
) -> CrawlResult:
    """Bounded, same-site breadth-first crawl from ``start_url``.

    Walks the site BFS, following ``<a href>`` links to other pages on the
    **same host** (:func:`_same_site`), sequentially fetching each page
    via ``httpx`` + stdlib (zero new deps). Reuses :func:`extract_text` +
    :func:`extract_links` per page (see :func:`_fetch_page_for_crawl`).

    Hard bounds (CLAUDE.md §10):

    * ``max_pages`` — at most this many pages are successfully ingested.
      The loop stops queuing/fetching once the cap is reached.
    * ``max_depth`` — the start page is depth 0; links from it are depth
      1; etc. Pages deeper than ``max_depth`` are never fetched.
    * **Same site only** — links to other hosts (and non-http(s),
      ``mailto:``, non-HTML files) are never followed.
    * **Dedup** — each normalized URL (fragment-stripped) is fetched at
      most once via a visited set.
    * **Per-page failure isolation** — a 404 / timeout / parse error /
      non-HTML / empty page is recorded in :attr:`CrawlResult.skipped`
      (and reported via ``on_skip``) but does NOT abort the crawl; the
      remaining queue is still processed.

    ``on_page(url, fetched_so_far, max_pages)`` and ``on_skip(url, reason)``
    are optional progress callbacks for the CLI to render human progress
    to stderr. ``_fetch`` is a test seam — when provided, it replaces the
    real network fetch with a callable mapping a URL to
    ``(extracted_text, discovered_links)`` (links already absolute).

    Returns a :class:`CrawlResult`. The caller embeds each
    :class:`CrawlPage` through the unchanged ``ingest_text`` pipeline with
    ``source = page.url``.
    """
    if max_pages <= 0 or max_depth < 0:
        return CrawlResult()

    fetch = _fetch or (lambda u: _fetch_page_for_crawl(u, timeout_s=timeout_s))

    result = CrawlResult()
    start_norm = _normalize_url(start_url)
    visited: set[str] = {start_norm}
    queue: deque[tuple[str, int]] = deque([(start_norm, 0)])

    while queue and result.fetched_count < max_pages:
        url, depth = queue.popleft()

        if delay_s > 0 and result.fetched_count > 0:
            time.sleep(delay_s)

        try:
            text, links = fetch(url)
        except WebFetchError as exc:
            reason = str(exc)
            result.skipped.append((url, reason))
            if on_skip is not None:
                on_skip(url, reason)
            continue
        except Exception as exc:  # pragma: no cover - defensive belt
            # Any unexpected error on one page must not kill the crawl.
            reason = f"unexpected error: {exc}"
            result.skipped.append((url, reason))
            if on_skip is not None:
                on_skip(url, reason)
            continue

        result.pages.append(CrawlPage(url=url, text=text))
        if on_page is not None:
            on_page(url, result.fetched_count, max_pages)

        # Enqueue same-site children if we have depth budget AND haven't
        # already hit the page cap (no point discovering links we can't
        # fetch). Children inherit depth+1.
        if depth < max_depth and result.fetched_count < max_pages:
            for link in links:
                norm = _normalize_url(link)
                if norm in visited:
                    continue
                if not _same_site(start_url, norm):
                    continue
                visited.add(norm)
                queue.append((norm, depth + 1))

    return result
