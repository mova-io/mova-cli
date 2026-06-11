"""``mdk certify`` — first-class front door for the certification suite.

Wraps ``python -m certification.run_suite`` (the scenario x capability gate
documented in ``certification/README.md``) so operators stop hand-assembling
``MDK_DEV_KEY=... uv run python -m certification.run_suite --target dev``:

* **Local mode** (default) — imports the suite's ``main()`` in-process and
  exits with its exit code (0 = all capabilities passed, 1 = at least one
  failure, 2 = configuration/usage error — the suite's documented contract).
* **``--in-env``** — starts the ``movate-cert-suite`` Azure Container Apps
  Job (the in-environment run that lights up the side-effects column and
  ships ``mdk.certification.scenario`` metrics — see
  ``infra/azure/containerapp-cert-job.bicep``), polls the execution until it
  is terminal, then prints the suite's matrix output fetched from Log
  Analytics. The exit code mirrors the execution status (Succeeded → 0).

**Concurrency guard.** Before ANY run (local or in-env) we check whether a
cert-job execution is already Running and refuse unless ``--force``. Two
concurrent suites against the dev runtime saturate the API rate limit and
false-fail each other with 429s — the guard codifies that operational lesson.
The check degrades gracefully (a warning, not a failure) when the ``az`` CLI
is unavailable, because a laptop without Azure tooling must still be able to
run the local suite.

The ``certification`` package is repo-level (not shipped in the wheel), so
local mode requires running from a movate-cli checkout — the import failure
message says exactly that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import typer
from rich.console import Console

from movate.credentials.store import CredentialsStore

# Azure coordinates of the in-env certification job (created 2026-06-11,
# captured in infra/azure/containerapp-cert-job.bicep). Overridable via
# --resource-group / --job-name for non-dev environments.
DEFAULT_RESOURCE_GROUP = "movate-dev-rg"
DEFAULT_JOB_NAME = "movate-cert-suite"

# Execution-status vocabulary for ``az containerapp job execution list``.
# Anything not in _TERMINAL_STATUSES keeps the poll loop alive.
_TERMINAL_STATUSES = frozenset({"Succeeded", "Failed", "Stopped", "Degraded"})

# Poll seam — module-level so tests can patch them to zero. The job's replica
# timeout is 1h (replicaRetryLimit 0), so 75 min covers a full run + start lag.
_POLL_INTERVAL_S: float = 15.0
_POLL_TIMEOUT_S: float = 4500.0

# Per-az-invocation subprocess timeout. Log Analytics queries can be slow;
# everything else returns in seconds.
_AZ_TIMEOUT_S: float = 120.0

console = Console()
err_console = Console(stderr=True)


def _az_available() -> bool:
    """Is the Azure CLI on PATH? Seam for tests."""
    return shutil.which("az") is not None


def _run_az(args: list[str]) -> tuple[int, str, str]:
    """Run one ``az`` command, captured. Returns (returncode, stdout, stderr).

    Never raises on a non-zero exit — callers decide whether the failure is
    fatal (job start) or degradable (concurrency check, log fetch). A timeout
    is reported as returncode 124 with the exception text on stderr.
    """
    try:
        # Fixed binary, list argv, no shell — not injectable.
        proc = subprocess.run(
            ["az", *args],
            capture_output=True,
            text=True,
            timeout=_AZ_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"az timed out after {_AZ_TIMEOUT_S:.0f}s: {exc}"
    except OSError as exc:  # az vanished between which() and exec
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _execution_status(item: dict[str, object]) -> str:
    """Status of one execution record — handles both az output shapes.

    ``az containerapp job execution list`` has emitted the status both at
    the top level (``"status"``) and nested (``"properties": {"status"}``)
    across CLI versions; read whichever is present.
    """
    props = item.get("properties")
    if isinstance(props, dict) and props.get("status"):
        return str(props["status"])
    return str(item.get("status", ""))


def _list_executions(resource_group: str, job_name: str) -> list[dict[str, object]] | None:
    """All execution records for the cert job, or ``None`` when unknowable.

    ``None`` (az missing / call failed / unparseable output) means "could not
    check" — callers degrade to a warning rather than blocking the run.
    """
    if not _az_available():
        return None
    rc, out, _err = _run_az(
        [
            "containerapp",
            "job",
            "execution",
            "list",
            "-g",
            resource_group,
            "-n",
            job_name,
            "-o",
            "json",
        ]
    )
    if rc != 0:
        return None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _running_executions(resource_group: str, job_name: str) -> list[str] | None:
    """Names of currently-Running cert-job executions; ``None`` = unknown."""
    executions = _list_executions(resource_group, job_name)
    if executions is None:
        return None
    return [
        str(e.get("name", "<unnamed>")) for e in executions if _execution_status(e) == "Running"
    ]


def _resolve_dev_key() -> str | None:
    """MDK_DEV_KEY from the environment, else from ``~/.movate/credentials``.

    Precedence (documented in --help): a value already in the environment
    (shell export / project .env / the CLI's startup autoload) wins; the
    machine-global credentials file's ``MDK_DEV_KEY=`` line is the fallback.
    """
    env_value = os.environ.get("MDK_DEV_KEY", "").strip()
    if env_value:
        return env_value
    file_value = (CredentialsStore().get("MDK_DEV_KEY") or "").strip()
    return file_value or None


def _invoke_suite(argv: list[str]) -> int:
    """Import + run the certification suite core, returning its exit code.

    Lazy import: ``certification`` is a repo-level package (not shipped in the
    wheel), so an installed-only ``mdk`` gets a clear "run from a checkout"
    error rather than a bare ModuleNotFoundError.
    """
    try:
        from certification.run_suite import main as suite_main  # noqa: PLC0415
    except ImportError:
        err_console.print(
            "[red]✗[/red] the [bold]certification[/bold] package is not importable — "
            "it ships in the movate-cli repo (not the wheel). Run "
            "[bold]mdk certify[/bold] from a movate-cli checkout "
            "(e.g. [bold]uv run mdk certify --target dev[/bold]), or use "
            "[bold]--in-env[/bold] to run the suite as the Azure cert job instead."
        )
        raise typer.Exit(code=2) from None
    return suite_main(argv)


def _check_not_already_running(resource_group: str, job_name: str, *, force: bool) -> None:
    """Refuse to start a run while a cert-job execution is Running.

    Two concurrent suites saturate the dev runtime's API rate limit and
    false-fail each other with 429s. ``--force`` overrides (e.g. when the
    Running execution is known-wedged); an unknowable state (no ``az``, call
    failed) degrades to a warning so laptop-local runs aren't blocked.
    """
    running = _running_executions(resource_group, job_name)
    if running is None:
        err_console.print(
            "[yellow]![/yellow] could not check for an already-running cert-job "
            "execution (is the [bold]az[/bold] CLI installed + logged in?) — "
            "proceeding without the concurrency guard."
        )
        return
    if not running:
        return
    if force:
        err_console.print(
            f"[yellow]![/yellow] cert-job execution(s) already Running "
            f"({', '.join(running)}) — proceeding anyway because [bold]--force[/bold] "
            "was passed. Expect 429-driven false failures if both runs overlap."
        )
        return
    err_console.print(
        f"[red]✗[/red] a certification run is already in progress: execution(s) "
        f"{', '.join(running)} of job [bold]{job_name}[/bold] in "
        f"[bold]{resource_group}[/bold] are Running. Two concurrent suites "
        "saturate the dev API rate limit and false-fail each other (429s).\n"
        "  wait for it to finish (az containerapp job execution list "
        f"-g {resource_group} -n {job_name}), or re-run with [bold]--force[/bold] "
        "to override."
    )
    raise typer.Exit(code=2)


def _start_job_execution(resource_group: str, job_name: str) -> str:
    """``az containerapp job start`` → the new execution's name."""
    rc, out, err = _run_az(
        ["containerapp", "job", "start", "-g", resource_group, "-n", job_name, "-o", "json"]
    )
    if rc != 0:
        err_console.print(
            f"[red]✗[/red] failed to start job [bold]{job_name}[/bold] in "
            f"[bold]{resource_group}[/bold] (az exit {rc}):\n{err.strip()}"
        )
        raise typer.Exit(code=1)
    try:
        payload = json.loads(out)
        name = str(payload.get("name") or "").strip()
        if not name and payload.get("id"):
            name = str(payload["id"]).rsplit("/", 1)[-1]
    except (json.JSONDecodeError, AttributeError):
        name = ""
    if not name:
        err_console.print(
            "[red]✗[/red] job started but az returned no execution name — "
            f"cannot tail it. Raw output:\n{out.strip()[:500]}"
        )
        raise typer.Exit(code=1)
    return name


def _poll_until_terminal(resource_group: str, job_name: str, execution_name: str) -> str:
    """Poll the execution list until ``execution_name`` reaches a terminal status."""
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    last_status = ""
    while time.monotonic() < deadline:
        executions = _list_executions(resource_group, job_name) or []
        status = next(
            (_execution_status(e) for e in executions if str(e.get("name", "")) == execution_name),
            "",
        )
        if status and status != last_status:
            console.print(f"  [dim]execution {execution_name}: {status}[/dim]")
            last_status = status
        if status in _TERMINAL_STATUSES:
            return status
        time.sleep(_POLL_INTERVAL_S)
    err_console.print(
        f"[red]✗[/red] timed out after {_POLL_TIMEOUT_S:.0f}s waiting for execution "
        f"[bold]{execution_name}[/bold] to finish (last status: "
        f"{last_status or 'unknown'})."
    )
    return "TimedOut"


def _discover_workspace_id(resource_group: str) -> str | None:
    """First Log Analytics workspace customerId in the resource group."""
    rc, out, _err = _run_az(
        ["monitor", "log-analytics", "workspace", "list", "-g", resource_group, "-o", "json"]
    )
    if rc != 0:
        return None
    try:
        workspaces = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(workspaces, list) or not workspaces:
        return None
    customer_id = workspaces[0].get("customerId")
    return str(customer_id) if customer_id else None


def _print_execution_logs(
    resource_group: str, execution_name: str, workspace_id: str | None
) -> None:
    """Best-effort: print the suite's console output (the matrix) from Log Analytics.

    Never fatal — log ingestion lags a few minutes, and the run's exit code is
    already decided by the execution status. A miss prints the manual query.
    """
    ws = workspace_id or _discover_workspace_id(resource_group)
    if not ws:
        err_console.print(
            "[yellow]![/yellow] no Log Analytics workspace found in "
            f"[bold]{resource_group}[/bold] — pass [bold]--workspace-id[/bold] to "
            "fetch the matrix output."
        )
        return
    query = (
        "ContainerAppConsoleLogs_CL "
        f'| where ContainerGroupName_s startswith "{execution_name}" '
        "| order by TimeGenerated asc | project Log_s"
    )
    rc, out, err = _run_az(
        ["monitor", "log-analytics", "query", "-w", ws, "--analytics-query", query, "-o", "json"]
    )
    if rc != 0:
        err_console.print(
            f"[yellow]![/yellow] Log Analytics query failed (az exit {rc}): "
            f"{err.strip()[:300]}\n  query it manually: {query}"
        )
        return
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        rows = []
    lines = [str(r.get("Log_s", "")) for r in rows if isinstance(r, dict)]
    if not lines:
        err_console.print(
            "[yellow]![/yellow] no console logs in Log Analytics yet (ingestion "
            f"lags a few minutes). Re-query: {query}"
        )
        return
    console.print(f"\n[bold]suite output[/bold] [dim](execution {execution_name})[/dim]:")
    for line in lines:
        console.print(line, highlight=False, markup=False)


def certify(
    target: str = typer.Option(
        "dev",
        "--target",
        help=(
            "Suite target: [bold]dev[/bold] = the deployed dev runtime API "
            "(the suite's default; 'local' is deferred by the suite itself)."
        ),
    ),
    scenario: str | None = typer.Option(
        None,
        "--scenario",
        help="Run only the scenario with this name (default: all discovered).",
    ),
    in_env: bool = typer.Option(
        False,
        "--in-env",
        help=(
            "Run the suite as the Azure Container Apps cert job instead of "
            "locally: starts the job, polls the execution to a terminal "
            "status, prints the matrix from Log Analytics, and mirrors the "
            "execution status as the exit code. Requires the az CLI."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Bypass the concurrency guard and start even when a cert-job "
            "execution is already Running (risks 429-driven false failures)."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="(local mode) print the suite's machine-readable JSON summary instead.",
    ),
    resource_group: str = typer.Option(
        DEFAULT_RESOURCE_GROUP,
        "--resource-group",
        "-g",
        help="Azure resource group of the cert job + Log Analytics workspace.",
    ),
    job_name: str = typer.Option(
        DEFAULT_JOB_NAME,
        "--job-name",
        help="Container Apps Job name of the in-env certification run.",
    ),
    workspace_id: str | None = typer.Option(
        None,
        "--workspace-id",
        help=(
            "Log Analytics workspace customer id for fetching the matrix "
            "output (--in-env). Auto-discovered from the resource group "
            "when omitted."
        ),
    ),
) -> None:
    """Run the certification suite — locally or as the in-env Azure job.

    Local mode wraps ``python -m certification.run_suite`` (same scenarios,
    matrix, and exit codes: 0 = pass, 1 = capability failure, 2 = usage/config
    error). The dev runtime bearer token resolves as: [bold]MDK_DEV_KEY[/bold]
    from the environment first (shell export / project .env), then the
    ``MDK_DEV_KEY=`` line in [bold]~/.movate/credentials[/bold] as a fallback.

    [bold]--in-env[/bold] runs the suite inside the Container Apps environment
    instead (side-effects verified against Postgres + cert metrics shipped via
    OTLP — neither is reachable from a laptop) and mirrors the job execution's
    status as the exit code.

    Both modes refuse to start while a cert-job execution is already Running
    ([bold]--force[/bold] overrides): two concurrent suites saturate the dev
    API rate limit and false-fail each other with 429s.

    [bold]Examples:[/bold]

      [dim]$ mdk certify --target dev                    # full local suite[/dim]
      [dim]$ mdk certify --scenario expense-approval     # one scenario[/dim]
      [dim]$ mdk certify --in-env                        # the Azure cert job, tailed[/dim]
    """
    if in_env and not _az_available():
        err_console.print(
            "[red]✗[/red] [bold]--in-env[/bold] needs the Azure CLI ([bold]az[/bold]) "
            "on PATH to start + tail the cert job, and it was not found. "
            "Install it (https://aka.ms/azure-cli) and run [bold]az login[/bold], "
            "or drop [bold]--in-env[/bold] to run the suite locally."
        )
        raise typer.Exit(code=2)

    # The 429-concurrency guard — both modes (a local run hits the same dev
    # runtime the in-env job does).
    _check_not_already_running(resource_group, job_name, force=force)

    if in_env:
        execution = _start_job_execution(resource_group, job_name)
        console.print(
            f"[green]✓[/green] started cert-job execution [bold]{execution}[/bold] "
            f"[dim](job {job_name}, rg {resource_group})[/dim]"
        )
        status = _poll_until_terminal(resource_group, job_name, execution)
        _print_execution_logs(resource_group, execution, workspace_id)
        if status == "Succeeded":
            console.print(f"[green]✓[/green] execution [bold]{execution}[/bold] Succeeded")
            return
        err_console.print(f"[red]✗[/red] execution [bold]{execution}[/bold]: {status}")
        raise typer.Exit(code=1)

    # Local mode — resolve the dev key (env first, credentials file fallback)
    # and hand the suite a ready environment.
    dev_key = _resolve_dev_key()
    if not dev_key:
        err_console.print(
            "[red]✗[/red] [bold]MDK_DEV_KEY[/bold] is not set and has no entry in "
            "[bold]~/.movate/credentials[/bold]. Export the dev runtime's bearer "
            "token (MDK_DEV_KEY=... mdk certify) or save it once with "
            "[bold]mdk auth save-runtime-key[/bold]."
        )
        raise typer.Exit(code=2)
    os.environ["MDK_DEV_KEY"] = dev_key

    argv = ["--target", target]
    if scenario:
        argv += ["--scenario", scenario]
    if json_output:
        argv.append("--json")
    raise typer.Exit(code=_invoke_suite(argv))
