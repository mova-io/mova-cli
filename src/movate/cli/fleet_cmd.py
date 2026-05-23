"""``mdk fleet`` — a READ-ONLY cross-target view of deployed runtimes.

Operators typically have several deployment targets registered in
``~/.movate/config.yaml`` (local, dev, staging, prod, per-customer
runtimes…). Each ``mdk`` subcommand already talks to ONE target at a
time via ``--target``; there was no single "what's deployed where /
is it healthy / what version / how many agents" view. ``mdk fleet``
is that view.

Two subcommands ship:

* ``mdk fleet status`` (also the default action when ``mdk fleet`` is
  run bare) — iterate EVERY configured target and, concurrently, gather:

    - liveness + version from ``GET /healthz`` (unauthenticated)
    - deployed-agent count + names from ``GET /api/v1/agents``
      (authenticated with the target's bearer token)

  Renders a ``rich`` table (Target | Env | URL | Health | Version |
  Agents), marking the active target. ``--json`` emits a machine-readable
  array instead. A dead target shows a dim "unreachable" rather than
  stalling or crashing the whole view — each target is probed under a
  short per-target timeout and its exceptions are caught in isolation.

* ``mdk fleet logs <target>`` — tail recent logs for a target's API
  Container App by shelling out to ``az containerapp logs show``
  (READ-ONLY). Requires the target to carry Azure config
  (``azure_subscription`` / ``azure_resource_group`` / ``azure_env``).

**This command is READ-ONLY by design.** It never restarts, scales,
rolls back, or otherwise mutates remote/Azure state — those touch the
deployment lifecycle and are deliberately deferred to a future
ADR-gated PR. The only ``az`` call made is ``logs show`` (a read).

Tokens never appear in output: ``status`` sends each target's bearer in
an ``Authorization`` header but only ever prints health/version/agent
metadata; ``logs`` shells out to ``az`` which authenticates from the
operator's own ``az login`` session, not from any movate-held secret.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field

import typer

from movate.cli._console import error, hint
from movate.core.user_config import (
    TargetConfig,
    UserConfig,
    load_user_config,
)

# Per-target HTTP timeout for the concurrent `fleet status` gather. Kept
# short on purpose: one dead/slow target must not stall the whole view.
# Health + agents are probed within this budget per target; a slow target
# simply renders "unreachable" while the rest of the fleet still shows.
_PER_TARGET_TIMEOUT_S = 5.0

# Default number of recent log lines `fleet logs` requests from ACA.
_DEFAULT_TAIL = 50

# How many agent names to preview inline in the status table's Agents
# column before collapsing the rest into "+N more".
_AGENT_PREVIEW_N = 3

# 2xx success window for the unauthenticated /healthz + authenticated
# /api/v1/agents probes. 401 from the agents endpoint is treated as
# "reachable but unauthorized" rather than a hard crash.
_HTTP_OK = 200
_HTTP_REDIRECT = 300
_HTTP_UNAUTHORIZED = 401


fleet_app = typer.Typer(
    name="fleet",
    help=(
        "READ-ONLY cross-target view of deployed runtimes. "
        "Run [bold]mdk fleet[/bold] with no subcommand for the status table."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)


@dataclass
class TargetStatus:
    """The gathered, render-ready status for one target.

    Populated by :func:`_probe_target` (one instance per configured
    target). All network failures collapse into ``reachable=False`` +
    an optional ``error`` note so the renderers never have to special-case
    exceptions — they read these plain fields.
    """

    name: str
    url: str
    env: str | None
    active: bool
    reachable: bool = False
    status: str | None = None
    version: str | None = None
    agent_count: int | None = None
    agent_names: list[str] = field(default_factory=list)
    authorized: bool = True
    error: str | None = None


def _agents_from_body(body: object) -> tuple[int, list[str]]:
    """Pull (count, names) out of a ``GET /api/v1/agents`` response body.

    The runtime returns ``{"agents": [{"name", "version", …}], "count"}``.
    We trust ``count`` when present but fall back to the list length so a
    schema tweak that drops ``count`` still yields a sensible number.
    """
    if not isinstance(body, dict):
        return 0, []
    agents = body.get("agents", [])
    names = [a.get("name", "?") for a in agents if isinstance(a, dict)]
    raw_count = body.get("count")
    count = int(raw_count) if isinstance(raw_count, int) else len(names)
    return count, names


async def _probe_target(
    *,
    name: str,
    target: TargetConfig,
    bearer: str | None,
    active: bool,
    timeout_s: float,
) -> TargetStatus:
    """Probe one target's /healthz + /api/v1/agents within ``timeout_s``.

    Never raises: any network / timeout / decode error collapses into a
    :class:`TargetStatus` with ``reachable=False``. ``/healthz`` is
    unauthenticated; the agents probe only fires if a bearer is available
    and health succeeded, and a 401 there is reported as "reachable but
    unauthorized" (so the operator sees the runtime is up but their token
    for it is missing/stale).
    """
    import httpx  # noqa: PLC0415  -- lazy: keep CLI import time down

    base_url = target.url.rstrip("/")
    st = TargetStatus(
        name=name,
        url=target.url,
        env=target.azure_env,
        active=active,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            # --- liveness + version (unauthenticated) ---------------------
            health = await client.get(f"{base_url}/healthz")
            if not (_HTTP_OK <= health.status_code < _HTTP_REDIRECT):
                st.error = f"healthz HTTP {health.status_code}"
                return st
            st.reachable = True
            hbody = health.json()
            if isinstance(hbody, dict):
                st.status = hbody.get("status")
                st.version = hbody.get("version")

            # --- deployed-agent inventory (authenticated) -----------------
            # Skip when no bearer is resolvable — the runtime is reachable,
            # we just can't enumerate its agents. Don't fail the whole row.
            if not bearer:
                st.authorized = False
                st.error = "no bearer (agents not listed)"
                return st

            agents_resp = await client.get(
                f"{base_url}/api/v1/agents",
                headers={"Authorization": f"Bearer {bearer}"},
            )
            if agents_resp.status_code == _HTTP_UNAUTHORIZED:
                st.authorized = False
                st.error = "agents 401 (bearer rejected)"
                return st
            if not (_HTTP_OK <= agents_resp.status_code < _HTTP_REDIRECT):
                st.error = f"agents HTTP {agents_resp.status_code}"
                return st
            st.agent_count, st.agent_names = _agents_from_body(agents_resp.json())
    except httpx.HTTPError as exc:
        # Timeouts, connection refused, DNS failures — all land here.
        # Keep whatever we managed to gather (health may have set version).
        st.error = type(exc).__name__
    except ValueError as exc:
        # JSON decode failure on an otherwise-200 response.
        st.error = f"bad JSON: {exc}"

    return st


def _resolve_bearer_quiet(target: TargetConfig) -> str | None:
    """Best-effort bearer resolution for ``fleet status`` — never raises.

    Mirrors how ``kb_cmd._resolve_target_bearer`` reads the token from the
    env var named by ``target.key_env`` (and the same autoloaded credential
    store, since that store hydrates ``os.environ`` at import time). Unlike
    that helper, a missing token is NOT fatal here: the fleet view should
    still show a target's health even when we can't authenticate to list
    its agents, so we return ``None`` and let the caller render
    "unauthorized" rather than exiting.
    """
    import os  # noqa: PLC0415

    token = os.environ.get(target.key_env, "").strip()
    return token or None


async def _gather_fleet(cfg: UserConfig, *, timeout_s: float) -> list[TargetStatus]:
    """Probe every configured target concurrently, one task each.

    ``asyncio.gather`` fans out the per-target probes so the whole view
    completes in ~one ``timeout_s`` window rather than the sum of all
    targets. Each :func:`_probe_target` swallows its own errors, so the
    gather can't be tripped by a single dead target. Results are returned
    in stable (sorted-by-name) order for deterministic rendering.
    """
    names = sorted(cfg.targets)
    tasks = [
        _probe_target(
            name=name,
            target=cfg.targets[name],
            bearer=_resolve_bearer_quiet(cfg.targets[name]),
            active=(name == cfg.active),
            timeout_s=timeout_s,
        )
        for name in names
    ]
    return list(await asyncio.gather(*tasks))


def _status_as_json(rows: list[TargetStatus]) -> list[dict[str, object]]:
    """Serializable shape for ``fleet status --json``.

    One object per target. Never includes the bearer token — only
    health/version/agent metadata. ``agents`` carries both the count and
    the names so downstream tooling (dashboards / CI gates) doesn't need a
    second call.
    """
    return [
        {
            "target": r.name,
            "active": r.active,
            "env": r.env,
            "url": r.url,
            "reachable": r.reachable,
            "status": r.status,
            "version": r.version,
            "authorized": r.authorized,
            "agent_count": r.agent_count,
            "agents": r.agent_names,
            "error": r.error,
        }
        for r in rows
    ]


def _render_status_table(rows: list[TargetStatus]) -> None:
    """Render the fleet status table to stdout.

    Columns: Target | Env | URL | Health | Version | Agents. The active
    target's name carries a green ``●``. Unreachable targets render a dim
    "unreachable" in Health and ``—`` placeholders elsewhere so the table
    stays aligned and no row crashes the view.
    """
    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    stdout = Console()
    table = Table(title="movate fleet")
    table.add_column("Target", style="bold", no_wrap=True)
    table.add_column("Env", no_wrap=True)
    table.add_column("URL", overflow="fold")
    table.add_column("Health", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("Agents", overflow="fold")

    for r in rows:
        name_cell = f"{r.name} [green]●[/green]" if r.active else r.name
        env_cell = r.env or "[dim]—[/dim]"

        if not r.reachable:
            health_cell = "[dim]unreachable[/dim]"
            version_cell = "[dim]—[/dim]"
            agents_cell = "[dim]—[/dim]"
        else:
            status = r.status or "ok"
            health_cell = "[green]up[/green]" if status == "ok" else f"[yellow]{status}[/yellow]"
            version_cell = r.version or "[dim]?[/dim]"
            if not r.authorized:
                # Reachable runtime, but we couldn't authenticate to list
                # its agents. Show why rather than a misleading "0".
                agents_cell = "[yellow]unauthorized[/yellow]"
            elif r.agent_count is None:
                agents_cell = "[dim]—[/dim]"
            else:
                preview = ", ".join(r.agent_names[:_AGENT_PREVIEW_N])
                if len(r.agent_names) > _AGENT_PREVIEW_N:
                    preview += f", +{len(r.agent_names) - _AGENT_PREVIEW_N} more"
                agents_cell = f"{r.agent_count}" + (f" [dim]({preview})[/dim]" if preview else "")

        table.add_row(name_cell, env_cell, r.url, health_cell, version_cell, agents_cell)

    stdout.print(table)


@fleet_app.callback()
def fleet_root(ctx: typer.Context) -> None:
    """READ-ONLY cross-target view of deployed runtimes.

    Run [bold]mdk fleet[/bold] (no subcommand) for the status table, or
    use [bold]status[/bold] / [bold]logs[/bold] explicitly.
    """
    if ctx.invoked_subcommand is None:
        # Bare `mdk fleet` defaults to the status table.
        _run_status(json_output=False)


@fleet_app.command("status")
def status(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a machine-readable JSON array instead of the table (pipe to jq).",
    ),
) -> None:
    """Show health, version, and agent inventory for every configured target.

    Probes every target in [bold]~/.movate/config.yaml[/bold] concurrently
    with a short per-target timeout, so one dead target doesn't stall the
    view. READ-ONLY — issues only ``GET /healthz`` + ``GET /api/v1/agents``.

    [bold]Examples:[/bold]

      [dim]# Human-readable table, active target marked with ●[/dim]
      $ mdk fleet

      [dim]# Machine-readable, e.g. CI health gate[/dim]
      $ mdk fleet status --json | jq '.[] | select(.reachable == false)'
    """
    _run_status(json_output=json_output)


def _run_status(*, json_output: bool) -> None:
    """Shared body for ``fleet status`` and the bare ``mdk fleet`` default."""
    from movate.core.user_config import UserConfigError  # noqa: PLC0415

    try:
        cfg = load_user_config()
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    if not cfg.targets:
        if json_output:
            from rich.console import Console  # noqa: PLC0415

            Console().print_json(json.dumps([]))
            return
        hint("[dim]no targets registered — run `mdk config add-target` first[/dim]")
        return

    rows = asyncio.run(_gather_fleet(cfg, timeout_s=_PER_TARGET_TIMEOUT_S))

    if json_output:
        from rich.console import Console  # noqa: PLC0415

        Console().print_json(json.dumps(_status_as_json(rows)))
        return

    _render_status_table(rows)


@fleet_app.command("logs")
def logs(
    target: str = typer.Argument(..., help="Name of a registered target (must have Azure config)."),
    tail: int = typer.Option(
        _DEFAULT_TAIL,
        "--tail",
        help="Number of recent log lines to show.",
    ),
) -> None:
    """Tail recent logs for a target's API Container App (READ-ONLY).

    Shells out to ``az containerapp logs show`` for the
    ``movate-{azure_env}-api`` app — the same name derivation
    ``mdk deploy`` uses. Authenticates from your own ``az login`` session;
    no movate-held secret is passed. The target must carry Azure config
    (``azure_subscription`` / ``azure_resource_group`` / ``azure_env``);
    a pure read-only HTTP target (no deploy config) can't be log-tailed.

    [bold]Example:[/bold]

      $ mdk fleet logs prod --tail 100
    """
    from movate.cli.deploy import _run_az  # noqa: PLC0415  -- reuse shared az runner
    from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    # `az containerapp logs show` needs the subscription, resource group,
    # and env (to derive the app name). A target registered for HTTP-only
    # use (submit/inspect via bearer) won't have these — fail clearly.
    missing = [
        label
        for label, value in (
            ("azure_subscription", target_cfg.azure_subscription),
            ("azure_resource_group", target_cfg.azure_resource_group),
            ("azure_env", target_cfg.azure_env),
        )
        if not value
    ]
    if missing:
        error(
            f"target {target_name!r} has no Azure config ({', '.join(missing)}). "
            f"`fleet logs` needs it to locate the Container App. "
            f"Add it: `mdk config add-target {target_name} ... "
            f"--azure-subscription <id> --azure-resource-group <rg> --azure-env <env>`."
        )
        raise typer.Exit(code=2)

    if shutil.which("az") is None:
        error(
            "`az` CLI not found on PATH. Install the Azure CLI and run "
            "`az login` to tail Container App logs."
        )
        raise typer.Exit(code=2)

    # Same name derivation as deploy.py's `_build_plan`: movate-{env}-api.
    app_name = f"movate-{target_cfg.azure_env}-api"
    hint(f"[dim]tailing logs for {app_name} on {target_name} (last {tail} lines)…[/dim]")

    # READ-ONLY az read: `logs show`. Never `update`/`restart`/`scale`/`exec`.
    cmd = [
        "az",
        "containerapp",
        "logs",
        "show",
        "--name",
        app_name,
        "--resource-group",
        str(target_cfg.azure_resource_group),
        "--subscription",
        str(target_cfg.azure_subscription),
        "--tail",
        str(tail),
    ]
    out = _run_az(cmd, what=f"containerapp logs show {app_name}")
    # `_run_az` captures stdout; print it verbatim so operators see the log lines.
    if out:
        from rich.console import Console  # noqa: PLC0415

        Console().print(out, end="" if out.endswith("\n") else "\n")


__all__ = ["fleet_app"]
