"""Hypermedia ``_links`` builders for the core-flow responses (ADR 061).

One place to construct the ``{rel: url}`` maps so the route templates live in a
single spot (ADR 061 D4) instead of scattered through the handlers — and so a
route rename is one edit here, cross-checked by the Postman anti-drift test and
the route table. Each builder returns absolute-from-root ``/api/v1`` paths with
the response's own ids substituted; only **real, registered** routes are linked
(no dead ``rel``s). The maps are intentionally small — the *sensible next
calls* from a given response, not every reachable endpoint.

These are pure string builders: no app, no I/O, no route introspection (the
core-flow routes are always registered on a runtime build). Unit-tested in
``tests/test_hypermedia_links.py``.
"""

from __future__ import annotations

_V1 = "/api/v1"


def project_links(project_id: str) -> dict[str, str]:
    """Next calls from a project create/get response."""
    base = f"{_V1}/projects/{project_id}"
    return {
        "self": base,
        "agents": f"{base}/agents",
        "members": f"{base}/members",
        "graph": f"{base}/graph",
    }


def agent_links(name: str) -> dict[str, str]:
    """Next calls from an agent create response — the core build→ship→run path."""
    base = f"{_V1}/agents/{name}"
    return {
        "self": base,
        "validate": f"{base}/validate",
        "kb": f"{base}/kb",
        "publish": f"{base}/publish",
        "run": f"{base}/runs",
        "versions": f"{base}/versions",
    }


def kb_links(agent_name: str) -> dict[str, str]:
    """Next calls from a KB-ingest response — search / inspect the corpus."""
    base = f"{_V1}/agents/{agent_name}/kb"
    return {
        "self": base,
        "search": f"{base}/search",
        "stats": f"{base}/stats",
    }


def run_links(run_id: str, agent_name: str | None = None) -> dict[str, str]:
    """Next calls from a run response — observe the run, hop to its agent."""
    base = f"{_V1}/runs/{run_id}"
    links = {
        "self": base,
        "trace": f"{base}/trace",
        "explain": f"{base}/explain",
    }
    if agent_name:
        links["agent"] = f"{_V1}/agents/{agent_name}"
    return links
