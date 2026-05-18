"""``mdk infra apply`` — provision Azure resources via Bicep + auto-seed bootstrap key.

Wraps two operator concerns that today require running two unrelated
commands in sequence:

1. ``az deployment group create -g <rg> -n main -f infra/azure/main.bicep``
   to provision (or update) Key Vault, ACR, Postgres, Container App
   Environment + the Container Apps themselves.
2. ``mdk auth bootstrap-seed <target> --keyvault <kv>`` to mint the
   bootstrap API key, upload it to Key Vault as ``bootstrap-api-key``,
   and save it locally. The Container App Bicep references this
   secret as ``MOVATE_SEED_API_KEY``; without it the pod boots but
   cannot authenticate any deploys.

After this command, the operator can run ``mdk deploy --target <name>``
immediately — no manual step in between.

What ``mdk infra apply`` does NOT do (deliberately):

* It does not run ``scripts/azure-bootstrap.sh`` — that one-time
  per-env identity setup (RG + service principal + OIDC) lives
  upstream of the Bicep deploy and is owned by the platform team,
  not by the agent operator.
* It does not build or push the runtime image — that's
  ``mdk deploy --target <name> --mode runtime``. The image must
  already be in ACR before the Container App Bicep can pull it.
* It does not populate provider secrets (``openai-api-key`` etc.)
  in Key Vault — those land via ``az keyvault secret set`` or the
  upstream platform's secret-injection flow.

See ``infra/azure/README.md`` for the two-pass deploy pattern; this
command implements the canonical second pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from movate.cli._console import error, hint, success
from movate.cli.auth import BootstrapSeedError, bootstrap_seed_inline
from movate.core.user_config import UserConfigError, load_user_config

infra_app = typer.Typer(
    name="infra",
    help=(
        "Provision Azure infrastructure for a movate target. Wraps "
        "the Bicep deploy + auto-chains into [bold]mdk auth bootstrap-seed[/bold] "
        "so a fresh environment becomes one command."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


_DEFAULT_BICEP = "infra/azure/main.bicep"


@infra_app.command("apply")
def apply(  # noqa: PLR0912 — orchestrator; validation + az + optional seed reads clearer flat
    target: str = typer.Argument(
        ...,
        help="Deployment target name (from `mdk config list-targets`).",
    ),
    keyvault: str = typer.Option(
        None,
        "--keyvault",
        help=(
            "Azure Key Vault name (no FQDN — e.g. `movate-dev-kv-mvt`). "
            "Required when --no-seed isn't set, because the auto-chain "
            "into [bold]bootstrap-seed[/bold] needs to know where to "
            "upload the secret. With --no-seed, the operator owns "
            "secret population separately."
        ),
    ),
    bicep: str = typer.Option(
        _DEFAULT_BICEP,
        "--bicep",
        help=(
            "Path to the Bicep template, relative to cwd. Defaults to "
            f"[bold]{_DEFAULT_BICEP}[/bold] — the canonical entry "
            "point in the movate-cli source tree."
        ),
    ),
    parameters: str = typer.Option(
        None,
        "--parameters",
        "-p",
        help=(
            "Path to a [bold].bicepparam[/bold] file. When omitted, "
            "defaults to [bold]infra/azure/main.<env>.bicepparam[/bold] "
            "(derived from the target's [bold]azure_env[/bold]). "
            "Pass [bold]@<file>[/bold]-style overrides on the az "
            "command line via [bold]--az-arg[/bold]."
        ),
    ),
    deployment_name: str = typer.Option(
        "main",
        "--name",
        "-n",
        help=(
            "Deployment name passed to [bold]az deployment group create -n[/bold]. "
            "Defaults to [bold]main[/bold] — overwrites the same named deployment "
            "each apply (the Bicep template itself is idempotent)."
        ),
    ),
    no_seed: bool = typer.Option(
        False,
        "--no-seed",
        help=(
            "Skip the auto-chain into [bold]mdk auth bootstrap-seed[/bold] "
            "after a successful Bicep deploy. Use this when the "
            "bootstrap key was already minted in a prior run + you "
            "just want to refresh infra (e.g. scale changes), or when "
            "you intend to mint the seed key manually for an unusual "
            "tenant id."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the [bold]az deployment group create[/bold] "
            "command that WOULD run, plus the bootstrap-seed "
            "follow-up, without executing either. Nothing on Azure "
            "is touched."
        ),
    ),
) -> None:
    """Provision Azure infrastructure + auto-seed the bootstrap key.

    Resolves the target's Azure addressing (subscription + resource
    group + env), pins the subscription via [bold]az account set[/bold]
    if needed, runs [bold]az deployment group create[/bold] against
    the canonical [bold]infra/azure/main.bicep[/bold], then chains
    into [bold]mdk auth bootstrap-seed[/bold] so the deployed runtime
    has a known seed key before any [bold]mdk deploy[/bold].

    [bold]Examples:[/bold]

      [dim]# Fresh environment bootstrap, end to end:[/dim]
      $ mdk infra apply dev --keyvault movate-dev-kv-mvt

      [dim]# Re-apply infra after a Bicep change (skip seed —[/dim]
      [dim]# the bootstrap key already exists):[/dim]
      $ mdk infra apply dev --no-seed

      [dim]# Preview the az command without executing:[/dim]
      $ mdk infra apply dev --keyvault movate-dev-kv-mvt --dry-run

      [dim]# Override the param file (CI / staging):[/dim]
      $ mdk infra apply staging --keyvault movate-staging-kv-mvt \\\\
          --parameters infra/azure/main.staging.bicepparam

    [bold]Exit codes:[/bold]

    * 0 — Bicep applied + seed minted (or seed skipped via --no-seed)
    * 1 — Bicep deploy failed (operator sees az stderr verbatim)
    * 2 — config / argument error before any az call ran
    """
    # ------------------------------------------------------------------
    # Argument validation — fail fast before any az call.
    # ------------------------------------------------------------------
    if not no_seed and not keyvault:
        error(
            "--keyvault is required unless --no-seed is set. The "
            "auto-chain into `mdk auth bootstrap-seed` needs to know "
            "where to upload the secret."
        )
        raise typer.Exit(code=2)

    try:
        cfg = load_user_config()
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    if target not in cfg.targets:
        registered = sorted(cfg.targets) or ["<none>"]
        error(
            f"unknown target {target!r}. Registered: "
            f"{', '.join(registered)}. Add one with `mdk config add-target`."
        )
        raise typer.Exit(code=2)
    target_cfg = cfg.targets[target]

    if not target_cfg.azure_subscription:
        error(
            f"target {target!r} has no `azure_subscription` configured. "
            f"Re-register with `mdk config add-target --azure-subscription <id>`."
        )
        raise typer.Exit(code=2)
    if not target_cfg.azure_resource_group:
        error(
            f"target {target!r} has no `azure_resource_group` configured. "
            f"Re-register with `mdk config add-target --azure-resource-group <name>`."
        )
        raise typer.Exit(code=2)
    if not target_cfg.azure_env:
        error(
            f"target {target!r} has no `azure_env` configured. "
            f"Pass `--azure-env dev|staging|prod` when registering."
        )
        raise typer.Exit(code=2)

    # Resolve the parameters file (default: derived from target's env).
    if parameters is None:
        param_path = f"infra/azure/main.{target_cfg.azure_env}.bicepparam"
    else:
        param_path = parameters

    bicep_path = Path(bicep)
    param_file_path = Path(param_path)
    if not dry_run:
        # In dry-run mode the operator may be auditing from outside
        # the source tree where infra/azure/ isn't checked out. Skip
        # the existence check there so they still see the preview.
        if not bicep_path.is_file():
            error(
                f"bicep template not found: {bicep_path}. Run from the "
                f"movate-cli source tree, or pass --bicep <path> to "
                f"point at a checkout."
            )
            raise typer.Exit(code=2)
        if not param_file_path.is_file():
            error(
                f"parameters file not found: {param_file_path}. Either "
                f"create it (start from `infra/azure/main.bicepparam.example`) "
                f"or pass --parameters <path> to point at an alternate "
                f"file. The default is derived from azure_env={target_cfg.azure_env}."
            )
            raise typer.Exit(code=2)

    if not dry_run and shutil.which("az") is None:
        error(
            "`az` (Azure CLI) not found on PATH. Install it from "
            "https://learn.microsoft.com/cli/azure/install-azure-cli."
        )
        raise typer.Exit(code=2)

    # ------------------------------------------------------------------
    # Build the az command + preview.
    # ------------------------------------------------------------------
    az_cmd = [
        "az",
        "deployment",
        "group",
        "create",
        "--subscription",
        target_cfg.azure_subscription,
        "-g",
        target_cfg.azure_resource_group,
        "-n",
        deployment_name,
        "-f",
        str(bicep_path),
        "--parameters",
        str(param_file_path),
    ]

    seed_summary = "skipped (--no-seed)" if no_seed else f"{keyvault}/bootstrap-api-key"
    hint(
        f"\n[bold]mdk infra apply[/bold] → {target}\n"
        f"  subscription:   {target_cfg.azure_subscription}\n"
        f"  resource group: {target_cfg.azure_resource_group}\n"
        f"  env:            {target_cfg.azure_env}\n"
        f"  bicep:          {bicep_path}\n"
        f"  parameters:     {param_file_path}\n"
        f"  deployment:     {deployment_name}\n"
        f"  bootstrap seed: {seed_summary}\n"
    )
    hint(f"[dim]→ {' '.join(az_cmd)}[/dim]")

    if dry_run:
        if not no_seed:
            hint(f"[dim]→ (chain) mdk auth bootstrap-seed {target} --keyvault {keyvault}[/dim]")
        success("dry-run complete — nothing applied.")
        _emit_summary(
            target=target,
            ok=True,
            dry_run=True,
            seeded=False,
        )
        return

    # ------------------------------------------------------------------
    # Execute Bicep deploy.
    # ------------------------------------------------------------------
    try:
        result = subprocess.run(az_cmd, check=False)
    except FileNotFoundError as exc:
        error(f"command not found: az ({exc})")
        raise typer.Exit(code=2) from None

    if result.returncode != 0:
        error(
            f"az deployment group create failed (exit {result.returncode}). "
            f"See the az output above for the underlying error — common causes: "
            f"missing RG, wrong subscription, malformed .bicepparam, "
            f"or referenced KV secret missing (e.g. `pg-admin-password`)."
        )
        _emit_summary(target=target, ok=False, dry_run=False, seeded=False)
        raise typer.Exit(code=1)

    success(f"infra applied to [bold]{target}[/bold].")

    # ------------------------------------------------------------------
    # Auto-chain into bootstrap-seed (unless --no-seed).
    # ------------------------------------------------------------------
    if no_seed:
        hint(
            "[dim]bootstrap-seed skipped (--no-seed). Run "
            f"`mdk auth bootstrap-seed {target} --keyvault <name>` "
            f"manually before the first `mdk deploy`, OR if the "
            f"bootstrap-api-key secret already exists in Key Vault, "
            f"run `mdk auth pull-runtime-key {target} --keyvault <name>` "
            f"to sync locally.[/dim]"
        )
        _emit_summary(target=target, ok=True, dry_run=False, seeded=False)
        return

    hint(f"[dim]→ chaining into `mdk auth bootstrap-seed {target} --keyvault {keyvault}`…[/dim]")
    try:
        _seed_key, env_var = bootstrap_seed_inline(target, keyvault=keyvault)
    except BootstrapSeedError as exc:
        msg = str(exc)
        # `bootstrap-api-key already exists` is the expected case on
        # any re-apply — surface a friendly hint and exit 0 (infra
        # succeeded, seed already minted). Other BootstrapSeedErrors
        # are real failures.
        if "already exists" in msg:
            hint(
                "[dim]bootstrap key already exists in Key Vault — "
                f"skipping mint. To sync locally, run "
                f"`mdk auth pull-runtime-key {target} --keyvault {keyvault}`. "
                f"To rotate, run `mdk auth bootstrap-seed {target} "
                f"--keyvault {keyvault} --force`.[/dim]"
            )
            _emit_summary(target=target, ok=True, dry_run=False, seeded=False)
            return
        error(f"bootstrap-seed chain failed: {msg}")
        _emit_summary(target=target, ok=False, dry_run=False, seeded=False)
        raise typer.Exit(code=1) from None

    success(
        f"bootstrap key minted + uploaded to [cyan]{keyvault}/bootstrap-api-key[/cyan] "
        f"+ saved locally as [cyan]{env_var}[/cyan]."
    )
    hint(
        f"[dim]Next: [bold]mdk deploy --target {target} --mode runtime[/bold] "
        f"to build + roll the image, then [bold]mdk deploy --target {target}[/bold] "
        f"to push agents.[/dim]"
    )
    _emit_summary(target=target, ok=True, dry_run=False, seeded=True)


def _emit_summary(*, target: str, ok: bool, dry_run: bool, seeded: bool) -> None:
    """Emit the greppable ``mdk_infra_summary:`` line for CI scrapers.

    Format mirrors ``mdk_deploy_summary:`` so the same CI shell
    scripts can parse both. Always one line, key=value, no rich
    markup so it survives ANSI-stripping.
    """
    hint(
        f"[dim]mdk_infra_summary: target={target} "
        f"dry_run={'true' if dry_run else 'false'} "
        f"seeded={'true' if seeded else 'false'} "
        f"ok={'true' if ok else 'false'}[/dim]"
    )
