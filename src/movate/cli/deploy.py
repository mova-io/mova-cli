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
import shlex
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
from movate.cli._console import echo_remote_context, error, hint, success
from movate.cli._progress import spinner
from movate.core.user_config import (
    TargetConfig,
    UserConfigError,
    resolve_target,
)
from movate.notify import DeployEvent, notify_deploy_success
from movate.utils.git import git_short_sha

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
    skip_validate: bool = typer.Option(
        False,
        "--skip-validate",
        help=(
            "Skip the validate-before-deploy guardrail. By default a "
            "runtime-mode deploy runs the same checks as [bold]mdk "
            "validate --all[/bold] on the project in cwd and aborts before "
            "building/pushing any image if any agent or workflow fails. Use "
            "this to deploy a known-good image despite a transient validation "
            "issue (e.g. an env var only set in the deployed pod)."
        ),
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
    with_kb: bool = typer.Option(
        False,
        "--with-kb",
        help=(
            "Agents-mode only. After a successful deploy, also ingest each "
            "deployed agent's bundled [bold]agents/<name>/kb/[/bold] directory "
            "to the target runtime — deploy ships the prompt + contexts, but "
            "the knowledge base is otherwise a separate [bold]mdk kb ingest[/bold] "
            "step. Agents without a non-empty kb/ dir are skipped. No effect "
            "under --dry-run / --diff."
        ),
    ),
    smoke_test: bool | None = typer.Option(
        None,
        "--smoke-test/--no-smoke-test",
        help=(
            "Agents-mode only. After a successful deploy, dispatch ONE remote "
            "run of each just-deployed agent against the target and show "
            "pass/fail + response + run_id + cost — proving the new behavior "
            "is live, not just that /healthz answers. Input comes from the "
            "first row of [bold]evals/dataset.jsonl[/bold] (or, interactively, "
            "a one-time prompt). Default: OFFER interactively in a terminal; "
            "SKIP non-interactively unless [bold]--smoke-test[/bold] is passed. "
            "[bold]--no-smoke-test[/bold] always skips. A failed smoke test "
            "warns but never fails the (already-succeeded) deploy."
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
    if with_kb and resolved_mode != "agents":
        hint("--with-kb only applies to agents-mode deploys; ignoring.")
    if resolved_mode == "agents":
        if status:
            _deploy_status(target=target)
            return
        _deploy_agents(
            target=target,
            dry_run=dry_run,
            diff=diff,
            auto_recover=not no_auto_recover,
            with_kb=with_kb,
            smoke_test=smoke_test,
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

    # Validate-before-deploy guardrail. Run the same checks as
    # `mdk validate --all` against the project in cwd BEFORE building or
    # pushing any image — a broken agent.yaml / failing schema should
    # never ship. Runs even under --dry-run (validate, then show the
    # plan). `--skip-validate` bypasses it for the "I know this image is
    # good, the validation failure is environmental" case. No-op when
    # cwd isn't inside a project (nothing on disk to validate).
    if not skip_validate:
        _run_predeploy_validation()

    _print_plan(plan, dry_run=dry_run)

    if dry_run:
        # Next steps as a PLAN — nothing was deployed, so the block
        # describes what the operator would have after running for real.
        _print_next_steps(
            target_name=target_name,
            base_url=target_cfg.url.rstrip("/"),
            first_agent=_first_agent_name() or None,
            phase="planned",
        )
        # Even dry-runs emit the summary line so CI can confirm the plan
        # parsed cleanly. ok=true means "the plan is well-formed"; the
        # real deploy will emit ok=true|false based on /healthz.
        err.print(
            f"[dim]mdk_deploy_summary: target={target_name} "
            f"image={plan.image_tag} apps={','.join(plan.apps_to_update)} "
            f"dry_run=true ok=true[/dim]"
        )
        return

    # Pre-flight: the target Postgres must allow-list pgvector or the new
    # revision will silently ActivationFail (see _preflight_pgvector). Runs
    # before the build + revision roll so a misconfig fails fast with the fix
    # rather than spinning the /healthz gate below.
    _preflight_pgvector(plan)

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
        # Next steps — wording adapted: under --no-wait health was NOT
        # verified, so the health line is "run this to confirm" rather
        # than "✓ confirmed".
        _print_next_steps(
            target_name=target_name,
            base_url=target_cfg.url.rstrip("/"),
            first_agent=_first_agent_name() or None,
            phase="submitted",
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
            plan=plan,
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
        if _runtime_key_is_resolvable(target_cfg.key_env):
            # POST /run takes a RunSubmission ({kind, target, input},
            # extra="forbid") — the legacy {agent, input} shape 422s.
            run_body = json.dumps({"kind": "agent", "target": first_agent, "input": {}})
            err.print(
                f"  [cyan]curl -sS -X POST {base_url}/run "
                f"-H 'content-type: application/json' "
                f'-H "Authorization: Bearer {_bearer_shell_expr(target_cfg.key_env)}" '
                f"-d '{run_body}'[/cyan]"
            )
        else:
            # No bearer resolves → a raw curl would send an empty token and
            # the runtime returns auth_required. Hand over the one-shot
            # bootstrap commands instead of a curl that silently 401s.
            _render_bearer_bootstrap_hint(target_name=target_name, key_env=target_cfg.key_env)
    err.print(
        f"  [cyan]az containerapp logs show -g {plan.resource_group} "
        f"-n {plan.apps_to_update[0]} --tail 20[/cyan]"
    )
    err.print()

    # Concise "next steps" block — the smallest set of commands an
    # operator reaches for right after a verified rollout: where the API
    # lives, how to invoke an agent, the health check, and where traces
    # land. Human-output only.
    _print_next_steps(
        target_name=target_name,
        base_url=base_url,
        first_agent=first_agent,
        phase="verified",
    )

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
                git_sha=git_short_sha(),
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
        sha = git_short_sha() or "unknown"
        image_tag = f"movate:{version}-{sha}"
    elif ":" not in image_tag:
        # A bare tag like '0.8.2.5-ca0e04e' (no repository segment) would
        # otherwise become '<acr>/0.8.2.5-ca0e04e' — ACR reads that as a repo
        # name with no tag, defaults to ':latest', and fails cryptically with
        # MANIFEST_UNKNOWN. Normalize it to the default 'movate' repo.
        bare = image_tag
        image_tag = f"movate:{bare}"
        err.print(
            f"[dim]normalized bare --image-tag {bare!r} → {image_tag!r} "
            f"(default 'movate' repo)[/dim]"
        )

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
_HTTP_FORBIDDEN = 403
_HTTP_CONFLICT = 409
_HTTP_SERVICE_UNAVAILABLE = 503

# Sentinel returned by the upload helpers when the runtime returns 401.
# The outer `_deploy_agents` loop watches for this value so it can
# decide whether to auto-recover (mint a fresh key inside the pod and
# retry) or fall back to the human-readable message rendered by
# :func:`_render_unauthorized_message`.
_REASON_UNAUTHORIZED = "__unauthorized__"


@dataclass(frozen=True)
class AgentUploadOutcome:
    """Per-agent result of :func:`_upload_one_agent_bundle` (ADR 021 D4).

    ``error`` is ``None`` on success, or a failure reason string (which may
    be the :data:`_REASON_UNAUTHORIZED` sentinel the auto-recovery loop
    watches for). On success, ``changed`` reports whether the re-deploy
    actually published new content to the durable registry (``False`` for a
    no-op whose bundle bytes were unchanged), and ``published_version`` is
    the registry version now serving as ``latest`` (the runtime derives a
    ``<version>+<hash8>`` label when content changed without a version bump).
    """

    error: str | None
    changed: bool = True
    published_version: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


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
    with_kb: bool = False,
    smoke_test: bool | None = None,
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
                f"  [dim]Recovering a runtime bearer for "
                f"[bold]{target_name}[/bold] (~10 sec.)…[/dim]"
            )
            new_key = _attempt_auto_recovery(
                target_name=target_name,
                base_url=target_cfg.url.rstrip("/"),
                target_cfg=target_cfg,
            )
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

    # Persist the working bearer to ~/.movate/credentials so the
    # post-deploy curl example works from any new shell without a manual
    # `export`. The auto-recovery path already saves via
    # CredentialsStore; this also covers the common case where the key
    # arrived via a shell export and was never written to the file.
    # Best-effort — a credentials write failure must never abort a
    # deploy that otherwise succeeded.
    try:
        from movate.credentials.store import CredentialsStore  # noqa: PLC0415

        CredentialsStore().set(target_cfg.key_env, api_key)
        err.print(
            f"  [dim]bearer key ensured in [cyan]~/.movate/credentials[/cyan] "
            f"as [cyan]{target_cfg.key_env}[/cyan] — "
            "the curl example below will work in any new shell.[/dim]"
        )
    except Exception:  # never fail deploy for a credentials write
        pass

    base_url = target_cfg.url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}

    # Echo the target + URL + credential source (masked) at the remote
    # preflight, so a 401 from the upload below is self-diagnosing
    # (which key, which URL). stderr-only, honors --quiet.
    echo_remote_context(target_name, target_cfg, action="deploy")

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
        # Per-agent success outcome (changed flag + published version) so
        # the summary can report "published (v)" vs "no change" (ADR 021 D4).
        outcomes: dict[str, AgentUploadOutcome] = {}
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
                if result.ok:
                    uploaded.append(agent_dir.name)
                    outcomes[agent_dir.name] = result
                else:
                    failed.append((agent_dir.name, result.error or "unknown error"))

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
                    f"  [dim]Recovering a runtime bearer for "
                    f"[bold]{target_name}[/bold] (~10 sec.)…[/dim]"
                )
                new_key = _attempt_auto_recovery(
                    target_name=target_name, base_url=base_url, target_cfg=target_cfg
                )
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
                        if result.ok:
                            retry_uploaded.append(agent_dir.name)
                            outcomes[agent_dir.name] = result
                        else:
                            retry_failures.append((agent_dir.name, result.error or "unknown error"))
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
        outcome = outcomes.get(name)
        pub = outcome.published_version if outcome is not None else None
        ver = f" ([cyan]{pub}[/cyan])" if pub else ""
        # ADR 021 D4: report what was actually published. A no-op re-deploy
        # (content unchanged) is "no change", NOT a misleading "uploaded".
        if outcome is not None and not outcome.changed:
            err.print(f"  [dim]•[/dim] agent [bold]{name}[/bold]{ver} — no change")
        else:
            err.print(f"  [green]✓[/green] published agent [bold]{name}[/bold]{ver}")
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
            base_url=target_cfg.url.rstrip("/"),
            key_env=target_cfg.key_env,
        )
        # Offer to RUN a smoke test — the copy-pasteable commands above tell
        # the operator how; this dispatches one real remote run per agent so
        # they can confirm the just-deployed behavior is live (closes the
        # loop with the iterate fix: a redeploy now propagates edits, and a
        # smoke test proves the NEW behavior answers). A failed smoke test
        # warns but never unwinds the (already-succeeded) deploy.
        _maybe_run_smoke_test(
            target_name=target_name,
            uploaded=uploaded,
            project_root=project_root,
            smoke_test=smoke_test,
        )

    # --with-kb: sync each uploaded agent's bundled kb/ directory to the
    # target. Deploy ships the prompt + contexts; the knowledge base is
    # otherwise a separate `mdk kb ingest` step, so this closes that gap
    # for the common "bundle docs alongside the agent" case. Only on a
    # clean deploy, and only for agents that actually ship a non-empty kb/.
    if with_kb and ok and uploaded and not dry_run:
        _ingest_bundled_kb(
            uploaded=uploaded,
            project_root=project_root,
            target_name=target_name,
        )

    # ADR 021 D4: distinguish agents that actually published new content
    # from no-op re-deploys (content unchanged), additive to the existing
    # ``uploaded`` count so the line stays back-compatible.
    published_count = sum(1 for n in uploaded if outcomes.get(n, AgentUploadOutcome(None)).changed)
    unchanged_count = len(uploaded) - published_count
    err.print(
        f"[dim]mdk_deploy_summary: target={target_name} mode=agents "
        f"agents={len(agent_dirs)} uploaded={len(uploaded)} "
        f"published={published_count} unchanged={unchanged_count} "
        f"failed={len(failed)} "
        f"skills_uploaded={len(skill_uploaded)} skills_failed={len(skill_failed)} "
        f"ok={'true' if ok else 'false'}[/dim]"
    )
    if not ok:
        raise typer.Exit(code=2)


def _ingest_bundled_kb(
    *,
    uploaded: list[str],
    project_root: Path,
    target_name: str,
) -> None:
    """Ingest each uploaded agent's ``agents/<name>/kb/`` dir to the target.

    Shells out to ``mdk kb ingest <name> <kb_dir> --target <target>`` — reuses
    the standalone ingest path (chunk → embed → POST to the runtime) rather
    than duplicating it. Agents with no kb/ dir, or an empty one, are skipped
    silently. A failed ingest is non-fatal: the deploy already succeeded, so
    we warn and move on rather than unwinding a live rollout.
    """
    from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

    bin_name = mdk_bin_name()
    for name in uploaded:
        kb_dir = project_root / "agents" / name / "kb"
        if not kb_dir.is_dir() or not any(p.is_file() for p in kb_dir.rglob("*")):
            continue
        argv = [bin_name, "kb", "ingest", name, str(kb_dir), "--target", target_name]
        err.print(f"\n[dim]$ {' '.join(argv)}[/dim]")
        try:
            result = subprocess.run(argv, check=False)
        except FileNotFoundError:
            err.print(
                f"[yellow]⚠[/yellow] couldn't run [bold]{bin_name}[/bold] for KB "
                f"ingest of [bold]{name}[/bold] — run it manually."
            )
            continue
        if result.returncode != 0:
            err.print(
                f"[yellow]⚠[/yellow] KB ingest for [bold]{name}[/bold] exited "
                f"{result.returncode} (deploy itself succeeded)."
            )


def _bearer_shell_expr(key_env: str) -> str:
    """Shell expression that resolves the runtime bearer at curl-run time.

    Prefers an exported ``$<key_env>``; otherwise reads it straight out of
    the credentials file ``mdk deploy`` already wrote the key to. This is
    why the printed curl works in any fresh shell without a manual
    ``export``: the shell never sources ``~/.movate/credentials`` (only the
    ``mdk`` process autoloads it), so a bare ``$<key_env>`` would expand to
    empty and the runtime would 401 with ``auth_required``. Reading the file
    at curl-time also keeps the secret out of terminal scrollback.
    """
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    path = CredentialsStore().path
    try:
        display = f"~/{path.relative_to(Path.home())}"
    except ValueError:
        display = str(path)  # honors MOVATE_CREDENTIALS_PATH override
    return f"${{{key_env}:-$(grep -m1 '^{key_env}=' {display} | cut -d= -f2-)}}"


def _runtime_key_is_resolvable(key_env: str) -> bool:
    """Whether the bearer the printed curl resolves at run time is non-empty.

    Mirrors :func:`_bearer_shell_expr`'s resolution order: an exported
    ``$<key_env>``, else a saved entry in the credentials store. When
    neither resolves, the curl's ``Authorization: Bearer`` header expands
    to an empty token and the runtime answers ``auth_required`` — callers
    use this to print a bootstrap hint instead of a curl that 401s.
    """
    if os.environ.get(key_env, "").strip():
        return True
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    saved = CredentialsStore().get(key_env)
    return bool(saved and saved.strip())


def _render_bearer_bootstrap_hint(*, target_name: str, key_env: str) -> None:
    """Tell the operator how to obtain a runtime bearer when none is saved.

    The printed curl resolves its token from ``$<key_env>`` or the
    credentials file; with neither present it would send ``Bearer`` (empty)
    and the runtime returns ``auth_required``. Rather than hand over a curl
    that silently 401s, point at the one-shot commands that pull/mint + save
    a key so the next run resolves it automatically.
    """
    from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

    bin_name = mdk_bin_name()
    err.print(
        f"[yellow]⚠[/yellow] No runtime key for [bold]{target_name}[/bold] yet "
        f"([cyan]${key_env}[/cyan] is unset and no entry in the credentials "
        f"store) — a raw curl would send an empty bearer and get "
        f"[bold]auth_required[/bold]. Bootstrap a key first:"
    )
    err.print(
        f"  [cyan]{bin_name} auth pull-runtime-key {target_name} --keyvault <kv>[/cyan]"
        f"   [dim]# if bootstrap-api-key is already in Key Vault[/dim]"
    )
    err.print(
        f"  [cyan]{bin_name} auth refresh-runtime-key {target_name}[/cyan]"
        f"              [dim]# mint + save a fresh key inside the deployed app[/dim]"
    )
    err.print(
        f'[dim]Then [cyan]{bin_name} run <agent> "<input>" --target '
        f"{target_name}[/cyan] (it reads the saved key automatically).[/dim]"
    )


def _render_post_deploy_next_steps(
    *,
    target_name: str,
    uploaded: list[str],
    project_root: Path,
    base_url: str,
    key_env: str,
) -> None:
    """Print a "Next: run inference" block after a successful deploy.

    Leads with the recommended ``mdk run <agent> "<input>" --target``
    line per uploaded agent — it resolves the saved bearer, hits the
    correct route, and builds the ``RunSubmission`` body, so it sidesteps
    the raw-curl footguns entirely. A raw ``curl`` against ``POST /run``
    follows as the no-``mdk``-required fallback (skipped in favor of a
    key-bootstrap hint when no bearer resolves). The body of each request
    uses the first row of the agent's ``evals/dataset.jsonl`` as the
    sample input so the example actually exercises the agent's real schema
    (falls back to ``{"text":"..."}`` if no dataset row is available).

    The curl block is intentionally emitted WITHOUT Rich markup
    (uses ``markup=False``) — color is decoration only, but operators
    want to copy this verbatim into a shell. Two ways the old
    markup-wrapped form bit operators:

    * ``\\[/cyan]`` made Rich's parser treat ``\\[`` as an escape
      for the literal bracket, so the close tag never fired and
      ``[/cyan]`` ended up in the rendered output → zsh threw
      ``no matches found: [/cyan]`` when pasted.
    * Even when markup parsed correctly, terminals that don't
      render ANSI escapes (CI logs, pipes, ``script -q``) saw the
      raw escape sequences in the curl body.

    The JSON body is shell-quoted via :func:`shlex.quote` so
    apostrophes in the dataset rows (e.g. ``"We're evaluating…"``)
    don't break the single-quote wrap. ``shlex.quote`` produces
    POSIX-portable output that works in bash, zsh, sh.

    Previously emitted ``mdk submit`` invocations + ``mdk jobs list``
    hints, but operators consistently wanted curl — it's portable
    across shells, scriptable in any language, and doesn't require
    a working ``mdk`` install on the box doing the inference.

    Body shape: ``--data-binary @- <<'JSON' ... JSON`` heredoc with
    pretty-printed JSON. Was ``-d 'JSON-on-one-line'`` until 2026-05
    — operator hit ``"Invalid control character at position 121"``
    because Rich's terminal-wrap of the long single-line ``-d``
    arg embedded a literal ``0x0A`` newline inside the JSON string
    value of ``input.diff`` when they copied the output. Heredocs
    don't suffer from quote-escape or terminal-wrap issues:
    pretty-printed JSON has structural whitespace between keys
    (which JSON parsers ignore) so even if the operator's terminal
    inserts wrap newlines between fields, the body stays valid.
    String VALUES still need ``\\n`` escapes for embedded newlines
    (e.g. ``diff: "--- a/auth.py\\n+++..."``), and pretty-printing
    keeps each value on its own less-likely-to-wrap line.
    """
    from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

    bin_name = mdk_bin_name()
    err.print("[bold]Next:[/bold] run inference against the deployed runtime")
    err.print()

    # Primary, recommended path: `mdk run … --target` resolves the saved
    # bearer, hits the correct route, and builds the RunSubmission body for
    # the operator — none of the curl footguns (empty bearer, wrong body
    # shape) can bite here.
    for agent_name in sorted(uploaded):
        sample_input_json = _sample_input_for_agent(project_root, agent_name)
        err.print(
            f"  [cyan]{bin_name} run {agent_name} {shlex.quote(sample_input_json)} "
            f"--target {target_name}[/cyan]"
        )
    err.print()

    # Lower-level alternative: a raw curl against POST /run, for boxes
    # without `mdk` installed. The curl resolves its bearer from
    # $<key_env> or the credentials file — if neither is present the token
    # is empty and the runtime answers auth_required, so steer the operator
    # to bootstrap a key instead of pasting a curl that silently 401s.
    if not _runtime_key_is_resolvable(key_env):
        _render_bearer_bootstrap_hint(target_name=target_name, key_env=key_env)
        return

    err.print("[dim]Or, without mdk, a raw curl:[/dim]")
    err.print()
    for agent_name in sorted(uploaded):
        sample_input_json = _sample_input_for_agent(project_root, agent_name)
        # POST /run takes a RunSubmission: {kind, target, input} with
        # extra="forbid". The legacy {agent, input} shape 422s (extra
        # `agent`, missing `kind`/`target`), so emit the wire shape.
        body = json.dumps(
            {"kind": "agent", "target": agent_name, "input": json.loads(sample_input_json)},
            indent=2,
        )
        # ``soft_wrap=True`` keeps Rich from inserting its own line
        # breaks; combined with the heredoc, copy-paste survives any
        # terminal width.
        err.print(f"  # {agent_name}", markup=False)
        err.print(
            f"  curl -sS -X POST {base_url}/run \\\n"
            f"    -H 'content-type: application/json' \\\n"
            f'    -H "Authorization: Bearer {_bearer_shell_expr(key_env)}" \\\n'
            f"    --data-binary @- <<'JSON'\n"
            f"{body}\n"
            f"JSON",
            markup=False,
            soft_wrap=True,
        )
        err.print()


def _sample_input_for_agent(project_root: Path, agent_name: str) -> str:
    """Return a JSON string for the first dataset.jsonl row's ``input``
    field, or ``{"text":"..."}`` if no dataset / no valid row."""
    fallback = '{"text":"..."}'
    dataset_path = project_root / "agents" / agent_name / "evals" / "dataset.jsonl"
    if not dataset_path.is_file():
        return fallback
    try:
        first_line = next(
            (line for line in dataset_path.read_text().splitlines() if line.strip()),
            "",
        )
        if not first_line:
            return fallback
        row = json.loads(first_line)
        if isinstance(row, dict) and isinstance(row.get("input"), dict):
            return json.dumps(row["input"])
    except (OSError, ValueError):
        return fallback
    return fallback


def _first_dataset_input(project_root: Path, agent_name: str) -> dict[str, Any] | None:
    """Return the first ``evals/dataset.jsonl`` row's ``input`` dict for an
    agent, or ``None`` when there's no dataset / no usable first row.

    Distinct from :func:`_sample_input_for_agent` (which returns a
    ``{"text":"..."}`` *placeholder* string for the copy-pasteable curl
    examples): a smoke test needs a REAL input to dispatch, so the absence
    of a dataset must be distinguishable from a present-but-trivial one.
    Returns the parsed dict so the caller can decide whether to prompt
    (interactive) or skip with a hint (non-interactive).
    """
    dataset_path = project_root / "agents" / agent_name / "evals" / "dataset.jsonl"
    if not dataset_path.is_file():
        return None
    try:
        first_line = next(
            (line for line in dataset_path.read_text().splitlines() if line.strip()),
            "",
        )
        if not first_line:
            return None
        row = json.loads(first_line)
    except (OSError, ValueError):
        return None
    if isinstance(row, dict):
        candidate = row.get("input")
        if isinstance(candidate, dict):
            return candidate
    return None


def _smoke_test_is_interactive() -> bool:
    """Whether the smoke-test offer/prompt may block on the operator.

    Same TTY scheme the rest of the deploy / next-steps UX uses
    (:func:`movate.cli._next_steps.prompt_next_step`): both stdin AND
    stdout must be a terminal. Factored to a single seam so tests can
    flip interactivity without fighting ``CliRunner``'s stdin swap.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _maybe_run_smoke_test(  # noqa: PLR0912 — gating + per-agent input-resolution state machine
    *,
    target_name: str,
    uploaded: list[str],
    project_root: Path,
    smoke_test: bool | None,
) -> None:
    """Optionally dispatch ONE remote run per just-deployed agent.

    Gating (matches the TTY scheme used by the rest of the deploy /
    next-steps UX — :func:`movate.cli._next_steps.prompt_next_step`):

    * ``smoke_test is False`` (``--no-smoke-test``) → always skip.
    * ``smoke_test is True`` (``--smoke-test``) → always run.
    * ``smoke_test is None`` (default) → OFFER interactively when stdin AND
      stdout are a TTY; SKIP silently otherwise (CI / pipes / pytest).

    Input resolution per agent: prefer the first ``evals/dataset.jsonl``
    row's ``input``; if there's no dataset and we're interactive, prompt
    once for the JSON; if there's no dataset and we're non-interactive,
    skip THAT agent with a copy-pasteable hint.

    A failed smoke run is surfaced loudly (the ``✗`` + run view come from
    :func:`_dispatch_remote_agent`) but NEVER changes the deploy's exit
    code — the upload already succeeded, and a flaky first inference
    shouldn't unwind a live rollout. ``_dispatch_remote_agent`` signals a
    bad run by raising :class:`typer.Exit`; we catch it, warn, and move on.
    """
    interactive = _smoke_test_is_interactive()

    if smoke_test is False:
        return
    if smoke_test is None:
        if not interactive:
            return
        err.print()
        try:
            if not typer.confirm("Run a smoke test against the deployed runtime now?"):
                return
        except (KeyboardInterrupt, EOFError, typer.Abort):
            return

    # Reuse the remote-run machinery — do NOT rebuild the HTTP/render path.
    # Function-local import dodges the run.py ↔ deploy.py circular import at
    # module load (both pull from movate.cli.*).
    from movate.cli._output import Run  # noqa: PLC0415
    from movate.cli.run import _dispatch_remote_agent  # noqa: PLC0415

    err.print()
    err.print("[bold]Smoke test:[/bold] dispatching one remote run per agent…")
    for agent_name in sorted(uploaded):
        sample_input = _first_dataset_input(project_root, agent_name)
        if sample_input is None:
            if interactive:
                try:
                    raw = typer.prompt(
                        f"  no evals/dataset.jsonl for {agent_name!r} — paste input JSON",
                        default="",
                    ).strip()
                except (KeyboardInterrupt, EOFError, typer.Abort):
                    raw = ""
                if not raw:
                    err.print(
                        f"  [yellow]⚠[/yellow] no input for [bold]{agent_name}[/bold] — "
                        "skipping its smoke test."
                    )
                    continue
            else:
                # Non-interactive + no dataset: skip with a copy-pasteable hint.
                err.print(
                    f"  [yellow]⚠[/yellow] no evals/dataset.jsonl for "
                    f"[bold]{agent_name}[/bold] — skipping smoke test; run "
                    f"[cyan]mdk run {agent_name} --target {target_name} "
                    f"-i '<json>'[/cyan]"
                )
                continue
        else:
            raw = json.dumps(sample_input)

        err.print(f"  [dim]→ smoke test [bold]{agent_name}[/bold][/dim]")
        try:
            # output_format=Run.TEXT so the human-readable ✓/✗ + run_id +
            # cost render to the operator's terminal (this path only runs
            # interactively-or-explicitly, never under machine output).
            _dispatch_remote_agent(
                agent_name=agent_name,
                raw=raw,
                target=target_name,
                mock=False,
                output_format=Run.TEXT,
            )
        except typer.Exit as exc:
            # A non-zero smoke run (bad HTTP, run errored, etc.) must NOT
            # fail the deploy — it already succeeded. Warn clearly and keep
            # going; the failure detail was already printed by the dispatch.
            code = getattr(exc, "exit_code", 1)
            if code != 0:
                err.print(
                    f"  [yellow]⚠[/yellow] smoke test for [bold]{agent_name}[/bold] "
                    f"did not pass (deploy itself succeeded)."
                )


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
                f"  [dim]Recovering a runtime bearer for "
                f"[bold]{target_name}[/bold] (~10 sec.)…[/dim]"
            )
            new_key = _attempt_auto_recovery(
                target_name=target_name, base_url=base_url, target_cfg=target_cfg
            )
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


def _warn_if_shell_shadows_runtime_key(*, key_env: str, fresh_key: str) -> None:
    """Warn when a stale ``$<key_env>`` in the shell will shadow the just-saved key.

    Shell-exported env vars take precedence over ``~/.movate/credentials``
    (autoload only fills a var that isn't already set; it never clobbers a
    shell export). So a stale bearer left over from an earlier deploy would
    OVERRIDE the fresh key this deploy just minted + saved — and the next
    ``mdk run --target`` would send the stale key and 401. Warn at save
    time, on stderr, rather than letting the operator discover it through a
    confusing 401 later. Only warns when the shell value DIFFERS (a match is
    harmless) and never fails the deploy.
    """
    shell_value = os.environ.get(key_env, "").strip()
    if shell_value and shell_value != fresh_key:
        err.print(
            f"[yellow]⚠[/yellow] a stale [bold]{key_env}[/bold] is exported in your "
            f"shell and will OVERRIDE the key just saved (shell wins) — run "
            f"[bold]unset {key_env}[/bold] (and remove it from your profile) so the "
            f"new key takes effect."
        )


def _resolve_keyvault_name(target_cfg: Any) -> str | None:
    """The Key Vault name to pull the seeded bootstrap key from, or ``None``.

    Reads the target's ``azure_keyvault`` (a first-class optional field;
    also resolvable from an operator-added extra of the same name, since
    ``TargetConfig`` allows extras). The vault name embeds an
    operator-chosen suffix (see the infra Bicep ``nameSuffix`` param), so it
    can't be derived from ``azure_env`` alone — it must be configured
    explicitly to enable deploy's auto-pull recovery.
    """
    value = getattr(target_cfg, "azure_keyvault", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _discover_keyvault_in_resource_group(target_cfg: Any) -> str | None:
    """Best-effort: find the runtime's Key Vault in the target's resource group.

    The vault name is ``movate-{env}-kv{suffix}`` where ``suffix`` is an
    operator-chosen value (see the infra Bicep ``nameSuffix`` param), so it
    can't be reconstructed from config alone — but it CAN be discovered by
    listing the vaults in the target's resource group and picking the one
    whose name matches the ``movate-{env}-kv`` prefix. This lets deploy's
    auto-recovery prefer the guaranteed-trusted bootstrap key even when the
    target never set ``azure_keyvault`` explicitly (the live regression
    skipped the KV path for exactly this reason and fell straight to an
    under-scoped in-pod mint).

    Returns the discovered vault name, or ``None`` when ``az`` is missing,
    the resource group / env aren't configured, the list call fails, or no
    name matches. Never raises — discovery is purely an optimization on top
    of the in-pod mint fallback.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    resource_group = getattr(target_cfg, "azure_resource_group", None)
    azure_env = getattr(target_cfg, "azure_env", None)
    if not resource_group or not azure_env or shutil.which("az") is None:
        return None

    prefix = f"movate-{azure_env}-kv"
    try:
        result = subprocess.run(
            [
                "az",
                "keyvault",
                "list",
                "-g",
                resource_group,
                "--query",
                "[].name",
                "-o",
                "tsv",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None

    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    # Exact ``movate-{env}-kv`` (empty suffix) wins; otherwise the first
    # prefixed vault (``movate-{env}-kv-mvt`` etc.). Deterministic ordering
    # keeps recovery reproducible when a resource group holds several.
    for name in sorted(names):
        if name == prefix:
            return name
    for name in sorted(names):
        if name.startswith(prefix):
            return name
    return None


def _verify_bearer_roundtrip(*, base_url: str, key: str) -> tuple[bool, str]:
    """Confirm a candidate bearer is ADMIN-capable — the capability a deploy needs.

    The deploy bearer performs admin uploads (``POST/PUT /api/v1/agents``,
    both gated on the ``admin`` scope). A bearer that merely authenticates
    (``read``) is NOT good enough: it sails through a ``GET /api/v1/agents``
    probe yet 403s on the very first agent upload. So this probes the
    admin-scoped, read-only ``GET /api/v1/auth/keys`` endpoint instead and
    only declares the bearer ready when the runtime grants admin.

    Returns ``(verified, reason)``:

    * **2xx** — authenticated AND admin-capable → ``(True, "")``.
    * **403** — authenticated but the key lacks the ``admin`` scope (the
      live regression: an in-pod mint defaulted to ``read,run,eval``) →
      ``(False, "HTTP 403 (key lacks admin scope; uploads need admin)")``.
    * **401** — bad/unknown bearer → ``(False, "HTTP 401")``.
    * transport error → ``(False, "runtime unreachable (...)")``.

    The recovery path uses this so it never declares "bearer key ready" — nor
    overwrites a previously-working saved key — for a candidate that can't
    actually deploy.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(
                f"{base_url}/api/v1/auth/keys",
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        return False, f"runtime unreachable ({type(exc).__name__})"
    if resp.status_code < _HTTP_BAD_REQUEST:
        return True, ""
    if resp.status_code == _HTTP_FORBIDDEN:
        return False, "HTTP 403 (key lacks admin scope; uploads need admin)"
    return False, f"HTTP {resp.status_code}"


def _restore_saved_key(store: Any, env_var: str, prior_saved: str | None) -> None:
    """Roll the credentials store back to ``prior_saved`` after a failed recovery.

    The obtain step (pull / mint) writes its candidate into the store before
    we get to verify it. When verification fails we must not leave that
    rejected value behind: restore the previously-saved working key if there
    was one, otherwise delete the bad candidate so a later resolve doesn't
    pick it up.
    """
    if prior_saved:
        store.set(env_var, prior_saved)
    else:
        store.delete(env_var)


def _obtain_recovery_key(*, target_name: str, keyvault: str | None) -> tuple[str | None, str]:
    """Obtain a candidate runtime bearer for recovery, preferring a KV pull.

    Returns ``(key_or_none, source_label)``. Tries, in order:

    1. **Pull** the seeded ``bootstrap-api-key`` from Key Vault when the
       target names one — guaranteed-trusted, because the runtime seeds the
       matching ``ApiKeyRecord`` from that same secret on every cold start.
    2. **Mint** a fresh key inside the Container App via ``az containerapp
       exec`` — best-effort: a non-durable (SQLite-in-pod) or multi-replica
       runtime may never read the store the exec wrote to, so this key can
       still 401. The caller VERIFIES before keeping it.

    Both steps SAVE the candidate to the credentials store as a side effect;
    the caller snapshots the prior key first and rolls back on failure.
    Prints the underlying reason on a failed step. Returns ``(None, "")`` when
    every available step failed.
    """
    if keyvault:
        from movate.cli.auth import (  # noqa: PLC0415
            PullRuntimeKeyError,
            pull_runtime_key_inline,
        )

        try:
            key, _env_var = pull_runtime_key_inline(target_name, keyvault=keyvault)
        except PullRuntimeKeyError as exc:
            err.print(
                f"  [yellow]⚠[/yellow] could not pull the bootstrap key from "
                f"[bold]{keyvault}[/bold] ({exc}); falling back to minting one "
                f"in-pod."
            )
        else:
            return key, f"pulled from Key Vault ({keyvault})"

    from movate.cli.auth import (  # noqa: PLC0415
        RefreshRuntimeKeyError,
        refresh_runtime_key_inline,
    )
    from movate.core.auth import SCOPE_FLEET_ADMIN  # noqa: PLC0415

    try:
        # The deploy bearer performs admin uploads (POST/PUT /api/v1/agents),
        # so the in-pod mint MUST carry an admin grant — a default-scoped
        # (read,run,eval) key authenticates but 403s on the first upload.
        # fleet-admin expands to the full scope set at the runtime, covering
        # admin + everything the rest of the deploy touches.
        key, _env_var = refresh_runtime_key_inline(target_name, scopes=[SCOPE_FLEET_ADMIN])
    except RefreshRuntimeKeyError as exc:
        err.print(
            f"  [red]✗[/red] auto-recovery failed: {exc}. "
            f"Run [bold]mdk auth refresh-runtime-key {target_name}[/bold] "
            "manually to debug."
        )
        return None, ""
    return key, "minted in-pod (fleet-admin)"


def _attempt_auto_recovery(*, target_name: str, base_url: str, target_cfg: Any) -> str | None:
    """Recover a working, ADMIN-CAPABLE runtime bearer for ``target_name``.

    Prefers PULLING the seeded ``fleet-admin`` bootstrap key from Key Vault
    (when the target names one, or when one can be DISCOVERED in the target's
    resource group) — it's the key the running runtime is guaranteed to trust
    AND it carries admin reach — and otherwise mints a ``fleet-admin`` key
    inside the Container App. EITHER way the candidate is verified for the
    capability the deploy NEEDS (``GET /api/v1/auth/keys``, an admin-scoped
    read) before it's declared ready: an in-pod mint can land in a store the
    serving replica never reads (non-durable SQLite, multi-replica) AND a
    read-only key authenticates but 403s on the admin uploads — so neither
    "minting succeeded" nor "it can authenticate" implies "it can deploy".

    On success: keeps the candidate saved, prints a verified-ready line, warns
    about a shell var that would shadow it, and returns the bearer.

    On failure (candidate rejected as non-admin-capable, or none obtainable):
    RESTORES the previously-saved key — never clobbering a working bearer with
    one that 401s/403s — points the operator at ``mdk auth pull-runtime-key``
    (the guaranteed-trusted path), and returns ``None``. Never raises —
    recovery is best-effort.
    """
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    env_var = target_cfg.key_env
    store = CredentialsStore()
    # Snapshot the previously-saved key BEFORE any obtain step clobbers it, so
    # a candidate that fails verification can be rolled back rather than
    # leaving a 401ing value where a working key used to be.
    prior_saved = store.get(env_var)
    keyvault = _resolve_keyvault_name(target_cfg)
    if keyvault is None:
        # The target didn't name a vault, but the bootstrap key (fleet-admin,
        # guaranteed-trusted) beats an in-pod mint — try to DISCOVER the
        # runtime's vault in the resource group before falling back.
        keyvault = _discover_keyvault_in_resource_group(target_cfg)
        if keyvault is not None:
            err.print(
                f"  [dim]Discovered Key Vault [bold]{keyvault}[/bold] in the "
                f"resource group; pulling the bootstrap key.[/dim]"
            )

    candidate, source = _obtain_recovery_key(target_name=target_name, keyvault=keyvault)
    if candidate is None:
        return None  # the obtain step already explained why

    verified, reason = _verify_bearer_roundtrip(base_url=base_url, key=candidate)
    if verified:
        store.set(env_var, candidate)
        err.print(
            f"  [green]✓[/green] bearer key ready (saved as [cyan]{env_var}[/cyan], "
            f"verified against the runtime — {source})."
        )
        # A freshly-saved key only takes effect for `mdk run` if no stale
        # shell export shadows it — warn now so the operator isn't surprised
        # by a 401 on the next run against this target.
        _warn_if_shell_shadows_runtime_key(key_env=env_var, fresh_key=candidate)
        return candidate

    # Candidate rejected — roll back so we never leave a 401ing key behind,
    # then steer the operator to the guaranteed-trusted recovery path.
    _restore_saved_key(store, env_var, prior_saved)
    kept = f"Kept your previously-saved [cyan]{env_var}[/cyan]. " if prior_saved else ""
    kv = f" {keyvault}" if keyvault else " <kv>"
    err.print(
        f"  [red]✗[/red] the key {source} was rejected by the runtime ({reason}) — "
        f"NOT saving it. {kept}The seeded bootstrap key in Key Vault is the one the "
        f"runtime trusts — recover with [bold]mdk auth pull-runtime-key "
        f"{target_name} --keyvault{kv}[/bold]."
    )
    return None


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
) -> AgentUploadOutcome:
    """Upload a single agent bundle via multipart POST /api/v1/agents.

    Tries ``POST /api/v1/agents`` first (creates the agent). On **409**
    (already-exists), falls back to ``PUT /api/v1/agents/{name}`` to
    re-publish the bundle — so a re-deploy of CHANGED content actually
    updates what runs (ADR 021). The runtime is content-addressed: an
    unchanged re-deploy is a no-op it reports as ``changed=false`` (we
    surface that as "no change" rather than a misleading "uploaded").

    Returns an :class:`AgentUploadOutcome`: ``error=None`` on success
    (with ``changed`` + ``published_version`` from the runtime's
    response), or a failure reason on ``error`` (possibly the
    :data:`_REASON_UNAUTHORIZED` sentinel). Caller renders the result; we
    don't print here so the loop can aggregate.
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
        return AgentUploadOutcome(error=f"missing {agent_yaml.relative_to(agent_dir)}")
    if not prompt_md.is_file():
        return AgentUploadOutcome(error=f"missing {prompt_md.relative_to(agent_dir)}")

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

    # Schema parts. Two ways an agent declares its I/O contract:
    #   1. **Path-ref** — `schema: {input: ./schema/input.yaml}` points at
    #      a file on disk. We upload that file (compiling YAML→JSON as
    #      needed) — primary path, unchanged.
    #   2. **Inline shorthand** — `schema: {input: {text: string}}` lives
    #      directly in agent.yaml; there's NO schema file on disk. The
    #      default `mdk add` template uses this form. Without a fallback
    #      the upload omits both schema parts and the runtime's
    #      individual-files endpoint rejects with HTTP 400 (it requires
    #      schema/input.json + schema/output.json). So when a file is
    #      absent we materialize the loader's COMPILED JSON Schema and
    #      upload it as input.json / output.json — exactly what the
    #      runtime persists.
    #
    # The loader is consulted lazily — only when at least one schema file
    # is missing — so path-ref agents stay a pure filesystem read.
    compiled = (
        _compiled_schemas_for_upload(agent_dir)
        if input_schema is None or output_schema is None
        else (None, None)
    )
    _append_schema_part(
        files, field="input_schema", label="input", file_path=input_schema, compiled=compiled[0]
    )
    _append_schema_part(
        files, field="output_schema", label="output", file_path=output_schema, compiled=compiled[1]
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
        return AgentUploadOutcome(error=f"network error: {exc}")

    if response.status_code == _HTTP_CREATED:
        return _agent_upload_success(response)
    if response.status_code == _HTTP_CONFLICT:
        # Already exists — re-publish via PUT so a re-deploy of CHANGED
        # content actually updates what runs (ADR 021). The runtime is
        # content-addressed: PUT writes a new immutable registry version
        # only when the bundle bytes changed, and reports ``changed=false``
        # for an unchanged re-deploy (which we surface as "no change"
        # rather than the misleading "✓ uploaded" this used to print).
        name = agent_dir.name
        try:
            put_response = client.put(
                f"{base_url}/api/v1/agents/{name}",
                files=files,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return AgentUploadOutcome(error=f"network error: {exc}")
        if put_response.status_code == _HTTP_OK:
            return _agent_upload_success(put_response)
        if put_response.status_code == _HTTP_UNAUTHORIZED:
            return AgentUploadOutcome(error=_REASON_UNAUTHORIZED)
        try:
            put_body = put_response.json()
        except Exception:
            put_body = {"raw": put_response.text[:200]}
        return AgentUploadOutcome(error=f"HTTP {put_response.status_code}: {put_body!r}")
    # 401 from the runtime means our bearer token was rejected. The
    # bearer was set (we passed the env-var-empty preflight) but the
    # runtime has no matching ApiKeyRecord. The auth path is opaque
    # token + DB lookup (no JWT signing), so a 401 here means the
    # record is missing from storage — almost always because the
    # ApiKeyRecord table didn't survive the last revision recycle
    # (SQLite-in-pod fallback). Return the sentinel so the outer
    # loop can decide between auto-recovery and reporting.
    if response.status_code == _HTTP_UNAUTHORIZED:
        return AgentUploadOutcome(error=_REASON_UNAUTHORIZED)
    # Try to surface the runtime's error body verbatim so the
    # operator sees the actual validation failure.
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:200]}
    return AgentUploadOutcome(error=f"HTTP {response.status_code}: {body!r}")


def _agent_upload_success(response: object) -> AgentUploadOutcome:
    """Build a success :class:`AgentUploadOutcome` from a 200/201 response.

    Parses the runtime's ``published_version`` + ``changed`` fields (ADR
    021 D4) so the deploy summary reports what was actually published.
    Tolerates an older runtime that doesn't send those fields — defaults to
    ``changed=True`` (the pre-ADR-021 assumption) so the output stays
    forward-compatible.
    """
    import httpx  # noqa: PLC0415

    assert isinstance(response, httpx.Response)
    try:
        body = response.json()
    except Exception:
        body = {}
    changed = bool(body.get("changed", True)) if isinstance(body, dict) else True
    published_version = (
        body.get("published_version") or body.get("version") if isinstance(body, dict) else None
    )
    return AgentUploadOutcome(error=None, changed=changed, published_version=published_version)


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


def _append_schema_part(
    files: list[tuple[str, tuple[str, bytes, str]]],
    *,
    field: str,
    label: str,
    file_path: Path | None,
    compiled: bytes | None,
) -> None:
    """Append one schema multipart part to ``files``, if we have content.

    Prefers the on-disk file (``file_path``, compiled YAML→JSON as
    needed) — the primary, unchanged path. Falls back to ``compiled``
    bytes (the loader's compiled inline schema) when no file exists. The
    fallback is uploaded under the canonical ``<label>.json`` name the
    runtime persists. No-op when neither source is available.
    """
    if file_path is not None:
        schema_bytes, name = _schema_bytes_for_upload(file_path, label=label)
        files.append((field, (name, schema_bytes, "application/json")))
    elif compiled is not None:
        files.append((field, (f"{label}.json", compiled, "application/json")))


def _compiled_schemas_for_upload(agent_dir: Path) -> tuple[bytes | None, bytes | None]:
    """Materialize an agent's COMPILED I/O JSON Schemas as upload bytes.

    For inline-shorthand agents (the default ``mdk add`` template:
    ``schema: {input: {text: string}, output: {message: string}}``)
    there's no ``schema/*.json`` file on disk, so the file-based upload
    path uploads nothing — and the runtime's individual-files endpoint
    rejects the bundle with HTTP 400 (it requires ``schema/input.json``
    + ``schema/output.json``). This fallback runs :func:`load_agent`,
    which compiles the inline shorthand into a full JSON Schema in
    memory, and serializes ``bundle.input_schema`` /
    ``bundle.output_schema`` to JSON bytes ready for the multipart
    upload. The on-disk files are never touched.

    Returns ``(input_bytes, output_bytes)``. On any load failure we
    return ``(None, None)`` so the upload proceeds unchanged and the
    runtime surfaces the canonical validation error — we don't want a
    transient load hiccup here to mask the real cause.
    """
    from movate.core.loader import AgentLoadError, load_agent  # noqa: PLC0415

    try:
        bundle = load_agent(agent_dir)
    except AgentLoadError:
        return None, None
    input_bytes = json.dumps(bundle.input_schema, separators=(",", ":")).encode()
    output_bytes = json.dumps(bundle.output_schema, separators=(",", ":")).encode()
    return input_bytes, output_bytes


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


def _run_predeploy_validation() -> None:
    """Validate the project in cwd before a runtime-mode deploy builds.

    Reuses the exact logic behind ``mdk validate --all`` — imports the
    per-item validators (:func:`movate.cli.validate._validate_agent` /
    ``_validate_workflow``) and runs each against the agents + workflows
    discovered under the project root (walked up from cwd). This is the
    same discovery + per-item validation ``_validate_all`` performs; we
    call the primitives directly rather than ``_validate_all`` itself so
    we skip its summary table + interactive "what next?" picker (both
    inappropriate mid-deploy) and keep this a clean pass/fail gate.

    No-op when cwd isn't inside a project — there's nothing on disk to
    validate (e.g. a ``--skip-build`` rollback from outside any project
    tree). On ANY agent/workflow failure, prints the count + a pointer
    and raises ``typer.Exit(1)`` so the caller aborts BEFORE building or
    pushing any image. Each item's own failure detail was already
    printed by the validator it delegates to.
    """
    from movate.cli._resolve import walk_up_for_project_root  # noqa: PLC0415

    project_root = walk_up_for_project_root()
    if project_root is None:
        # Outside a project (or a bare rollback) — nothing to validate.
        return

    # Same discovery as validate._validate_all: every agent.yaml under
    # agents/ and every workflow.yaml under workflows/, sorted for
    # deterministic output.
    agent_dirs = (
        sorted(p.parent for p in (project_root / "agents").glob("*/agent.yaml"))
        if (project_root / "agents").is_dir()
        else []
    )
    workflow_dirs = (
        sorted(p.parent for p in (project_root / "workflows").glob("*/workflow.yaml"))
        if (project_root / "workflows").is_dir()
        else []
    )
    if not agent_dirs and not workflow_dirs:
        # Empty workspace — vacuous pass, same as `mdk validate --all`.
        return

    # Import the underlying validators (the functions behind
    # `mdk validate`) — do NOT re-implement validation here.
    from movate.cli.validate import _validate_agent, _validate_workflow  # noqa: PLC0415

    err.print()
    err.print(
        "[bold]Validating project before deploy[/bold] [dim](--skip-validate to bypass)[/dim]"
    )

    failed: list[str] = []
    for agent_dir in agent_dirs:
        try:
            _validate_agent(agent_dir, strict=False, run_linter=True)
        except typer.Exit:
            # _validate_agent already printed the failure detail.
            failed.append(agent_dir.name)
    for workflow_dir in workflow_dirs:
        try:
            _validate_workflow(workflow_dir)
        except typer.Exit:
            failed.append(workflow_dir.name)

    if failed:
        error(
            f"validation failed for {len(failed)} item(s) "
            f"({', '.join(failed)}); aborting before build. Fix the "
            f"issue(s) above, or re-run with [bold]--skip-validate[/bold] "
            f"to deploy anyway."
        )
        raise typer.Exit(code=1)


def _print_next_steps(
    *,
    target_name: str,
    base_url: str,
    first_agent: str | None,
    phase: str,
) -> None:
    """Print a concise post-deploy "next steps" block (human output only).

    ``phase`` adapts the wording to what's actually true at the call site:

    * ``"verified"`` — full rollout + ``/healthz`` confirmed. Leads with
      ``✓ deployed``.
    * ``"submitted"`` — ``--no-wait``: the update was submitted but health
      was NOT polled, so the header says "submitted" and the health line
      reads as "confirm with…" rather than implying it's already up.
    * ``"planned"`` — ``--dry-run``: nothing ran; the block previews what
      the operator would have after a real deploy.

    The API FQDN is taken from ``base_url`` (the target's resolved URL —
    the same one ``/healthz`` is polled against), so it's always the
    real deployed endpoint. The ``test:`` line uses the first project
    agent when one exists, else a ``<agent>`` placeholder.
    """
    from movate.cli._next_steps import mdk_bin_name  # noqa: PLC0415

    bin_name = mdk_bin_name()
    agent_token = first_agent or "<agent>"

    if phase == "planned":
        header = f"[bold](dry-run)[/bold] after deploy you'd have → {target_name}"
        health_label = "health"
    elif phase == "submitted":
        header = (
            f"[green]✓[/green] deploy submitted to {target_name} "
            "[dim](health not yet verified — --no-wait)[/dim]"
        )
        health_label = "verify"
    else:  # "verified"
        header = f"[green]✓[/green] deployed to {target_name}"
        health_label = "health"

    err.print()
    err.print(header)
    err.print(f"  [bold]API:[/bold]    {base_url}")
    err.print(
        f"  [bold]test:[/bold]   [cyan]{bin_name} run {agent_token} "
        f'"<input>" --target {target_name}[/cyan]'
    )
    err.print(
        f"  [bold]{health_label}:[/bold] [cyan]{bin_name} doctor --target {target_name}[/cyan]"
    )
    err.print(
        "  [bold]traces:[/bold] App Insights → Transaction search "
        "[dim](paste a run's trace_id)[/dim]"
    )
    err.print()


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


def _preflight_pgvector(plan: DeployPlan) -> None:
    """Abort the deploy if the target Postgres doesn't allow-list pgvector.

    The runtime runs ``CREATE EXTENSION IF NOT EXISTS vector`` at startup
    (``storage/postgres.py`` ``_ensure_pgvector``) and raises if it fails. On
    Azure Postgres Flexible Server that ``CREATE EXTENSION`` only succeeds when
    ``vector`` is in the *value* of the ``azure.extensions`` server parameter
    (being in ``allowedValues`` isn't enough). When it isn't, the new revision
    ActivationFails, ACA silently keeps the OLD revision serving, and the
    ``/healthz`` gate below spins ("still seeing version X, retrying…") with no
    clear cause. Catch it here — before rolling the revision.

    Acts only on a confirmed misconfig. A missing Postgres server, an
    sqlite-backed target, or any ``az`` error all degrade to a no-op (the
    shared check returns ``skip``), so this never blocks a deploy it can't
    reason about. Reuses the doctor's ``az`` helpers — no hand-rolled
    subprocess here.
    """
    from movate.cli._azure_doctor import check_pgvector_allowlisted  # noqa: PLC0415

    result = check_pgvector_allowlisted(plan.subscription, plan.resource_group)
    if result.status == "ok":
        err.print(f"  [green]✓[/green] pgvector allow-listed on {result.server}")
        return
    if result.status == "skip":
        # No Postgres server to gate on (sqlite target / not deployed) or az
        # couldn't resolve it — proceed silently rather than block a deploy we
        # can't reason about.
        return
    # status == "error": vector isn't enabled — rolling the revision now would
    # ActivationFail. Abort with the one-shot fix (the detail names the exact
    # `az ... parameter set` + restart commands).
    error(f"pre-flight failed — {result.detail}")
    raise typer.Exit(code=2)


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


def _diagnose_failed_revision(*, resource_group: str, app_name: str) -> str | None:
    """Inspect the latest ACA revision and return a root-cause string when
    it's unhealthy, else ``None``.

    The ``/healthz`` gate can only ever observe the OLD revision: when the new
    revision fails to start (image needs an unprovisioned dependency, container
    crashes/OOMs, bad env, image-pull failure, non-zero exit) ACA marks it
    ``ActivationFailed`` (or leaves a failed running-state) and *silently keeps
    the old revision serving*. The poll then just spins until it times out,
    telling the operator nothing. This queries the latest revision directly so
    the timeout can surface the actual cause.

    Returns a concise human-readable cause (e.g.
    ``revision movate-prod-api--abc123 ActivationFailed: <detail>`` or
    ``container exited (code 1) — likely a startup/config error``) when the
    revision looks broken; returns ``None`` when it looks healthy or still
    in-progress, so the caller falls back to the generic timeout message.

    This runs at *failure* time on the critical exit path, so it must never
    raise: ``az`` missing, a non-zero exit, non-JSON output, a network error,
    or an unexpected shape all degrade to ``None``.
    """
    if shutil.which("az") is None:
        return None
    # Sort newest-first and take the latest revision. ``revision list`` is more
    # robust than ``revision show`` (which needs the exact revision name, which
    # we don't always know here). Suppress all output noise — we only want JSON.
    cmd = [
        "az",
        "containerapp",
        "revision",
        "list",
        "--resource-group",
        resource_group,
        "--name",
        app_name,
        "--query",
        # Newest revision's health-relevant fields. createdTime sorts it.
        "sort_by([].{name:name, created:properties.createdTime, "
        "active:properties.active, provisioningState:properties.provisioningState, "
        "runningState:properties.runningState, "
        "healthState:properties.healthState}, &created)[-1]",
        "-o",
        "json",
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except (OSError, ValueError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        rev = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rev, dict):
        return None

    name = rev.get("name") or "?"
    provisioning = (rev.get("provisioningState") or "").strip()
    running = (rev.get("runningState") or "").strip()
    health = (rev.get("healthState") or "").strip()

    # Provisioning-level failure: the revision never came up. This is the
    # pgvector-incident signature (ActivationFailed) and image-pull / bad-env
    # failures.
    if provisioning in {"Failed", "ActivationFailed"}:
        detail = provisioning
        if running and running.lower() not in {"running", "processing", ""}:
            detail += f", runningState={running}"
        return f"revision {name} {detail}"

    # Running-state failure: provisioned but the container is crash-looping /
    # degraded / stopped (OOM, non-zero exit, failing startup probe).
    running_lc = running.lower()
    if running_lc and running_lc not in {"running", "processing", "activating", "unknown"}:
        cause = f"revision {name} runningState={running}"
        if health and health.lower() not in {"healthy", "none", "unknown", ""}:
            cause += f" (healthState={health})"
        cause += " — container did not stay up (likely a startup/config error or crash)"
        return cause

    # Healthy / still-provisioning / indeterminate → let the caller fall back
    # to the generic timeout message.
    return None


async def _wait_for_healthz(
    *, url: str, expected_version: str, timeout: float, plan: DeployPlan | None = None
) -> None:
    """Poll ``GET /healthz`` until the response's ``version`` matches the
    new deploy. ACA's rolling restart can take 30s-2min; we give it
    ``timeout`` seconds, then bail with exit 124.

    On timeout, if ``plan`` is supplied, query the latest ACA revision and
    surface its actual failure reason (ActivationFailed / crash / image-pull)
    instead of the bare "rollout may still be in progress" message — the
    common case when the new revision never started and ACA silently kept the
    old one serving."""
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
                await _emit_healthz_timeout(
                    expected_version=expected_version, timeout=timeout, plan=plan
                )
                # 124 is the conventional `timeout` exit code so bash
                # scripts can branch on it.
                sys.exit(124)
            await asyncio.sleep(poll_interval)


async def _emit_healthz_timeout(
    *, expected_version: str, timeout: float, plan: DeployPlan | None
) -> None:
    """Print the health-gate timeout message — with the ACA revision's root
    cause when we can determine one, else the generic fallback. Never raises."""
    cause: str | None = None
    app_name = plan.apps_to_update[0] if plan and plan.apps_to_update else None
    if plan is not None and app_name is not None:
        # Blocking subprocess off the event loop. _diagnose_failed_revision is
        # contracted never to raise, but guard the thread hop too.
        try:
            cause = await asyncio.to_thread(
                _diagnose_failed_revision,
                resource_group=plan.resource_group,
                app_name=app_name,
            )
        except Exception:
            # diagnosis is best-effort — never let it sink the timeout path
            cause = None

    if cause and plan is not None and app_name is not None:
        err.print(
            f"[red]✗[/red] new revision failed to start after {timeout:.0f}s — "
            "the OLD revision is still serving (ACA kept it running).\n"
            f"  [bold]cause:[/bold] {cause}\n"
            f"  [bold]logs:[/bold]  [cyan]az containerapp logs show "
            f"-g {plan.resource_group} -n {app_name} --tail 50[/cyan]"
        )
        return

    err.print(
        f"[yellow]⏱[/yellow] timed out after {timeout:.0f}s waiting "
        f"for version {expected_version}; ACA rollout may still be "
        "in progress. Check manually with `az containerapp revision list`."
    )
