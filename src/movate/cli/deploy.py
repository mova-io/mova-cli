"""``movate deploy`` — one-command Azure Container Apps deploy.

Builds the runtime image in ACR, pushes a versioned tag, updates both
the API + worker Container Apps to the new image, then polls
``GET /healthz`` until the new revision is live.

Integration surface: the ``az`` CLI (Azure SDKs would add 100MB+ of
deps; operators already have ``az`` installed for everything else).
Shell-out is intentional — `az acr build` runs the actual docker
build in ACR, which means deploy works without local Docker installed.

Auth: inherits from whatever ``az login`` (or `az login --service-principal`)
session the caller has. GitHub Actions wires this via federated OIDC —
see ``.github/workflows/deploy.yml``.

Image-tag strategy: ``<version>-<git-sha-short>`` by default. The
version is read from ``movate.__version__``; the sha from
``git rev-parse --short HEAD``. ``--image-tag <tag>`` overrides for
rollbacks / redeploys of an existing image.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console

import movate
from movate.cli._console import error, hint, success
from movate.cli._progress import spinner
from movate.core.user_config import (
    TargetConfig,
    UserConfigError,
    resolve_target,
)
from movate.notify import DeployEvent, notify_deploy_success

err = Console(stderr=True)
stdout = Console()


def deploy(  # noqa: PLR0912 — orchestrator; branch count reflects mode dispatch + flag combinations
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target (from `movate config list-targets`). Omit for active.",
    ),
    image_tag: str = typer.Option(
        None,
        "--image-tag",
        help=(
            "Explicit image tag (e.g. movate:0.5.0-abc1234). Defaults to "
            "<version>-<git-sha-short>. Use to redeploy an existing image."
        ),
    ),
    skip_build: bool = typer.Option(
        False,
        "--skip-build",
        help=(
            "Don't run `az acr build`; just update Container Apps to --image-tag. "
            "Useful for rollbacks: --skip-build --image-tag movate:0.5.0-prev_sha."
        ),
    ),
    no_wait: bool = typer.Option(
        False,
        "--no-wait",
        help=(
            "Update Container Apps and exit immediately, without polling "
            "/healthz. CI fire-and-forget mode."
        ),
    ),
    wait_timeout: float = typer.Option(
        300.0,
        "--wait-timeout",
        help="Max seconds to poll /healthz for the new version. Exits 124 on timeout.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the `az` commands that would run; don't execute.",
    ),
    only: str = typer.Option(
        None,
        "--only",
        help=(
            "Update only one Container App: 'api' or 'worker'. Default updates "
            "both. Useful when a code change is API-only or worker-only."
        ),
    ),
    notify: bool = typer.Option(
        False,
        "--notify",
        help=(
            "On successful deploy, fire outbound notifications. Reads "
            "[bold]TELEGRAM_BOT_TOKEN[/bold] + [bold]TELEGRAM_CHAT_ID[/bold] "
            "for a Telegram message, [bold]MOVATE_DEPLOY_WEBHOOK[/bold] for "
            "a generic JSON POST (Slack/Teams/Discord/custom). Both fire if "
            "both are configured. Failures are non-fatal — deploy stays green."
        ),
    ),
    mode: str = typer.Option(
        "auto",
        "--mode",
        help=(
            "Deploy mode. [bold]runtime[/bold]: build + roll the movate "
            "container image (requires Dockerfile in cwd — i.e. the "
            "movate-cli source tree). [bold]agents[/bold]: upload the "
            "customer agents under [bold]agents/*/[/bold] to the "
            "deployed runtime (requires project.yaml in cwd or an "
            "ancestor; doesn't rebuild the image). [bold]auto[/bold] "
            "(default): pick by what's in cwd — Dockerfile → runtime, "
            "project.yaml → agents."
        ),
    ),
    diff: bool = typer.Option(
        False,
        "--diff",
        help=(
            "Preview what would change without uploading. Compares each local "
            "agent's [bold]agent.yaml[/bold] hash against the deployed version "
            "and prints a table of new / changed / unchanged agents. Exits 0; "
            "nothing is uploaded. Only applies to agents-mode."
        ),
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help=(
            "List all live agents on the target runtime. Calls "
            "[bold]GET /api/v1/agents[/bold] and renders a table of "
            "name / version / created-at. Nothing is deployed. "
            "Only applies to agents-mode."
        ),
    ),
    no_auto_recover: bool = typer.Option(
        False,
        "--no-auto-recover",
        help=(
            "Disable the 401 auto-recovery path. Default behavior: on a "
            "401 from the runtime, mint a fresh key inside the Container "
            "App and retry once. Use this flag in CI or when debugging "
            "key-storage issues directly."
        ),
    ),
) -> None:
    """Build the runtime image + roll out to Azure Container Apps.

    [bold]Examples:[/bold]

      [dim]# Default — build + push + update both apps + verify[/dim]
      $ movate deploy --target prod

      [dim]# CI fire-and-forget (don't block on /healthz)[/dim]
      $ movate deploy --target prod --no-wait

      [dim]# Redeploy an existing image (rollback to prev sha)[/dim]
      $ movate deploy --target prod --skip-build --image-tag movate:0.5.0-abc1234

      [dim]# Worker-only update (e.g. dispatch logic change)[/dim]
      $ movate deploy --target prod --only worker

      [dim]# Plan the deploy without running it[/dim]
      $ movate deploy --target prod --dry-run

    [bold]Requires:[/bold]

      * ``az`` CLI installed and authenticated (``az login``).
      * Target registered with full Azure config:
        ``movate config add-target ... --azure-subscription ... --azure-resource-group ...
          --azure-acr ... --azure-env ...``
    """
    if not dry_run and shutil.which("az") is None and mode != "agents":
        # `az` is only required for the runtime path (ACR build +
        # Container App roll). Agents-mode only talks to /api/v1/agents
        # over HTTPS and doesn't need the Azure CLI.
        err.print(
            "[red]✗[/red] `az` CLI not found on PATH. "
            "Install: https://learn.microsoft.com/cli/azure/install-azure-cli"
        )
        raise typer.Exit(code=2)

    # Mode dispatch — `auto` is the default and picks by what's in cwd:
    # Dockerfile (we're in the movate-cli source tree) → runtime; or
    # a project.yaml on the path up (we're in a customer project) →
    # agents. Operators can force either mode with `--mode`.
    if mode not in ("auto", "runtime", "agents"):
        error(f"--mode must be 'auto', 'runtime', or 'agents'; got {mode!r}")
        raise typer.Exit(code=2)
    resolved_mode = _resolve_deploy_mode(mode=mode, cwd=Path.cwd())
    if resolved_mode == "agents":
        if status:
            _deploy_status(target=target)
            return
        _deploy_agents(
            target=target,
            dry_run=dry_run,
            diff=diff,
            auto_recover=not no_auto_recover,
        )
        return

    # Below here we're in runtime mode — building + rolling the image.
    # Skipped under --skip-build (operator is reusing an existing
    # image; no build will happen).
    if not skip_build and not (Path.cwd() / "Dockerfile").is_file():
        err.print(
            "[red]✗[/red] no [bold]Dockerfile[/bold] in current directory "
            f"([bold]{Path.cwd()}[/bold])."
        )
        err.print()
        err.print(
            "[dim]Runtime-mode [bold]mdk deploy[/bold] builds the "
            "[bold]movate runtime image[/bold] from the [bold]Dockerfile[/bold] "
            "in the movate-cli source tree. Two paths forward:[/dim]"
        )
        err.print()
        target_hint = target or "<target>"
        err.print(
            "  [bold]A.[/bold] To push your [bold]agents[/bold]: run "
            f"[cyan]mdk deploy --target {target_hint}[/cyan] from your "
            "project folder (the one with [bold]project.yaml[/bold]) — "
            "auto-detected as agents-mode."
        )
        err.print(
            "  [bold]B.[/bold] To push runtime code: "
            "[cyan]cd[/cyan] to the movate-cli repo, then re-run "
            f"[cyan]mdk deploy --target {target_hint}[/cyan]."
        )
        err.print()
        err.print(
            "[dim]Override this check with [bold]--skip-build "
            "--image-tag <existing-tag>[/bold] to just roll Container Apps "
            "to a pre-built image.[/dim]"
        )
        raise typer.Exit(code=2)

    if only is not None and only not in ("api", "worker"):
        error(f"--only must be 'api' or 'worker'; got {only!r}")
        raise typer.Exit(code=2)

    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    try:
        plan = _build_plan(
            target_name=target_name,
            target_cfg=target_cfg,
            image_tag=image_tag,
            skip_build=skip_build,
            only=only,
        )
    except DeployConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    _print_plan(plan, dry_run=dry_run)

    if dry_run:
        # Even dry-runs emit the summary line so CI can confirm the plan
        # parsed cleanly. ok=true means "the plan is well-formed"; the
        # real deploy will emit ok=true|false based on /healthz.
        err.print(
            f"[dim]mdk_deploy_summary: target={target_name} "
            f"image={plan.image_tag} apps={','.join(plan.apps_to_update)} "
            f"dry_run=true ok=true[/dim]"
        )
        return

    # Track wall-clock duration of the deploy from this point forward
    # so the notification carries an accurate "took N seconds" figure.
    started_at = time.monotonic()

    if not skip_build:
        _run_acr_build(plan)
    for app_name in plan.apps_to_update:
        _run_containerapp_update(plan, app_name)

    if no_wait:
        err.print(
            f"[green]✓[/green] deploy submitted to {target_name}. "
            "Skipping /healthz poll (--no-wait)."
        )
        # --no-wait + --notify is intentionally a no-op for the
        # notification: we don't know if the deploy actually succeeded
        # without /healthz. Surface that mismatch on stderr.
        if notify:
            hint(
                "[dim]→ --notify skipped under --no-wait "
                "(success unconfirmed without /healthz poll)[/dim]"
            )
        # Greppable summary — under --no-wait we report submitted=true
        # but cannot prove ok=true, so emit health=unknown.
        err.print(
            f"[dim]mdk_deploy_summary: target={target_name} "
            f"image={plan.image_tag} apps={','.join(plan.apps_to_update)} "
            f"dry_run=false health=unknown ok=true[/dim]"
        )
        return

    asyncio.run(
        _wait_for_healthz(
            url=target_cfg.url,
            expected_version=plan.version,
            timeout=wait_timeout,
        )
    )
    success(f"{target_name} is now serving {plan.image_tag}")

    # Copy-pasteable smoke-test commands so the operator can confirm
    # the deployed runtime answers from outside, not just /healthz from
    # inside. Pick the FIRST agent in the project as the example so the
    # /run line is real, not parameterized. Falls back to /healthz only
    # if the project has no agents (vacuous-pass deploy of an empty
    # workspace).
    first_agent = _first_agent_name() or None
    base_url = target_cfg.url.rstrip("/")
    err.print()
    err.print("[bold]Smoke-test the deployment:[/bold]")
    err.print(f"  [cyan]curl -sS {base_url}/healthz[/cyan]")
    if first_agent:
        err.print(
            f"  [cyan]curl -sS -X POST {base_url}/run "
            f"-H 'content-type: application/json' "
            f'-H "x-api-key: $MDK_DEV_KEY" '
            f'-d \'{{"agent": "{first_agent}", "input": {{}}}}\'[/cyan]'
        )
    err.print(
        f"  [cyan]az containerapp logs show -g {plan.resource_group} "
        f"-n {plan.apps_to_update[0]} --tail 20[/cyan]"
    )
    err.print()

    # Greppable summary — full success path: build + roll + /healthz
    # confirmed. CI gates branch on ok=true here.
    duration_s = round(time.monotonic() - started_at, 1)
    err.print(
        f"[dim]mdk_deploy_summary: target={target_name} "
        f"image={plan.image_tag} apps={','.join(plan.apps_to_update)} "
        f"dry_run=false health=ok duration_s={duration_s} ok=true[/dim]"
    )

    # Notification — fires AFTER success() so an operator running
    # interactively sees the success line before the network round-trip
    # to Telegram / their webhook.
    if notify:
        notify_deploy_success(
            DeployEvent(
                target=target_name,
                image_tag=plan.image_tag,
                runtime_url=target_cfg.url,
                git_sha=_git_short_sha() or "",
                deployer=os.environ.get("USER", "unknown"),
                duration_seconds=time.monotonic() - started_at,
                version=plan.version,
            )
        )


# ---------------------------------------------------------------------------
# Plan + helpers
# ---------------------------------------------------------------------------


class DeployConfigError(Exception):
    """Raised when the target is missing Azure deploy metadata."""


@dataclass
class DeployPlan:
    """All the resolved values for a single deploy invocation.

    Built once at the top of ``deploy()`` so dry-run output + the
    actual execution see exactly the same plan.
    """

    target_name: str
    subscription: str
    resource_group: str
    acr_name: str
    env: str
    image_tag: str
    """Just the tag portion (e.g. 'movate:0.5.0-abc1234'). The
    fully-qualified image is built on the fly via :meth:`fq_image`."""
    skip_build: bool
    apps_to_update: list[str]
    """Container App resource names (e.g. ['movate-prod-api', 'movate-prod-worker'])."""
    version: str
    """The semver portion of the image tag, used for /healthz verification."""

    @property
    def acr_login_server(self) -> str:
        return f"{self.acr_name}.azurecr.io"

    @property
    def fq_image(self) -> str:
        return f"{self.acr_login_server}/{self.image_tag}"


def _build_plan(
    *,
    target_name: str,
    target_cfg: TargetConfig,
    image_tag: str | None,
    skip_build: bool,
    only: str | None,
) -> DeployPlan:
    """Resolve the target's Azure config + image tag into a concrete plan.

    Errors loudly if the target is missing any required Azure field —
    points the operator at `movate config add-target` to fix it.
    """
    missing = [
        name
        for name, value in (
            ("--azure-subscription", target_cfg.azure_subscription),
            ("--azure-resource-group", target_cfg.azure_resource_group),
            ("--azure-acr", target_cfg.azure_acr_name),
            ("--azure-env", target_cfg.azure_env),
        )
        if not value
    ]
    if missing:
        raise DeployConfigError(
            f"target {target_name!r} is missing Azure config: {', '.join(missing)}. "
            f"Run `movate config add-target {target_name} ...` with the missing flags."
        )
    # Pydantic narrows these once they're truthy.
    assert target_cfg.azure_subscription
    assert target_cfg.azure_resource_group
    assert target_cfg.azure_acr_name
    assert target_cfg.azure_env

    version = movate.__version__
    if image_tag is None:
        sha = _git_short_sha() or "unknown"
        image_tag = f"movate:{version}-{sha}"

    apps = [f"movate-{target_cfg.azure_env}-api", f"movate-{target_cfg.azure_env}-worker"]
    if only == "api":
        apps = [f"movate-{target_cfg.azure_env}-api"]
    elif only == "worker":
        apps = [f"movate-{target_cfg.azure_env}-worker"]

    return DeployPlan(
        target_name=target_name,
        subscription=target_cfg.azure_subscription,
        resource_group=target_cfg.azure_resource_group,
        acr_name=target_cfg.azure_acr_name,
        env=target_cfg.azure_env,
        image_tag=image_tag,
        skip_build=skip_build,
        apps_to_update=apps,
        version=version,
    )


def _resolve_deploy_mode(*, mode: str, cwd: Path) -> str:
    """Resolve the deploy mode: ``runtime`` or ``agents``.

    Explicit ``--mode runtime|agents`` always wins. With the default
    ``auto`` mode we pick based on what the operator's cwd looks like:

    * Dockerfile present → ``runtime`` (we're in the movate-cli source
      tree, building + rolling the runtime image)
    * project.yaml present (or any ancestor has one) → ``agents``
      (we're in a customer project, uploading agent bundles to a
      live runtime)
    * Neither → ``runtime`` (let the downstream Dockerfile preflight
      surface the canonical "no Dockerfile" hint; we don't have enough
      signal to confidently pick agents)

    The walk-up for project.yaml means ``mdk deploy`` works from any
    sub-directory of a project (not just the root).
    """
    if mode in ("runtime", "agents"):
        return mode
    if (cwd / "Dockerfile").is_file():
        return "runtime"
    # Walk up looking for the canonical project marker file. Mirrors
    # the same walk-up `mdk validate` / `mdk loader` use to find the
    # project root.
    from movate.core.config import is_project_root  # noqa: PLC0415

    for ancestor in (cwd, *cwd.parents):
        if is_project_root(ancestor):
            return "agents"
    return "runtime"


_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_CONFLICT = 409
_HTTP_SERVICE_UNAVAILABLE = 503

# Sentinel returned by the upload helpers when the runtime returns 401.
# The outer `_deploy_agents` loop watches for this value so it can
# decide whether to auto-recover (mint a fresh key inside the pod and
# retry) or fall back to the human-readable message rendered by
# :func:`_render_unauthorized_message`.
_REASON_UNAUTHORIZED = "__unauthorized__"


def _deploy_status(*, target: str | None) -> None:
    """List live agents on the target runtime via GET /api/v1/agents."""
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    api_key = os.environ.get(target_cfg.key_env, "").strip()
    base_url = target_cfg.url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.get(f"{base_url}/api/v1/agents", headers=headers)
    except httpx.HTTPError as exc:
        error(f"could not reach {base_url}: {exc}")
        raise typer.Exit(code=2) from None

    if resp.status_code != httpx.codes.OK:
        error(f"GET /api/v1/agents returned HTTP {resp.status_code}: {resp.text[:200]!r}")
        raise typer.Exit(code=2)

    try:
        agents = resp.json()
    except Exception:
        error("response is not valid JSON")
        raise typer.Exit(code=2) from None

    if not isinstance(agents, list):
        # Some runtimes wrap in {"agents": [...]}.
        agents = agents.get("agents", []) if isinstance(agents, dict) else []

    table = Table(
        title=f"Live agents on [bold]{target_name}[/bold] ({base_url})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Name", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("Created at", no_wrap=True)

    for agent in sorted(agents, key=lambda a: a.get("name", "") if isinstance(a, dict) else ""):
        if not isinstance(agent, dict):
            continue
        table.add_row(
            agent.get("name", "?"),
            agent.get("version", "?"),
            agent.get("created_at", agent.get("createdAt", "?")),
        )

    if agents:
        err.print(table)
    else:
        err.print(
            f"[yellow]⚠[/yellow] no agents found on [bold]{target_name}[/bold]. "
            "Run [bold]mdk deploy --mode agents[/bold] to upload some."
        )


def _deploy_agents(  # noqa: PLR0912 — orchestrator; branch count reflects per-agent state machine
    *,
    target: str | None,
    dry_run: bool,
    diff: bool = False,
    auto_recover: bool = True,
) -> None:
    """Upload every agent under ``<project>/agents/*/`` to the deployed
    runtime via ``POST /api/v1/agents``.

    Unlike runtime-mode, this:

    * Doesn't rebuild the image — uses whatever's currently serving
    * Doesn't roll Container Apps — agents land on the API pod's
      filesystem and become available via ``?wait=true`` immediately
      (cross-pod sync to workers is BACKLOG item 109; not blocking)
    * Doesn't need the ``az`` CLI — pure HTTPS multipart upload to the
      target's FQDN with the operator's API key from the env var
      named by ``target.key_env``

    Emits a greppable ``mdk_deploy_summary: mode=agents …`` line so CI
    workflows can scrape the same shape as runtime-mode deploys.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from movate.core.config import is_project_root  # noqa: PLC0415

    # Resolve project root by walking up from cwd. Same logic as
    # _resolve_deploy_mode used to pick the mode in the first place.
    cwd = Path.cwd()
    project_root: Path | None = None
    for ancestor in (cwd, *cwd.parents):
        if is_project_root(ancestor):
            project_root = ancestor
            break
    if project_root is None:
        error(
            "agents-mode deploy requires a project (project.yaml / policy.yaml "
            "/ movate.yaml). None found in cwd or any ancestor."
        )
        raise typer.Exit(code=2)

    # Resolve target — must have a URL + key_env. The Azure-specific
    # fields (ACR, RG) aren't used by agents-mode but we keep the
    # same target-resolution path for consistency.
    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    # Find every agent bundle in the project.
    agents_dir = project_root / "agents"
    if not agents_dir.is_dir():
        error(f"no agents/ directory in {project_root}")
        raise typer.Exit(code=2)
    agent_dirs = sorted(
        d for d in agents_dir.iterdir() if d.is_dir() and (d / "agent.yaml").is_file()
    )
    if not agent_dirs:
        err.print(
            f"[yellow]⚠[/yellow] no agents found under [bold]{agents_dir}[/bold]; "
            "nothing to upload. Run [bold]mdk add <template>[/bold] first."
        )
        # Vacuous-pass summary so CI can branch on ok=true|false.
        err.print(
            f"[dim]mdk_deploy_summary: target={target_name} mode=agents agents=0 ok=true[/dim]"
        )
        return

    err.print()
    err.print(
        f"[bold]mdk deploy[/bold] → {target_name} "
        f"[dim](mode=agents, {len(agent_dirs)} agent(s))[/dim]"
    )
    err.print(f"  runtime:        {target_cfg.url}")
    err.print(f"  project root:   {project_root}")
    err.print(f"  agents:         {', '.join(d.name for d in agent_dirs)}")
    err.print()

    if dry_run:
        err.print(
            f"[dim]mdk_deploy_summary: target={target_name} mode=agents "
            f"agents={len(agent_dirs)} dry_run=true ok=true[/dim]"
        )
        return

    # --diff: preview new/changed/unchanged without uploading. Calls
    # GET /api/v1/agents/<name> for each local agent and checks whether
    # the deployed version's agent_yaml_hash matches the local file.
    if diff:
        import hashlib  # noqa: PLC0415

        from rich.table import Table  # noqa: PLC0415

        api_key_diff = os.environ.get(target_cfg.key_env, "").strip()
        base_url_diff = target_cfg.url.rstrip("/")
        headers_diff = {"Authorization": f"Bearer {api_key_diff}"} if api_key_diff else {}

        diff_table = Table(show_header=True, header_style="bold")
        diff_table.add_column("Agent", no_wrap=True)
        diff_table.add_column("Status", no_wrap=True)
        diff_table.add_column("Note", no_wrap=True)

        with httpx.Client(timeout=httpx.Timeout(10.0)) as diff_client:
            for agent_dir in agent_dirs:
                local_hash = hashlib.sha256((agent_dir / "agent.yaml").read_bytes()).hexdigest()[
                    :12
                ]
                try:
                    resp = diff_client.get(
                        f"{base_url_diff}/api/v1/agents/{agent_dir.name}",
                        headers=headers_diff,
                    )
                    if resp.status_code == httpx.codes.OK:
                        deployed = resp.json()
                        deployed_hash = (deployed.get("agent_yaml_hash") or "")[:12]
                        if deployed_hash and local_hash == deployed_hash:
                            diff_table.add_row(
                                agent_dir.name, "[dim]unchanged[/dim]", f"hash={local_hash}"
                            )
                        else:
                            note = (
                                f"local={local_hash} deployed={deployed_hash}"
                                if deployed_hash
                                else f"local={local_hash} (no hash in API)"
                            )
                            diff_table.add_row(agent_dir.name, "[yellow]changed[/yellow]", note)
                    elif resp.status_code == httpx.codes.NOT_FOUND:
                        diff_table.add_row(agent_dir.name, "[green]new[/green]", "not yet deployed")
                    else:
                        diff_table.add_row(
                            agent_dir.name, "[yellow]?[/yellow]", f"HTTP {resp.status_code}"
                        )
                except httpx.HTTPError:
                    diff_table.add_row(agent_dir.name, "[yellow]?[/yellow]", "runtime unreachable")

        err.print(diff_table)
        err.print(
            f"[dim]mdk_deploy_summary: target={target_name} mode=agents "
            f"agents={len(agent_dirs)} diff=true ok=true[/dim]"
        )
        return

    # Resolve the bearer token from the env var named by the target.
    # The variable holds the FULL `mvt_<env>_<tenant>_<keyid>_<secret>`
    # string — same one used for `Authorization: Bearer ...`.
    api_key = os.environ.get(target_cfg.key_env, "").strip()
    if not api_key:
        # Empty env var = same operator pain as a 401 from the
        # runtime: there's a deployed environment + Azure addressing
        # in the target config; we know how to mint a fresh key
        # inside the pod. Run the same auto-recovery the 401 path
        # uses, then continue with the freshly-minted bearer.
        # Targets without Azure addressing OR with --no-auto-recover
        # fall through to the original error message.
        azure_addressable = bool(target_cfg.azure_resource_group and target_cfg.azure_env)
        if auto_recover and azure_addressable:
            err.print(
                f"  [dim]Minting fresh bearer key for [bold]{target_name}[/bold] (~10 sec.)…[/dim]"
            )
            new_key = _attempt_auto_recovery(target_name=target_name)
            if new_key is not None:
                api_key = new_key
        if not api_key:
            error(
                f"env var ${target_cfg.key_env} is empty. One-shot fix: "
                f"`mdk auth refresh-runtime-key {target_name}` "
                f"(mints + saves a fresh key inside the deployed Container "
                f"App). Or if `bootstrap-api-key` is already in Key Vault: "
                f"`mdk auth pull-runtime-key {target_name} --keyvault <name>`."
            )
            raise typer.Exit(code=2)

    base_url = target_cfg.url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}

    # Pre-deploy bearer validation. Catching a stale bearer with a
    # single cheap GET is way better than failing mid-multipart-upload
    # with ✗ on every skill + agent before the auto-recovery retry
    # finally lands. The preflight either confirms the bearer works
    # (silent), or auto-recovers up-front so the upload loop only ever
    # sees a known-good token.
    headers = _preflight_bearer(
        base_url=base_url,
        headers=headers,
        target_name=target_name,
        target_cfg=target_cfg,
        auto_recover=auto_recover,
    )

    # Upload skills BEFORE agents: agent upload triggers scan_agents on
    # the runtime, which validates skill references. Skills must already
    # be in the registry at that point or agents that reference them 422.
    with httpx.Client(timeout=httpx.Timeout(60.0)) as skill_client:
        skill_uploaded, skill_failed = _upload_skills(
            client=skill_client,
            base_url=base_url,
            headers=headers,
            project_root=project_root,
        )

        uploaded: list[str] = []
        failed: list[tuple[str, str]] = []  # (name, reason)

        with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
            for agent_dir in agent_dirs:
                result = _upload_one_agent_bundle(
                    client=client,
                    base_url=base_url,
                    headers=headers,
                    agent_dir=agent_dir,
                    project_root=project_root,
                )
                if result is None:
                    uploaded.append(agent_dir.name)
                else:
                    failed.append((agent_dir.name, result))

            # Auto-recovery: if anything 401'd, try minting a fresh key
            # inside the Container App + retry the failing items once.
            # Only viable when the target has Azure addressing — for
            # custom remotes / BYO Container Apps, fall through to the
            # descriptive message.
            had_unauthorized = any(r == _REASON_UNAUTHORIZED for _, r in skill_failed) or any(
                r == _REASON_UNAUTHORIZED for _, r in failed
            )
            azure_addressable = bool(target_cfg.azure_resource_group and target_cfg.azure_env)
            if had_unauthorized and auto_recover and azure_addressable:
                err.print(
                    f"  [dim]Minting fresh bearer key for "
                    f"[bold]{target_name}[/bold] (~10 sec.)…[/dim]"
                )
                new_key = _attempt_auto_recovery(target_name=target_name)
                if new_key is not None:
                    headers["Authorization"] = f"Bearer {new_key}"
                    # Retry skills first if any 401'd, then retry agents.
                    if any(r == _REASON_UNAUTHORIZED for _, r in skill_failed):
                        re_uploaded, re_failed = _upload_skills(
                            client=skill_client,
                            base_url=base_url,
                            headers=headers,
                            project_root=project_root,
                        )
                        skill_uploaded = re_uploaded
                        skill_failed = re_failed
                    retry_failures: list[tuple[str, str]] = []
                    retry_uploaded: list[str] = []
                    for agent_dir in agent_dirs:
                        # Only retry agents whose first attempt 401'd.
                        prior = next(
                            (r for n, r in failed if n == agent_dir.name),
                            None,
                        )
                        if prior != _REASON_UNAUTHORIZED:
                            continue
                        result = _upload_one_agent_bundle(
                            client=client,
                            base_url=base_url,
                            headers=headers,
                            agent_dir=agent_dir,
                            project_root=project_root,
                        )
                        if result is None:
                            retry_uploaded.append(agent_dir.name)
                        else:
                            retry_failures.append((agent_dir.name, result))
                    # Splice retry results into the original lists,
                    # dropping the prior unauthorized entries.
                    failed = [
                        (n, r) for n, r in failed if r != _REASON_UNAUTHORIZED
                    ] + retry_failures
                    uploaded = [*uploaded, *retry_uploaded]
                # If recovery failed, leave the sentinel rows in place
                # so they get rendered as the descriptive message below.

    # Translate any remaining sentinels into the operator-facing
    # description so the summary table reads as English, not as the
    # internal token.
    failed = [
        (n, _render_unauthorized_message(headers, target_name) if r == _REASON_UNAUTHORIZED else r)
        for n, r in failed
    ]
    skill_failed = [
        (n, _render_unauthorized_message(headers, target_name) if r == _REASON_UNAUTHORIZED else r)
        for n, r in skill_failed
    ]

    # Render summary — skills first (they upload first).
    err.print()
    for name in skill_uploaded:
        err.print(f"  [green]✓[/green] uploaded skill [bold]{name}[/bold]")
    for name, reason in skill_failed:
        err.print(f"  [red]✗[/red] skill [bold]{name}[/bold] — {reason}")
    for name in uploaded:
        err.print(f"  [green]✓[/green] uploaded agent [bold]{name}[/bold]")
    for name, reason in failed:
        err.print(f"  [red]✗[/red] agent [bold]{name}[/bold] — {reason}")
    err.print()
    ok = not failed and not skill_failed

    # Post-deploy "now what?" block — surfaces the smallest set of
    # commands an operator needs to invoke the just-deployed agents.
    # Renders only on successful deploys (no point if nothing landed)
    # and only when at least one agent uploaded (a skills-only deploy
    # has nothing to submit against).
    if ok and uploaded:
        _render_post_deploy_next_steps(
            target_name=target_name,
            uploaded=uploaded,
            project_root=project_root,
        )

    err.print(
        f"[dim]mdk_deploy_summary: target={target_name} mode=agents "
        f"agents={len(agent_dirs)} uploaded={len(uploaded)} "
        f"failed={len(failed)} "
        f"skills_uploaded={len(skill_uploaded)} skills_failed={len(skill_failed)} "
        f"ok={'true' if ok else 'false'}[/dim]"
    )
    if not ok:
        raise typer.Exit(code=2)


def _render_post_deploy_next_steps(
    *,
    target_name: str,
    uploaded: list[str],
    project_root: Path,
) -> None:
    """Print a tight "Next: run inference" block after a successful deploy.

    Shows one ``mdk submit`` example using the first uploaded agent
    (alphabetical for determinism — operators with a favorite can
    re-target by name), plus the two ``mdk jobs`` verbs that complete
    the inference loop. Uses the agent's first dataset row as the
    sample input when one is available so the example is
    copy-pasteable against the operator's actual schema.
    """
    sample_agent = sorted(uploaded)[0]
    sample_dir = project_root / "agents" / sample_agent
    sample_input_json: str = '{"text":"..."}'
    dataset_path = sample_dir / "evals" / "dataset.jsonl"
    if dataset_path.is_file():
        try:
            first_line = next(
                (line for line in dataset_path.read_text().splitlines() if line.strip()),
                "",
            )
            row = json.loads(first_line) if first_line else None
            if isinstance(row, dict) and isinstance(row.get("input"), dict):
                sample_input_json = json.dumps(row["input"])
        except (OSError, ValueError):
            pass

    err.print("[bold]Next:[/bold] run inference against the deployed runtime")
    err.print(
        f"  [cyan]mdk submit {sample_agent}[/cyan] "
        f"[yellow]'{sample_input_json}'[/yellow] "
        f"--target {target_name} --wait"
    )
    if len(uploaded) > 1:
        others = ", ".join(sorted(uploaded)[1:])
        err.print(f"  [dim]other agents: {others}[/dim]")
    err.print(
        f"  [dim]mdk jobs list --target {target_name}[/dim]           [dim]# recent jobs[/dim]"
    )
    err.print(
        f"  [dim]mdk jobs show <id> --target {target_name}[/dim]       [dim]# inspect one run[/dim]"
    )
    err.print()


def _append_context_files(
    files: list[tuple[str, tuple[str, bytes, str]]],
    agent_yaml_bytes: bytes,
    agent_dir: Path,
    project_root: Path | None,
) -> None:
    """Resolve context names declared in agent.yaml and append them to files.

    Two-tier resolution mirrors the local loader: agent-local
    ``contexts/<name>.md`` overrides the project-level one. Files found
    are appended as repeating ``contexts`` multipart fields so the
    runtime stores them inside the agent dir, making the deployed bundle
    self-contained without a shared volume.
    """
    try:
        import yaml as _yaml  # noqa: PLC0415

        raw_spec = _yaml.safe_load(agent_yaml_bytes)
        context_names: list[str] = (
            list(raw_spec.get("contexts") or []) if isinstance(raw_spec, dict) else []
        )
    except Exception:
        context_names = []

    for ctx_name in context_names:
        candidates = [agent_dir / "contexts" / f"{ctx_name}.md"]
        if project_root is not None:
            candidates.append(project_root / "contexts" / f"{ctx_name}.md")
        for candidate in candidates:
            if candidate.is_file():
                files.append(
                    (
                        "contexts",
                        (f"contexts/{ctx_name}.md", candidate.read_bytes(), "text/markdown"),
                    )
                )
                break


def _append_kb_files(
    files: list[tuple[str, tuple[str, bytes, str]]],
    project_root: Path | None,
) -> None:
    """Append KB corpus files from ``<project_root>/kb/*.json`` to the
    multipart upload.

    Each file is sent as a repeating ``kb`` multipart field so the
    runtime stores it at ``<agent_dir>/kb/<filename>``. The deployed
    skill's ``resolve_kb_file()`` then finds it via its agent-local
    tier without needing a shared project volume.

    Only ``.json`` files are included — index files, YAML corpora, and
    other assets under ``kb/`` are silently skipped (the skill's corpus
    format is always JSON).
    """
    if project_root is None:
        return
    kb_dir = project_root / "kb"
    if not kb_dir.is_dir():
        return
    for kb_file in sorted(kb_dir.iterdir()):
        if kb_file.is_file() and kb_file.suffix.lower() == ".json":
            files.append(
                (
                    "kb",
                    (f"kb/{kb_file.name}", kb_file.read_bytes(), "application/json"),
                )
            )


def _preflight_bearer(
    *,
    base_url: str,
    headers: dict[str, str],
    target_name: str,
    target_cfg: Any,
    auto_recover: bool,
) -> dict[str, str]:
    """Validate the saved bearer with one cheap authenticated call.

    Issues a single ``GET /api/v1/agents`` against the deployed
    runtime before the multipart upload loop starts. Three outcomes:

    * ``200`` (or any 2xx) — bearer works; return ``headers`` unchanged.
    * ``401`` and auto-recovery is enabled and the target has Azure
      addressing — silently mint a fresh key inside the Container App
      via :func:`_attempt_auto_recovery`, update the ``Authorization``
      header, and return the new headers dict. The upload loop never
      sees the original 401.
    * Anything else — surface a descriptive error and ``typer.Exit``
      so the operator doesn't burn bandwidth on a doomed upload.

    Returns the (possibly-rotated) headers dict so the caller assigns
    the result back. Mutates ``headers`` in-place as a no-op safety
    net for the success path.
    """
    import httpx  # noqa: PLC0415

    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(f"{base_url}/api/v1/agents", headers=headers)
    except httpx.HTTPError as exc:
        error(
            f"preflight failed: cannot reach {base_url} ({type(exc).__name__}). "
            f"Check the target URL and network."
        )
        raise typer.Exit(code=2) from None

    if resp.status_code < _HTTP_BAD_REQUEST:
        return headers

    if resp.status_code == _HTTP_UNAUTHORIZED:
        azure_addressable = bool(target_cfg.azure_resource_group and target_cfg.azure_env)
        if auto_recover and azure_addressable:
            err.print(
                f"  [dim]Minting fresh bearer key for [bold]{target_name}[/bold] (~10 sec.)…[/dim]"
            )
            new_key = _attempt_auto_recovery(target_name=target_name)
            if new_key is not None:
                headers["Authorization"] = f"Bearer {new_key}"
                return headers
            # _attempt_auto_recovery already printed why; fall through
            # to the descriptive error so the operator has one place
            # to look.
        error(_render_unauthorized_message(headers, target_name))
        raise typer.Exit(code=2)

    # Any other non-2xx — could be a 5xx from the runtime, a 4xx from
    # a request shape issue, etc. Don't auto-recover (auth is not the
    # problem here); surface the body so the operator can triage.
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:200]}
    error(f"preflight failed: HTTP {resp.status_code} from {base_url}/api/v1/agents: {body!r}")
    raise typer.Exit(code=2)


def _attempt_auto_recovery(*, target_name: str) -> str | None:
    """Mint + save a fresh runtime key for ``target_name`` inside the pod.

    Wraps :func:`movate.cli.auth.refresh_runtime_key_inline` so the
    deploy 401 path can recover silently. Returns the new bearer on
    success, ``None`` on any failure (prints the underlying reason to
    stderr so the operator can act on it). Never raises — recovery is
    best-effort.
    """
    from movate.cli.auth import (  # noqa: PLC0415
        RefreshRuntimeKeyError,
        refresh_runtime_key_inline,
    )

    try:
        new_key, env_var = refresh_runtime_key_inline(target_name)
    except RefreshRuntimeKeyError as exc:
        err.print(
            f"  [red]✗[/red] auto-recovery failed: {exc}. "
            f"Run [bold]mdk auth refresh-runtime-key {target_name}[/bold] "
            "manually to debug."
        )
        return None
    err.print(f"  [green]✓[/green] bearer key ready (saved as [cyan]{env_var}[/cyan]).")
    return new_key


def _render_unauthorized_message(headers: dict[str, str], target_name: str) -> str:
    """Operator-facing description of a 401 the auto-recovery couldn't fix.

    Shows the first 16 chars of the rejected bearer (enough to spot
    "wrong tenant" / "stale shell rc") without leaking the secret, and
    points at ``mdk doctor target`` for the underlying storage check
    that explains WHY a freshly-minted key would also 401.
    """
    bearer_header = headers.get("Authorization", "")
    prefix = bearer_header.removeprefix("Bearer ").strip()[:16]
    return (
        f"runtime rejected the bearer token "
        f"(value starts with: '{prefix}…'). The saved key is not "
        f"present in the runtime's ApiKeyRecord storage — typically "
        f"because a revision recycle wiped a non-durable backend "
        f"(SQLite-in-pod). Run `mdk doctor --target {target_name}` "
        f"to confirm the storage backend. Manual one-shot recovery: "
        f"`mdk auth refresh-runtime-key {target_name}`."
    )


def _upload_skills(
    *,
    client: object,  # httpx.Client
    base_url: str,
    headers: dict[str, str],
    project_root: Path,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Upload every skill under ``<project_root>/skills/*/`` to ``POST /api/v1/skills``.

    Returns ``(uploaded_names, failed)`` where ``failed`` is a list of
    ``(name, reason)`` pairs. The runtime endpoint uses PUT semantics —
    re-uploading an existing skill overwrites it atomically, so this is
    safe to call on every deploy.

    Silently returns two empty lists if the project has no ``skills/``
    directory (most demo projects don't have custom skills).
    """
    import httpx as _httpx  # noqa: PLC0415

    assert isinstance(client, _httpx.Client)

    skills_dir = project_root / "skills"
    if not skills_dir.is_dir():
        return [], []

    skill_dirs = sorted(
        d for d in skills_dir.iterdir() if d.is_dir() and (d / "skill.yaml").is_file()
    )
    if not skill_dirs:
        return [], []

    uploaded: list[str] = []
    failed: list[tuple[str, str]] = []

    for skill_dir in skill_dirs:
        name = skill_dir.name
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("skill_yaml", ("skill.yaml", (skill_dir / "skill.yaml").read_bytes(), "text/yaml")),
        ]
        impl = skill_dir / "impl.py"
        if impl.is_file():
            files.append(("impl", ("impl.py", impl.read_bytes(), "text/x-python")))
        for corpus_name in ("corpus.json", "kb-lookup-corpus.json"):
            corpus = skill_dir / corpus_name
            if corpus.is_file():
                files.append(("corpus", (corpus_name, corpus.read_bytes(), "application/json")))
                break
        readme = skill_dir / "README.md"
        if readme.is_file():
            files.append(("readme", ("README.md", readme.read_bytes(), "text/markdown")))

        try:
            resp = client.post(f"{base_url}/api/v1/skills", files=files, headers=headers)
        except _httpx.HTTPError as exc:
            failed.append((name, f"network error: {exc}"))
            continue

        if resp.status_code in (_HTTP_OK, _HTTP_CREATED):
            uploaded.append(name)
        elif resp.status_code == _HTTP_SERVICE_UNAVAILABLE:
            failed.append(
                (
                    name,
                    "runtime has no skills_path — restart with skills_path configured",
                )
            )
        elif resp.status_code == _HTTP_UNAUTHORIZED:
            failed.append((name, _REASON_UNAUTHORIZED))
        else:
            failed.append((name, f"HTTP {resp.status_code}: {resp.text[:120]}"))

    return uploaded, failed


def _upload_one_agent_bundle(
    *,
    client: object,  # httpx.Client; typed as object to avoid top-level httpx import
    base_url: str,
    headers: dict[str, str],
    agent_dir: Path,
    project_root: Path | None = None,
) -> str | None:
    """Upload a single agent bundle via multipart POST /api/v1/agents.

    Tries ``POST /api/v1/agents`` first (creates the agent). On 409
    (already-exists), falls back to the runtime's PUT endpoint to
    replace the on-disk bundle — agents-mode deploy is idempotent.

    Returns ``None`` on success, or a string reason on failure.
    Caller renders the reason; we don't print here so the loop can
    aggregate.
    """
    import httpx  # noqa: PLC0415

    # Required files. Schemas can be JSON or YAML once Schemas Part 2
    # ships — for now they have to be where the canonical file lives.
    agent_yaml = agent_dir / "agent.yaml"
    prompt_md = agent_dir / "prompt.md"
    # Prefer schema/*.yaml (PR #95 Schemas Part 2) and fall back to
    # *.json; the runtime's upload endpoint accepts either via its
    # generic file slot.
    input_schema = _pick_first_existing(
        agent_dir / "schema" / "input.yaml",
        agent_dir / "schema" / "input.yml",
        agent_dir / "schema" / "input.json",
    )
    output_schema = _pick_first_existing(
        agent_dir / "schema" / "output.yaml",
        agent_dir / "schema" / "output.yml",
        agent_dir / "schema" / "output.json",
    )
    dataset = agent_dir / "evals" / "dataset.jsonl"

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    if not agent_yaml.is_file():
        return f"missing {agent_yaml.relative_to(agent_dir)}"
    if not prompt_md.is_file():
        return f"missing {prompt_md.relative_to(agent_dir)}"

    # YAML-schema accommodation. The deployed runtime's multipart
    # endpoint hard-codes the persistence paths as schema/input.json
    # + schema/output.json (see runtime/agent_creation.py
    # `_collect_bundle_files`). If the operator's local agent ships
    # YAML schemas (shorthand or hand-written JSON-Schema in YAML),
    # we compile them to JSON in-flight + rewrite the agent.yaml
    # schema paths so the runtime's loader resolves the persisted
    # `.json` files. Operator's on-disk files are untouched.
    agent_yaml_bytes, rewrote_paths = _maybe_rewrite_agent_yaml_for_upload(
        agent_yaml,
        input_schema=input_schema,
        output_schema=output_schema,
    )
    files.append(("agent_yaml", ("agent.yaml", agent_yaml_bytes, "text/yaml")))
    files.append(("prompt", ("prompt.md", prompt_md.read_bytes(), "text/markdown")))

    if input_schema is not None:
        input_bytes, input_name = _schema_bytes_for_upload(input_schema, label="input")
        files.append(
            (
                "input_schema",
                (input_name, input_bytes, "application/json"),
            )
        )
    if output_schema is not None:
        output_bytes, output_name = _schema_bytes_for_upload(output_schema, label="output")
        files.append(
            (
                "output_schema",
                (output_name, output_bytes, "application/json"),
            )
        )
    if dataset.is_file():
        files.append(
            (
                "dataset",
                ("dataset.jsonl", dataset.read_bytes(), "application/jsonl"),
            )
        )
    _ = rewrote_paths  # accepted for future telemetry / debug log

    # Context files — two-tier resolution mirrors the local loader.
    _append_context_files(files, agent_yaml_bytes, agent_dir, project_root)

    # KB corpus files — bundled into the agent dir so deployed skills
    # can resolve their corpus via resolve_kb_file()'s agent-local tier.
    _append_kb_files(files, project_root)

    # httpx requires the client to be typed precisely here; the
    # `client: object` parameter signature lets the outer function
    # avoid the top-level httpx import for fast cold-starts.
    assert isinstance(client, httpx.Client)
    try:
        response = client.post(
            f"{base_url}/api/v1/agents",
            files=files,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        return f"network error: {exc}"

    if response.status_code == _HTTP_CREATED:
        return None
    if response.status_code == _HTTP_CONFLICT:
        # Already exists — replace via PUT for idempotency. PR #95
        # ships POST-only; PUT support is gated on the runtime's
        # PUT /api/v1/agents/{name} endpoint (item 76 in BACKLOG).
        # For now, treat 409 as a soft success (agent IS deployed,
        # just not from this exact bundle) so the demo keeps moving.
        return None
    # 401 from the runtime means our bearer token was rejected. The
    # bearer was set (we passed the env-var-empty preflight) but the
    # runtime has no matching ApiKeyRecord. The auth path is opaque
    # token + DB lookup (no JWT signing), so a 401 here means the
    # record is missing from storage — almost always because the
    # ApiKeyRecord table didn't survive the last revision recycle
    # (SQLite-in-pod fallback). Return the sentinel so the outer
    # loop can decide between auto-recovery and reporting.
    if response.status_code == _HTTP_UNAUTHORIZED:
        return _REASON_UNAUTHORIZED
    # Try to surface the runtime's error body verbatim so the
    # operator sees the actual validation failure.
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:200]}
    return f"HTTP {response.status_code}: {body!r}"


def _schema_bytes_for_upload(path: Path, *, label: str) -> tuple[bytes, str]:
    """Read a local schema file + return ``(bytes, canonical_name)``
    ready for multipart upload.

    The deployed runtime's multipart endpoint hard-codes the
    persistence paths as ``schema/input.json`` + ``schema/output.json``
    regardless of what we send. So for ``.yaml`` / ``.yml`` schemas we
    parse the file, detect whether it's hand-written JSON Schema or
    the shorthand form, and serialize the compiled JSON Schema as
    bytes — that way the on-pod ``.json`` file the runtime persists
    is a valid Draft 2020-12 schema regardless of which form the
    operator authored locally. ``.json`` files pass through untouched.
    """
    import yaml as _yaml  # noqa: PLC0415

    suffix = path.suffix.lower()
    if suffix == ".json":
        return path.read_bytes(), f"{label}.json"
    if suffix not in (".yaml", ".yml"):
        # Unsupported extension — pass through; the runtime will
        # surface a clear error.
        return path.read_bytes(), path.name
    try:
        data = _yaml.safe_load(path.read_text())
    except _yaml.YAMLError as exc:
        # Defer to the runtime's error surface; just send bytes verbatim.
        _ = exc
        return path.read_bytes(), f"{label}.json"
    if not isinstance(data, dict):
        return path.read_bytes(), f"{label}.json"
    # Shape-sniff: canonical MDK format (version: 1 + fields:) first, then
    # hand-written JSON Schema, then shorthand. Same detection order as the
    # loader (movate.core.loader._load_schema_doc).
    from movate.core.canonical_schema import (  # noqa: PLC0415
        CanonicalSchemaError,
        compile_canonical,
        is_canonical_format,
    )

    if is_canonical_format(data):
        try:
            data = compile_canonical(data)
        except CanonicalSchemaError:
            return path.read_bytes(), f"{label}.json"
        return json.dumps(data, separators=(",", ":")).encode(), f"{label}.json"

    is_json_schema = "$schema" in data or (data.get("type") == "object" and "properties" in data)
    if not is_json_schema:
        # Shorthand → compile to JSON Schema via the same compiler the
        # loader uses, so the upload is bit-for-bit equivalent.
        from movate.core.schema_shorthand import (  # noqa: PLC0415
            SchemaShorthandError,
            compile_shorthand,
        )

        try:
            data = compile_shorthand(data, root_label=label)
        except SchemaShorthandError:
            # Don't block the upload — let the runtime's validation
            # surface the canonical error message.
            return path.read_bytes(), f"{label}.json"
    return json.dumps(data, separators=(",", ":")).encode(), f"{label}.json"


def _maybe_rewrite_agent_yaml_for_upload(
    agent_yaml_path: Path,
    *,
    input_schema: Path | None,
    output_schema: Path | None,
) -> tuple[bytes, bool]:
    """Return ``(bytes, rewrote)`` where bytes is the agent.yaml content
    to upload.

    If the operator's agent.yaml declares schemas via path strings
    pointing at ``.yaml`` / ``.yml`` files, rewrite those paths to
    ``.json`` so the runtime's loader resolves the on-pod files
    correctly (the runtime persists everything we upload as
    ``schema/input.json`` + ``schema/output.json``). On-disk
    agent.yaml is untouched — this is a transient rewrite for the
    upload only.

    Returns ``rewrote=False`` (and the original bytes) when no
    rewrite was needed (already pointing at ``.json``, or schemas are
    inline shorthand in agent.yaml directly).
    """
    import yaml as _yaml  # noqa: PLC0415

    raw = agent_yaml_path.read_bytes()
    try:
        data = _yaml.safe_load(raw.decode())
    except _yaml.YAMLError:
        return raw, False
    if not isinstance(data, dict) or "schema" not in data:
        return raw, False
    schema_block = data.get("schema")
    if not isinstance(schema_block, dict):
        return raw, False
    rewrote = False
    for slot, _local_path in (("input", input_schema), ("output", output_schema)):
        current = schema_block.get(slot)
        if isinstance(current, str) and current.lower().endswith((".yaml", ".yml")):
            # Rewrite ./schema/input.yaml → ./schema/input.json
            schema_block[slot] = f"./schema/{slot}.json"
            rewrote = True
    if not rewrote:
        return raw, False
    return _yaml.safe_dump(data, sort_keys=False).encode(), True


def _pick_first_existing(*paths: Path) -> Path | None:
    """Return the first path in ``paths`` that exists, or None."""
    for path in paths:
        if path.is_file():
            return path
    return None


def _first_agent_name() -> str | None:
    """Return the first agent name in the current project's
    ``agents/`` dir, or None if there are no agents (or no project).

    Used by the post-deploy success message to build a copy-pasteable
    ``curl POST /run`` example. Pure filesystem lookup — no YAML
    parsing — so it stays cheap and never raises.
    """
    agents_dir = Path.cwd() / "agents"
    if not agents_dir.is_dir():
        return None
    for entry in sorted(agents_dir.iterdir()):
        if entry.is_dir() and (entry / "agent.yaml").is_file():
            return entry.name
    return None


def _git_short_sha() -> str | None:
    """Return the short git sha of HEAD, or None if not in a git repo
    or git isn't on PATH."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _print_plan(plan: DeployPlan, *, dry_run: bool) -> None:
    """Show the operator what's about to happen (or what would).

    Prints to stderr so an operator piping to a log file still sees it
    interactively. The actual ``az`` invocations stream to stdout
    when run (uncaptured) so operators can watch progress.
    """
    label = "[dim](dry-run)[/dim] " if dry_run else ""
    err.print()
    # Use the canonical `mdk` brand in user-facing output (`movate`
    # still works as a binary alias but mixing names in the demo
    # confuses operators).
    err.print(f"{label}[bold]mdk deploy[/bold] → {plan.target_name}")
    err.print(f"  subscription:    {plan.subscription}")
    err.print(f"  resource group:  {plan.resource_group}")
    err.print(f"  ACR:             {plan.acr_login_server}")
    err.print(f"  env:             {plan.env}")
    err.print(f"  image:           {plan.fq_image}")
    if plan.skip_build:
        err.print("  build:           [dim]skipped (--skip-build)[/dim]")
    else:
        err.print("  build:           az acr build (multi-stage Dockerfile)")
    err.print(f"  apps to update:  {', '.join(plan.apps_to_update)}")
    err.print()


def _run_acr_build(plan: DeployPlan) -> None:
    """``az acr build`` — builds the image inside ACR (no local Docker).

    Uses the multi-stage Dockerfile's ``runtime`` target (the worker
    Container App reuses the same image and overrides the command).
    Build log is captured and suppressed — the spinner conveys progress,
    and the verbose build output (layer hashes, timing, etc.) is not
    useful at deploy time. On failure, captured stderr is still shown.
    """
    cmd = [
        "az",
        "acr",
        "build",
        "--subscription",
        plan.subscription,
        "--registry",
        plan.acr_name,
        "--image",
        plan.image_tag,
        "--file",
        "Dockerfile",
        "--target",
        "runtime",
        ".",
    ]
    with spinner(f"building {plan.image_tag} in ACR..."):
        _run_az(cmd, what="acr build")
    err.print(f"  [green]✓[/green] image built: {plan.fq_image}")


def _run_containerapp_update(plan: DeployPlan, app_name: str) -> None:
    """``az containerapp update --image ...`` — rolls out the new image
    to a single Container App. ACA handles the rolling restart; if
    ``minReplicas >= 2`` there's zero downtime."""
    cmd = [
        "az",
        "containerapp",
        "update",
        "--subscription",
        plan.subscription,
        "--resource-group",
        plan.resource_group,
        "--name",
        app_name,
        "--image",
        plan.fq_image,
    ]
    with spinner(f"updating {app_name}..."):
        stdout = _run_az(cmd, what=f"containerapp update {app_name}")

    # Parse the JSON response and surface only the fields operators care about.
    # The full blob (outbound IPs, all properties) is suppressed — it's noise
    # at deploy time.
    try:
        data = json.loads(stdout)
        props = data.get("properties", {})
        state = props.get("provisioningState", "?")
        revision = props.get("latestRevisionName", "")
        fqdn = (props.get("configuration") or {}).get("ingress", {}).get("fqdn", "")
        detail = f"state={state}"
        if revision:
            # Trim "movate-prod-api--<sha>" → just the revision suffix
            detail += f"  revision={revision.split('--')[-1]}"
        if fqdn:
            detail += f"  fqdn={fqdn}"
        err.print(f"  [green]✓[/green] {app_name}: {detail}")
    except (json.JSONDecodeError, AttributeError, KeyError):
        err.print(f"  [green]✓[/green] {app_name} updated")


def _run_az(cmd: list[str], *, what: str) -> str:
    """Run an ``az`` command, capturing stdout. Returns the stdout text.

    Capturing (rather than streaming) keeps the terminal clean — ``az
    containerapp update`` returns hundreds of lines of JSON that would
    swamp the spinner. Callers that want to surface key fields can parse
    the returned text. On failure, captured stderr is printed before the
    error line so operators still see the Azure error message.
    """
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        # Caught upstream by shutil.which check, but defensive.
        error(f"command not found: {cmd[0]}")
        raise typer.Exit(code=2) from exc

    if result.returncode != 0:
        if result.stderr:
            err.print(result.stderr.strip())
        err.print(
            f"[red]✗ az command failed:[/red] {what} (exit {result.returncode})\n"
            f"[dim]command: {' '.join(cmd)}[/dim]"
        )
        raise typer.Exit(code=1)
    return result.stdout


async def _wait_for_healthz(*, url: str, expected_version: str, timeout: float) -> None:
    """Poll ``GET /healthz`` until the response's ``version`` matches the
    new deploy. ACA's rolling restart can take 30s-2min; we give it
    ``timeout`` seconds, then bail with exit 124."""
    deadline = asyncio.get_event_loop().time() + timeout
    poll_interval = 5.0
    hint(f"[dim]waiting for /healthz to report version {expected_version}...[/dim]")
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                r = await client.get(f"{url.rstrip('/')}/healthz")
                if r.status_code == httpx.codes.OK:
                    body = r.json()
                    seen = body.get("version", "?")
                    if seen == expected_version:
                        return
                    hint(f"[dim]  still seeing version {seen}, retrying...[/dim]")
            except (httpx.HTTPError, ValueError):
                hint("[dim]  /healthz unreachable, retrying...[/dim]")
            if asyncio.get_event_loop().time() >= deadline:
                err.print(
                    f"[yellow]⏱[/yellow] timed out after {timeout:.0f}s waiting "
                    f"for version {expected_version}; ACA rollout may still be "
                    "in progress. Check manually with `az containerapp revision list`."
                )
                # 124 is the conventional `timeout` exit code so bash
                # scripts can branch on it.
                sys.exit(124)
            await asyncio.sleep(poll_interval)
