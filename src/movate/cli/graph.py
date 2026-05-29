"""``mdk graph serve`` — local reference viewer for the knowledge graph.

Starts a tiny local web server (stdlib :mod:`http.server` — no new heavy web
dep) that serves a self-contained sigma.js + graphology viewer and **proxies**
the runtime's graph query API. The viewer is **read-only**.

Security model (the load-bearing part)::

    browser  ──GET /api/v1/...──▶  local mdk graph serve
                                       │  (adds Authorization: Bearer …)
                                       ▼
                                  deployed runtime

The runtime bearer token is resolved by the CLI from the local config
(``~/.movate/config.yaml`` + the target's ``key_env`` env var, or OIDC) and
stays **inside this local process**. The browser never sees it: it is never
templated into the served HTML/JS, never sent to the browser in a header, and
never logged. The local server adds the ``Authorization`` header server-side
when forwarding each ``/api/*`` request to the runtime.

Quickstart::

    export MOVATE_PROD_KEY=mvt_live_...     # or `mdk auth login --target prod`
    mdk graph serve --target prod --project acme-kb

Reuses the same ``--target`` → runtime-URL + bearer resolution pipeline as
``mdk kb`` (:func:`movate.cli.kb_cmd._resolve_target_bearer`), so a target
configured for ``mdk kb`` / ``mdk deploy`` works here with no extra setup.

The viewer talks to the runtime's graph query API (graphology JSON):

* ``GET /api/v1/projects/{id}/graph?mode=knowledge`` — full graph
* ``GET /api/v1/graph/nodes/{id}`` — node detail + provenance + agents
* ``GET /api/v1/graph/nodes/{id}/neighbors`` — drill-deeper expand
* ``GET /api/v1/projects/{id}/graph/stream`` — live-growth SSE

Against an older runtime that doesn't expose these (pre-ADR-046), the viewer
degrades gracefully: the proxy returns a 501 with an actionable hint and the
UI shows a banner pointing at ``mdk capabilities``.
"""

from __future__ import annotations

import contextlib
import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import typer

from movate.cli._console import stderr as err_console

# `serve` is registered onto the SAME `graph` Typer group that owns the
# read-only query commands (`show`/`node`, ADR 046 / #559), so `mdk graph`
# exposes the query API *and* this sigma.js viewer under one group. The group
# itself is defined once in :mod:`movate.cli.graph_cmd`; we only attach a
# command here (no duplicate `graph` group, no double registration).
from movate.cli.graph_cmd import graph_app
from movate.cli.graph_viewer import ASSETS_DIR

# Only these API path prefixes are proxied. The viewer is read-only, so the
# proxy also refuses any non-GET method (defense in depth — the browser code
# never mutates, and the proxy enforces it too).
_PROXY_PREFIXES = ("/api/v1/projects/", "/api/v1/graph/")

# Static assets we serve, mapped to their content types. Everything else under
# /static/ is rejected (no path traversal, no serving arbitrary files).
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# Network timeout (seconds) for proxied calls to the runtime. The SSE stream
# uses no read timeout (it's long-lived); regular calls use this.
_PROXY_TIMEOUT_S = 60.0


def _content_type_for(path: Path) -> str:
    return _STATIC_TYPES.get(path.suffix, "application/octet-stream")


def _render_index(project: str | None, target_name: str) -> bytes:
    """Read index.html and inject the NON-SECRET runtime config.

    Only the project id + target display name are injected — never the
    bearer token, never the runtime URL credentials. The placeholder
    comment in the HTML is replaced with a tiny JS config object.
    """
    html = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
    cfg = {"project": project, "target": target_name}
    inject = "window.MDK_GRAPH_CONFIG = " + json.dumps(cfg) + ";"
    return html.replace("/* __MDK_GRAPH_CONFIG__ */", inject).encode("utf-8")


class _GraphViewerHandler(BaseHTTPRequestHandler):
    """Serves the viewer + proxies /api/* to the runtime with the bearer.

    Instances are created per-request by the server; the runtime base URL,
    bearer, project, and target name are read from class attributes set on a
    dynamically-built subclass (so we avoid globals and keep the bearer off
    the wire to the browser).
    """

    # Set on the subclass built in :func:`_build_handler`.
    base_url: str = ""
    bearer: str = ""
    project: str | None = None
    target_name: str = ""

    # Silence the default stdlib request logging — it would dump request
    # lines to stderr. We never want a proxied path (which could contain a
    # node id) or anything else logged here; the bearer is never in the URL
    # anyway, but quiet-by-default is the right posture.
    def log_message(self, *_args: object) -> None:
        return

    # ---- request entry points ------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
        elif path.startswith("/api/"):
            self._proxy(path, parsed.query)
        else:
            self._send_plain(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        # Read-only viewer: mutating methods are never proxied.
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "viewer is read-only"})

    do_PUT = do_POST  # noqa: N815  (stdlib BaseHTTPRequestHandler dispatch names)
    do_DELETE = do_POST  # noqa: N815
    do_PATCH = do_POST  # noqa: N815

    # ---- static + index -------------------------------------------------
    def _serve_index(self) -> None:
        body = _render_index(self.project, self.target_name)
        self._send_bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def _serve_static(self, rel: str) -> None:
        # Resolve under ASSETS_DIR and refuse anything that escapes it.
        candidate = (ASSETS_DIR / rel).resolve()
        try:
            candidate.relative_to(ASSETS_DIR)
        except ValueError:
            self._send_plain(HTTPStatus.FORBIDDEN, "forbidden")
            return
        if not candidate.is_file() or candidate.suffix not in _STATIC_TYPES:
            self._send_plain(HTTPStatus.NOT_FOUND, "not found")
            return
        self._send_bytes(HTTPStatus.OK, candidate.read_bytes(), _content_type_for(candidate))

    # ---- proxy ----------------------------------------------------------
    def _proxy(self, path: str, query: str) -> None:
        if not path.startswith(_PROXY_PREFIXES):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "path not proxied"})
            return
        upstream = f"{self.base_url}{path}"
        if query:
            upstream += f"?{query}"
        is_stream = path.endswith("/graph/stream")
        if is_stream:
            self._proxy_stream(upstream)
        else:
            self._proxy_json(upstream)

    def _proxy_json(self, upstream: str) -> None:
        import httpx  # noqa: PLC0415

        # The bearer is added HERE, server-side. It is never echoed back to
        # the browser in any response header.
        headers = {"Authorization": f"Bearer {self.bearer}", "Accept": "application/json"}
        try:
            with httpx.Client(timeout=httpx.Timeout(_PROXY_TIMEOUT_S)) as client:
                resp = client.get(upstream, headers=headers)
        except httpx.HTTPError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": f"could not reach runtime: {exc}", "hint": "is the target reachable?"},
            )
            return

        # Graceful degradation: an older runtime (pre-ADR-046) has no graph
        # routes, so it 404s the path. Translate to a 501 with an actionable
        # hint the UI surfaces as a banner.
        if resp.status_code == HTTPStatus.NOT_FOUND:
            self._send_json(
                HTTPStatus.NOT_IMPLEMENTED,
                {
                    "error": "this runtime does not expose the graph query API",
                    "hint": (
                        "needs a runtime with /api/v1/.../graph (ADR 046). "
                        "Upgrade the runtime and check `mdk capabilities`."
                    ),
                },
            )
            return

        # Pass through the runtime's body + status. Strip hop-by-hop and any
        # auth-bearing headers; only forward content-type.
        ctype = resp.headers.get("content-type", "application/json")
        self._send_bytes(HTTPStatus(resp.status_code), resp.content, ctype)

    def _proxy_stream(self, upstream: str) -> None:
        import httpx  # noqa: PLC0415

        headers = {"Authorization": f"Bearer {self.bearer}", "Accept": "text/event-stream"}
        try:
            # No read timeout for the long-lived SSE stream; keep a connect
            # timeout so an unreachable runtime fails fast.
            timeout = httpx.Timeout(_PROXY_TIMEOUT_S, read=None)
            with (
                httpx.Client(timeout=timeout) as client,
                client.stream("GET", upstream, headers=headers) as resp,
            ):
                if resp.status_code == HTTPStatus.NOT_FOUND:
                    self._send_json(
                        HTTPStatus.NOT_IMPLEMENTED,
                        {
                            "error": "this runtime does not expose graph streaming",
                            "hint": "needs a runtime with graph/stream (ADR 046).",
                        },
                    )
                    return
                if resp.status_code != HTTPStatus.OK:
                    self._send_json(
                        HTTPStatus(resp.status_code),
                        {"error": f"stream upstream returned {resp.status_code}"},
                    )
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                for raw in resp.iter_raw():
                    if not raw:
                        continue
                    try:
                        self.wfile.write(raw)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break  # browser closed the EventSource — stop relaying
        except httpx.HTTPError:
            # The connection may already be (partly) committed; best-effort.
            with contextlib.suppress(Exception):
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "stream unavailable"})

    # ---- low-level response helpers ------------------------------------
    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Lock the browser down: no referrers leaking the local URL, and a
        # CSP that forbids any external network egress (air-gapped viewer).
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:",
        )
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        self._send_bytes(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _send_plain(self, status: HTTPStatus, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")


def _build_handler(
    *, base_url: str, bearer: str, project: str | None, target_name: str
) -> type[_GraphViewerHandler]:
    """Build a handler subclass carrying the per-serve config.

    The bearer lives only on this in-process class object — it is never
    written to disk, never injected into the served HTML, and never sent to
    the browser.
    """
    return type(
        "_BoundGraphViewerHandler",
        (_GraphViewerHandler,),
        {
            "base_url": base_url.rstrip("/"),
            "bearer": bearer,
            "project": project,
            "target_name": target_name,
        },
    )


def _probe_graph_api(base_url: str, bearer: str) -> tuple[bool, str]:
    """Best-effort check that the runtime exposes the graph query API.

    Returns ``(available, detail)``. Fetches the runtime's OpenAPI spec and
    looks for a graph path; falls back to a HEAD/GET on the graph endpoint.
    Never raises — degradation is the UI's job; this is just a friendly
    up-front heads-up so the operator isn't surprised by an empty viewer.
    """
    import httpx  # noqa: PLC0415

    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(f"{base_url}/api/v1/openapi.json", headers=headers)
            if resp.status_code == HTTPStatus.OK:
                spec = resp.json()
                paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
                has_graph = any("graph" in p for p in paths)
                if has_graph:
                    return True, "graph query API detected"
                return False, "runtime OpenAPI spec exposes no graph paths"
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    # Couldn't introspect — let the viewer try and degrade if needed.
    return True, "could not introspect capabilities; viewer will probe on load"


@graph_app.command("serve")
def serve(
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Deployed runtime target to view (from ~/.movate/config.yaml). "
            "Defaults to the active target. The bearer is resolved from the "
            "target's key_env / OIDC and kept server-side."
        ),
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Project id whose knowledge graph to view. Required to fetch "
            "the project graph + live-growth stream."
        ),
    ),
    port: int = typer.Option(
        8900,
        "--port",
        help="Local bind port for the viewer. Defaults to 8900.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Local bind host. 127.0.0.1 (default) restricts to localhost — "
            "the right default since the bearer lives in this process."
        ),
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Don't auto-open the browser (useful for CI / SSH tunnels / headless).",
    ),
) -> None:
    """Start the local knowledge-graph viewer and open it in a browser.

    Resolves the runtime URL + bearer for ``--target``, starts a local proxy
    server, prints the URL, and (unless ``--no-open``) opens it. The bearer
    stays server-side; the browser only ever talks to this local server.
    """
    # Reuse the exact target → URL + bearer pipeline `mdk kb` uses (OIDC +
    # static-key both supported), so a target set up for kb/deploy works here.
    from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

    # ``_resolve_target_bearer`` delegates to ``resolve_target``, which treats
    # ``None`` as "use the active target". Typer gives us ``None`` when
    # ``--target`` is omitted, so pass it through unchanged for that fallback.
    target_name, _target_cfg, base_url, bearer = _resolve_target_bearer(target)  # type: ignore[arg-type]

    # Up-front, friendly capability heads-up (the viewer degrades regardless).
    available, detail = _probe_graph_api(base_url, bearer)
    if not available:
        err_console.print(
            f"[yellow]⚠[/yellow] {detail} — this runtime may predate the graph "
            "query API (ADR 046). The viewer will show a hint if so; check "
            "[bold]mdk capabilities[/bold]."
        )

    if not project:
        err_console.print(
            "[yellow]⚠[/yellow] no [bold]--project[/bold] given — the viewer needs a "
            "project id to load a graph. Pass [bold]--project <id>[/bold]."
        )

    handler = _build_handler(
        base_url=base_url, bearer=bearer, project=project, target_name=target_name
    )

    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        err_console.print(
            f"[red]✗[/red] could not bind [bold]{host}:{port}[/bold]: {exc}. "
            "Try another [bold]--port[/bold]."
        )
        raise typer.Exit(code=2) from None

    url = f"http://{host}:{port}/"
    err_console.print(
        f"[bold cyan]mdk graph[/bold cyan] → target [bold]{target_name}[/bold], "
        f"viewer on [bold]{url}[/bold]"
    )
    err_console.print(
        "[dim]Read-only viewer. The runtime bearer stays in this local process "
        "(proxied server-side); it is never sent to the browser.[/dim]"
    )

    if not no_open:
        # Open in a background thread so a slow/blocking browser launcher
        # never delays serve_forever.
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        err_console.print("\n[dim]graph viewer stopped.[/dim]")
    finally:
        httpd.server_close()


__all__ = ["graph_app", "serve"]
