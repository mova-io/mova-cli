"""``mdk trigger`` — inbound event/webhook triggers (ADR 017 D2).

The trigger sibling of ``mdk schedule``: where a schedule enqueues an
agent/workflow job on a cron cadence, a **trigger** enqueues one when an
external system POSTs an event to a stable movate webhook URL ("process this
incoming ticket"). The external caller has no ``mvt_*`` API key — it
authenticates with a **per-trigger secret** via an ``X-Movate-Signature``
HMAC over the request body.

The enqueued job is the same ``JobRecord`` shape ``mdk submit`` / ``POST
/run`` produce, so it runs through the existing worker dispatch and is
observable + retryable as a normal job.

Subcommands:

* ``create <target> --kind agent|workflow [--name N] [--input-defaults JSON]``
  — register a trigger; prints the webhook URL + the secret ONCE + an example
  ``curl`` with the HMAC signature.
* ``list`` — show this project's triggers (no secrets).
* ``delete <name>`` — remove a trigger by its handle.

Triggers are additive + default-off: nothing fires until one is created, and
existing behaviour is unchanged otherwise.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._output import Report
from movate.core.models import JobKind, Trigger
from movate.core.triggers import mint_trigger, signing_key
from movate.storage.base import StorageProvider

console = Console()
err_console = Console(stderr=True)

# Local CLI storage scopes records under the "local" tenant — matches
# schedule_cmd / build_local_runtime's Executor tenant_id.
_LOCAL_TENANT = "local"

# Placeholder base URL for the printed example. The operator swaps in their
# deployed runtime's URL; the local CLI doesn't know it.
_EXAMPLE_BASE_URL = "https://<your-runtime>"

trigger_app = typer.Typer(
    name="trigger",
    help="Manage inbound event/webhook triggers that enqueue agent/workflow jobs (ADR 017 D2).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@trigger_app.command("create")
def create_trigger(
    target: str = typer.Argument(
        ...,
        help="Agent or workflow name to run when the trigger fires.",
        shell_complete=complete_agent_path,
    ),
    kind: JobKind = typer.Option(
        JobKind.AGENT,
        "--kind",
        "-k",
        case_sensitive=False,
        help="Job kind to enqueue: agent | workflow.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Trigger handle (unique per tenant). Defaults to the target name.",
    ),
    input_defaults_arg: str | None = typer.Option(
        None,
        "--input-defaults",
        "-i",
        help="Default job payload merged UNDER the event body: JSON object, file, or '-'. "
        "Default: {}.",
    ),
    event_key: str | None = typer.Option(
        None,
        "--event-key",
        help="Nest the raw event body under this single state key (e.g. 'event') "
        "instead of merging it at top level (ADR 100 D2).",
    ),
    input_map_arg: str | None = typer.Option(
        None,
        "--input-map",
        help="JSON object mapping output state key -> dotted path into the event body, "
        'e.g. \'{"work_item_id": "resource.id"}\'. Missing paths are omitted (ADR 100 D2).',
    ),
    dedup_key: str | None = typer.Option(
        None,
        "--dedup-key",
        help="Dotted path into the event body used as the delivery id when the "
        "X-Movate-Delivery-Id header is absent (ADR 100 D2).",
    ),
    auth_mode: str = typer.Option(
        "hmac",
        "--auth-mode",
        help="Fire-endpoint auth: hmac (default; body-bound X-Movate-Signature, "
        "X-Hub-Signature-256 accepted as an alias) or token (static "
        "X-Movate-Trigger-Token header — weaker, pair with --dedup-key) (ADR 100 D3).",
    ),
    disabled: bool = typer.Option(
        False, "--disabled", help="Create the trigger but leave it dormant (won't fire)."
    ),
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """Register an inbound event/webhook trigger.

    Mints a per-trigger secret + a stable public webhook id, persists the
    trigger (secret hashed at rest), and prints the webhook URL, the secret
    [bold]once[/bold] (irrecoverable after), and an example signed ``curl``.

    [bold]Examples:[/bold]

      [dim]# Fire a ticket-triage agent on an inbound ticket webhook[/dim]
      $ mdk trigger create triage-agent --name zendesk-ticket \\
          --input-defaults '{"source": "zendesk"}'

      [dim]# A disabled (dormant) workflow trigger[/dim]
      $ mdk trigger create returns-pipeline -k workflow --disabled

      [dim]# ADO work-item triage: static-token auth + body-id dedup +[/dim]
      [dim]# declared event mapping (ADR 100 D2/D3)[/dim]
      $ mdk trigger create work-item-triage -k workflow --name ado-work-items \\
          --auth-mode token --dedup-key id --event-key event \\
          --input-map '{"work_item_id": "resource.id", "event_type": "eventType"}'
    """
    if kind not in (JobKind.AGENT, JobKind.WORKFLOW):
        err_console.print(
            f"[red]✗[/red] --kind must be agent|workflow, got {kind.value!r} "
            "(eval has its own scheduler: mdk eval-schedule)"
        )
        raise typer.Exit(code=2)
    if auth_mode not in ("hmac", "token"):
        err_console.print(f"[red]✗[/red] --auth-mode must be hmac|token, got {auth_mode!r}")
        raise typer.Exit(code=2)

    try:
        defaults = _coerce_input(input_defaults_arg) if input_defaults_arg is not None else {}
    except ValueError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    input_map: dict[str, str] | None = None
    if input_map_arg is not None:
        try:
            input_map = _coerce_input_map(input_map_arg)
        except ValueError as exc:
            err_console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=2) from None

    if auth_mode == "token":
        # ADR 100 D3: token mode is explicitly weaker — the secret travels
        # on the wire (TLS-protected) and a captured request is replayable
        # until rotation. Warn, and push the dedup pairing.
        err_console.print(
            "[yellow]⚠ --auth-mode token is weaker than hmac:[/yellow] the secret travels "
            "on the wire and a captured request is replayable until rotation. "
            "Pair it with --dedup-key so a replay can at worst re-run what already ran once."
        )

    target_name = _resolve_target_name(target)
    trigger_name = name or target_name

    minted = mint_trigger(
        tenant_id=_LOCAL_TENANT,
        name=trigger_name,
        kind=kind,
        target=target_name,
        input_defaults=defaults,
        event_key=event_key,
        input_map=input_map,
        dedup_key=dedup_key,
        auth_mode=auth_mode,
        enabled=not disabled,
    )
    asyncio.run(_save(minted.record))

    webhook_path = f"/api/v1/triggers/{minted.record.trigger_id}/events"

    if output_format == Report.JSON:
        # Secret + salt are shown once even in JSON mode (scripting capture).
        console.print_json(
            data={
                "trigger_id": minted.record.trigger_id,
                "name": minted.record.name,
                "kind": minted.record.kind.value,
                "target": minted.record.target,
                "event_key": minted.record.event_key,
                "input_map": minted.record.input_map,
                "dedup_key": minted.record.dedup_key,
                "auth_mode": minted.record.auth_mode,
                "enabled": minted.record.enabled,
                "webhook_path": webhook_path,
                "secret": minted.secret,
                "salt": minted.salt,
            }
        )
        return

    state = "enabled" if minted.record.enabled else "disabled (dormant)"
    console.print(
        f"[green]✓[/green] trigger [bold]{trigger_name}[/bold] created: "
        f"{kind.value} [bold]{target_name}[/bold] ({state})"
    )
    console.print(f"[dim]webhook:[/dim] [bold]POST[/bold] {_EXAMPLE_BASE_URL}{webhook_path}")

    # Secret-reveal UX (mirrors `mdk auth create-key`): the secret goes to
    # stderr with a save-now warning so a scripted `> file` redirect of
    # stdout doesn't lose it, and the warning doesn't pollute the capture.
    err_console.print()
    err_console.print(
        "[bold yellow]save the trigger secret now — never shown again[/bold yellow]\n"
        f"  secret: {minted.secret}\n"
        f"  salt:   {minted.salt}"
    )

    # The caller signs the request body with HMAC-SHA256 keyed by
    # hash_secret(secret, salt). Show a copy-paste example so an operator can
    # wire a webhook without reading the source.
    example_body = '{"text": "hello"}'
    key = signing_key(minted.secret, minted.salt)
    sig = _example_signature(key, example_body)
    console.print("\n[dim]example signed request:[/dim]")
    console.print(
        f"[dim]  BODY='{example_body}'\n"
        f"  SIG=$(printf '%s' \"$BODY\" | openssl dgst -sha256 -hmac '{key}' | sed 's/^.* //')\n"
        f"  curl -X POST {_EXAMPLE_BASE_URL}{webhook_path} \\\\\n"
        f'    -H "X-Movate-Signature: sha256=$SIG" \\\\\n'
        f'    -H "Content-Type: application/json" \\\\\n'
        f'    -d "$BODY"[/dim]'
    )
    console.print(f"[dim]  # for the example body above, sha256={sig}[/dim]")


@trigger_app.command("list")
def list_triggers(
    output_format: Report = typer.Option(Report.TABLE, "--format", case_sensitive=False),
) -> None:
    """List this project's triggers (no secrets)."""
    triggers = asyncio.run(_list())
    if output_format == Report.JSON:
        console.print_json(
            data=[
                {
                    "trigger_id": t.trigger_id,
                    "name": t.name,
                    "kind": t.kind.value,
                    "target": t.target,
                    "enabled": t.enabled,
                    "input_defaults": t.input_defaults,
                    "event_key": t.event_key,
                    "input_map": t.input_map,
                    "dedup_key": t.dedup_key,
                    "auth_mode": t.auth_mode,
                    "last_fired_at": t.last_fired_at.isoformat() if t.last_fired_at else None,
                }
                for t in triggers
            ]
        )
        return
    if not triggers:
        console.print("[dim]no triggers — create one with[/dim] mdk trigger create <target>")
        return
    table = Table(title="Event/webhook triggers")
    table.add_column("name", style="bold")
    table.add_column("kind")
    table.add_column("target")
    table.add_column("trigger_id")
    table.add_column("enabled")
    table.add_column("last fired")
    for t in triggers:
        table.add_row(
            t.name,
            t.kind.value,
            t.target,
            t.trigger_id,
            "yes" if t.enabled else "no",
            t.last_fired_at.isoformat(timespec="seconds") if t.last_fired_at else "never",
        )
    console.print(table)


@trigger_app.command("delete")
def delete_trigger(
    name: str = typer.Argument(..., help="Trigger handle to remove."),
) -> None:
    """Remove a trigger by its handle."""
    deleted = asyncio.run(_delete(name))
    if deleted:
        console.print(f"[green]✓[/green] deleted trigger [bold]{name}[/bold]")
    else:
        console.print(f"[dim]no trigger[/dim] {name} [dim]— nothing to delete[/dim]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _example_signature(key: str, body: str) -> str:
    """Compute the bare-hex HMAC of ``body`` under ``key`` for the example."""
    import hashlib  # noqa: PLC0415
    import hmac  # noqa: PLC0415

    return hmac.new(key.encode("ascii"), body.encode("utf-8"), hashlib.sha256).hexdigest()


# Input coercion — same rules as `mdk schedule set` / `mdk submit`.
def _coerce_input(arg: str) -> dict[str, Any]:
    """Parse a job payload from a JSON object string, a file path, or '-' (stdin)."""
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    stripped = arg.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return _ensure_dict(json.loads(arg))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"--input-defaults looks like JSON but failed to parse: {exc}"
            ) from exc
    try:
        is_file = Path(arg).is_file()
    except OSError:
        is_file = False
    if is_file:
        return _ensure_dict(json.loads(Path(arg).read_text()))
    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"--input-defaults must be a JSON object, file path, or '-': {exc}"
        ) from exc
    return _ensure_dict(parsed)


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"--input-defaults must be a JSON object, got {type(value).__name__}")
    return value


def _coerce_input_map(arg: str) -> dict[str, str]:
    """Parse ``--input-map`` — a JSON object of state key → dotted body path."""
    import json  # noqa: PLC0415

    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--input-map must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError(
            "--input-map must be a JSON object of string -> dotted path, e.g. "
            '\'{"work_item_id": "resource.id"}\''
        )
    return parsed


def _resolve_target_name(target: str) -> str:
    """Resolve a directory-or-name argument to the declared agent/workflow name.

    Jobs key off the ``agent.yaml`` name, which can differ from the directory
    name. When the argument doesn't resolve to a bundle on disk, fall back to
    the bare argument so an operator can trigger by name.
    """
    from pathlib import Path  # noqa: PLC0415

    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    try:
        resolved = Path(resolve_agent_or_workflow_arg(target))
        if (resolved / "agent.yaml").is_file():
            return load_agent(resolved).spec.name
    except Exception:
        pass
    return target


@asynccontextmanager
async def _local_storage() -> AsyncIterator[StorageProvider]:
    """Build the local runtime, yield its storage, tear down cleanly."""
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415

    runtime = await build_local_runtime(mock=True)
    try:
        yield runtime.storage
    finally:
        await shutdown_runtime(runtime.storage, runtime.tracer)


async def _save(trigger: Trigger) -> None:
    async with _local_storage() as storage:
        await storage.save_trigger(trigger)


async def _list() -> list[Trigger]:
    async with _local_storage() as storage:
        return await storage.list_triggers(tenant_id=_LOCAL_TENANT)


async def _delete(name: str) -> bool:
    async with _local_storage() as storage:
        return await storage.delete_trigger(name, tenant_id=_LOCAL_TENANT)


__all__ = ["trigger_app"]
