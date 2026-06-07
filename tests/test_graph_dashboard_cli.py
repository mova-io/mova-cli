"""``mdk graph dashboard`` — hosted knowledge graph explorer CLI + viewer.

Covers:
* the command exists + parses its flags (``--target``/``--project``/``--port``/
  ``--host``/``--no-open``);
* the dashboard HTML loads with all new panels (DOM element checks);
* the bearer key is never in the served dashboard HTML/JS;
* the dashboard inherits the proxy (bearer stays server-side);
* vendored assets are reused from the base viewer.
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
from typer.testing import CliRunner

from movate.cli import graph as graph_cmd
from movate.cli.graph import DASHBOARD_DIR, _build_dashboard_handler, _render_dashboard_index
from movate.cli.main import app

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner(mix_stderr=False)

_BEARER = "mvt_live_SUPER_SECRET_should_never_reach_browser"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_client_factory(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.Client", factory)


@pytest.fixture
def dashboard_served(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, dict[str, object]]]:
    """Start the dashboard handler on an ephemeral port with a mocked runtime."""
    captured: dict[str, object] = {}

    def runtime_handler(request: httpx.Request) -> httpx.Response:
        captured["last_path"] = request.url.path
        captured["last_auth"] = request.headers.get("authorization")
        node = {"key": "n1", "attributes": {"label": "Acme", "type": "org"}}
        if request.url.path.endswith("/graph"):
            return httpx.Response(HTTPStatus.OK, json={"nodes": [node], "edges": []})
        if "/graph/nodes/" in request.url.path:
            return httpx.Response(
                HTTPStatus.OK, json={"label": "Acme", "type": "org", "properties": {}}
            )
        if request.url.path.endswith("/projects"):
            return httpx.Response(HTTPStatus.OK, json=[{"id": "acme-kb"}, {"id": "demo-kb"}])
        return httpx.Response(HTTPStatus.NOT_FOUND, json={"detail": "not found"})

    _make_client_factory(httpx.MockTransport(runtime_handler), monkeypatch)

    handler = _build_dashboard_handler(
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
def test_dashboard_command_is_registered_and_help_lists_flags() -> None:
    result = runner.invoke(app, ["graph", "dashboard", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", result.stdout)
    plain = " ".join(plain.split())
    assert "--target" in plain
    assert "--project" in plain
    assert "--port" in plain
    assert "--host" in plain
    assert "--no-open" in plain


@pytest.mark.unit
def test_dashboard_no_open_and_server_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--no-open`` must not launch a browser; the server must bind + start."""
    opened: list[str] = []

    def _record_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(graph_cmd.webbrowser, "open", _record_open)
    monkeypatch.setattr(
        "movate.cli.kb_cmd._resolve_target_bearer",
        lambda _t: ("prod", object(), "https://runtime.example.com", _BEARER),
    )
    monkeypatch.setattr(graph_cmd, "_probe_graph_api", lambda *_a, **_k: (True, "ok"))

    started: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, addr: tuple[str, int], handler: object) -> None:
            started["addr"] = addr
            started["handler"] = handler

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            started["closed"] = True

    monkeypatch.setattr(graph_cmd, "ThreadingHTTPServer", _FakeServer)

    argv = ["graph", "dashboard", "--target", "prod", "--project", "acme-kb"]
    argv += ["--port", "8921", "--no-open"]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    assert started["addr"] == ("127.0.0.1", 8921)
    assert opened == []  # --no-open honored
    assert started.get("closed") is True


@pytest.mark.unit
def test_dashboard_host_flag_allows_all_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--host 0.0.0.0`` binds on all interfaces for hosted/shared access."""
    monkeypatch.setattr(graph_cmd.webbrowser, "open", lambda _u: True)
    monkeypatch.setattr(
        "movate.cli.kb_cmd._resolve_target_bearer",
        lambda _t: ("prod", object(), "https://runtime.example.com", _BEARER),
    )
    monkeypatch.setattr(graph_cmd, "_probe_graph_api", lambda *_a, **_k: (True, "ok"))

    started: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, addr: tuple[str, int], handler: object) -> None:
            started["addr"] = addr

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(graph_cmd, "ThreadingHTTPServer", _FakeServer)

    argv = [
        "graph",
        "dashboard",
        "--target",
        "prod",
        "--project",
        "acme-kb",
        "--host",
        "0.0.0.0",
        "--port",
        "8901",
        "--no-open",
    ]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    assert started["addr"] == ("0.0.0.0", 8901)


# --------------------------------------------------------------------------- #
# Dashboard HTML: new panels present
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_dashboard_html_has_all_new_panels() -> None:
    """The enhanced dashboard index.html must contain all six new panel elements."""
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")

    # (a) Entity search bar
    assert 'id="search"' in html
    assert 'id="suggestions"' in html
    assert 'id="search-indicator"' in html

    # (b) Analytics sidebar with centrality leaderboard, shortest-path, community browser
    assert 'id="analytics-sidebar"' in html
    assert 'id="leaderboard-list"' in html
    assert 'id="sidebar-path-from"' in html
    assert 'id="sidebar-path-to"' in html
    assert 'id="sidebar-path-btn"' in html
    assert 'id="community-list"' in html

    # (c) Growth timeline sparkline
    assert 'id="growth-timeline"' in html
    assert 'id="sparkline-canvas"' in html

    # (d) Project switcher
    assert 'id="project-switcher"' in html
    assert 'id="project-select"' in html

    # (e) KB provenance view (in the detail panel)
    assert 'id="detail-provenance"' in html
    assert "KB Provenance" in html

    # (f) Visual polish: watermark
    assert 'id="watermark"' in html


@pytest.mark.unit
def test_dashboard_js_wires_new_features() -> None:
    """The dashboard JS must reference the new API endpoints and features."""
    js = (DASHBOARD_DIR / "dashboard.js").read_text(encoding="utf-8")

    # Entity search via runtime API
    assert "/graph/search" in js

    # Analytics sidebar
    assert "leaderboard" in js.lower()
    assert "community-list" in js

    # Growth timeline sparkline
    assert "sparkline" in js.lower()
    assert "drawSparkline" in js

    # Project switcher
    assert "switchProject" in js
    assert "/api/v1/projects" in js

    # KB provenance (snippet rendering)
    assert "provenance" in js.lower()


@pytest.mark.unit
def test_dashboard_css_has_movate_palette() -> None:
    """The dashboard CSS must reference the Movate brand palette colors."""
    css = (DASHBOARD_DIR / "dashboard.css").read_text(encoding="utf-8")
    assert "#2D6CDF" in css  # brand-primary
    assert "#5BC0EB" in css  # brand-accent
    assert "#2BB673" in css  # brand-success
    assert "#F2A93B" in css  # brand-warning
    assert "#D64550" in css  # brand-danger


# --------------------------------------------------------------------------- #
# Serving + security
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_dashboard_index_served(dashboard_served: tuple[str, dict[str, object]]) -> None:
    base, _captured = dashboard_served
    status, body, ctype = _get(f"{base}/")
    assert status == HTTPStatus.OK
    assert "text/html" in ctype
    text = body.decode()
    assert "/static/vendor/sigma.min.js" in text
    assert "/static/dashboard/dashboard.js" in text
    assert "acme-kb" in text  # injected config


@pytest.mark.unit
def test_dashboard_bearer_never_in_html_or_js(
    dashboard_served: tuple[str, dict[str, object]],
) -> None:
    base, _captured = dashboard_served
    for path in ("/", "/static/dashboard/dashboard.js", "/static/dashboard/dashboard.css"):
        status, body, _ctype = _get(f"{base}{path}")
        assert status == HTTPStatus.OK, f"{path} -> {status}"
        text = body.decode()
        assert _BEARER not in text, f"bearer leaked into {path}"
        assert "Bearer " not in text, f"a bearer-style header appears in {path}"


@pytest.mark.unit
def test_dashboard_render_index_injects_config_but_not_bearer() -> None:
    html = _render_dashboard_index("acme-kb", "prod").decode()
    assert "acme-kb" in html
    assert "prod" in html
    assert _BEARER not in html
    assert "window.MDK_GRAPH_CONFIG =" in html


@pytest.mark.unit
def test_dashboard_proxy_adds_bearer(dashboard_served: tuple[str, dict[str, object]]) -> None:
    base, captured = dashboard_served
    status, body, _ctype = _get(f"{base}/api/v1/projects/acme-kb/graph?mode=knowledge")
    assert status == HTTPStatus.OK
    payload = json.loads(body)
    assert payload["nodes"][0]["key"] == "n1"
    assert captured["last_auth"] == f"Bearer {_BEARER}"


@pytest.mark.unit
def test_dashboard_serves_vendored_assets(
    dashboard_served: tuple[str, dict[str, object]],
) -> None:
    """The dashboard reuses the base viewer's vendored sigma/graphology JS."""
    base, _captured = dashboard_served
    status, body, _ctype = _get(f"{base}/static/vendor/sigma.min.js")
    assert status == HTTPStatus.OK
    assert len(body) > 1000  # vendored file exists and is non-trivial


@pytest.mark.unit
def test_dashboard_is_read_only(dashboard_served: tuple[str, dict[str, object]]) -> None:
    base, _captured = dashboard_served
    for method in ("POST", "PUT", "DELETE"):
        status, _body, _ctype = _get(f"{base}/api/v1/graph/nodes/n1", method=method)
        assert status == HTTPStatus.METHOD_NOT_ALLOWED


# --------------------------------------------------------------------------- #
# Dashboard assets exist
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_dashboard_assets_present() -> None:
    assert DASHBOARD_DIR.is_dir(), "dashboard directory missing"
    for name in ("index.html", "dashboard.js", "dashboard.css"):
        path = DASHBOARD_DIR / name
        assert path.is_file(), f"missing dashboard asset: {name}"
        assert path.stat().st_size > 100, f"dashboard asset suspiciously small: {name}"


@pytest.mark.unit
def test_no_cdn_references_in_dashboard_assets() -> None:
    """The dashboard must not load anything from a CDN (air-gapped guarantee)."""
    for fname in ("index.html", "dashboard.js"):
        text = (DASHBOARD_DIR / fname).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "unpkg.com" not in lowered
        assert "cdn.jsdelivr" not in lowered
        assert "cdnjs" not in lowered
