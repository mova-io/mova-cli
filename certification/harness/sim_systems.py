"""Simulated external systems for the MDK certification harness.

The certification suite runs the **real platform** — real Temporal, real
governance, real HITL, real tracing, real agents, and **real persistence in the
same database MDK itself uses** — but the **external destinations** (email, SMS,
SAP, ServiceNow, ERP, Slack, identity provisioning) are *simulated*: instead of
calling a live SaaS, each call appends an auditable row to a ``sim_side_effects``
table **in the same DB as the run records + governance audit**. So you see real
end-to-end behaviour (the side-effect log sits alongside the real run data,
inspectable together), the workflow is durably executed for real, governance and
guardrails fire for real — only the *destination* is faked. Wipe the DBs after a
suite run (the harness's ``reset()`` clears just this table).

Same-DB resolution mirrors :func:`movate.storage.build_storage`:
``MOVATE_DB_URL`` / ``MOVATE_PG_URL`` → that **Postgres**; otherwise the SQLite
file at ``MOVATE_DB`` (default ``~/.movate/local.db``). So pointing MDK at the
deployed Azure Postgres puts the simulated side-effects there too, next to
everything else.

Each system is also exposed as an **mdk `kind: python` skill entrypoint**, so a
scenario's agents call it through the real tool-calling path — exercising that
capability too. ``record`` / ``read`` / ``reset`` are the storage-agnostic core;
the ``sim_*`` functions are the per-system facades.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_TABLE = "sim_side_effects"


# ---------------------------------------------------------------------------
# Backend resolution — share MDK's database exactly.
# ---------------------------------------------------------------------------


def _pg_url() -> str | None:
    """The Postgres DSN MDK would use, via MDK's own resolver (so we hit the
    identical DB — including the deployed Azure Postgres)."""
    try:
        from movate.storage import _resolve_pg_url  # noqa: PLC0415

        return _resolve_pg_url()
    except Exception:
        for var in ("MOVATE_DB_URL", "MOVATE_PG_URL"):
            val = os.environ.get(var, "").strip()
            if val:
                return val
        return None


def _sqlite_path() -> str:
    """The SQLite file MDK would use (so side-effects share the local DB file)."""
    val = os.environ.get("MOVATE_DB", "").strip()
    if val:
        return val
    return str(Path.home() / ".movate" / "local.db")


def _run_async(make_coro: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async DB op from sync skill code in any context (incl. inside a
    running event loop, e.g. a Temporal activity) — execute it on a dedicated
    thread with its own loop."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(make_coro())).result()


# ---------------------------------------------------------------------------
# Core ledger — record / read / reset, backed by the shared DB.
# ---------------------------------------------------------------------------

_PG_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "  id BIGSERIAL PRIMARY KEY,"
    "  ts DOUBLE PRECISION NOT NULL,"
    "  run_id TEXT NOT NULL DEFAULT '',"
    "  system TEXT NOT NULL,"
    "  action TEXT NOT NULL,"
    "  payload TEXT NOT NULL"
    ")"
)
_SQLITE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  ts REAL NOT NULL,"
    "  run_id TEXT NOT NULL DEFAULT '',"
    "  system TEXT NOT NULL,"
    "  action TEXT NOT NULL,"
    "  payload TEXT NOT NULL"
    ")"
)


def record(
    system: str, action: str, payload: dict[str, Any], *, run_id: str = ""
) -> dict[str, Any]:
    """Append one simulated side-effect to the shared DB. Returns a skill-friendly ack."""
    ts, body = time.time(), json.dumps(payload, default=str)
    url = _pg_url()
    if url:

        async def _do() -> None:
            import asyncpg  # noqa: PLC0415

            conn = await asyncpg.connect(url)
            try:
                await conn.execute(_PG_DDL)
                await conn.execute(
                    f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    ts,
                    run_id,
                    system,
                    action,
                    body,
                )
            finally:
                await conn.close()

        _run_async(_do)
    else:
        conn = sqlite3.connect(_sqlite_path())
        try:
            conn.execute(_SQLITE_DDL)
            conn.execute(
                f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) VALUES (?,?,?,?,?)",
                (ts, run_id, system, action, body),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "system": system, "action": action, "payload": payload}


def read(run_id: str | None = None) -> list[dict[str, Any]]:
    """Read recorded side-effects (all, or for one ``run_id``), oldest first."""
    url = _pg_url()
    if url:

        async def _do() -> list[tuple[Any, ...]]:
            import asyncpg  # noqa: PLC0415

            conn = await asyncpg.connect(url)
            try:
                await conn.execute(_PG_DDL)
                if run_id:
                    rows = await conn.fetch(
                        f"SELECT ts, run_id, system, action, payload FROM {_TABLE} "
                        "WHERE run_id=$1 ORDER BY id",
                        run_id,
                    )
                else:
                    rows = await conn.fetch(
                        f"SELECT ts, run_id, system, action, payload FROM {_TABLE} ORDER BY id"
                    )
                return [tuple(r) for r in rows]
            finally:
                await conn.close()

        raw = _run_async(_do)
    else:
        conn = sqlite3.connect(_sqlite_path())
        try:
            conn.execute(_SQLITE_DDL)
            sql = f"SELECT ts, run_id, system, action, payload FROM {_TABLE}"
            if run_id:
                raw = conn.execute(sql + " WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            else:
                raw = conn.execute(sql + " ORDER BY id").fetchall()
        finally:
            conn.close()
    return [
        {"ts": r[0], "run_id": r[1], "system": r[2], "action": r[3], "payload": json.loads(r[4])}
        for r in raw
    ]


def reset() -> None:
    """Clear the simulated side-effect table (call in a scenario's setup).

    Drops only ``sim_side_effects`` — the real run records / audit stay put (wipe
    those with the suite's broader teardown)."""
    url = _pg_url()
    if url:

        async def _do() -> None:
            import asyncpg  # noqa: PLC0415

            conn = await asyncpg.connect(url)
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")
            finally:
                await conn.close()

        _run_async(_do)
    else:
        conn = sqlite3.connect(_sqlite_path())
        try:
            conn.execute(f"DROP TABLE IF EXISTS {_TABLE}")
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Per-system facades — also usable as mdk `kind: python` skill entrypoints.
# A skill entrypoint receives the agent's tool-call args dict and returns a
# JSON-serialisable result. ``run_id`` rides in the args so each side-effect is
# attributable to its workflow run (the scenario stamps it into state).
# ---------------------------------------------------------------------------


def sim_email(args: dict[str, Any]) -> dict[str, Any]:
    """Simulate sending an email (records to/subject/body)."""
    return record(
        "email",
        "send",
        {"to": args.get("to"), "subject": args.get("subject"), "body": args.get("body")},
        run_id=str(args.get("run_id", "")),
    )


def sim_sms(args: dict[str, Any]) -> dict[str, Any]:
    """Simulate sending an SMS."""
    return record(
        "sms",
        "send",
        {"to": args.get("to"), "text": args.get("text")},
        run_id=str(args.get("run_id", "")),
    )


def sim_erp_submit(args: dict[str, Any]) -> dict[str, Any]:
    """Simulate an ERP/finance submission (e.g. SAP expense posting)."""
    return record(
        "erp",
        "submit",
        {
            "document": args.get("document"),
            "amount": args.get("amount"),
            "approver": args.get("approver"),
        },
        run_id=str(args.get("run_id", "")),
    )


def sim_servicenow(args: dict[str, Any]) -> dict[str, Any]:
    """Simulate creating a ServiceNow ticket."""
    return record(
        "servicenow",
        "create_ticket",
        {"short_description": args.get("short_description"), "priority": args.get("priority")},
        run_id=str(args.get("run_id", "")),
    )


def sim_slack(args: dict[str, Any]) -> dict[str, Any]:
    """Simulate posting a Slack message."""
    return record(
        "slack",
        "post",
        {"channel": args.get("channel"), "text": args.get("text")},
        run_id=str(args.get("run_id", "")),
    )


def sim_provision_account(args: dict[str, Any]) -> dict[str, Any]:
    """Simulate provisioning an account in an identity system (AD/Okta)."""
    return record(
        "identity",
        "provision",
        {"user": args.get("user"), "system": args.get("system")},
        run_id=str(args.get("run_id", "")),
    )
