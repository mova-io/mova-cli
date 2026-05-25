"""``mdk export`` / ``mdk import`` — DR escape hatch for control-plane state (item 26).

A **portable logical backup/restore** of the operator-critical, hard-to-recreate
control-plane state — the few rows an operator genuinely cannot reconstruct after
a disaster:

* the **agent registry** (every published bundle version),
* **api keys** (hash + salt only — existing keys keep working after restore),
* **canary configs**,
* **eval + job schedules**,
* **per-tenant provider keys** (BYOK ciphertext, ADR 018).

[bold]This is the escape hatch, not the primary DR.[/bold] For a deployed
runtime the primary disaster-recovery story is **Azure Database for PostgreSQL
Flexible Server point-in-time-restore (PITR)** — automated, transactionally
consistent, covering *every* table. See [bold]docs/runbooks/dr-backup.md[/bold].
This logical export exists for portability (migrate state to a fresh
sqlite/postgres of any version, on any cloud), seeding a new deployment, and a
belt-and-suspenders off-Azure copy.

[bold]Out of scope by design:[/bold] high-volume/reconstructible history — runs,
jobs, eval/bench records, KB chunks, knowledge-graph, threads, memory, feedback,
dedup ledgers, tenant budgets. PITR owns history; this command does not export
it (and there is no ``--include-history`` flag — a partial history restore is
worse than none; use PITR).

[bold]Storage target:[/bold] this is a **local / DB-direct operator command** —
it runs against the storage backend selected by environment
([bold]MOVATE_DB_URL[/bold] → Postgres; otherwise the SQLite default), exactly
like [bold]mdk worker[/bold]. To back up a remote production database, run it
where ``MOVATE_DB_URL`` points at that database (e.g. from a bastion / ACA Job).
There is no remote HTTP export endpoint, so no ``--target`` flag.

[bold]Secrets:[/bold] api-key rows carry only ``secret_hash`` + ``salt`` (the
plaintext was never stored), so a restore keeps every issued key working.
Provider keys are Fernet ciphertext decryptable ONLY with
[bold]MOVATE_PROVIDER_KEY_SECRET[/bold], which is NOT in the export — the
restore environment must set the SAME secret or the restored provider keys
won't decrypt (re-set them with ``mdk keys set`` if it differs).
"""

from __future__ import annotations

import gzip
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli import _console
from movate.cli._output import TableJson
from movate.core.dr_backup import SnapshotError
from movate.storage import StorageProvider, build_storage

console = Console()


# ---------------------------------------------------------------------------
# Storage — DB-direct, respecting MOVATE_DB_URL (same selection as the worker).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _open_storage() -> AsyncIterator[StorageProvider]:
    """Open the env-selected storage backend, init it, and close on exit.

    Mirrors ``mdk worker`` / the scheduler tick: an operator command that
    talks to the configured backend directly (``MOVATE_DB_URL`` → Postgres,
    else the SQLite default) rather than over HTTP.
    """
    storage = build_storage()
    await storage.init()
    try:
        yield storage
    finally:
        await storage.close()


async def _do_export() -> dict[str, Any]:
    async with _open_storage() as storage:
        return await storage.export_state()


async def _do_import(snapshot: dict[str, Any], *, mode: str) -> dict[str, Any]:
    async with _open_storage() as storage:
        result = await storage.import_state(snapshot, mode=mode)
        return result.as_dict()


# ---------------------------------------------------------------------------
# File IO — plain or gzipped JSON, detected by the .gz suffix.
# ---------------------------------------------------------------------------


def _write_snapshot(snapshot: dict[str, Any], out: Path | None) -> Path | None:
    """Write the snapshot to ``out`` (gzip if ``.gz``), or stdout if ``None``.

    Returns the path written, or ``None`` when streamed to stdout.
    """
    blob = json.dumps(snapshot, indent=2, sort_keys=False)
    if out is None:
        sys.stdout.write(blob + "\n")
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".gz":
        out.write_bytes(gzip.compress(blob.encode("utf-8")))
    else:
        out.write_text(blob + "\n", encoding="utf-8")
    return out


def _read_snapshot(path: Path) -> dict[str, Any]:
    """Read a snapshot file (gzip if ``.gz``), returning the parsed dict.

    Raises :class:`SnapshotError` on a missing file or unparseable JSON, so the
    caller renders a clean operator-facing error rather than a traceback.
    """
    if not path.is_file():
        raise SnapshotError(f"no such backup file: {path}")
    try:
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"failed to read {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SnapshotError(f"{path} must contain a JSON object, got {type(parsed).__name__}")
    return parsed


def _default_backup_name() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"movate-backup-{ts}.json"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def export_state_cmd(
    file: Path | None = typer.Argument(
        None,
        metavar="[FILE]",
        help=(
            "Where to write the backup. Defaults to "
            "[bold]./movate-backup-<ts>.json[/bold]. A [bold].gz[/bold] suffix "
            "gzips it. Pass [bold]-[/bold] to stream JSON to stdout."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite the output file if it already exists."
    ),
) -> None:
    """Export operator-critical control-plane state to a portable JSON backup.

    Exports the [bold]agent registry[/bold] (all versions), [bold]api keys[/bold]
    (hash+salt only), [bold]canary configs[/bold], [bold]eval/job schedules[/bold],
    and [bold]per-tenant provider keys[/bold] (BYOK ciphertext). High-volume /
    reconstructible history (runs, jobs, evals, KB, threads, memory) is
    [bold]excluded[/bold] — the primary DR for history is Azure Postgres PITR
    (see [bold]docs/runbooks/dr-backup.md[/bold]).

    Runs DB-direct against the env-selected backend ([bold]MOVATE_DB_URL[/bold]
    → Postgres, else SQLite). To back up a remote DB, run where that env points.

    [bold]Examples:[/bold]

      [dim]# Default-named file in the cwd[/dim]
      $ mdk export

      [dim]# Named, gzipped[/dim]
      $ MOVATE_DB_URL=postgresql://... mdk export prod-backup.json.gz

      [dim]# Stream to stdout (pipe to a vault / object store)[/dim]
      $ mdk export - | gzip > backup.json.gz
    """
    import asyncio  # noqa: PLC0415

    out: Path | None
    if file is None:
        out = Path.cwd() / _default_backup_name()
    elif str(file) == "-":
        out = None  # stdout
    else:
        out = file

    if out is not None and out.exists() and not force:
        _console.error(
            f"{out} already exists (pass [bold]--force[/bold] to overwrite)",
        )
        raise typer.Exit(code=2)

    try:
        snapshot = asyncio.run(_do_export())
    except Exception as exc:
        _console.error(str(exc), context="export")
        raise typer.Exit(code=1) from exc

    written = _write_snapshot(snapshot, out)

    entities = snapshot.get("entities", {})
    counts = {k: len(v) for k, v in entities.items()}
    total = sum(counts.values())
    if written is None:
        # Streamed to stdout — keep the summary on stderr so the pipe stays clean.
        _console.hint(f"[dim]exported {total} control-plane row(s) to stdout[/dim]")
        return
    _console.success(f"exported {total} control-plane row(s) → [bold]{written}[/bold]")
    parts = ", ".join(f"{k}={n}" for k, n in counts.items() if n)
    if parts:
        _console.hint(f"[dim]{parts}[/dim]")
    _console.hint(
        "[dim]note: history (runs/jobs/evals/KB/threads/memory) is excluded — "
        "primary DR is Azure Postgres PITR (docs/runbooks/dr-backup.md).[/dim]"
    )


def import_state_cmd(
    file: Path = typer.Argument(
        ...,
        metavar="FILE",
        help="The backup file to restore (gzip auto-detected by a .gz suffix).",
    ),
    mode: str = typer.Option(
        "skip-existing",
        "--mode",
        case_sensitive=False,
        help=(
            "[bold]skip-existing[/bold] (safe default — never clobber rows that "
            "already exist) or [bold]overwrite[/bold] (re-save every row)."
        ),
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--format", case_sensitive=False, help="table | json"
    ),
) -> None:
    """Restore a control-plane backup made by [bold]mdk export[/bold].

    Loads the snapshot into the env-selected backend ([bold]MOVATE_DB_URL[/bold]
    → Postgres, else SQLite). [bold]Idempotent[/bold]: ``skip-existing`` (the
    default) leaves rows that already exist untouched, so a re-run imports 0 new
    rows. Reports per-entity imported/skipped counts.

    [bold]Provider keys[/bold] restore as Fernet ciphertext — the target
    environment MUST set the same [bold]MOVATE_PROVIDER_KEY_SECRET[/bold] used
    at export time, or the restored keys won't decrypt at run time (re-set them
    with [bold]mdk keys set[/bold] if the secret differs). [bold]Api keys[/bold]
    restore with their original hash+salt, so every issued key keeps working.

    [bold]Examples:[/bold]

      [dim]# Safe restore into a fresh deployment[/dim]
      $ MOVATE_DB_URL=postgresql://... mdk import movate-backup-....json

      [dim]# Force-refresh every row from the backup[/dim]
      $ mdk import backup.json.gz --mode overwrite
    """
    import asyncio  # noqa: PLC0415

    normalized = mode.lower()
    if normalized not in ("skip-existing", "overwrite"):
        _console.error(f"--mode must be skip-existing|overwrite, got {mode!r}")
        raise typer.Exit(code=2)

    try:
        snapshot = _read_snapshot(file)
        summary = asyncio.run(_do_import(snapshot, mode=normalized))
    except SnapshotError as exc:
        _console.error(str(exc), context="import")
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _console.error(str(exc), context="import")
        raise typer.Exit(code=1) from exc

    if output_format == TableJson.JSON:
        console.print_json(data=summary)
        return

    imported: dict[str, int] = summary["imported"]
    skipped: dict[str, int] = summary["skipped"]
    table = Table(title=f"Import ({normalized})")
    table.add_column("entity", style="bold")
    table.add_column("imported", justify="right")
    table.add_column("skipped", justify="right")
    all_entities = sorted(set(imported) | set(skipped))
    for entity in all_entities:
        table.add_row(entity, str(imported.get(entity, 0)), str(skipped.get(entity, 0)))
    console.print(table)
    _console.success(
        f"imported {summary['total_imported']} row(s); "
        f"skipped {summary['total_skipped']} already-present"
    )
    if summary.get("unknown"):
        _console.warn(
            f"snapshot had unrecognised entity keys (skipped): {', '.join(summary['unknown'])}"
        )


__all__ = ["export_state_cmd", "import_state_cmd"]
