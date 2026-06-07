"""``mdk graph serve`` — local sigma.js knowledge-graph viewer + proxy.

Covers the contract that matters for a read-only viewer that keeps the runtime
bearer server-side:

* the command exists + parses its flags (``--target``/``--project``/``--port``/
  ``--no-open``);
* the local server serves the viewer HTML (200, contains the sigma bootstrap);
* the bearer key is **never** present in the served HTML/JS (it stays in the
  local proxy process — the security guarantee);
* ``/api/*`` is proxied to the runtime with an ``Authorization: Bearer`` header
  added **server-side** (asserted via a MockTransport that captures the header);
* graceful degradation: a runtime that 404s the graph path is translated to a
  501 + actionable hint for the UI banner;
* the vendored sigma/graphology/forceatlas2 UMD files are present and carry
  their MIT license headers (air-gapped, no CDN).

The upstream runtime is always mocked — no network.
"""

from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import httpx
import pytest
import typer
from typer.testing import CliRunner

from movate.cli import graph as graph_cmd
from movate.cli.graph import _build_handler, _render_index, _resolve_viewer_runtime
from movate.cli.graph_viewer import ASSETS_DIR
from movate.cli.main import app

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner(mix_stderr=False)

_BEARER = "mvt_live_SUPER_SECRET_should_never_reach_browser"
_VENDORED = (
    "graphology.umd.min.js",
    "graphology-layout-forceatlas2.umd.js",
    "sigma.min.js",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_client_factory(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the lazily-imported ``httpx.Client`` through a MockTransport."""
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.Client", factory)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, dict[str, object]]]:
    """Start the real handler on an ephemeral port with a mocked runtime.

    Yields ``(base_local_url, captured)`` where ``captured`` records what the
    proxy forwarded to the (mocked) runtime — notably the Authorization header.
    """
    captured: dict[str, object] = {}

    def runtime_handler(request: httpx.Request) -> httpx.Response:
        captured["last_path"] = request.url.path
        captured["last_auth"] = request.headers.get("authorization")
        node = {"key": "n1", "attributes": {"label": "Acme", "type": "org"}}
        if request.url.path.endswith("/graph"):
            return httpx.Response(HTTPStatus.OK, json={"nodes": [node], "edges": []})
        if "/graph/nodes/" in request.url.path and not request.url.path.endswith("/neighbors"):
            return httpx.Response(
                HTTPStatus.OK, json={"label": "Acme", "type": "org", "properties": {}}
            )
        # Anything else — including the neighbors path on this "older" runtime —
        # 404s, so the degradation path (404 -> 501) is exercised.
        return httpx.Response(HTTPStatus.NOT_FOUND, json={"detail": "not found"})

    _make_client_factory(httpx.MockTransport(runtime_handler), monkeypatch)

    handler = _build_handler(
        base_url="https://runtime.example.com",
        bearer=_BEARER,
        project="acme-kb",
        target_name="prod",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", captured
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str, method: str = "GET") -> tuple[int, bytes, str]:
    """GET (or other method) the LOCAL server via urllib.

    We deliberately use urllib here, not httpx: the fixture monkeypatches
    ``httpx.Client`` to route through the runtime MockTransport, so any httpx
    call would hit the mock, not the local viewer server.
    """
    req = Request(url, method=method)
    try:
        with urlopen(req) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_command_is_registered_and_help_lists_flags() -> None:
    # CI gotchas (same recipe as tests/test_playground_cmd.py): Rich renders the
    # options panel too narrow in CI's non-TTY terminal (flag names wrap/elide)
    # and styles `--`/flag-name as separate ANSI spans under FORCE_COLOR=1. Force
    # a wide terminal, strip ANSI, then collapse whitespace so each flag flattens
    # to a single searchable substring.
    result = runner.invoke(app, ["graph", "serve", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", result.stdout)
    plain = " ".join(plain.split())
    assert "--target" in plain
    assert "--project" in plain
    assert "--port" in plain
    assert "--no-open" in plain
    # ADR 081 headless-mode flags (hosted container path).
    assert "--runtime-url" in plain
    assert "--api-key" in plain


@pytest.mark.unit
def test_headless_resolver_uses_env_without_reading_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 081: with both --runtime-url + --api-key, never touch ~/.movate config.

    Proves the container path resolves the runtime purely from the supplied
    values (and strips a trailing slash), without invoking the local
    ``_resolve_target_bearer`` config pipeline.
    """

    def _boom(_target: object) -> object:  # pragma: no cover - must NOT run
        raise AssertionError("config resolver must not be called in headless mode")

    monkeypatch.setattr("movate.cli.kb_cmd._resolve_target_bearer", _boom)

    name, base_url, bearer = _resolve_viewer_runtime(
        target=None,
        runtime_url="https://api.example.com/",
        api_key="mvt_live_read_scoped",
    )
    assert name == "(env)"
    assert base_url == "https://api.example.com"  # trailing slash stripped
    assert bearer == "mvt_live_read_scoped"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("runtime_url", "api_key"),
    [("https://api.example.com", None), (None, "mvt_live_x")],
)
def test_headless_resolver_requires_both_url_and_key(
    runtime_url: str | None, api_key: str | None
) -> None:
    """Half-configured headless mode fails loud (exit 2), not silently."""
    with pytest.raises(typer.Exit) as exc:
        _resolve_viewer_runtime(target=None, runtime_url=runtime_url, api_key=api_key)
    assert exc.value.exit_code == 2


@pytest.mark.unit
def test_resolver_falls_back_to_target_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No headless env → the unchanged ``--target`` → URL+bearer path is used."""
    calls: list[object] = []

    def _fake(target: object) -> tuple[str, object, str, str]:
        calls.append(target)
        return ("dev", object(), "https://dev.example.com", "mvt_dev")

    monkeypatch.setattr("movate.cli.kb_cmd._resolve_target_bearer", _fake)

    name, base_url, bearer = _resolve_viewer_runtime(target="dev", runtime_url=None, api_key=None)
    assert (name, base_url, bearer) == ("dev", "https://dev.example.com", "mvt_dev")
    assert calls == ["dev"]


@pytest.mark.unit
def test_no_open_respected_and_server_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--no-open`` must not launch a browser; the server must bind + start."""
    opened: list[str] = []

    def _record_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(graph_cmd.webbrowser, "open", _record_open)

    # Resolve target → bearer without touching the real config / network.
    monkeypatch.setattr(
        "movate.cli.kb_cmd._resolve_target_bearer",
        lambda _t: ("prod", object(), "https://runtime.example.com", _BEARER),
    )
    # Skip the capability probe (would hit httpx).
    monkeypatch.setattr(graph_cmd, "_probe_graph_api", lambda *_a, **_k: (True, "ok"))

    started: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, addr: tuple[str, int], handler: object) -> None:
            started["addr"] = addr
            started["handler"] = handler

        def serve_forever(self) -> None:
            raise KeyboardInterrupt  # exit serve immediately

        def server_close(self) -> None:
            started["closed"] = True

    monkeypatch.setattr(graph_cmd, "ThreadingHTTPServer", _FakeServer)

    argv = ["graph", "serve", "--target", "prod", "--project", "acme-kb"]
    argv += ["--port", "8911", "--no-open"]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    assert started["addr"] == ("127.0.0.1", 8911)
    assert opened == []  # --no-open honored
    assert started.get("closed") is True


# --------------------------------------------------------------------------- #
# serving + security
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_index_served_with_sigma_bootstrap(served: tuple[str, dict[str, object]]) -> None:
    base, _captured = served
    status, body, ctype = _get(f"{base}/")
    assert status == HTTPStatus.OK
    assert "text/html" in ctype
    text = body.decode()
    # The sigma bootstrap: vendored script tags + the app entry point.
    assert "/static/vendor/sigma.min.js" in text
    assert "/static/vendor/graphology.umd.min.js" in text
    assert "/static/app.js" in text
    # Non-secret config is injected.
    assert "acme-kb" in text


@pytest.mark.unit
def test_bearer_never_in_served_html_or_js(served: tuple[str, dict[str, object]]) -> None:
    """The bearer must not appear in any browser-delivered asset."""
    base, _captured = served
    for path in ("/", "/static/app.js", "/static/index.html", "/static/style.css"):
        status, body, _ctype = _get(f"{base}{path}")
        assert status == HTTPStatus.OK, f"{path} -> {status}"
        text = body.decode()
        assert _BEARER not in text, f"bearer leaked into {path}"
        # The viewer code must not even construct an Authorization header — the
        # proxy does that server-side. (It DOES reference fetch() to /api/*.)
        assert "Bearer " not in text, f"a bearer-style header appears in {path}"


@pytest.mark.unit
def test_render_index_injects_config_but_not_bearer() -> None:
    html = _render_index("acme-kb", "prod").decode()
    assert "acme-kb" in html
    assert "prod" in html
    assert _BEARER not in html
    # The injected config object is valid-looking JS, not the raw placeholder.
    assert "window.MDK_GRAPH_CONFIG =" in html
    assert "__MDK_GRAPH_CONFIG__" not in html


@pytest.mark.unit
def test_proxy_adds_bearer_server_side(served: tuple[str, dict[str, object]]) -> None:
    """The proxy forwards /api/* to the runtime WITH the bearer added here."""
    base, captured = served
    status, body, _ctype = _get(f"{base}/api/v1/projects/acme-kb/graph?mode=knowledge")
    assert status == HTTPStatus.OK
    payload = json.loads(body)
    assert payload["nodes"][0]["key"] == "n1"
    # The runtime saw the bearer — added server-side by the proxy.
    assert captured["last_auth"] == f"Bearer {_BEARER}"
    assert captured["last_path"] == "/api/v1/projects/acme-kb/graph"


@pytest.mark.unit
def test_proxy_refuses_non_proxied_paths(served: tuple[str, dict[str, object]]) -> None:
    base, captured = served
    # /api/ path outside the graph allowlist is rejected by the proxy itself
    # (404) and is NEVER forwarded upstream — so the bearer is not leaked to an
    # arbitrary runtime path.
    captured.clear()
    status, body, _ctype = _get(f"{base}/api/v1/agents/secret")
    assert status == HTTPStatus.NOT_FOUND
    assert json.loads(body)["error"] == "path not proxied"
    assert "last_auth" not in captured  # nothing was forwarded


@pytest.mark.unit
def test_graceful_degradation_404_becomes_501(served: tuple[str, dict[str, object]]) -> None:
    """An older runtime 404ing a graph path → 501 with a capabilities hint."""
    base, _captured = served
    # The neighbors path is proxied (allowlisted) but the mocked older runtime
    # 404s it (catch-all) — the proxy translates that to a 501 the UI can show.
    status, body, _ctype = _get(f"{base}/api/v1/graph/nodes/n1/neighbors")
    assert status == HTTPStatus.NOT_IMPLEMENTED
    payload = json.loads(body)
    assert "graph query API" in payload["error"]
    assert "ADR 046" in payload["hint"] or "capabilities" in payload["hint"]


@pytest.mark.unit
def test_viewer_is_read_only_rejects_mutations(served: tuple[str, dict[str, object]]) -> None:
    base, _captured = served
    for method in ("POST", "PUT", "DELETE"):
        status, body, _ctype = _get(f"{base}/api/v1/graph/nodes/n1", method=method)
        assert status == HTTPStatus.METHOD_NOT_ALLOWED
        assert "read-only" in json.loads(body)["error"]


# --------------------------------------------------------------------------- #
# vendored assets
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_vendored_assets_present() -> None:
    vendor = ASSETS_DIR / "vendor"
    for name in _VENDORED:
        path = vendor / name
        assert path.is_file(), f"missing vendored asset: {name}"
        assert path.stat().st_size > 1000, f"vendored asset suspiciously small: {name}"


@pytest.mark.unit
def test_vendored_assets_carry_mit_license_header() -> None:
    vendor = ASSETS_DIR / "vendor"
    for name in _VENDORED:
        head = (vendor / name).read_text(encoding="utf-8")[:2000]
        assert "MIT License" in head, f"{name} missing MIT license header"
        assert "Permission is hereby granted" in head, f"{name} missing MIT permission grant"


@pytest.mark.unit
def test_vendor_licenses_doc_lists_all_assets() -> None:
    doc = (ASSETS_DIR / "VENDOR_LICENSES.md").read_text(encoding="utf-8")
    assert "MIT" in doc
    for name in _VENDORED:
        assert name in doc, f"{name} not recorded in VENDOR_LICENSES.md"


@pytest.mark.unit
def test_no_cdn_references_in_viewer_assets() -> None:
    """The viewer must not load anything from a CDN (air-gapped guarantee)."""
    for fname in ("index.html", "app.js"):
        text = (ASSETS_DIR / fname).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "unpkg.com" not in lowered
        assert "cdn.jsdelivr" not in lowered
        assert "cdnjs" not in lowered


# --------------------------------------------------------------------------- #
# analytics wiring (ADR 046) — the viewer surfaces centrality / communities /
# shortest-path; assert the controls + the analytics fetch paths are present
# and that the analytics endpoints fall under the proxy allowlist.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_viewer_has_analytics_controls() -> None:
    html = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
    # Toggles + measure select + the shortest-path + top-hubs controls.
    for control_id in (
        "centrality-toggle",
        "centrality-measure",
        "community-toggle",
        "hubs-btn",
        "path-from",
        "path-to",
        "path-btn",
    ):
        assert f'id="{control_id}"' in html, f"missing analytics control: {control_id}"


@pytest.mark.unit
def test_viewer_app_wires_analytics_endpoints() -> None:
    app_js = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
    # The viewer calls the three project-scoped analytics endpoints.
    assert "/graph/analytics/" in app_js
    for sub in ("centrality", "communities", "path"):
        assert sub in app_js, f"app.js does not reference analytics/{sub}"


@pytest.mark.unit
def test_analytics_paths_are_proxied(served: tuple[str, dict[str, object]]) -> None:
    """The analytics endpoints fall under the proxy allowlist (/api/v1/projects/).

    The mocked older runtime 404s them → the proxy degrades to 501 (not a hard
    'path not proxied' 404), proving the path IS forwarded with the bearer.
    """
    base, captured = served
    captured.clear()
    status, _body, _ctype = _get(
        f"{base}/api/v1/projects/acme-kb/graph/analytics/centrality?measure=degree"
    )
    # Forwarded (allowlisted) — the older runtime 404s, translated to a 501.
    assert status == HTTPStatus.NOT_IMPLEMENTED
    assert captured["last_auth"] == f"Bearer {_BEARER}"
    assert captured["last_path"] == "/api/v1/projects/acme-kb/graph/analytics/centrality"
