"""``mdk demo doctor`` — is the Monday demo environment GO?

A readiness checklist that confirms a seeded demo env is ready to walk through
live. Mirrors the ``mdk doctor`` style (green/red rows + a greppable summary
line) but answers a narrower question: *did ``mdk demo seed`` light everything
up, and will each beat of the runbook have data to show?*

The checks read the seeded state back through the **same StorageProvider
Protocol** the seeder wrote it with (CLAUDE.md rule 6 — this is control-plane
diagnostics over the storage seam; it never imports ``runtime``):

* **graph** — the knowledge graph has ≥ a threshold of nodes/edges, so the
  graph viewer + node drill-down render a real network (beat 2).
* **dashboards/insights** — runs + evals + analyzer insights exist, so the
  dashboards aren't empty (beats 1 + 5).
* **sample agents** — the demo agents are in the registry, parse as valid
  ``AgentSpec``, and (with ``--run-agents``) load cleanly through the agent
  loader (beats 1 + 3).
* **voice** — at least one sample agent carries a ``voice:`` block (beat 3).
* **workflow** — a workflow bundle is registered (beat 4).
* **playground** — the optional playground port is probed for reachability
  (beat 5) — a soft check (the playground is started on demand).

Every check degrades gracefully: a backend missing a seam, or an empty env,
yields a clear red row with the fix command (``mdk demo seed``), never a stack
trace. Exit code is non-zero when any **hard** check fails, so CI / a
pre-demo script can gate on it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

# Readiness thresholds. Deliberately low — enough to prove "non-empty + a real
# network", not to assert an exact count (the seed's volume wobbles with flags).
_MIN_GRAPH_NODES = 8
_MIN_GRAPH_EDGES = 8
_MIN_RUNS = 20
_MIN_EVALS = 5
_MIN_INSIGHTS = 1
_MIN_AGENTS = 2

# Default playground port (mdk playground binds 8765 by default). Probed only
# for reachability; a closed port is a soft warning (the playground is started
# on demand, not by the seed).
_PLAYGROUND_PORT = int(os.environ.get("MDK_PLAYGROUND_PORT", "8765"))

# HTTP status at/above which a live-surface probe counts as failed (4xx/5xx).
# 401/403/405 are special-cased as "reachable but gated" in _probe_surface.
_HTTP_ERROR_FLOOR = 400


@dataclass
class Check:
    """One readiness check result.

    ``hard`` checks gate the GO verdict + the exit code; ``soft`` checks are
    advisory (rendered, but never flip the env to NO-GO on their own).
    """

    name: str
    ok: bool
    detail: str
    hard: bool = True


def _ok_mark(c: Check) -> str:
    if c.ok:
        return "[green]✓ ready[/green]"
    return "[red]✗ MISSING[/red]" if c.hard else "[yellow]⚠ optional[/yellow]"


async def _gather_checks(  # noqa: PLR0912 — branch count is inherent to a multi-section readiness scan
    *, run_agents: bool, surfaces: bool = False
) -> list[Check]:
    """Run every readiness check against the seeded demo state.

    Each check is independently guarded so one failing seam (or an empty env)
    produces a red row rather than aborting the whole readiness report.
    """
    # Local imports keep the cold doctor path light + honor cli ⊥ core lazily.
    from movate.core.demo import (  # noqa: PLC0415
        DEMO_GRAPH_AGENT,
        DEMO_PROJECT_ID,
        DEMO_TELEMETRY_TENANT_ID,
        DEMO_TENANT_ID,
    )
    from movate.storage import build_storage  # noqa: PLC0415

    checks: list[Check] = []
    storage = build_storage()
    await storage.init()
    try:
        # --- Knowledge graph (beat 2) ---
        nodes = 0
        edges = 0
        try:
            ents = await storage.list_entities(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEMO_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            rels = await storage.list_relations(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEMO_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            nodes, edges = len(ents), len(rels)
        except Exception as exc:  # graph seam missing / read failed
            checks.append(Check("knowledge graph", False, f"graph read failed: {str(exc)[:60]}"))
        else:
            ok = nodes >= _MIN_GRAPH_NODES and edges >= _MIN_GRAPH_EDGES
            checks.append(
                Check(
                    "knowledge graph",
                    ok,
                    (
                        f"{nodes} nodes / {edges} edges under "
                        f"{DEMO_TENANT_ID}/{DEMO_GRAPH_AGENT} "
                        f"(need ≥ {_MIN_GRAPH_NODES}/{_MIN_GRAPH_EDGES})"
                    ),
                )
            )
            # Drill-down works (a non-trivial node has neighbors) — proves beat 2's
            # node-expand interaction will return something.
            if nodes:
                try:
                    root = max(
                        ents,
                        key=lambda e: sum(
                            1 for r in rels if e.entity_id in (r.src_entity_id, r.dst_entity_id)
                        ),
                    )
                    sub = await storage.expand_neighbors(
                        agent=DEMO_GRAPH_AGENT,
                        tenant_id=DEMO_TENANT_ID,
                        entity_ids=[root.entity_id],
                        hops=1,
                        limit=50,
                        project_id=DEMO_PROJECT_ID,
                    )
                    n_neigh = max(0, len(sub.entities) - 1)
                    checks.append(
                        Check(
                            "graph drill-down",
                            n_neigh > 0,
                            f"'{root.name}' has {n_neigh} neighbor(s) (node-expand works)",
                        )
                    )
                except Exception as exc:
                    checks.append(
                        Check("graph drill-down", False, f"expand failed: {str(exc)[:60]}")
                    )

        # --- Telemetry / dashboards (beats 1 + 5) ---
        try:
            runs = await storage.list_runs(tenant_id=None, limit=_MIN_RUNS + 5)
            demo_runs = [r for r in runs if r.tenant_id.startswith("demo-")]
            checks.append(
                Check(
                    "dashboard telemetry (runs)",
                    len(demo_runs) >= _MIN_RUNS or len(runs) >= _MIN_RUNS,
                    f"{len(runs)} recent runs in store (need ≥ {_MIN_RUNS})",
                )
            )
        except Exception as exc:
            checks.append(
                Check("dashboard telemetry (runs)", False, f"read failed: {str(exc)[:60]}")
            )

        try:
            evals = await storage.list_evals(tenant_id=None, limit=_MIN_EVALS + 5)
            checks.append(
                Check(
                    "eval scorecards",
                    len(evals) >= _MIN_EVALS,
                    f"{len(evals)} recent evals in store (need ≥ {_MIN_EVALS})",
                )
            )
        except Exception as exc:
            checks.append(Check("eval scorecards", False, f"read failed: {str(exc)[:60]}"))

        # --- Insights (ADR 047) — the insight-fed + exec dashboards ---
        list_insights = getattr(storage, "list_insights", None)
        if callable(list_insights):
            try:
                # Insights are written per ``demo-`` telemetry tenant by the
                # analyzer (NOT under the scenario tenant DEMO_TENANT_ID, which
                # is the dash-free serve --dev tenant the agents + graph live
                # under). Read them back under the canonical telemetry tenant.
                insights = await list_insights(
                    DEMO_TELEMETRY_TENANT_ID, project_id="default", limit=90
                )
                checks.append(
                    Check(
                        "analyzer insights",
                        len(insights) >= _MIN_INSIGHTS,
                        f"{len(insights)} daily insight(s) for {DEMO_TELEMETRY_TENANT_ID} "
                        f"(need ≥ {_MIN_INSIGHTS})",
                    )
                )
            except Exception as exc:
                checks.append(Check("analyzer insights", False, f"read failed: {str(exc)[:60]}"))
        else:
            checks.append(
                Check(
                    "analyzer insights",
                    False,
                    "backend has no insight store — insight-fed dashboards stay empty",
                    hard=False,
                )
            )

        # --- Sample agents (beats 1 + 3) ---
        agents: list[Any] = []
        try:
            agents = await storage.list_agents(tenant_id=DEMO_TENANT_ID, limit=20)
        except Exception as exc:
            checks.append(Check("sample agents registered", False, f"read failed: {str(exc)[:60]}"))
        else:
            names = [a.name for a in agents]
            checks.append(
                Check(
                    "sample agents registered",
                    len(agents) >= _MIN_AGENTS,
                    f"{len(agents)} agent(s): {', '.join(names) or '—'} (need ≥ {_MIN_AGENTS})",
                )
            )
            # Only validate / voice-probe when there are bundles — a vacuous
            # green on an empty registry would be misleading (the "registered"
            # check above already hard-fails the empty case).
            if agents:
                checks.append(_check_agents_valid(agents))
                checks.append(_check_voice(agents))

        # --- Workflow (beat 4, stretch) ---
        list_workflows = getattr(storage, "list_workflows", None)
        if callable(list_workflows):
            try:
                wfs = await list_workflows(tenant_id=DEMO_TENANT_ID, limit=20)
                wf_names = [w.name for w in wfs]
                checks.append(
                    Check(
                        "workflow registered",
                        len(wfs) >= 1,
                        f"{len(wfs)} workflow(s): {', '.join(wf_names) or '—'}",
                        hard=False,  # beat 4 is marked stretch/optional in the runbook
                    )
                )
            except Exception as exc:
                checks.append(
                    Check("workflow registered", False, f"read failed: {str(exc)[:60]}", hard=False)
                )
        else:
            checks.append(
                Check("workflow registered", False, "backend has no workflow registry", hard=False)
            )

        # --- Load one sample agent (beats 1 + 3) — opt-in (--run-agents) ---
        if run_agents and agents:
            checks.append(_check_mock_run(agents))
    finally:
        await storage.close()

    # --- Playground reachability (beat 5) — soft, off-storage ---
    checks.append(_check_playground())

    # --- Live deployed-surface health (opt-in via --surfaces) ---
    # Probes the actual hosted endpoints (api / temporal / langfuse / grafana /
    # playgrounds / voice demos) so a pre-demo flight catches a down service
    # before you're in front of the customer. URLs come from MDK_DEMO_SURFACES
    # (never hardcoded), so this stays generic across environments.
    if surfaces:
        checks.extend(await _check_surfaces())
    return checks


def _check_agents_valid(agents: list[Any]) -> Check:
    """Every sample agent's bundle parses as a valid ``AgentSpec``."""
    from movate.core.models import AgentSpec  # noqa: PLC0415

    try:
        import yaml  # noqa: PLC0415
    except Exception:  # pragma: no cover - yaml is a required dep
        return Check("sample agents valid", False, "pyyaml unavailable")

    bad: list[str] = []
    for a in agents:
        try:
            raw = yaml.safe_load(a.files["agent.yaml"])
            AgentSpec.model_validate(raw)
        except Exception as exc:  # invalid agent.yaml in the bundle
            bad.append(f"{a.name} ({str(exc).splitlines()[0][:40]})")
    if bad:
        return Check("sample agents valid", False, "invalid: " + "; ".join(bad))
    return Check("sample agents valid", True, f"{len(agents)} bundle(s) parse as AgentSpec")


def _check_voice(agents: list[Any]) -> Check:
    """At least one sample agent carries a ``voice:`` block (beat 3)."""
    voice_agents = [a.name for a in agents if "voice:" in a.files.get("agent.yaml", "")]
    return Check(
        "voice-capable agent",
        bool(voice_agents),
        f"voice config present on: {', '.join(voice_agents) or '— none'}",
    )


def _check_mock_run(agents: list[Any]) -> Check:
    """Confirm one sample agent's bundle materializes + loads as a runnable agent.

    Materializes the first bundle to a temp dir and runs it through
    :func:`movate.core.loader.load_agent` — the same loader ``mdk run`` /
    ``mdk eval`` use. A clean load proves the bundle is runnable end-to-end
    (schemas parse, the prompt resolves, the ``runtime:`` adapter is available)
    without standing up the full Executor (pricing/storage/tracer) — the
    runbook's ``mdk run <agent> --mock`` step is the live execution proof. A
    failure is an advisory red row, never a crash.
    """
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    agent = agents[0]
    try:
        from movate.core.loader import load_agent  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - loader is core
        return Check(
            "sample agent loadable", False, f"loader unavailable: {str(exc)[:50]}", hard=False
        )

    try:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / agent.name
            agent_dir.mkdir(parents=True)
            for rel, content in agent.files.items():
                dest = agent_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
            load_agent(agent_dir)
            return Check(
                "sample agent loadable",
                True,
                f"'{agent.name}' bundle loads (schemas+prompt+runtime ok) — "
                'try `mdk run support-triage --mock \'{"question":"hi"}\'`',
                hard=False,
            )
    except Exception as exc:
        return Check(
            "sample agent loadable",
            False,
            f"'{agent.name}' load failed: {str(exc).splitlines()[0][:60]}",
            hard=False,
        )


def _check_playground() -> Check:
    """Soft TCP probe of the default playground port (beat 5).

    The playground is started on demand (``mdk playground``), not by the seed,
    so a closed port is an advisory warning with the start command — never a
    NO-GO on its own.
    """
    import socket  # noqa: PLC0415

    try:
        with socket.create_connection(("127.0.0.1", _PLAYGROUND_PORT), timeout=0.4):
            return Check(
                "playground reachable",
                True,
                f"listening on :{_PLAYGROUND_PORT}",
                hard=False,
            )
    except OSError:
        return Check(
            "playground reachable",
            False,
            f"not running on :{_PLAYGROUND_PORT} — start with `mdk playground` before beat 5",
            hard=False,
        )


def _demo_surfaces() -> list[tuple[str, str]]:
    """Parse the live surfaces to probe from ``MDK_DEMO_SURFACES``.

    Format: a comma-separated ``name=url`` list, e.g.::

        MDK_DEMO_SURFACES="api=https://…/health,temporal=http://…:8080,langfuse=http://…:3000"

    Deliberately env-driven (never a hardcoded host) so the same check works
    across dev / demo / customer environments. Empty/unset ⇒ no surface rows.
    """
    raw = os.environ.get("MDK_DEMO_SURFACES", "").strip()
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        part = entry.strip()
        if "=" not in part:
            continue
        name, url = part.split("=", 1)
        name, url = name.strip(), url.strip()
        if name and url:
            pairs.append((name, url))
    return pairs


def _probe_surface(name: str, url: str) -> Check:
    """HTTP-GET one deployed surface; report reachable + status + latency.

    Stdlib only (no httpx dep on the core CLI path). A 401/403/405 still means
    the service is UP (auth-gated or method-restricted), so those count as
    reachable — only a connection/timeout/5xx failure is a red row. Hard so a
    pre-demo gate (``mdk demo doctor --surfaces``) exits non-zero on a down one.
    """
    import time  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "mdk-demo-doctor"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            ms = int((time.monotonic() - t0) * 1000)
            code = int(resp.status)
            return Check(
                f"surface: {name}", code < _HTTP_ERROR_FLOOR, f"HTTP {code} in {ms}ms — {url}"
            )
    except urllib.error.HTTPError as exc:
        ms = int((time.monotonic() - t0) * 1000)
        # Reachable but gated/method-restricted → the surface is alive.
        alive = exc.code in (401, 403, 405)
        return Check(f"surface: {name}", alive, f"HTTP {exc.code} in {ms}ms — {url}")
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return Check(
            f"surface: {name}", False, f"unreachable after {ms}ms — {type(exc).__name__} — {url}"
        )


async def _check_surfaces() -> list[Check]:
    """Probe every configured live surface concurrently (off-storage)."""
    pairs = _demo_surfaces()
    if not pairs:
        return [
            Check(
                "live surfaces",
                False,
                'set MDK_DEMO_SURFACES="name=url,…" to health-check deployed endpoints',
                hard=False,
            )
        ]
    return list(await asyncio.gather(*[asyncio.to_thread(_probe_surface, n, u) for n, u in pairs]))


def doctor(
    run_agents: bool = typer.Option(
        False,
        "--run-agents/--no-run-agents",
        help=(
            "Also materialize + load one sample agent bundle through the agent "
            "loader (the same path `mdk run`/`mdk eval` use) to prove it's "
            "runnable end-to-end — schemas, prompt, and runtime adapter all "
            "resolve. Offline, $0. Off by default to keep the check fast."
        ),
    ),
    surfaces: bool = typer.Option(
        False,
        "--surfaces/--no-surfaces",
        help=(
            "Also HTTP-probe the live deployed surfaces (api / temporal / "
            "langfuse / grafana / playgrounds / voice demos) so a pre-demo "
            'flight catches a down service. URLs come from MDK_DEMO_SURFACES="'
            'name=url,…" — never hardcoded. A down surface fails the verdict.'
        ),
    ),
) -> None:
    """Confirm the Monday-demo environment is GO (run after ``mdk demo seed``).

    A green/red checklist mirroring ``mdk doctor``: the knowledge graph has
    nodes, the dashboards have telemetry + insights, the sample agents are
    registered + valid (one voice-capable), and a workflow is present. Exits
    non-zero if any hard check fails, so a pre-demo script can gate on it.

    With ``--surfaces`` it also pings the live hosted endpoints (from
    ``MDK_DEMO_SURFACES``) — the pre-demo "is everything up?" pre-flight.

    [bold]Examples:[/bold]

      [dim]$ mdk demo doctor                 # readiness checklist[/dim]
      [dim]$ mdk demo doctor --run-agents    # + a mock agent run[/dim]
      [dim]$ MDK_DEMO_SURFACES="api=https://…/health,langfuse=http://…:3000" \\[/dim]
      [dim]    mdk demo doctor --surfaces    # + live deployed-surface health[/dim]
    """
    checks = asyncio.run(_gather_checks(run_agents=run_agents, surfaces=surfaces))

    table = Table(title="mdk demo doctor — Monday readiness", title_justify="left")
    table.add_column("check", style="cyan")
    table.add_column("status")
    table.add_column("detail", style="dim", overflow="fold")
    for c in checks:
        table.add_row(c.name, _ok_mark(c), c.detail)
    console.print(table)

    hard_fail = [c for c in checks if c.hard and not c.ok]
    soft_fail = [c for c in checks if not c.hard and not c.ok]
    n_ok = sum(1 for c in checks if c.ok)

    # Greppable single-line summary — same key=value shape as mdk_doctor_summary
    # so a CI / pre-demo script can grep one consistent prefix.
    console.print(
        f"[dim]mdk_demo_doctor_summary: checks={len(checks)} ok={n_ok} "
        f"hard_fail={len(hard_fail)} soft_fail={len(soft_fail)}[/dim]"
    )

    if hard_fail:
        err_console.print(
            f"\n[red]✗ Demo NOT ready[/red] — {len(hard_fail)} blocking issue(s). "
            "Run [bold]mdk demo seed[/bold] to populate the demo state, then re-check. "
            "[dim](Confirm MOVATE_DB / MOVATE_DB_URL points at the seeded store.)[/dim]"
        )
        raise typer.Exit(code=1)

    if soft_fail:
        console.print(
            f"\n[green]✓ Demo is GO[/green] [dim](with {len(soft_fail)} optional warning(s) — "
            "e.g. start the playground before beat 5).[/dim]"
        )
    else:
        console.print("\n[green]✓ Demo is GO[/green] — every beat has data to show.")
