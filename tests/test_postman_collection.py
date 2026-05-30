"""Anti-drift guard for the Postman demo collection.

``postman/movate-core-flow.postman_collection.json`` is a hand-authored live
demo of the runtime ``/api/v1`` surface (see ``postman/README.md``). It is
easy for a demo collection to rot — a route gets renamed/removed and the
collection silently points at a dead endpoint, so the demo 404s in front of an
audience.

This test parses the collection JSON, extracts every request's ``(method,
path)`` (Postman ``{{var}}`` placeholders normalised to FastAPI ``{param}``
style), and asserts each one corresponds to a real route on the in-process
app. It also checks the collection is well-formed (v2.1 schema, collection-level
Bearer auth using a variable, no hardcoded token).

WebSocket entries are documented in the collection with ``Connection: Upgrade``
headers (Postman's WebSocket request type uses the same shape). The route
validator skips those entries — WebSocket routes are registered via
``@v1.websocket(...)`` and do not appear as ``APIRoute`` instances, so they
can't be introspected the same way as HTTP routes. A dedicated assertion in
``test_websocket_voice_route_documented`` verifies the WS entry is present.

Hermetic by construction: builds the FastAPI app from an ``InMemoryStorage``
double (no ``init()``, no network, no live server) and introspects the route
table — same approach as ``test_front_end_api_contract.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

from movate.runtime import build_app
from movate.testing import InMemoryStorage

_COLLECTION_PATH = (
    Path(__file__).resolve().parent.parent / "postman" / "movate-core-flow.postman_collection.json"
)

# Postman ``{{agent_name}}`` / ``{{run_id}}`` path segments map onto the
# FastAPI route's ``{name}`` / ``{run_id}`` template params. We don't try to
# match variable NAMES (the collection uses ``agent_name`` where the route uses
# ``name``) — only that a templated segment lines up with a templated segment.
_PM_VAR = re.compile(r"\{\{[^}]+\}\}")


@pytest.fixture(scope="module")
def app() -> Any:
    """Hermetic in-process app — no ``init()``, no I/O, no server."""
    return build_app(InMemoryStorage())


@pytest.fixture(scope="module")
def collection() -> dict[str, Any]:
    return json.loads(_COLLECTION_PATH.read_text(encoding="utf-8"))


def _route_templates(app: Any) -> set[tuple[str, str]]:
    """``{(METHOD, normalised_path)}`` for every real route on the app.

    Normalised path: each ``{param}`` segment collapsed to ``{}`` so it
    matches a Postman ``{{var}}`` segment regardless of the param's name.
    """
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        norm = re.sub(r"\{[^}]+\}", "{}", route.path)
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.add((method, norm))
    return out


def _is_websocket_request(request: dict[str, Any]) -> bool:
    """Return True when a Postman request is a WebSocket upgrade.

    Postman represents WebSocket requests with a ``Connection: Upgrade``
    header (or an ``Upgrade: websocket`` header). WebSocket routes are
    registered via ``@router.websocket(...)`` and do not appear in the
    ``APIRoute`` table — so they are excluded from the HTTP route-validation
    test and covered by a dedicated assertion instead.
    """
    for header in request.get("header", []):
        key = header.get("key", "").lower()
        value = header.get("value", "").lower()
        if key == "connection" and value == "upgrade":
            return True
        if key == "upgrade" and value == "websocket":
            return True
    return False


def _iter_requests(node: Any):
    """Depth-first walk of a Postman item tree, yielding request dicts."""
    items = node.get("item") if isinstance(node, dict) else None
    if items is None:
        return
    for entry in items:
        if "request" in entry:
            yield entry["name"], entry["request"]
        # Folders nest further items.
        yield from _iter_requests(entry)


def _request_method_path(request: dict[str, Any]) -> tuple[str, str]:
    """``(METHOD, normalised_path)`` for one Postman request.

    The path is taken from ``url.path`` (a list of segments), each Postman
    ``{{var}}`` segment collapsed to ``{}`` to match a route template.
    """
    method = request["method"].upper()
    url = request["url"]
    segments = url["path"] if isinstance(url, dict) else []
    norm_segments = ["{}" if _PM_VAR.search(seg) else seg for seg in segments]
    return method, "/" + "/".join(norm_segments)


def _folder_names(collection: dict[str, Any]) -> list[str]:
    """Return the names of all top-level folders in the collection."""
    return [item["name"] for item in collection.get("item", []) if "item" in item]


def test_every_request_path_maps_to_a_real_route(app: Any, collection: dict[str, Any]) -> None:
    """No request in the demo collection points at a dead endpoint.

    WebSocket requests (detected by ``Connection: Upgrade`` header) are
    skipped here — they live on ``@router.websocket(...)`` routes which are
    not ``APIRoute`` instances and are validated separately.
    """
    valid = _route_templates(app)
    requests = list(_iter_requests(collection))
    assert requests, "collection has no requests — parse/shape regression?"

    dead: list[str] = []
    for name, request in requests:
        if _is_websocket_request(request):
            # WebSocket entries are documented for Postman's WS tab;
            # they don't appear in the APIRoute table.
            continue
        method, path = _request_method_path(request)
        # The collection targets the versioned surface; routes on the app
        # already carry the ``/api/v1`` prefix, so compare directly.
        if (method, path) not in valid:
            dead.append(f"{name}: {method} {path}")

    assert not dead, (
        "Postman demo collection references endpoints that don't exist on the "
        "runtime (rename/removal drift — fix the collection or the route):\n" + "\n".join(dead)
    )


def test_collection_is_v21_with_variable_bearer_auth(
    collection: dict[str, Any],
) -> None:
    """Schema is v2.1 and auth is collection-level Bearer via a variable
    (never a hardcoded token)."""
    schema = collection["info"]["schema"]
    assert "v2.1.0" in schema, f"expected Postman v2.1 schema, got {schema}"

    auth = collection.get("auth", {})
    assert auth.get("type") == "bearer", "collection-level auth must be bearer"
    token_entry = next((e for e in auth.get("bearer", []) if e.get("key") == "token"), None)
    assert token_entry is not None, "bearer auth must declare a 'token'"
    token_value = token_entry["value"]
    assert _PM_VAR.fullmatch(token_value), (
        f"bearer token must be a {{{{var}}}} placeholder, not a literal: {token_value!r}"
    )


def test_no_hardcoded_secret_in_collection(collection: dict[str, Any]) -> None:
    """No opaque ``mvt_*`` key and no literal ``Authorization: Bearer <token>``
    header is committed — the token must ride the collection-level
    ``{{bearer_token}}`` variable, never a literal."""
    raw = _COLLECTION_PATH.read_text(encoding="utf-8")
    assert "mvt_" not in raw, "collection appears to contain a literal mvt_* key"

    # Walk every request's headers; an Authorization header value must be a
    # ``{{var}}`` placeholder if present at all (collection-level auth means it
    # usually isn't present per-request — Postman injects it).
    for name, request in _iter_requests(collection):
        for header in request.get("header", []):
            if header.get("key", "").lower() == "authorization":
                value = header.get("value", "")
                assert _PM_VAR.search(value), (
                    f"{name}: Authorization header has a non-variable literal: {value!r}"
                )


# ---------------------------------------------------------------------------
# Structural presence tests for the new API groups added in the Wave-2 tail
# integration (sessions, voice, graph, capabilities, projects/catalog).
# These are shape tests — they guard against folder renames or accidental
# deletion, not live API behaviour.
# ---------------------------------------------------------------------------


def test_capabilities_folder_present(collection: dict[str, Any]) -> None:
    """The Capabilities folder exists and contains at least one request."""
    folders = _folder_names(collection)
    assert "Capabilities" in folders, (
        f"Expected a 'Capabilities' top-level folder; found: {folders}"
    )
    cap_folder = next(item for item in collection["item"] if item["name"] == "Capabilities")
    requests = list(_iter_requests(cap_folder))
    assert len(requests) >= 1, "Capabilities folder must contain at least one request"
    paths = [_request_method_path(r)[1] for _, r in requests]
    assert any("/capabilities" in p for p in paths), (
        f"Capabilities folder must include a GET /capabilities request; found paths: {paths}"
    )


def test_stateful_sessions_folder_present(collection: dict[str, Any]) -> None:
    """The Stateful Sessions folder exists and covers create/fetch/delete + stateful run."""
    folders = _folder_names(collection)
    assert "Stateful Sessions" in folders, (
        f"Expected a 'Stateful Sessions' top-level folder; found: {folders}"
    )
    sess_folder = next(item for item in collection["item"] if item["name"] == "Stateful Sessions")
    requests = list(_iter_requests(sess_folder))
    assert len(requests) >= 4, (
        f"Stateful Sessions folder must have at least 4 requests (create/list/fetch/delete + "
        f"stateful run); found {len(requests)}"
    )
    method_paths = [_request_method_path(r) for _, r in requests]
    methods = {m for m, _ in method_paths}
    assert "POST" in methods, "Sessions folder must include a POST request (create session)"
    assert "GET" in methods, "Sessions folder must include a GET request (fetch/list sessions)"
    assert "DELETE" in methods, "Sessions folder must include a DELETE request (delete session)"
    # Verify session-id-scoped endpoint is present
    paths = [p for _, p in method_paths]
    assert any("/sessions/{}" in p for p in paths), (
        f"Sessions folder must include a /sessions/{{session_id}} request; paths: {paths}"
    )


def test_voice_folder_present(collection: dict[str, Any]) -> None:
    """The Voice folder exists and documents the WebSocket voice endpoint."""
    folders = _folder_names(collection)
    assert "Voice" in folders, f"Expected a 'Voice' top-level folder; found: {folders}"
    voice_folder = next(item for item in collection["item"] if item["name"] == "Voice")
    requests = list(_iter_requests(voice_folder))
    assert len(requests) >= 1, "Voice folder must contain at least one request"
    # Every request in the voice folder must be a WebSocket entry
    ws_requests = [r for _, r in requests if _is_websocket_request(r)]
    assert len(ws_requests) >= 1, (
        "Voice folder must contain at least one WebSocket (Connection: Upgrade) request "
        "documenting the WS /agents/{name}/voice endpoint"
    )
    # Realtime mode variant should be documented
    realtime_requests = [
        (name, r)
        for name, r in requests
        if "realtime" in (r.get("url", {}).get("raw", "") if isinstance(r.get("url"), dict) else "")
        or "realtime" in name.lower()
    ]
    assert len(realtime_requests) >= 1, (
        "Voice folder must include the ?mode=realtime variant of the WebSocket endpoint"
    )


def test_graph_analytics_folder_present(collection: dict[str, Any]) -> None:
    """The Graph Analytics folder exists and covers node drill-down + subgraph endpoints."""
    folders = _folder_names(collection)
    assert "Graph Analytics" in folders, (
        f"Expected a 'Graph Analytics' top-level folder; found: {folders}"
    )
    graph_folder = next(item for item in collection["item"] if item["name"] == "Graph Analytics")
    requests = list(_iter_requests(graph_folder))
    assert len(requests) >= 3, (
        f"Graph Analytics folder must have at least 3 requests "
        f"(subgraph, node detail, neighbors); found {len(requests)}"
    )
    paths = [_request_method_path(r)[1] for _, r in requests]
    assert any("/graph" in p for p in paths), (
        f"Graph Analytics folder must include a /graph endpoint; paths: {paths}"
    )
    assert any("/graph/nodes/{}" in p for p in paths), (
        "Graph Analytics folder must include a /graph/nodes/{id} (node drill-down); "
        f"paths: {paths}"
    )


def test_projects_catalog_folder_present(collection: dict[str, Any]) -> None:
    """The Projects + Catalog folder exists and covers create-project, add-agent, catalog list."""
    folders = _folder_names(collection)
    assert "Projects + Catalog" in folders, (
        f"Expected a 'Projects + Catalog' top-level folder; found: {folders}"
    )
    proj_folder = next(item for item in collection["item"] if item["name"] == "Projects + Catalog")
    requests = list(_iter_requests(proj_folder))
    assert len(requests) >= 3, (
        f"Projects + Catalog folder must have at least 3 requests; found {len(requests)}"
    )
    method_paths = [_request_method_path(r) for _, r in requests]
    paths = [p for _, p in method_paths]
    assert any("/projects" in p for p in paths), (
        "Projects + Catalog folder must include a /projects request"
    )
    assert any("/catalog/agents" in p for p in paths), (
        "Projects + Catalog folder must include a /catalog/agents request"
    )


def test_cli_parity_descriptions_present(collection: dict[str, Any]) -> None:
    """Every HTTP request (non-WebSocket) must have a non-empty description
    that references either a CLI command (``mdk ...``) or the label
    ``API-first`` (for endpoints with no CLI equivalent)."""
    missing: list[str] = []
    for name, request in _iter_requests(collection):
        if _is_websocket_request(request):
            # WebSocket entries reference CLI in folder description; skip per-request check.
            continue
        desc = request.get("description", "")
        if not desc:
            missing.append(f"{name}: no description")
        elif "mdk" not in desc and "API-first" not in desc:
            missing.append(f"{name}: description lacks 'mdk ...' or 'API-first' label")

    assert not missing, (
        "All non-WebSocket requests must carry a description with a CLI equivalent "
        "('CLI equivalent: mdk ...') or the 'API-first' label:\n" + "\n".join(missing)
    )


def test_websocket_voice_route_documented(collection: dict[str, Any]) -> None:
    """The WS /agents/{name}/voice endpoint is documented in the Voice folder."""
    voice_folder = next((item for item in collection["item"] if item["name"] == "Voice"), None)
    assert voice_folder is not None, "Voice folder must be present"
    requests = list(_iter_requests(voice_folder))
    ws_voice = [
        (name, r)
        for name, r in requests
        if _is_websocket_request(r)
        and "voice"
        in ("/".join(r.get("url", {}).get("path", [])) if isinstance(r.get("url"), dict) else "")
    ]
    assert ws_voice, "Voice folder must document the WS /api/v1/agents/{name}/voice endpoint"


def test_environment_variables_complete(collection: dict[str, Any]) -> None:
    """Collection-level variables include the new ids: session_id, project_id,
    graph_node_id (added with the Wave-2 tail integration)."""
    coll_vars = {v["key"] for v in collection.get("variable", [])}
    required = {"agent_name", "run_id", "session_id", "project_id", "graph_node_id"}
    missing = required - coll_vars
    assert not missing, (
        f"Collection is missing required variable(s): {missing}. "
        "Add them to the top-level 'variable' array."
    )
