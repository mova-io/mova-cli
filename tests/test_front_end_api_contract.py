"""Front-end `/api/v1` contract test (compat guard).

The Mova iO Angular front end drives the MDK runtime over HTTP. The
``/api/v1`` surface is a documented compatibility contract (CLAUDE.md
rule 5) — a rename or removal of a path the front end depends on is a
breaking change that must bump ``/api/v2`` and be done deliberately, not
slip through silently.

This test pins the **key** front-end paths + methods + required scopes so
such a change fails CI. It is intentionally a *floor*, not a full
snapshot: it asserts the routes the front end's init / add / validate /
deploy / monitor flows depend on still exist with their expected scope —
it does NOT assert the exact route count (additive growth is fine).

Hermetic by construction: builds the FastAPI app in-process from an
``InMemoryStorage`` double (no ``init()``, no network, no live server, no
real DB) and introspects the route table + generated OpenAPI. See
``docs/front-end-api.md`` for the full mapping + inventory this guards.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Expected contract: (method, path) -> required scope.
#
# ``None`` means "no ``_scope(...)`` gate on this route" (it is still
# authenticated unless it is a public probe; the gate is what we pin).
# This is the subset the front-end flows depend on — NOT the full
# inventory. Adding routes to the runtime never breaks this; renaming /
# removing one of these, or changing its required scope, does.
# ---------------------------------------------------------------------------

EXPECTED_ROUTES: dict[tuple[str, str], str | None] = {
    # init / add — create + catalog the agent
    ("POST", "/api/v1/agents"): "admin",
    ("POST", "/api/v1/agents/from-wizard"): "admin",
    ("POST", "/api/v1/skills"): "admin",
    ("GET", "/api/v1/agents"): "read",
    ("GET", "/api/v1/agents/{name}"): "read",
    ("PUT", "/api/v1/agents/{name}"): "admin",
    ("DELETE", "/api/v1/agents/{name}"): "admin",
    ("GET", "/api/v1/agents/{name}/versions"): "read",
    # validate
    ("POST", "/api/v1/agents/{name}/validate"): "read",
    # deploy — ship / promote an agent
    ("POST", "/api/v1/agents/{name}/publish"): "admin",
    ("POST", "/api/v1/agents/{name}/canary"): "admin",
    ("POST", "/api/v1/agents/{name}/canary/promote"): "admin",
    ("POST", "/api/v1/agents/{name}/canary/rollback"): "admin",
    ("POST", "/api/v1/agents/{name}/revert"): "admin",
    # monitor — run, poll, fetch, trace, eval
    ("POST", "/api/v1/agents/{name}/runs"): "run",
    ("POST", "/api/v1/agents/{name}/runs/stream"): "run",
    ("GET", "/api/v1/jobs/{job_id}"): "read",
    ("GET", "/api/v1/jobs"): "read",
    ("GET", "/api/v1/runs/{run_id}"): "read",
    ("GET", "/api/v1/runs/{run_id}/trace"): "read",
    ("GET", "/api/v1/runs/{run_id}/explain"): "read",
    ("POST", "/api/v1/agents/{name}/evals"): "eval",
    ("GET", "/api/v1/evals"): "read",
    ("GET", "/api/v1/evals/{eval_id}"): "read",
    # ADR 043 D1 — Failure Pattern Diagnoser (read-only diagnose phase).
    ("POST", "/api/v1/agents/{name}/diagnose"): "eval",
    ("GET", "/api/v1/diagnoses/{diagnosis_id}"): "read",
    # monitor (aggregate feed, ADR 032 D2) — the in-product report / metrics
    ("GET", "/api/v1/report"): "read",
    ("GET", "/api/v1/agents/{name}/metrics"): "read",
    # auth — how the front end discovers + mints scoped keys
    ("GET", "/api/v1/auth/me"): None,
    ("POST", "/api/v1/auth/keys"): "admin",
}


@pytest.fixture(scope="module")
def app():
    """Hermetic in-process app — no ``init()``, no I/O, no server."""
    return build_app(InMemoryStorage())


def _route_index(app) -> dict[tuple[str, str], APIRoute]:
    """Map ``(METHOD, path)`` -> the APIRoute (HEAD/OPTIONS dropped)."""
    index: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            index[(method, route.path)] = route
    return index


def _route_scopes(route: APIRoute) -> set[str]:
    """Extract the scopes a route's ``require_scope`` gate requires.

    ``_scope(*needed)`` wires ``Depends(require_scope(auth_dep, *needed))``;
    the returned ``scope_dependency`` closure captures ``needed`` as a
    tuple of scope strings (``app.py``). We read that cell to recover the
    declared scope(s) without standing the auth stack up.
    """
    scopes: set[str] = set()
    for dep in route.dependant.dependencies:
        call = dep.call
        if call is None or getattr(call, "__name__", "") != "scope_dependency":
            continue
        for cell in call.__closure__ or ():
            value = cell.cell_contents
            if isinstance(value, tuple) and all(isinstance(s, str) for s in value):
                scopes.update(value)
    return scopes


def test_front_end_api_paths_present_with_expected_scopes(app) -> None:
    """Every front-end-critical (method, path) exists with its scope."""
    index = _route_index(app)
    missing: list[tuple[str, str]] = []
    wrong_scope: list[str] = []

    for (method, path), expected_scope in EXPECTED_ROUTES.items():
        route = index.get((method, path))
        if route is None:
            missing.append((method, path))
            continue
        scopes = _route_scopes(route)
        if expected_scope is None:
            if scopes:
                wrong_scope.append(f"{method} {path}: expected no scope gate, got {sorted(scopes)}")
        elif expected_scope not in scopes:
            wrong_scope.append(
                f"{method} {path}: expected scope {expected_scope!r}, got {sorted(scopes)}"
            )

    assert not missing, (
        "front-end /api/v1 contract broken — these (method, path) pairs are gone "
        f"(rename/removal is a breaking change, must bump /api/v2): {missing}"
    )
    assert not wrong_scope, "front-end /api/v1 scope contract broken:\n" + "\n".join(wrong_scope)


def test_v1_prefix_is_applied(app) -> None:
    """The versioned surface is mounted under the documented prefix."""
    index = _route_index(app)
    v1_paths = {path for (_method, path) in index if path.startswith("/api/v1")}
    # Sanity floor: the versioned surface is non-trivial and prefixed.
    assert len(v1_paths) >= 30, (
        f"too few /api/v1 paths ({len(v1_paths)}) — prefix wiring regressed?"
    )


def test_generated_openapi_exposes_front_end_paths(app) -> None:
    """The generated OpenAPI spec (what the Angular client codegens from)
    advertises the front-end-critical paths with the right methods.

    Guards the client-generation contract end to end: the front-end team
    generates a TypeScript client from ``app.openapi()`` / the
    ``/openapi.json`` it produces, so a path/method dropping out of the
    spec breaks codegen even if some internal route still exists.
    """
    spec = app.openapi()
    paths = spec["paths"]
    missing: list[str] = []
    for (method, path), _scope in EXPECTED_ROUTES.items():
        entry = paths.get(path)
        if entry is None or method.lower() not in entry:
            missing.append(f"{method} {path}")
    assert not missing, f"OpenAPI spec missing front-end paths/methods: {missing}"


def test_wizard_create_is_structured_not_llm(app) -> None:
    """The wizard-create endpoint accepts STRUCTURED fields, not a free-text
    description that gets LLM-expanded (see docs/front-end-api.md, the
    ``--llm`` finding). Pin the load-bearing fields so a schema change that
    would silently turn this into / away from a structured contract is caught.

    This is the runtime-side guard for the audit conclusion: no /api/v1
    endpoint LLM-generates an agent from natural language; ``agent_prompt``
    is the actual prompt template, taken verbatim.
    """
    schema = app.openapi()["components"]["schemas"]["WizardAgentSubmission"]
    props = schema["properties"]
    # The prompt template + model are required structured fields — the
    # wizard collects the real prompt, the server does not generate it.
    assert "agent_prompt" in props
    assert "ai_model" in props
    assert "name" in props
    assert set(schema.get("required", [])) >= {"name", "agent_prompt", "ai_model"}
    # No free-text "describe the agent and we'll build it" field exists.
    assert "describe" not in props
    assert "nl_description" not in props
