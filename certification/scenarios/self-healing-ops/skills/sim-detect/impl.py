"""Simulated infrastructure fault detection for the `sim-detect` skill.

SELF-CONTAINED ON PURPOSE (ADR 097 D2). The deployed temporal-worker image
bakes ``src/`` + ``workflows/`` + ``agents/`` only — ``certification/`` (and
its ``certification.harness.sim_systems`` module) is NOT importable there. So
this impl re-implements the harness ledger minimally: it appends one row to
the SAME ``sim_side_effects`` table, with the SAME DDL + insert shape and the
SAME backend resolution (``MOVATE_DB_URL``/``MOVATE_PG_URL`` → Postgres via
asyncpg — already a shipped dependency; otherwise the SQLite file at
``MOVATE_DB``, default ``~/.movate/local.db``). The certification driver's
side-effect asserts (``certification/harness/asserts.py``) read the row back
from the shared DB by ``run_id``.

The fault catalog is CANNED + CLOSED: a fixed table keyed off the monitor's
``signal``, so the fault that drives the workflow's two-attempt remediation
replays identically on Temporal. A signal outside the catalog is a loud
``KeyError`` (the sim-audit-store posture) — no silent "unknown fault"
default that would send the remediator after a phantom.

The python skill backend puts this skill dir's PARENT on ``sys.path``
(``movate/core/skill_backend/python.py::_resolve``), so the
``entry: sim-detect.impl:run`` in skill.yaml resolves this file via a
PEP 420 namespace package — hyphenated dir names are fine through
``importlib.import_module``.

Contract: ``run(input_payload, ctx) -> dict`` — the validated skill input
plus a ``SkillExecutionContext``. ``ctx.run_id`` is the workflow run id (both
``call_skill_activity`` and the native runner's ``_run_tool`` thread it), so
the ledger row is attributable to its run; an explicit ``run_id`` in the
input wins when provided (the harness facades' convention). ``ctx.mock``
short-circuits the ledger write (the canned lookup itself is pure), so
``mdk run --mock`` stays hermetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_TABLE = "sim_side_effects"
_SYSTEM = "monitor"
_ACTION = "detect"

# The canned fault catalog: signal → (fault, component). One fault attempt 1
# fixes outright, one TRANSIENT fault ("stuck" — attempt 1 fails, the retry
# applies), and one "hardware" fault that deterministically fails BOTH
# attempts (software remediation cannot fix hardware) and must escalate.
_CANNED: dict[str, tuple[str, str]] = {
    "checkout-latency-spike": ("connection pool exhaustion", "checkout-api"),
    "queue-backlog-alarm": ("stuck consumer after deploy", "billing-worker"),
    "disk-failure-alert": ("hardware fault: failing disk on node-7", "etcd-cluster"),
}

# Same DDL as certification/harness/sim_systems.py — the row must land in the
# identical table the harness reads.
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


def _pg_url() -> str | None:
    """The shared Postgres DSN, mirroring movate.storage's env precedence."""
    for var in ("MOVATE_DB_URL", "MOVATE_PG_URL"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


def _sqlite_path() -> str:
    """The SQLite file MDK would use (shared local DB file)."""
    val = os.environ.get("MOVATE_DB", "").strip()
    if val:
        return val
    return str(Path.home() / ".movate" / "local.db")


def _record(run_id: str, payload: dict[str, Any]) -> None:
    """Append one ledger row — Postgres when configured, else SQLite."""
    ts, body = time.time(), json.dumps(payload, default=str)
    url = _pg_url()
    if url:

        async def _do() -> None:
            import asyncpg  # noqa: PLC0415 — shipped dep; deferred like sim_systems

            conn = await asyncpg.connect(url)
            try:
                await conn.execute(_PG_DDL)
                await conn.execute(
                    f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    ts,
                    run_id,
                    _SYSTEM,
                    _ACTION,
                    body,
                )
            finally:
                await conn.close()

        # Sync skill code may already sit inside a running event loop (a
        # Temporal activity) — run the async op on its own thread + loop,
        # the sim_systems pattern.
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(lambda: asyncio.run(_do())).result()
    else:
        conn = sqlite3.connect(_sqlite_path())
        try:
            conn.execute(_SQLITE_DDL)
            conn.execute(
                f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) VALUES (?,?,?,?,?)",
                (ts, run_id, _SYSTEM, _ACTION, body),
            )
            conn.commit()
        finally:
            conn.close()


def run(input_payload: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Return the canned fault for the signal; record the detect ledger row.

    Raises ``KeyError`` for a signal outside the canned catalog — fail loud,
    never a phantom fault.
    """
    signal = str(input_payload.get("signal", ""))
    fault, component = _CANNED[signal]
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    if not getattr(ctx, "mock", False):
        _record(run_id, {"signal": signal, "fault": fault, "component": component})
    return {"fault": fault, "component": component}
