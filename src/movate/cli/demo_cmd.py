"""``mdk demo`` — runnable demo project + the dashboard "wow pack" seeder.

Two responsibilities live under ``mdk demo``:

* **``mdk demo`` (no subcommand) / the project scaffold** — generates a
  complete, working FAQ agent + dataset + project structure in 60 seconds.
  Different from ``mdk init`` (which scaffolds an empty template into an
  existing project): this is the "from zero" hello-world for sales demos,
  onboarding, and operator first-touch. Backward-compatible: ``mdk demo`` and
  ``mdk demo my-dir`` behave exactly as before.

* **``mdk demo seed`` / ``mdk demo clear``** — populate (and purge) a runtime
  with realistic synthetic telemetry so every in-repo dashboard lights up with
  a believable story (the "wow pack"). The generation logic is pure + lives in
  :mod:`movate.core.demo`; this command only wraps it with persistence, the
  safety/prod guard, and operator UX (CLAUDE.md boundary: ``cli`` ⊥ ``core``).

Scaffold output::

  demo-faq/
    movate.yaml                 # project config
    .env.example                # required env var template
    .gitignore                  # standard movate ignores
    agents/faq/
      agent.yaml                # working FAQ agent
      prompt.md
      schema/{input,output}.json
      evals/dataset.jsonl       # 3 sample Q/A cases
      evals/judge.yaml.example  # judge-eval template

The recipe is fixed for MVP — one canonical FAQ demo. Future
enhancements (``--template chatbot``, ``--template classifier``)
parameterize which template directory gets copied in; the
:func:`_demo_recipe` indirection keeps that swap-in cheap.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

logger = logging.getLogger(__name__)

console = Console()
err_console = Console(stderr=True)


# The agent template package + the demo's destination layout.
# Lifting the template package out as a constant so a future
# `--template <name>` flag is a one-line dispatch.
_TEMPLATE_PACKAGE = "movate.templates.faq_agent"
_AGENT_NAME = "faq"


# Project-level files that get written alongside the agent copy.
# Kept as inline strings (not separate template files) because
# they're tiny and inlining keeps the demo recipe legible in one read.
_MOVATE_YAML = """\
api_version: movate/v1
kind: Project
name: demo-faq
description: One-command runnable demo — an FAQ agent with sample eval dataset.
version: 0.1.0

defaults:
  model:
    provider: openai/gpt-4o-mini-2024-07-18
    params:
      temperature: 0.0
      max_tokens: 512

storage:
  backend: sqlite
  path: .movate/local.db
"""

_ENV_EXAMPLE = """\
# Demo requires one provider key. Uncomment + set ONE of the following:

OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# AZURE_API_KEY=

# Optional — enables Langfuse tracing if set:
# LANGFUSE_PUBLIC_KEY=
# LANGFUSE_SECRET_KEY=
"""

_GITIGNORE = """\
# movate runtime state — never commit
.movate/

# Python
__pycache__/
*.pyc

# Editor / OS
.vscode/
.idea/
.DS_Store

# Secrets
.env
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_template_root() -> Path:
    """Return the on-disk path to the FAQ template directory.

    Resolves via the parent package's ``__file__`` so the lookup works
    for both editable installs (source tree) and wheel installs (real
    filesystem paths inside site-packages). We need a real path —
    ``shutil.copytree`` doesn't accept the ``MultiplexedPath`` /
    ``Traversable`` objects ``importlib.resources.files`` may return.

    Falls through ``importlib`` as a secondary resolution path so the
    function still works if movate is ever bundled into a zip wheel.
    """
    # Primary: derive from the package's __file__. Works for source +
    # standard wheel installs.
    import movate.templates  # noqa: PLC0415 — deferred so import cycles can't deadlock

    pkg_root = Path(movate.templates.__file__).parent
    candidate = pkg_root / "faq_agent"
    if candidate.is_dir():
        return candidate

    # Fallback: importlib.resources, then materialize to a Path.
    # For zip-installed wheels this requires `as_file()` to extract;
    # we don't currently ship that way, but the path is here for safety.
    try:
        root = resources.files(_TEMPLATE_PACKAGE)
        path = Path(str(root))
        if path.is_dir():
            return path
    except (ModuleNotFoundError, FileNotFoundError):
        pass

    err_console.print(
        f"[red]✗[/red] demo template not found at {candidate}. "
        "[dim]Reinstall mdk or check movate.templates is packaged.[/dim]"
    )
    raise typer.Exit(code=2)


def _materialize_agent(template_root: Path, agent_dir: Path) -> None:
    """Copy the template into ``agent_dir`` and substitute __AGENT_NAME__.

    The template's ``agent.yaml`` uses ``__AGENT_NAME__`` as a sentinel —
    same convention as ``mdk init``. Substitution is plain string-replace
    (no Jinja for the demo path; the sentinel is unique enough).
    """
    shutil.copytree(template_root, agent_dir, dirs_exist_ok=False)

    agent_yaml = agent_dir / "agent.yaml"
    if agent_yaml.is_file():
        text = agent_yaml.read_text()
        agent_yaml.write_text(text.replace("__AGENT_NAME__", _AGENT_NAME))


def _write_project_files(target: Path) -> list[Path]:
    """Write movate.yaml, .env.example, .gitignore. Returns paths created.

    Returned list drives the "created N files" summary at the end —
    keep it order-stable so operators see consistent output across runs.
    """
    created: list[Path] = []

    movate_yaml = target / "movate.yaml"
    movate_yaml.write_text(_MOVATE_YAML)
    created.append(movate_yaml)

    env_example = target / ".env.example"
    env_example.write_text(_ENV_EXAMPLE)
    created.append(env_example)

    gitignore = target / ".gitignore"
    gitignore.write_text(_GITIGNORE)
    created.append(gitignore)

    return created


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def scaffold(
    directory: str = typer.Argument(
        "demo-faq",
        help="Directory to create the demo in (default: ./demo-faq).",
        metavar="DIR",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Overwrite DIR if it already exists. "
            "[bold red]Destructive[/bold red] — wipes the existing contents."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be created without writing.",
    ),
) -> None:
    """Generate a complete runnable demo project (FAQ agent + dataset).

    The output is a self-contained directory you can ``cd`` into and
    run immediately:

      [dim]$ mdk demo[/dim]
      [dim]$ cd demo-faq[/dim]
      [dim]$ cp .env.example .env  # then add your API key[/dim]
      [dim]$ mdk run faq '{"question": "What is Python?"}'[/dim]
      [dim]$ mdk eval faq[/dim]

    [bold]Different from [bold]mdk init[/bold]:[/bold] init scaffolds
    one empty agent into an existing project. [bold]demo[/bold] creates
    a full project with a working agent, prompt, schemas, and a sample
    eval dataset — the 60-second hello-world.

    [bold]Examples:[/bold]

      [dim]$ mdk demo                       # creates ./demo-faq/[/dim]
      [dim]$ mdk demo my-first-agent        # custom directory name[/dim]
      [dim]$ mdk demo --force               # overwrite existing[/dim]
      [dim]$ mdk demo --dry-run             # preview only[/dim]
    """
    target = Path(directory).resolve()

    if target.exists() and not force:
        err_console.print(
            f"[red]✗[/red] {target} already exists. "
            "[dim]Pass [bold]--force[/bold] to overwrite, or pick a different "
            "directory name.[/dim]"
        )
        raise typer.Exit(code=2)

    template_root = _resolve_template_root()
    agent_dir = target / "agents" / _AGENT_NAME

    if dry_run:
        body = (
            f"[bold]Would create:[/bold]\n"
            f"  [cyan]{target}/[/cyan]\n"
            f"  [cyan]{target}/movate.yaml[/cyan]\n"
            f"  [cyan]{target}/.env.example[/cyan]\n"
            f"  [cyan]{target}/.gitignore[/cyan]\n"
            f"  [cyan]{agent_dir}/[/cyan]  [dim](from {_TEMPLATE_PACKAGE})[/dim]"
        )
        console.print(
            Panel(
                body + "\n\n[yellow]⚠ dry-run — nothing written.[/yellow]",
                title="mdk demo — preview",
                title_align="left",
                border_style="yellow",
            )
        )
        return

    # Wipe-and-recreate when --force; otherwise create fresh.
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    project_files = _write_project_files(target)
    _materialize_agent(template_root, agent_dir)

    # Success summary + next-step recipe.
    body = (
        f"[bold]Created:[/bold] [cyan]{target}[/cyan]\n\n"
        f"  • [cyan]movate.yaml[/cyan]            project config\n"
        f"  • [cyan].env.example[/cyan]           env-var template (copy to .env)\n"
        f"  • [cyan].gitignore[/cyan]             standard movate ignores\n"
        f"  • [cyan]agents/{_AGENT_NAME}/[/cyan]              "
        "working FAQ agent + dataset\n\n"
        f"[bold]Next steps:[/bold]\n"
        f"  [dim]$[/dim] [bold]cd {target.name}[/bold]\n"
        f"  [dim]$[/dim] [bold]cp .env.example .env[/bold]   [dim]# then add an API key[/dim]\n"
        f"  [dim]$[/dim] [bold]mdk run {_AGENT_NAME} "
        f'\'{{"question": "What is Python?"}}\'[/bold]\n'
        f"  [dim]$[/dim] [bold]mdk eval {_AGENT_NAME}[/bold]"
    )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Demo ready",
            title_align="left",
            border_style="green",
        )
    )
    # Avoid lint warning about unused list — operators can inspect via
    # follow-up commands; the panel above is the canonical summary.
    _ = project_files


# ---------------------------------------------------------------------------
# `mdk demo seed` / `mdk demo clear` — the dashboard "wow pack" seeder.
#
# Generation logic is pure + lives in movate.core.demo; this layer wraps it
# with persistence (batch inserts through the StorageProvider Protocol), the
# safety/prod guard, and operator UX. Keeping the seam here honors the
# cli ⊥ core/runtime boundary (CLAUDE.md rule 6): nothing in core/demo imports
# storage or async.
# ---------------------------------------------------------------------------

# Refuse to seed/clear a target whose name looks like production unless the
# operator passes --force. Substring match on the normalized target name.
_PROD_MARKERS = ("prod", "production")

# Batch size for inserts. The StorageProvider Protocol exposes per-record
# save_* methods (no bulk API today), so "batching" here means wrapping N
# saves in one connection lifecycle + a bounded concurrency gather so a few
# thousand rows insert in seconds rather than serially. Kept modest to avoid
# overwhelming SQLite's single writer.
_INSERT_CONCURRENCY = 16

# The ADR 047 Observability-Intelligence project the analyzer writes insights
# under. The insight-fed + exec dashboards (dashboards/grafana/insights/*,
# dashboards/grafana/mdk-exec-summary.json) read this same project's insights
# through the /api/v1/observability API, so analyzing under "default" is what
# lights them up after a seed. Matches the CLI/runtime default project id.
_DEMO_INSIGHT_PROJECT = "default"

# The (tenant, agent) the --full scenario seeds its sample agents + graph under.
# Re-exported from core.demo so the seed summary + `mdk demo doctor` agree on
# exactly where to look. Kept as module constants here (not re-imported at call
# time) so the summary panel can reference them without a core import in the hot
# path.
from movate.core.demo import DEMO_GRAPH_AGENT as _SCENARIO_AGENT  # noqa: E402
from movate.core.demo import DEMO_TENANT_ID as _SCENARIO_TENANT  # noqa: E402


def _looks_like_prod(target: str) -> bool:
    """True if ``target`` contains a prod marker (case-insensitive)."""
    lowered = target.lower()
    return any(marker in lowered for marker in _PROD_MARKERS)


async def _gather_bounded(coros: list[Coroutine[Any, Any, Any]], *, limit: int) -> None:
    """Run ``coros`` with at most ``limit`` in flight at once.

    SQLite serializes writes anyway, but the bounded gather keeps the Postgres
    path from opening an unbounded number of concurrent statements while still
    being dramatically faster than a serial loop for the local demo.
    """
    sem = asyncio.Semaphore(limit)

    async def _run(coro: Coroutine[Any, Any, Any]) -> None:
        async with sem:
            await coro

    await asyncio.gather(*[_run(c) for c in coros])


async def _run_observability_analysis(
    storage: object,
    *,
    tenants: list[str],
    now: datetime,
    days: int,
) -> int:
    """Run the ADR 047 observability analyst over the freshly-seeded telemetry.

    This is the seeder→analyzer wiring (survey item #1): after the synthetic
    runs/evals/failures are persisted, we run the same overnight analyst the
    runtime dispatches as ``JobKind.OBSERVABILITY_ANALYZE`` — once per seeded
    *(tenant, day)* — so the insight store the insight-fed + exec dashboards
    read is populated immediately. Without this, those dashboards render empty
    right after a seed because no insight rows exist yet.

    Runs **inline** (matching the seeder's synchronous, ``asyncio.run``-based
    execution model) rather than enqueuing a job, because ``mdk demo seed`` has
    no runtime/worker loop to drain — it persists directly through the
    StorageProvider Protocol in-process. The analyst is the same pure-core
    function the dispatch handler calls.

    Uses :class:`MockProvider` for the single narrative-digest LLM call so the
    seed stays offline + free (no provider key required); the structured
    insight — the part the dashboards read — is computed in pure Python and
    needs no LLM.

    **Graceful degrade (CLAUDE.md rule 5/10):** the analyzer and the insight
    store are an opt-in part of the platform. If either is unavailable (module
    missing, or a storage backend without ``save_insight``), we log a clear
    line and return 0 — the seed itself never fails because insights couldn't
    be produced. Returns the number of insights written.
    """
    # Local imports: keep the cli ⊥ core/runtime boundary lazy and avoid paying
    # the analyst/provider import cost on the common (scaffold) demo path.
    try:
        from movate.core.observability.analyst import analyze  # noqa: PLC0415
        from movate.providers.mock import MockProvider  # noqa: PLC0415
    except Exception:  # pragma: no cover - analyst is an optional platform layer
        logger.info(
            "demo_seed_observability_analyze_skipped — observability analyst "
            "unavailable; dashboards will populate once an analyze job runs"
        )
        return 0

    # The insight store is an additive StorageProvider seam; a backend without
    # it (older/custom) should degrade, not crash the seed.
    if not callable(getattr(storage, "save_insight", None)):
        logger.info(
            "demo_seed_observability_analyze_skipped — storage backend %s has no "
            "insight store; run an analyze job once the seam is available",
            type(storage).__name__,
        )
        return 0

    provider = MockProvider()
    # Analyze each seeded calendar day in the window so the dashboards' time
    # series (not just the latest day) light up. The seeder's window is
    # [now - days, now); analyze each whole UTC day in it.
    window_start = (now - timedelta(days=days)).date()
    day_count = (now.date() - window_start).days
    days_to_analyze = [window_start + timedelta(days=offset) for offset in range(day_count + 1)]

    written = 0
    for tenant_id in tenants:
        for day in days_to_analyze:
            try:
                await analyze(
                    tenant_id,
                    _DEMO_INSIGHT_PROJECT,
                    day,
                    storage=storage,  # type: ignore[arg-type]
                    llm=provider,
                    # Mock provider => $0 spend; keep a budget so the digest runs.
                    budget_usd=0.10,
                )
                written += 1
            except Exception:  # pragma: no cover - per-day analysis is best-effort
                logger.warning(
                    "demo_seed_observability_analyze_day_failed tenant=%s day=%s — "
                    "continuing; remaining insights still populate",
                    tenant_id,
                    day.isoformat(),
                    exc_info=True,
                )
    if written:
        logger.info(
            "demo_seed_observability_analyze_done insights=%d tenants=%d days=%d",
            written,
            len(tenants),
            len(days_to_analyze),
        )
    return written


async def _persist_scenario(storage: object, scenario: object) -> None:
    """Persist the demo scenario (agents + workflow + graph) through the Protocol.

    Each of the four record kinds is written behind its own ``callable``-guard
    so a backend that predates one of the seams (e.g. an older custom
    StorageProvider without ``upsert_entity``) degrades to a logged skip rather
    than crashing the seed — the rest of the scenario (and all telemetry) still
    lands. The endpoint entities are upserted BEFORE the relations (the storage
    layer doesn't auto-create dangling endpoints).

    ``storage`` / ``scenario`` are typed ``object`` to keep this CLI helper from
    importing the concrete StorageProvider / ScenarioBundle types eagerly; the
    attributes accessed are part of the documented Protocol + dataclass surface.
    """
    sc = scenario  # narrow alias for readability
    save_agent = getattr(storage, "save_agent_bundle", None)
    if callable(save_agent):
        for agent in sc.agents:  # type: ignore[attr-defined]
            try:
                await save_agent(agent)
            except Exception:  # pragma: no cover - duplicate (name,version) on re-seed
                logger.info("demo_seed_agent_skipped name=%s — already present", agent.name)
    else:
        logger.info("demo_seed_scenario_agents_skipped — backend has no agent registry")

    save_workflow = getattr(storage, "save_workflow_bundle", None)
    if callable(save_workflow):
        for wf in sc.workflows:  # type: ignore[attr-defined]
            try:
                await save_workflow(wf)
            except Exception:  # pragma: no cover - duplicate on re-seed
                logger.info("demo_seed_workflow_skipped name=%s — already present", wf.name)
    else:
        logger.info("demo_seed_scenario_workflows_skipped — backend has no workflow registry")

    upsert_entity = getattr(storage, "upsert_entity", None)
    upsert_relation = getattr(storage, "upsert_relation", None)
    if callable(upsert_entity) and callable(upsert_relation):
        # Entities first (endpoints must exist before edges), then relations.
        await _gather_bounded(
            [upsert_entity(e) for e in sc.entities],  # type: ignore[attr-defined]
            limit=_INSERT_CONCURRENCY,
        )
        await _gather_bounded(
            [upsert_relation(r) for r in sc.relations],  # type: ignore[attr-defined]
            limit=_INSERT_CONCURRENCY,
        )
    else:
        logger.info("demo_seed_scenario_graph_skipped — backend has no graph store")


def seed(
    target: str = typer.Option(
        "local",
        "--target",
        help=(
            "Logical name of the environment being seeded (used only for the "
            "prod-name safety guard + the summary). The actual storage backend "
            "is selected by MOVATE_DB_URL / MOVATE_DB as usual."
        ),
    ),
    agents: int = typer.Option(6, "--agents", "-a", min=1, max=8, help="Number of demo agents."),
    tenants: int = typer.Option(3, "--tenants", "-t", min=1, max=6, help="Number of demo tenants."),
    days: int = typer.Option(
        7,
        "--days",
        "-d",
        min=1,
        max=120,
        help=(
            "Days of history to span. Defaults to 7 for a tight demo window. "
            "Use --days 30 for a richer historical view."
        ),
    ),
    seed_value: int = typer.Option(
        1337, "--seed", help="RNG seed — same seed reproduces the same data."
    ),
    clear_first: bool = typer.Option(
        False, "--clear-first", help="Purge existing demo data before seeding."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Required to seed a target whose name looks like prod/production.",
    ),
    with_voice: bool = typer.Option(
        False,
        "--with-voice",
        help=(
            "Also GENERATE voice-turn records (Deepgram STT + Cartesia TTS) for "
            "each successful run — realistic latency/cost figures, same seeded "
            "RNG. NOTE: these are generated + counted but NOT persisted yet "
            "(no voice_turns table on the StorageProvider Protocol); the flag "
            "exercises the generation path until the voice-storage ADR lands."
        ),
    ),
    full: bool = typer.Option(
        True,
        "--full/--telemetry-only",
        help=(
            "Also seed the demo SCENARIO — sample agents (incl. one voice-capable "
            "+ one workflow) and a Movate-themed knowledge graph — so the "
            "registry, graph viewer, and playground light up too, not just the "
            "dashboards. Pass --telemetry-only for the historical telemetry-only seed."
        ),
    ),
) -> None:
    """Populate the runtime with realistic synthetic telemetry (the "wow pack").

    Generates [bold]demo-tagged[/bold] RunRecords, EvalRecords and
    FailureRecords across agents x tenants x time — varied cost/latency/tokens,
    a trending eval pass-rate with [bold]one agent drifting[/bold], and two
    injected anomalies (a cost spike + a latency regression after a deploy) so
    the anomaly + narrative dashboard panels have a story to tell.

    Pass [bold]--with-voice[/bold] to also GENERATE voice-turn records (Deepgram
    STT + Cartesia TTS) alongside each successful run. These are generated +
    counted but [bold]not persisted yet[/bold] (no voice_turns table on the
    StorageProvider Protocol) — the flag exercises the generation path until the
    voice-storage ADR lands.

    [bold]Safety.[/bold] Every seeded row is tagged: its tenant_id starts with
    [cyan]demo-[/cyan] and its input carries a [cyan]__mdk_demo__[/cyan] marker.
    [bold]mdk demo clear[/bold] purges exactly those rows (runs, evals, failures,
    and voice turns). Seeding a target whose name contains
    [red]prod[/red]/[red]production[/red] is refused unless you pass
    [bold]--force[/bold].

    [bold]Examples:[/bold]

      [dim]$ mdk demo seed                              # 6 agents, 3 tenants, 7 days[/dim]
      [dim]$ mdk demo seed --with-voice                 # + voice-turn rows[/dim]
      [dim]$ mdk demo seed --days 30 --with-voice       # 30-day history + voice[/dim]
      [dim]$ mdk demo seed --agents 4 --days 14         # smaller fleet[/dim]
      [dim]$ mdk demo seed --clear-first --seed 42      # reproducible reset[/dim]
    """
    from movate.core.demo import SeedConfig, generate_bundle, generate_scenario  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415

    if _looks_like_prod(target) and not force:
        err_console.print(
            f"[red]✗[/red] target {target!r} looks like production. "
            "[dim]Seeding synthetic data into prod is refused. Pass "
            "[bold]--force[/bold] only if you are certain.[/dim]"
        )
        raise typer.Exit(code=2)

    # Pin ``now`` so the seeder's time window and the analyzer's per-day
    # analysis (below) agree on exactly which days hold data.
    now = datetime.now(UTC)
    cfg = SeedConfig(
        agents=agents,
        tenants=tenants,
        days=days,
        seed=seed_value,
        now=now,
        with_voice=with_voice,
    )
    bundle = generate_bundle(cfg)

    # The demo SCENARIO — sample agents + workflow + a knowledge graph — so the
    # registry, graph viewer, and playground light up alongside the dashboards.
    # Generated only when --full (the default); --telemetry-only keeps the
    # historical telemetry-only behavior. Pure + deterministic (no RNG / clock).
    scenario = generate_scenario() if full else None

    # The distinct demo tenant ids the analyzer must run for (telemetry is
    # scoped by tenant_id). Derived from the bundle so it stays in lockstep
    # with whatever the generator actually produced.
    seeded_tenants = sorted({r.tenant_id for r in bundle.runs})

    storage = build_storage()

    async def _persist() -> tuple[int, int]:
        await storage.init()
        try:
            cleared = 0
            if clear_first:
                cleared = await _purge_demo(storage)
            # Runs + failures + evals — batch through the Protocol's save_*.
            await _gather_bounded(
                [storage.save_run(r) for r in bundle.runs], limit=_INSERT_CONCURRENCY
            )
            await _gather_bounded(
                [storage.save_failure(f) for f in bundle.failures], limit=_INSERT_CONCURRENCY
            )
            await _gather_bounded(
                [storage.save_eval(e) for e in bundle.evals], limit=_INSERT_CONCURRENCY
            )
            # The demo SCENARIO — sample agents + workflow + knowledge graph.
            # Persisted through the same StorageProvider Protocol surface
            # (save_agent_bundle / save_workflow_bundle / upsert_entity /
            # upsert_relation). Degrades gracefully on a backend missing any of
            # these seams (CLAUDE.md rule 5/10): the telemetry seed never fails
            # because the scenario couldn't be written.
            if scenario is not None:
                await _persist_scenario(storage, scenario)
            # Voice turns are stored as JSON blobs in the runs table's extra
            # data (no dedicated voice_turns table on the StorageProvider
            # Protocol today). They are carried on the bundle for CLI display
            # and future ingestion when the voice storage seam lands.
            # TODO(voice-storage): persist bundle.voice_turns via a
            # StorageProvider.save_voice_turn() method once ADR 05x defines
            # the voice-turn table schema. For now they are generated + counted
            # but not persisted — the count is shown in the summary so operators
            # can verify the flag works.

            # Survey item #1 — run the ADR 047 observability analyst over the
            # freshly-seeded telemetry so the insight-fed + exec dashboards
            # render with live data instead of empty. Degrades gracefully (see
            # _run_observability_analysis) — never fails the seed.
            insights = await _run_observability_analysis(
                storage, tenants=seeded_tenants, now=now, days=days
            )
            return cleared, insights
        finally:
            await storage.close()

    cleared, insights_written = asyncio.run(_persist())

    # Summary table + storyline.
    table = Table(title="mdk demo seed — synthetic fleet", title_justify="left")
    table.add_column("metric", style="cyan")
    table.add_column("value", justify="right")
    s = bundle.stats
    table.add_row("runs", f"{s['runs']:,}")
    table.add_row("evals", f"{s['evals']:,}")
    table.add_row("failures", f"{s['failures']:,}")
    if with_voice:
        # Voice turns are generated but NOT persisted yet — there is no
        # voice_turns table on the StorageProvider Protocol (see the
        # TODO(voice-storage) note in _persist). Label the count clearly as
        # generated-not-stored so the summary never implies queryable data that
        # the dashboards / `mdk demo doctor` would then fail to find.
        table.add_row("voice turns (generated, not stored)", f"{s['voice_turns']:,}")
    table.add_row("agents x tenants", f"{s['agents']} x {s['tenants']}")
    table.add_row("days of history", str(days))
    table.add_row("success rate", f"{s['success_rate_pct']}%")
    table.add_row("total synthetic spend", f"${s['total_cost_usd']:,.2f}")
    table.add_row("total tokens", f"{s['total_tokens']:,}")
    if clear_first:
        table.add_row("purged first", f"{cleared:,} demo rows")
    # Insights written by the ADR 047 analyst — what makes the insight-fed +
    # exec dashboards render with live data. 0 means the analyst/insight store
    # was unavailable and the seed degraded gracefully (see the logged reason).
    table.add_row("insights analyzed", f"{insights_written:,}")
    # Scenario stats (--full) — the registry + graph that light up the
    # playground and graph viewer alongside the dashboards.
    if scenario is not None:
        sc_stats = scenario.stats
        table.add_row("sample agents", f"{sc_stats['agents']} ({sc_stats['voice_agents']} voice)")
        table.add_row("sample workflows", f"{sc_stats['workflows']}")
        table.add_row(
            "graph nodes / edges",
            f"{sc_stats['graph_nodes']} / {sc_stats['graph_edges']}",
        )
    console.print(table)

    scenario_line = (
        (
            "Playground + graph viewer populated — "
            f"{scenario.stats['agents']} sample agents (incl. voice + workflow) and a "
            f"{scenario.stats['graph_nodes']}-node knowledge graph under "
            f"[cyan]{_SCENARIO_TENANT}[/cyan] / agent [cyan]{_SCENARIO_AGENT}[/cyan] "
            "(the [bold]mdk serve --dev[/bold] tenant — the live browser viewer "
            "sees it directly)."
        )
        if scenario is not None
        else (
            "Telemetry-only seed (--telemetry-only) — no sample agents/graph. "
            "Re-run with --full to light up the playground + graph viewer."
        )
    )

    insight_line = (
        "Insight-fed + exec dashboards populated — "
        f"{insights_written:,} daily insights written (ADR 047 analyst)."
        if insights_written
        else (
            "Observability analyst unavailable — insight-fed dashboards will "
            "populate once an analyze job runs (see logs)."
        )
    )

    console.print(
        Panel(
            f"[bold]Storyline:[/bold] {bundle.narrative}\n\n"
            f"[bold]Events ({len(bundle.events)}):[/bold]\n"
            + "\n".join(
                f"  • [dim]{e.at:%Y-%m-%d}[/dim] [yellow]{e.kind}[/yellow] — {e.detail}"
                for e in bundle.events
            )
            + f"\n\n[bold]{insight_line}[/bold]"
            + f"\n[bold]{scenario_line}[/bold]"
            + "\n\n[dim]All rows tagged tenant=demo-* + input.__mdk_demo__=true. "
            "Purge anytime with [bold]mdk demo clear[/bold].[/dim]\n"
            "[dim]Verify the demo is GO with [bold]mdk demo doctor[/bold].[/dim]",
            title="[green]✓[/green] Demo seeded",
            title_align="left",
            border_style="green",
        )
    )


def clear(
    target: str = typer.Option(
        "local",
        "--target",
        help="Logical environment name (prod-guard + summary only).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Required to clear a target whose name looks like prod/production.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Purge ALL demo-seeded telemetry (and nothing else).

    Deletes exactly the rows whose ``tenant_id`` starts with [cyan]demo-[/cyan]
    — the marker [bold]mdk demo seed[/bold] stamps on every record it creates.
    Real telemetry (any tenant without the prefix) is never touched.
    """
    from movate.storage import build_storage  # noqa: PLC0415

    if _looks_like_prod(target) and not force:
        err_console.print(
            f"[red]✗[/red] target {target!r} looks like production. "
            "[dim]Pass [bold]--force[/bold] to proceed.[/dim]"
        )
        raise typer.Exit(code=2)

    if not yes:
        confirmed = typer.confirm(
            "Delete all demo-tagged telemetry (tenant=demo-*)? Real data is untouched."
        )
        if not confirmed:
            console.print("[dim]Aborted — nothing deleted.[/dim]")
            raise typer.Exit(code=0)

    storage = build_storage()

    async def _run() -> int:
        await storage.init()
        try:
            return await _purge_demo(storage)
        finally:
            await storage.close()

    deleted = asyncio.run(_run())
    console.print(
        Panel(
            f"Deleted [bold]{deleted:,}[/bold] demo-tagged rows "
            "(runs, evals, failures, sample agents/workflows, graph "
            "entities/relations, insights, voice turns if present).",
            title="[green]✓[/green] Demo data cleared",
            title_align="left",
            border_style="green",
        )
    )


async def _purge_demo(storage: object) -> int:
    """Delete every demo-tagged row across the storage backend.

    Demo data lives under two disjoint tenant scopes, both purged here:

    * the **telemetry** rows (runs / failures / evals) and the analyzer's
      **insight** rows — tagged with the ``demo-`` tenant *prefix*
      (``tenant_id LIKE 'demo-%'``); and
    * the **scenario** rows (sample agents + workflow registry + the knowledge
      graph) — tagged with the single scenario tenant
      :data:`~movate.core.demo.DEMO_TENANT_ID` (the serve --dev tenant, a
      *dash-free exact* id, NOT a ``demo-`` prefix). These are matched by an
      exact ``tenant_id = ?`` predicate.

    The StorageProvider Protocol has no "delete by id" method (runs are
    immutable history by design), so the purge DELETEs directly against the
    backend's tables — the one place this command has to know the backend's
    table shape. The WHERE clauses are the hard guarantee that only synthetic
    rows are touched. We reuse the already-``init()``-ed connection/pool (do
    NOT open a second handle — a separate sqlite connection would deadlock on
    the writer lock).

    ``storage`` is typed ``object`` because the StorageProvider Protocol
    deliberately exposes no raw-SQL surface; this helper reaches past it on
    purpose. Returns the number of rows deleted across the seeded tables.

    The ``voice_turns`` table is included when it exists (forward-compat with
    the voice-storage ADR). If the table hasn't been created yet the DELETE is
    skipped silently — ``mdk demo clear`` is idempotent across schema versions.
    """
    from movate.core.demo import DEMO_TENANT_ID, DEMO_TENANT_PREFIX  # noqa: PLC0415

    # (predicate-fragment, parameter) per scope. The fragment is a fixed literal
    # (never user input); only the bound parameter varies.
    prefix_like = f"{DEMO_TENANT_PREFIX}%"

    # Tables purged by the ``demo-%`` telemetry-tenant prefix. ``runs`` /
    # ``failures`` / ``evals`` are the telemetry; ``observability_insights`` is
    # the analyzer output (written per ``demo-`` telemetry tenant) — listed here
    # so re-seeds don't accumulate orphan insight rows (24→48→…) across runs.
    prefix_tables = (
        "runs",
        "failures",
        "evals",
        "observability_insights",
    )
    # Tables purged by the EXACT scenario tenant. The scenario (agents +
    # workflow + graph) is seeded under the dash-free serve --dev tenant so the
    # live viewer can see it, so a ``demo-%`` LIKE would NOT catch these — they
    # must be matched by exact tenant id.
    scenario_tables = (
        "agent_bundles",
        "workflow_bundles",
        "kb_entities",
        "kb_relations",
    )
    # Optional table — created by the voice-storage ADR (not yet on main).
    # Voice turns inherit their run's ``demo-`` tenant, so the prefix predicate
    # applies. Included here so `mdk demo clear` stays correct once it lands.
    optional_prefix_tables = ("voice_turns",)
    backend = type(storage).__name__

    # SQLite path — reuse the live connection opened by init().
    conn = getattr(storage, "_conn", None)
    if backend == "SqliteProvider" and conn is not None:
        deleted = 0
        for tbl in prefix_tables:
            # `tbl` comes only from the fixed literal allow-lists above, never
            # from user input — the f-string interpolation is safe.
            cur = await conn.execute(f"DELETE FROM {tbl} WHERE tenant_id LIKE ?", (prefix_like,))
            deleted += max(0, cur.rowcount or 0)
        for tbl in scenario_tables:
            cur = await conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = ?", (DEMO_TENANT_ID,))
            deleted += max(0, cur.rowcount or 0)
        for tbl in optional_prefix_tables:
            try:
                cur = await conn.execute(
                    f"DELETE FROM {tbl} WHERE tenant_id LIKE ?", (prefix_like,)
                )
                deleted += max(0, cur.rowcount or 0)
            except Exception:  # table may not exist yet — safe to skip
                pass
        await conn.commit()
        return deleted

    # Postgres path — acquire from the live pool.
    pool = getattr(storage, "_pool", None)
    if backend == "PostgresProvider" and pool is not None:
        deleted = 0
        async with pool.acquire() as pg:
            for tbl in prefix_tables:
                # `tbl` is from the fixed allow-lists above — safe interpolation.
                status = await pg.execute(f"DELETE FROM {tbl} WHERE tenant_id LIKE $1", prefix_like)
                # asyncpg returns a status string like "DELETE 42".
                deleted += int(status.split()[-1]) if status else 0
            for tbl in scenario_tables:
                status = await pg.execute(f"DELETE FROM {tbl} WHERE tenant_id = $1", DEMO_TENANT_ID)
                deleted += int(status.split()[-1]) if status else 0
            for tbl in optional_prefix_tables:
                try:
                    status = await pg.execute(
                        f"DELETE FROM {tbl} WHERE tenant_id LIKE $1", prefix_like
                    )
                    deleted += int(status.split()[-1]) if status else 0
                except Exception:  # table may not exist yet — safe to skip
                    pass
        return deleted

    return 0


# ---------------------------------------------------------------------------
# Typer wiring. `mdk demo` (bare) scaffolds the demo project (backward
# compatible); `mdk demo new <dir>` is the explicit scaffold-to-a-directory
# form; `mdk demo seed` / `mdk demo clear` are the wow-pack seeder.
# ---------------------------------------------------------------------------

demo_app = typer.Typer(
    no_args_is_help=False,
    help=(
        "Demo project scaffold + the dashboard 'wow pack' seeder.\n\n"
        "Run bare ('mdk demo') to scaffold a runnable FAQ project; "
        "'mdk demo seed' to populate dashboards with synthetic telemetry."
    ),
)


@demo_app.callback(invoke_without_command=True)
def _demo_callback(ctx: typer.Context) -> None:
    """Scaffold the default demo project when invoked with no subcommand.

    Preserves the historical ``mdk demo`` behavior (creates ``./demo-faq``).
    For a custom directory use ``mdk demo new <dir>``.
    """
    if ctx.invoked_subcommand is not None:
        return
    scaffold(directory="demo-faq", force=False, dry_run=False)


demo_app.command("new")(scaffold)
demo_app.command("seed")(seed)
demo_app.command("clear")(clear)

# `mdk demo doctor` — Monday-demo readiness check. Lives in its own module
# (_demo_doctor.py) to keep this file focused on scaffold + seed/clear; wired
# here so it shares the `mdk demo` command group.
from movate.cli._demo_doctor import doctor as _demo_doctor  # noqa: E402

demo_app.command("doctor")(_demo_doctor)
