"""CLI ↔ ``/api/v1`` parity gate (drift guard).

`mdk` is a control plane; the deployed runtime is the execution plane
(CLAUDE.md rule 6). A CLI verb that talks to a *remote* runtime does so
through :class:`movate.core.client.MovateClient`, and every such verb is
supposed to map onto a real, documented ``/api/v1`` (or legacy
unprefixed) route. ADR 050 D11 promised this parity would be **enforced
by CI** — a voice CLI verb with no backing endpoint should *fail CI*.

The existing ``tests/test_front_end_api_contract.py`` only pins a fixed
*snapshot* of front-end paths + scopes — it never walks the CLI, so a
remote verb shipping without a route slips through silently. That is
exactly how two known gaps shipped unnoticed:

* ``mdk replay`` has no ``POST /api/v1/runs/{id}/replay`` (ADR 045 D13,
  designed but not built — ``replay`` is local-only today).
* ``mdk voice say`` / ``mdk voice transcribe`` have no
  ``POST /api/v1/agents/{name}/voice`` (ADR 050 D2 — only the streaming
  ``WS`` ships; the REST one-shot parity and the ``say``/``transcribe``
  verbs were never built).

The voice gap is now CLOSED: ``feat/voice-rest-oneshot`` built
``POST /api/v1/agents/{name}/voice`` (ADR 050 D2) and the ``mdk voice say`` /
``transcribe`` / ``ask`` verbs (ADR 050 D11), so those verbs are mapped in
``REMOTE_VERB_ROUTES`` and the former voice ``xfail`` is removed. The ``replay``
gap (ADR 045 D13) remains the one documented, unbuilt parity gap.

This module closes that loop. It:

1. **Enumerates remote-capable CLI verbs** by walking the Typer/Click
   command tree and selecting commands that BOTH (a) take ``--target``
   AND (b) live in a module that imports ``MovateClient`` — the audit's
   exact criterion for "routes through ``MovateClient`` to a remote
   runtime." This is a robust, self-updating signal: add a new remote
   verb and it appears here automatically.
2. **Introspects the runtime's route table** (hermetic, in-process —
   same ``build_app(InMemoryStorage())`` double the contract test uses;
   no network, no server, no DB).
3. **Asserts every enumerated remote verb is classified** as one of:
   a declared route mapping (the endpoint exists), a control-plane-only
   allowlist entry (intentionally no API), or a documented ``xfail`` gap
   (designed-but-unbuilt, ADR-referenced). An UNCLASSIFIED remote verb —
   i.e. a NEW one added without any of the three — **fails the test**.
   That is the drift guard.

The two known gaps are recorded as ``xfail`` so the gate is *truthful,
not blocking*: it records the gap without failing CI, and the day
someone builds the endpoint the ``xfail`` flips to ``XPASS`` and prompts
its removal (+ wiring the CLI verb into ``REMOTE_VERB_ROUTES``).

See ``docs/front-end-api.md`` (the CLI/ops-only inventory) and ADR 045 /
ADR 050 for the source of truth this guards.
"""

from __future__ import annotations

import importlib
import inspect

import click
import pytest
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute
from typer.main import get_command

from movate.cli.main import app as cli_app
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# 1. Mapping table — remote CLI verb -> the (METHOD, path) route(s) it calls.
#
# Each entry is keyed by the space-joined command path (as printed by
# ``mdk --help`` drill-down, e.g. ``"jobs cancel"``) and lists EVERY
# runtime route the verb hits through ``MovateClient``. The route paths
# are the *served* paths (some predate the ``/api/v1`` prefix: ``/run``,
# ``/jobs``, ``/runs/{id}``, ``/agents``). The endpoint -> client-method
# mapping is read straight off ``movate/core/client.py``.
#
# Adding a remote verb here is the ONLY way (besides the two allowlists
# below) to satisfy the drift guard, so this table is the live record of
# "which CLI verb drives which endpoint."
# ---------------------------------------------------------------------------

REMOTE_VERB_ROUTES: dict[str, list[tuple[str, str]]] = {
    # submit / jobs / runs — the run-and-poll loop
    "submit": [
        ("POST", "/run"),
        ("GET", "/jobs/{job_id}"),
        ("GET", "/runs/{run_id}"),
    ],
    "jobs show": [("GET", "/jobs/{job_id}")],
    "jobs wait": [("GET", "/jobs/{job_id}")],
    "jobs cancel": [("POST", "/api/v1/jobs/{job_id}/cancel")],
    "jobs list": [("GET", "/jobs")],
    "jobs list-agents": [("GET", "/agents")],
    # dead-letter management — operate retry-exhausted jobs
    "jobs dead-letter list": [("GET", "/api/v1/jobs/dead-letter")],
    # `show` reuses the standard job-poll route (the id is a dead-lettered job)
    "jobs dead-letter show": [("GET", "/jobs/{job_id}")],
    "jobs dead-letter retry": [
        ("GET", "/api/v1/jobs/dead-letter"),  # --all lists then requeues each
        ("POST", "/api/v1/jobs/{job_id}/requeue"),
    ],
    "jobs dead-letter purge": [("POST", "/api/v1/jobs/dead-letter/purge")],
    "runs show": [("GET", "/runs/{run_id}")],
    # run replay / time-travel (ADR 045 D13) — remote via --target
    "replay": [("POST", "/api/v1/runs/{run_id}/replay")],
    # batch inference
    "batch submit": [("POST", "/api/v1/agents/{name}/batch")],
    "batch status": [("GET", "/api/v1/batches/{batch_id}")],
    "batch list": [("GET", "/api/v1/batches")],
    # eval scorecard — remote scoring runs via the RemoteExecutor, which
    # drives the same submit/poll/fetch loop (POST /run + GET /jobs + /runs).
    "eval-scorecard": [
        ("POST", "/run"),
        ("GET", "/jobs/{job_id}"),
        ("GET", "/runs/{run_id}"),
    ],
    # judge engineer
    "judge generate": [("POST", "/api/v1/agents/{name}/judge/generate")],
    "judge commit": [("POST", "/api/v1/agents/{name}/judge/commit")],
    # observability intelligence (ADR 047)
    "observability ask": [("POST", "/api/v1/observability/ask")],
    "observability troubleshoot": [("POST", "/api/v1/observability/troubleshoot")],
    "observability health": [("GET", "/api/v1/observability/health")],
    "observability digest": [("GET", "/api/v1/observability/insights")],
    "observability analyze": [("POST", "/api/v1/observability/analyze")],
    # projects (ADR 040)
    "project create": [("POST", "/api/v1/projects")],
    "project list": [("GET", "/api/v1/projects")],
    "project show": [("GET", "/api/v1/projects/{project_id}")],
    "project update": [("PUT", "/api/v1/projects/{project_id}")],
    "project archive": [("DELETE", "/api/v1/projects/{project_id}")],
    "project add-agent": [("POST", "/api/v1/projects/{project_id}/agents")],
    "project members list": [("GET", "/api/v1/projects/{project_id}/members")],
    "project members add": [("POST", "/api/v1/projects/{project_id}/members")],
    "project members remove": [("DELETE", "/api/v1/projects/{project_id}/members/{principal_id}")],
    # workflow definitions + runs (ADR 037 D1, ADR 017 D5)
    "workflow list": [("GET", "/api/v1/workflows")],
    "workflow show": [("GET", "/api/v1/workflows/{name}")],
    "workflow publish": [("POST", "/api/v1/workflows/{name}/publish")],
    "workflow revert": [("POST", "/api/v1/workflows/{name}/revert")],
    "workflow validate": [("POST", "/api/v1/workflows/{name}/validate/from-spec")],
    "workflow runs": [("GET", "/api/v1/workflow-runs")],
    "workflow signal": [("POST", "/api/v1/workflow-runs/{workflow_run_id}/signal")],
    # webhooks (ADR 035 D2)
    "webhooks list": [("GET", "/api/v1/webhooks")],
    "webhooks create": [("POST", "/api/v1/webhooks")],
    "webhooks show": [("GET", "/api/v1/webhooks/{webhook_id}")],
    "webhooks delete": [("DELETE", "/api/v1/webhooks/{webhook_id}")],
    "webhooks enable": [("PATCH", "/api/v1/webhooks/{webhook_id}")],
    "webhooks disable": [("PATCH", "/api/v1/webhooks/{webhook_id}")],
    "webhooks attempts": [("GET", "/api/v1/webhooks/{webhook_id}/attempts")],
    # capability discovery (ADR 045 D9)
    "capabilities": [("GET", "/api/v1/capabilities")],
    # voice (ADR 050 D11) — the streaming WS turn + its one-shot REST siblings.
    # ``try`` drives the streaming WS transport; ``say``/``transcribe``/``ask``
    # all drive the SAME one-shot POST (the WS's request/response sibling);
    # ``providers list`` reads capability discovery. All now route through
    # MovateClient-importing voice_cmd, so the gate enumerates + classifies them.
    "voice try": [("WS", "/api/v1/agents/{name}/voice")],
    "voice say": [("POST", "/api/v1/agents/{name}/voice")],
    "voice transcribe": [("POST", "/api/v1/agents/{name}/voice")],
    "voice ask": [("POST", "/api/v1/agents/{name}/voice")],
    "voice call": [("POST", "/api/v1/agents/{name}/call/twilio")],
    "voice providers list": [("GET", "/api/v1/capabilities")],
}

# ---------------------------------------------------------------------------
# 2. Control-plane-only allowlist — remote-looking verbs that INTENTIONALLY
#    have no ``/api/v1`` mirror (per ``docs/front-end-api.md`` "CLI / ops-only").
#
# These run on the operator's machine / a CI runner, not on the runtime
# (control plane ⊥ execution plane, CLAUDE.md rule 6). They are listed
# here so the drift guard treats them as deliberately-no-API rather than
# as an unmapped verb. NONE of these should map to an ``/api/v1`` route.
#
# NOTE: this is the allowlist of verbs that the enumeration (``--target``
# AND a ``MovateClient``-importing module) currently flags. The broader
# set of control-plane groups the audit names — ``tenants``, ``deploy``,
# ``fleet``, ``infra``, ``secrets``, ``config``, ``profiles``, ``memory``,
# local ``costs``/``report`` — are NOT flagged by the enumeration at all
# (their modules don't import ``MovateClient``), so they never reach this
# gate; they're documented as control-plane in ``docs/front-end-api.md``.
# ---------------------------------------------------------------------------

CONTROL_PLANE_ONLY: dict[str, str] = {
    # ``jobs reap`` re-queues jobs whose visibility-timeout lease expired by
    # operating DIRECTLY on the local storage queue (a maintenance/ops task
    # on the worker host) — it does not poll the runtime over HTTP. It only
    # rides in a ``MovateClient``-importing module (``jobs.py``); the verb
    # itself is control-plane. (It also takes no ``--target``, so the strict
    # enumeration excludes it — kept here as a documented belt-and-braces.)
    "jobs reap": "local storage-queue maintenance; no runtime HTTP call",
    # ``teams-bot serve`` STARTS a long-lived bot process locally (it serves
    # an inbound webhook surface); it is the thing that *calls* the runtime,
    # not a runtime endpoint. Server lifecycle = control plane.
    "teams-bot serve": "starts a local bot server process; lifecycle, not an API verb",
    # ``workflow lint`` parses + compiles workflow YAML purely locally
    # (no ``MovateClient`` call despite living in ``workflow_cmd.py``); it is
    # a local static check. It takes no ``--target`` so strict enumeration
    # already excludes it — documented here for the same belt-and-braces.
    "workflow lint": "local YAML compile/lint; no runtime HTTP call",
    # ``workflow history`` and ``workflow replay`` (#697) query the Temporal
    # workflow engine directly — they are control-plane ops tooling, not
    # runtime API verbs.
    "workflow history": "queries Temporal workflow history; control-plane only",
    "workflow replay": "replays a Temporal workflow execution; control-plane only",
    "voice bench": "local STT/TTS eval harness; no runtime HTTP call",
}

# ---------------------------------------------------------------------------
# 3. Known parity gaps — designed-but-unbuilt remote verbs / endpoints,
#    recorded as xfail so the gate is TRUTHFUL (records the gap) without
#    BLOCKING CI. Each references the ADR that designed it. The day the
#    endpoint ships, the matching ``test_known_gap_*`` XPASSes — that
#    failure is the prompt to delete the xfail + wire the verb into
#    ``REMOTE_VERB_ROUTES`` above.
# ---------------------------------------------------------------------------

# (method, path) the gap's CLI verb is *supposed* to call once built.
GAP_REPLAY_ROUTE = ("POST", "/api/v1/runs/{run_id}/replay")  # ADR 045 D13


@pytest.fixture(scope="module")
def app():
    """Hermetic in-process runtime app — no ``init()``, no I/O, no server."""
    return build_app(InMemoryStorage())


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def _route_index(app) -> dict[tuple[str, str], object]:
    """Map ``(METHOD, served-path)`` -> route (HEAD/OPTIONS dropped).

    Mirrors ``test_front_end_api_contract._route_index`` so both guards
    read the route table the same way, but ALSO indexes WebSocket routes
    under the synthetic method ``"WS"`` — voice's streaming ``mdk voice try``
    maps to the ``WS /api/v1/agents/{name}/voice`` transport (ADR 050 D11),
    which is a ``WebSocketRoute``, not an ``APIRoute``.
    """
    index: dict[tuple[str, str], object] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                index[(method, route.path)] = route
        elif isinstance(route, WebSocketRoute):
            index[("WS", route.path)] = route
    return index


def _iter_cli_commands() -> list[tuple[str, click.Command]]:
    """Walk the Typer/Click tree -> ``[(space-joined path, leaf command)]``."""
    root = get_command(cli_app)
    out: list[tuple[str, click.Command]] = []

    def walk(cmd: click.Command, prefix: tuple[str, ...]) -> None:
        for name, sub in getattr(cmd, "commands", {}).items():
            path = (*prefix, name)
            if isinstance(sub, click.Group):
                walk(sub, path)
            else:
                out.append((" ".join(path), sub))

    walk(root, ())
    return out


def _command_takes_target(cmd: click.Command) -> bool:
    """True if the leaf command declares a ``--target`` option."""
    return any("--target" in (getattr(param, "opts", None) or []) for param in cmd.params)


_module_uses_client_cache: dict[str, bool] = {}


def _module_uses_movate_client(module_name: str) -> bool:
    """True if the command's defining module imports/uses ``MovateClient``.

    Read off the module SOURCE (not a runtime attribute) so a transitive
    re-export doesn't produce a false positive — the verb has to actually
    name the client in its own module to count as remote-capable.
    """
    if module_name in _module_uses_client_cache:
        return _module_uses_client_cache[module_name]
    try:
        source = inspect.getsource(importlib.import_module(module_name))
    except (OSError, TypeError, ImportError):
        source = ""
    result = "MovateClient" in source
    _module_uses_client_cache[module_name] = result
    return result


def _remote_capable_verbs() -> dict[str, click.Command]:
    """The enumerated set: commands that take ``--target`` AND live in a
    ``MovateClient``-importing module.

    This is the audit's exact criterion ("commands taking ``--target``,
    which route through ``MovateClient``"). It is self-updating: a new
    remote verb shows up here the moment it's wired, which is what makes
    the drift guard below meaningful.
    """
    verbs: dict[str, click.Command] = {}
    for path, cmd in _iter_cli_commands():
        callback = cmd.callback
        module = getattr(callback, "__module__", "") if callback else ""
        if _command_takes_target(cmd) and _module_uses_movate_client(module):
            verbs[path] = cmd
    return verbs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_enumeration_is_non_trivial() -> None:
    """Sanity floor: the enumeration actually finds the remote verbs.

    Guards against the walk silently returning nothing (a refactor that
    breaks ``--target`` detection or the module-source read) — which
    would make the parity guard below vacuously pass.
    """
    verbs = _remote_capable_verbs()
    assert len(verbs) >= 30, (
        f"too few remote CLI verbs enumerated ({len(verbs)}) — did the CLI "
        "tree walk or --target / MovateClient detection regress? "
        f"found: {sorted(verbs)}"
    )


def test_every_remote_verb_is_classified() -> None:
    """THE DRIFT GUARD. Every enumerated remote verb must be either mapped
    to a route, control-plane-allowlisted, or a documented xfail gap.

    A NEW remote verb added without any of those three lands here as
    ``unclassified`` and fails — forcing the author to declare its
    endpoint (or mark it control-plane / a known gap). This is the
    enforcement ADR 050 D11 promised and the snapshot-only contract test
    never provided.
    """
    verbs = _remote_capable_verbs()
    classified = set(REMOTE_VERB_ROUTES) | set(CONTROL_PLANE_ONLY)
    unclassified = sorted(set(verbs) - classified)
    assert not unclassified, (
        "new remote CLI verb(s) with no declared /api/v1 route, no "
        "control-plane-only allowlist entry, and no documented xfail gap:\n  "
        + "\n  ".join(unclassified)
        + "\n\nDeclare each in REMOTE_VERB_ROUTES (with the route it calls), "
        "or in CONTROL_PLANE_ONLY (if it deliberately has no API), or add a "
        "documented xfail (if the endpoint is designed-but-unbuilt). "
        "See tests/test_cli_api_parity.py."
    )


def test_mapping_table_only_lists_real_remote_verbs() -> None:
    """The mapping table must not drift the other way: every key in
    ``REMOTE_VERB_ROUTES`` is an actually-enumerated remote verb.

    Catches a verb being renamed/removed while a stale mapping entry
    lingers (which would otherwise rot silently — the route still exists,
    so the route-existence test below keeps passing).
    """
    verbs = set(_remote_capable_verbs())
    stale = sorted(set(REMOTE_VERB_ROUTES) - verbs)
    assert not stale, (
        "REMOTE_VERB_ROUTES lists verb(s) that are no longer enumerated as "
        f"remote (renamed/removed/lost --target?): {stale}"
    )


def test_control_plane_allowlist_has_no_api_mirror(app) -> None:
    """Each control-plane-only verb genuinely has no ``/api/v1`` sibling.

    We assert the allowlist is *honest*: a verb can't be in BOTH
    allowlists (control-plane-only AND route-mapped). These verbs run
    locally, so there's no route to assert against directly — the real
    invariant is mutual exclusion.
    """
    overlap = sorted(set(CONTROL_PLANE_ONLY) & set(REMOTE_VERB_ROUTES))
    assert not overlap, (
        f"verb(s) listed as BOTH control-plane-only AND route-mapped — pick one: {overlap}"
    )


def test_mapped_routes_exist_in_runtime(app) -> None:
    """Every (method, path) a mapped remote verb calls is a REAL route on
    the runtime. This is the parity assertion proper: ``mdk webhooks
    create`` claims ``POST /api/v1/webhooks`` — prove the runtime serves
    it. A client method pointed at a renamed/removed path fails here.
    """
    index = _route_index(app)
    missing: list[str] = []
    for verb, routes in REMOTE_VERB_ROUTES.items():
        for method, path in routes:
            if (method, path) not in index:
                missing.append(f"{verb!r} -> {method} {path}")
    assert not missing, (
        "remote CLI verb(s) mapped to a route the runtime does NOT serve "
        "(CLI↔API parity broken — renamed/removed endpoint?):\n  " + "\n  ".join(missing)
    )


# ---------------------------------------------------------------------------
# Known gaps (xfail) — see section 3. These assert the PROMISED endpoint
# exists; today it does not, so they xfail. When the endpoint ships, the
# test XPASSes (strict) and CI fails, prompting removal of the xfail and
# wiring the CLI verb into REMOTE_VERB_ROUTES.
# ---------------------------------------------------------------------------


def test_replay_endpoint_built(app) -> None:
    """ADR 045 D13 — ``POST /api/v1/runs/{run_id}/replay`` is now built.

    (Was a strict-xfail "known gap" until the run-replay endpoint shipped; the
    matching remote CLI verb + its REMOTE_VERB_ROUTES entry land alongside it.)
    """
    index = _route_index(app)
    assert GAP_REPLAY_ROUTE in index


def test_voice_rest_endpoint_built_and_mapped(app) -> None:
    """ADR 050 D2 — ``POST /api/v1/agents/{name}/voice`` now ships (gap closed).

    The former ``xfail`` gap: the REST one-shot voice endpoint is built (this
    PR), so the route exists and the ``voice say``/``transcribe``/``ask`` verbs
    are mapped in ``REMOTE_VERB_ROUTES``. This asserts the endpoint is real (the
    parity proper for these verbs runs in ``test_mapped_routes_exist_in_runtime``).
    """
    index = _route_index(app)
    assert ("POST", "/api/v1/agents/{name}/voice") in index
