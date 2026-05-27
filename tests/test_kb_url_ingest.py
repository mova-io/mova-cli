"""F5 (#114) — single-page URL ingest for ``mdk kb ingest``.

``mdk kb ingest <agent> <url>`` (where ``<url>`` starts with
``http://`` / ``https://``) fetches the page, strips it to readable
text with the stdlib (zero new deps), and routes that text through the
SAME chunk → embed → store pipeline a local file uses — so the chunks
land in the KB with ``source == the URL`` and ``mdk kb stats`` /
retrieval see them identically.

These tests are hermetic: no real network. ``httpx.get`` is
monkeypatched to return a fixture HTML body, and the embedding call is
stubbed. The end-to-end CLI tests run against the parametrized
``storage`` fixture's sqlite backend via ``MOVATE_DB`` (no ~/.movate
writes).

Coverage:

* URL detection: ``https://…`` routes to the fetcher; a local path
  still reads files.
* Ingest a URL with a fixture HTML body → extracted text is chunked
  and the chunks land in the store with ``source == the URL``.
* ``<script>`` / ``<style>`` / ``<nav>`` / ``<footer>`` content is
  stripped from the extracted text.
* Fetch error (non-2xx / httpx error) → clean typed error, exit 2, no
  partial write.
* Empty / near-empty page → clear "nothing ingestible" error.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.kb.web import (
    WebFetchError,
    extract_text,
    fetch_and_extract,
    fetch_url,
    is_url,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PROSE = (
    "This is the first substantial paragraph of the article body, long "
    "enough to clear the minimum chunk size threshold easily."
)
_PROSE2 = (
    "Here is a second meaty paragraph that also comfortably exceeds the "
    "minimum chunk length so it becomes its own retrievable chunk."
)

_FIXTURE_HTML = f"""\
<!DOCTYPE html>
<html>
<head>
  <title>Page Title</title>
  <style>body {{ color: red; }} .secret-style-text {{ display: none }}</style>
  <script>var leakyScriptVariable = "should-not-be-ingested";</script>
</head>
<body>
  <nav>Home About NavigationMenuJunk Contact</nav>
  <article>
    <h1>The Article Heading</h1>
    <p>{_PROSE}</p>
    <p>{_PROSE2}</p>
  </article>
  <footer>Copyright FooterBoilerplate 2026 All rights reserved.</footer>
</body>
</html>
"""


def _fake_response(text: str, status_code: int = 200) -> httpx.Response:
    """Build an httpx.Response detached from any real transport."""
    return httpx.Response(
        status_code=status_code,
        text=text,
        request=httpx.Request("GET", "https://example.com/page"),
    )


async def _fake_embed(
    texts: list[str], *, model: str = "", api_key: str | None = None, timeout_s: float = 60.0
) -> list[list[float]]:
    """Deterministic stub — no embedding-provider traffic."""
    return [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts]


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point storage at a tmp sqlite file + provide an embedding key.

    ``MOVATE_DB`` overrides the default ``~/.movate/local.db`` so the CLI
    path never writes to the developer's home dir.
    """
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "kb.db"))
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")


# ---------------------------------------------------------------------------
# URL detection (movate.kb.web.is_url)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://example.com/page",
        "HTTPS://EXAMPLE.COM",  # scheme is case-insensitive
        "http://localhost:8080/docs",
    ],
)
def test_is_url_detects_web_urls(value: str) -> None:
    assert is_url(value) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "kb/",
        "agents/rag-qa/kb",
        "./docs/file.md",
        "/abs/path/to/file.txt",
        "ftp://example.com/file",  # not http(s)
        "file.html",
        "C:/Users/me/docs",
    ],
)
def test_is_url_treats_paths_as_paths(value: str) -> None:
    assert is_url(value) is False


# ---------------------------------------------------------------------------
# Stdlib text extraction (movate.kb.web.extract_text)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_text_keeps_prose() -> None:
    text = extract_text(_FIXTURE_HTML)
    assert _PROSE in text
    assert _PROSE2 in text
    assert "The Article Heading" in text


@pytest.mark.unit
def test_extract_text_strips_script_style_nav_footer() -> None:
    """Script / style / nav / footer content must NOT appear in output."""
    text = extract_text(_FIXTURE_HTML)
    assert "leakyScriptVariable" not in text
    assert "should-not-be-ingested" not in text
    assert "color: red" not in text
    assert "secret-style-text" not in text
    assert "NavigationMenuJunk" not in text
    assert "FooterBoilerplate" not in text


@pytest.mark.unit
def test_extract_text_collapses_whitespace_and_keeps_paragraph_breaks() -> None:
    html = "<p>one    two\n\tthree</p><p>second paragraph here</p>"
    text = extract_text(html)
    # Internal whitespace collapsed to single spaces…
    assert "one two three" in text
    # …and paragraphs separated by a blank line for the chunker.
    assert "\n\n" in text


@pytest.mark.unit
def test_extract_text_empty_html_is_empty() -> None:
    assert extract_text("<html><head></head><body></body></html>") == ""
    assert extract_text("") == ""


# ---------------------------------------------------------------------------
# Fetch (movate.kb.web.fetch_url) — typed errors, no traceback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_url_returns_body_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response("<p>hi there</p>"))
    assert fetch_url("https://example.com") == "<p>hi there</p>"


@pytest.mark.unit
def test_fetch_url_non_2xx_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response("nope", status_code=404))
    with pytest.raises(WebFetchError) as exc:
        fetch_url("https://example.com/missing")
    msg = str(exc.value)
    assert "https://example.com/missing" in msg
    assert "404" in msg


@pytest.mark.unit
def test_fetch_url_transport_error_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(WebFetchError) as exc:
        fetch_url("https://unreachable.example")
    assert "https://unreachable.example" in str(exc.value)


@pytest.mark.unit
def test_fetch_and_extract_empty_page_raises_nothing_ingestible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response("<html><body></body></html>"))
    with pytest.raises(WebFetchError) as exc:
        fetch_and_extract("https://example.com/empty")
    assert "nothing ingestible" in str(exc.value)
    assert "https://example.com/empty" in str(exc.value)


# ---------------------------------------------------------------------------
# End-to-end CLI ingest — chunks land in the store with source == the URL
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_ingest_url_stores_chunks_with_url_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, monkeypatch)
    url = "https://example.com/article"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(_FIXTURE_HTML))

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(app, ["kb", "ingest", "web-agent", url])

    assert result.exit_code == 0, result.stderr

    # Verify the chunks actually landed in the store with source == URL.
    async def _check() -> None:
        from movate.storage import build_storage  # noqa: PLC0415

        storage = build_storage()
        await storage.init()
        try:
            chunks = await storage.list_kb_chunks(agent="web-agent", tenant_id="local")
            assert chunks, "expected chunks ingested from the URL"
            assert all(c.source == url for c in chunks), (
                f"every chunk's source should be the URL; got {sorted({c.source for c in chunks})}"
            )
            # The fixture prose made it into a chunk…
            joined = "\n".join(c.text for c in chunks)
            assert "first substantial paragraph" in joined
            # …and the stripped junk did NOT.
            assert "leakyScriptVariable" not in joined
            assert "FooterBoilerplate" not in joined
            assert "NavigationMenuJunk" not in joined
        finally:
            await storage.close()

    import asyncio  # noqa: PLC0415

    asyncio.run(_check())


@pytest.mark.unit
def test_cli_ingest_local_path_still_reads_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-URL argument must keep the unchanged local-file behavior:
    it reads from disk and never hits the fetcher."""
    _cli_env(tmp_path, monkeypatch)
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "doc.md").write_text(
        "A local document paragraph with plenty of words to be a chunk.\n\n"
        "A second local paragraph that also clears the minimum length.\n",
        encoding="utf-8",
    )

    # If the local path wrongly routed to the fetcher, this would raise.
    def _should_not_be_called(*a: object, **k: object) -> httpx.Response:
        raise AssertionError("local path must not call httpx.get")

    monkeypatch.setattr(httpx, "get", _should_not_be_called)

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(app, ["kb", "ingest", "web-agent", str(kb)])

    assert result.exit_code == 0, result.stderr

    async def _check() -> None:
        from movate.storage import build_storage  # noqa: PLC0415

        storage = build_storage()
        await storage.init()
        try:
            chunks = await storage.list_kb_chunks(agent="web-agent", tenant_id="local")
            assert chunks, "expected chunks from the local file"
            # Source is the resolved filesystem path, not a URL.
            assert all(c.source.endswith("doc.md") for c in chunks)
        finally:
            await storage.close()

    import asyncio  # noqa: PLC0415

    asyncio.run(_check())


@pytest.mark.unit
def test_cli_ingest_url_fetch_error_is_clean_and_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-2xx fetch → exit 2, a one-line error naming the URL, and NO
    chunks written (the fetch fails before any storage call)."""
    _cli_env(tmp_path, monkeypatch)
    url = "https://example.com/missing"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response("not found", status_code=404))

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(app, ["kb", "ingest", "web-agent", url])

    assert result.exit_code == 2
    assert url in result.stderr
    assert "404" in result.stderr
    # No traceback leaked to the operator.
    assert "Traceback" not in result.stderr

    async def _check() -> None:
        from movate.storage import build_storage  # noqa: PLC0415

        storage = build_storage()
        await storage.init()
        try:
            chunks = await storage.list_kb_chunks(agent="web-agent", tenant_id="local")
            assert chunks == [], "a failed fetch must not write any chunks"
        finally:
            await storage.close()

    import asyncio  # noqa: PLC0415

    asyncio.run(_check())


@pytest.mark.unit
def test_cli_ingest_url_empty_page_reports_nothing_ingestible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, monkeypatch)
    url = "https://example.com/empty"
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _fake_response("<html><body><nav>menu</nav></body></html>")
    )

    with mock.patch("movate.kb.ingest.embed_texts", side_effect=_fake_embed):
        result = runner.invoke(app, ["kb", "ingest", "web-agent", url])

    assert result.exit_code == 2
    assert "nothing ingestible" in result.stderr
    assert url in result.stderr


@pytest.mark.unit
def test_cli_ingest_url_rejects_target_combo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--target + URL is rejected (the remote endpoint takes file uploads
    only); URL ingest is a local-only path."""
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _fake_response(_FIXTURE_HTML))
    result = runner.invoke(
        app, ["kb", "ingest", "web-agent", "https://example.com", "--target", "dev"]
    )
    assert result.exit_code == 2
    assert "--target" in result.stderr
