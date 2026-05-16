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
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

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


def deploy(
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
    if not dry_run and shutil.which("az") is None:
        err.print(
            "[red]✗[/red] `az` CLI not found on PATH. "
            "Install: https://learn.microsoft.com/cli/azure/install-azure-cli"
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
    err.print(f"{label}[bold]movate deploy[/bold] → {plan.target_name}")
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
    Output streams to stdout so operators see build progress.
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
        _run_az(cmd, what=f"containerapp update {app_name}")


def _run_az(cmd: list[str], *, what: str) -> None:
    """Run an ``az`` command. Streams output to the caller's stdout/stderr
    so the operator sees progress. Non-zero exit → typer.Exit(1)."""
    try:
        # check=False so we can render our own error message; az's
        # default stderr is already noisy enough that wrapping with
        # CalledProcessError adds nothing.
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        # Caught upstream by shutil.which check, but defensive.
        error(f"command not found: {cmd[0]}")
        raise typer.Exit(code=2) from exc

    if result.returncode != 0:
        err.print(
            f"[red]✗ az command failed:[/red] {what} (exit {result.returncode})\n"
            f"[dim]command: {' '.join(cmd)}[/dim]"
        )
        raise typer.Exit(code=1)


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
