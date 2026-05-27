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


def test_every_request_path_maps_to_a_real_route(app: Any, collection: dict[str, Any]) -> None:
    """No request in the demo collection points at a dead endpoint."""
    valid = _route_templates(app)
    requests = list(_iter_requests(collection))
    assert requests, "collection has no requests — parse/shape regression?"

    dead: list[str] = []
    for name, request in requests:
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
