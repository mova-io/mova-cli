"""F6 (#115) — bounded multi-page same-site crawl for ``mdk kb ingest``.

``mdk kb ingest <agent> <start-url> --crawl`` fetches the start page AND
follows ``<a href>`` links to ingest a BOUNDED set of pages from the SAME
site, all through the existing chunk → embed → ``save_kb_chunk`` pipeline
(each chunk's ``source`` = its own page URL).

These tests are hermetic — NO real network. The crawler exposes a
``_fetch`` test seam (URL → ``(extracted_text, absolute_links)``); the
end-to-end CLI tests monkeypatch ``httpx.get`` to serve a small in-memory
link graph and stub the embedding call. The CLI runs against the sqlite
backend via ``MOVATE_DB`` (no ~/.movate writes).

Link graph used by most tests::

    start ─┬─> /a ─┬─> /c
           │       └─> https://external.example/x   (off-site, NOT followed)
           ├─> /b
           └─> /a  (duplicate link — fetched once)

Coverage:

* BFS reaches same-site pages within depth/page limits.
* The external link is NOT followed (same-registrable-domain guard).
* ``--max-pages`` / ``--max-depth`` hard caps are enforced.
* Visited-URL dedup: a page linked twice is fetched exactly once.
* A failing page is skipped (warned) and the crawl ingests the rest.
* Each ingested chunk's ``source`` == its own page URL.
* ``--crawl`` with a filesystem path → clean error (exit 2).
* Non-crawl path (no ``--crawl``) still single-page (F5 regression).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.kb.web import (
    WebFetchError,
    crawl_site,
    extract_links,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# In-memory site fixtures
# ---------------------------------------------------------------------------

_PROSE = (
    "This is a substantial paragraph of page body text, comfortably long "
    "enough to clear the minimum chunk size threshold for {label}."
)


def _page_html(label: str, links: list[str]) -> str:
    """Build a small HTML page with a prose block + the given anchor links."""
    anchors = "\n".join(f'<a href="{href}">{href}</a>' for href in links)
    return (
        "<!DOCTYPE html><html><head><title>"
        f"{label}</title></head><body><article>"
        f"<h1>{label}</h1><p>{_PROSE.format(label=label)}</p>"
        f"<nav>{anchors}</nav>"
        "</article></body></html>"
    )


# A small same-site link graph rooted at https://site.example/.
_SITE: dict[str, str] = {
    "https://site.example/": _page_html(
        "start",
        [
            "/a",
            "/b",
            "/a",  # duplicate link → must be fetched once
            "https://external.example/x",  # off-site → never followed
            "mailto:hi@site.example",  # non-page scheme
            "/files/report.pdf",  # non-HTML extension
        ],
    ),
    "https://site.example/a": _page_html(
        "page-a",
        ["/c", "https://external.example/y", "/"],  # links back to start (dedup)
    ),
    "https://site.example/b": _page_html("page-b", []),
    "https://site.example/c": _page_html("page-c", []),
    "https://external.example/x": _page_html("external-x", []),
}


def _fake_response(
    url: str, status_code: int = 200, content_type: str = "text/html"
) -> httpx.Response:
    body = _SITE.get(url, "")
    return httpx.Response(
        status_code=status_code,
        text=body,
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


def _make_httpx_get(
    *, error_urls: set[str] | None = None, status_overrides: dict[str, int] | None = None
):
    """Return a ``httpx.get`` replacement that serves ``_SITE``.

    ``error_urls`` raise a transport error (simulating timeout/connection
    failure); ``status_overrides`` return a specific non-2xx status.
    """
    error_urls = error_urls or set()
    status_overrides = status_overrides or {}

    def _get(url: str, *a: object, **k: object) -> httpx.Response:
        if url in error_urls:
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))
        if url in status_overrides:
            return _fake_response(url, status_code=status_overrides[url])
        return _fake_response(url)

    return _get


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
) -> list[list[float]]:
    """Deterministic embedding stub — no provider traffic."""
    return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "kb.db"))
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")


# ---------------------------------------------------------------------------
# extract_links — unit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_links_resolves_dedups_and_skips_non_pages() -> None:
    html = _SITE["https://site.example/"]
    links = extract_links(html, base_url="https://site.example/")
    # Relative links resolved to absolute, fragment-free.
    assert "https://site.example/a" in links
    assert "https://site.example/b" in links
    # External link is still returned here (same-domain filtering is the
    # crawler's job, not extract_links').
    assert "https://external.example/x" in links
    # mailto: + the .pdf are dropped (non-page scheme / non-HTML ext).
    assert not any(link.startswith("mailto:") for link in links)
    assert not any(link.endswith(".pdf") for link in links)
    # Dedup: /a appears twice in the HTML, once in the result.
    assert links.count("https://site.example/a") == 1


@pytest.mark.unit
def test_extract_links_strips_fragments() -> None:
    html = '<a href="/page#section">x</a><a href="/page">y</a>'
    links = extract_links(html, base_url="https://site.example/")
    assert links == ["https://site.example/page"]


# ---------------------------------------------------------------------------
# crawl_site — unit (using the _fetch test seam, no httpx involved)
# ---------------------------------------------------------------------------


def _seam_fetch(url: str) -> tuple[str, list[str]]:
    """Test-seam fetch: map a URL in _SITE to (text, absolute_links)."""
    if url not in _SITE:
        raise WebFetchError(f"could not fetch {url}: HTTP 404")
    html = _SITE[url]
    from movate.kb.web import extract_text  # noqa: PLC0415

    return extract_text(html), extract_links(html, base_url=url)


@pytest.mark.unit
def test_crawl_bfs_reaches_same_site_pages_within_limits() -> None:
    result = crawl_site("https://site.example/", max_pages=25, max_depth=2, _fetch=_seam_fetch)
    urls = {p.url for p in result.pages}
    # start (depth 0) + /a /b (depth 1) + /c (depth 2, via /a)
    assert urls == {
        "https://site.example/",
        "https://site.example/a",
        "https://site.example/b",
        "https://site.example/c",
    }


@pytest.mark.unit
def test_crawl_never_follows_external_domain() -> None:
    result = crawl_site("https://site.example/", max_pages=25, max_depth=5, _fetch=_seam_fetch)
    urls = {p.url for p in result.pages}
    assert not any("external.example" in u for u in urls)


@pytest.mark.unit
def test_crawl_max_pages_is_a_hard_cap() -> None:
    result = crawl_site("https://site.example/", max_pages=2, max_depth=5, _fetch=_seam_fetch)
    assert result.fetched_count == 2


@pytest.mark.unit
def test_crawl_max_depth_limits_reach() -> None:
    # depth 0 only → just the start page.
    result0 = crawl_site("https://site.example/", max_pages=25, max_depth=0, _fetch=_seam_fetch)
    assert {p.url for p in result0.pages} == {"https://site.example/"}

    # depth 1 → start + its direct same-site links (/a, /b) but NOT /c
    # (which is depth 2, reached only via /a).
    result1 = crawl_site("https://site.example/", max_pages=25, max_depth=1, _fetch=_seam_fetch)
    assert {p.url for p in result1.pages} == {
        "https://site.example/",
        "https://site.example/a",
        "https://site.example/b",
    }


@pytest.mark.unit
def test_crawl_dedups_visited_urls() -> None:
    """/a links back to /; / links to /a twice. Each fetched once."""
    fetched: list[str] = []

    def _counting_fetch(url: str) -> tuple[str, list[str]]:
        fetched.append(url)
        return _seam_fetch(url)

    crawl_site("https://site.example/", max_pages=25, max_depth=3, _fetch=_counting_fetch)
    # No URL fetched more than once despite duplicate + back-links.
    assert len(fetched) == len(set(fetched))
    assert fetched.count("https://site.example/a") == 1
    assert fetched.count("https://site.example/") == 1


@pytest.mark.unit
def test_crawl_failing_page_is_skipped_and_crawl_continues() -> None:
    """A page that errors is recorded in skipped; the rest still ingest."""

    def _flaky_fetch(url: str) -> tuple[str, list[str]]:
        if url == "https://site.example/a":
            raise WebFetchError(f"could not fetch {url}: HTTP 500")
        return _seam_fetch(url)

    result = crawl_site("https://site.example/", max_pages=25, max_depth=2, _fetch=_flaky_fetch)
    fetched_urls = {p.url for p in result.pages}
    # /a failed → skipped; /b still ingested. /c was only reachable via /a,
    # so it's correctly absent (no orphan fetch).
    assert "https://site.example/a" not in fetched_urls
    assert "https://site.example/b" in fetched_urls
    assert "https://site.example/" in fetched_urls
    assert any(url == "https://site.example/a" for url, _reason in result.skipped)
    assert result.skipped_count >= 1


@pytest.mark.unit
def test_crawl_zero_caps_return_empty() -> None:
    assert crawl_site("https://site.example/", max_pages=0, _fetch=_seam_fetch).pages == []


# ---------------------------------------------------------------------------
# crawl_site — Content-Type filtering through the real fetch path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crawl_skips_non_html_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx page advertising a non-HTML Content-Type is skipped."""

    def _get(url: str, *a: object, **k: object) -> httpx.Response:
        if url == "https://site.example/a":
            return _fake_response(url, content_type="application/pdf")
        return _fake_response(url)

    monkeypatch.setattr(httpx, "get", _get)
    result = crawl_site("https://site.example/", max_pages=25, max_depth=2)
    urls = {p.url for p in result.pages}
    assert "https://site.example/a" not in urls
    assert any(u == "https://site.example/a" for u, _r in result.skipped)


# ---------------------------------------------------------------------------
# End-to-end CLI — chunks land with source == each page's own URL
# ---------------------------------------------------------------------------


def _list_chunks(agent: str = "crawl-agent") -> list[object]:
    async def _go() -> list[object]:
        from movate.storage import build_storage  # noqa: PLC0415

        storage = build_storage()
        await storage.init()
        try:
            return await storage.list_kb_chunks(agent=agent, tenant_id="local")
        finally:
            await storage.close()

    return asyncio.run(_go())


@pytest.mark.unit
def test_cli_crawl_ingests_same_site_pages_with_own_url_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setattr(httpx, "get", _make_httpx_get())

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(
            app,
            ["kb", "ingest", "crawl-agent", "https://site.example/", "--crawl"],
        )

    assert result.exit_code == 0, result.stderr

    chunks = _list_chunks()
    assert chunks, "expected chunks ingested from the crawl"
    sources = {c.source for c in chunks}
    # Each chunk's source is its OWN page URL — all four same-site pages.
    assert sources == {
        "https://site.example/",
        "https://site.example/a",
        "https://site.example/b",
        "https://site.example/c",
    }
    # External page never crawled → no chunk from it.
    assert not any("external.example" in s for s in sources)


@pytest.mark.unit
def test_cli_crawl_max_pages_caps_ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setattr(httpx, "get", _make_httpx_get())

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(
            app,
            [
                "kb",
                "ingest",
                "crawl-agent",
                "https://site.example/",
                "--crawl",
                "--max-pages",
                "2",
            ],
        )

    assert result.exit_code == 0, result.stderr
    chunks = _list_chunks()
    # Exactly 2 distinct page sources ingested.
    assert len({c.source for c in chunks}) == 2


@pytest.mark.unit
def test_cli_crawl_failing_page_skipped_rest_ingested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, monkeypatch)
    # /a 404s; the crawl must skip it (warn on stderr) and ingest the rest.
    monkeypatch.setattr(
        httpx,
        "get",
        _make_httpx_get(status_overrides={"https://site.example/a": 404}),
    )

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(
            app,
            ["kb", "ingest", "crawl-agent", "https://site.example/", "--crawl"],
        )

    assert result.exit_code == 0, result.stderr
    # Warning surfaced to stderr, not stdout.
    assert "skipped" in result.stderr
    assert "site.example/a" in result.stderr

    sources = {c.source for c in _list_chunks()}
    assert "https://site.example/a" not in sources
    assert "https://site.example/b" in sources
    assert "https://site.example/" in sources


@pytest.mark.unit
def test_cli_crawl_with_filesystem_path_is_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, monkeypatch)
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "doc.md").write_text("Some local content here.\n", encoding="utf-8")

    def _should_not_be_called(*a: object, **k: object) -> httpx.Response:
        raise AssertionError("--crawl on a path must not hit the network")

    monkeypatch.setattr(httpx, "get", _should_not_be_called)

    result = runner.invoke(app, ["kb", "ingest", "crawl-agent", str(kb), "--crawl"])
    assert result.exit_code == 2
    assert "--crawl" in result.stderr
    assert "Traceback" not in result.stderr
    # Nothing written.
    assert _list_chunks() == []


@pytest.mark.unit
def test_cli_without_crawl_is_single_page_f5_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --crawl: the URL path stays single-page (F5). Only the start
    URL is fetched even though it links to other same-site pages."""
    _cli_env(tmp_path, monkeypatch)
    fetched: list[str] = []

    def _tracking_get(url: str, *a: object, **k: object) -> httpx.Response:
        fetched.append(url)
        return _fake_response(url)

    monkeypatch.setattr(httpx, "get", _tracking_get)

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(app, ["kb", "ingest", "crawl-agent", "https://site.example/"])

    assert result.exit_code == 0, result.stderr
    # Single fetch only — no link following.
    assert fetched == ["https://site.example/"]
    sources = {c.source for c in _list_chunks()}
    assert sources == {"https://site.example/"}
