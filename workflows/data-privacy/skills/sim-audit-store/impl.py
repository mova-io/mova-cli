"""Simulated audited document storage for the `sim-audit-store` skill.

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

The python skill backend puts this skill dir's PARENT on ``sys.path``
(``movate/core/skill_backend/python.py::_resolve``), so the
``entry: sim-audit-store.impl:run`` in skill.yaml resolves this file via a PEP 420
namespace package — hyphenated dir names are fine through
``importlib.import_module``.

Contract: ``run(input_payload, ctx) -> dict`` — the validated skill input
plus a ``SkillExecutionContext``. ``ctx.run_id`` is the workflow run id (both
``call_skill_activity`` and the native runner's ``_run_tool`` thread it), so
the ledger row is attributable to its run; an explicit ``run_id`` in the
input wins when provided (the harness facades' convention). ``ctx.mock``
short-circuits to a stub without touching any DB (the documented convention
for externally-recording backends), so ``mdk run --mock`` stays hermetic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_TABLE = "sim_side_effects"
_SYSTEM = "dlp"

# The audited action is derived 1:1 from the document's classification — the
# compliance trail the data-privacy scenario asserts. The skill's input
# schema enum-pins `classification` to exactly these keys, so dispatch_skill
# fails the call loudly BEFORE this impl runs on any out-of-vocabulary value
# (no silent default bucket that would green-wash the audit trail).
_ACTIONS: dict[str, str] = {
    "public": "store_public",
    "internal": "store_internal",
    "regulated": "store_regulated",
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


def _record(run_id: str, action: str, payload: dict[str, Any]) -> None:
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
                    action,
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
                (ts, run_id, _SYSTEM, action, body),
            )
            conn.commit()
        finally:
            conn.close()


def _reference(run_id: str, classification: str) -> str:
    """A stable synthetic audit reference — same (run, classification) ⇒ same
    ref, so a Temporal activity retry confirms the same audit row id."""
    digest = hashlib.sha256(f"{run_id}:{classification}".encode()).hexdigest()[:6].upper()
    return f"DLP-AUD-{digest}"


def run(input_payload: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Record the classification-keyed audit row; return the confirmation."""
    classification = str(input_payload.get("classification", ""))
    requester = str(input_payload.get("requester", ""))
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    # Input-schema enum makes a miss impossible through dispatch_skill; the
    # KeyError below is the fail-loud backstop for hand-built calls.
    action = _ACTIONS[classification]
    ref = _reference(run_id, classification)
    if not getattr(ctx, "mock", False):
        _record(run_id, action, {"classification": classification, "requester": requester})
    return {
        "audit_result": (
            f"Stored {classification} document for {requester or 'requester'} "
            f"with audit action {action} (reference {ref})."
        )
    }
