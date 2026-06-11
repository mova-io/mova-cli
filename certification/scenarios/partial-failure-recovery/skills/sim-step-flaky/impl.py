"""Simulated FLAKY pipeline step for the `sim-step-flaky` skill.

THE PARTIAL-FAILURE-RECOVERY TRICK. The scenario's claim is that Temporal
re-executes only the FAILED activity, never the completed ones. This skill
makes both halves auditable: every invocation FIRST appends one
``{system: pipeline, action: step2_attempt}`` row to the shared
``sim_side_effects`` ledger keyed by the workflow run id, THEN counts its own
attempt rows for that run — and RAISES while ``attempts <= fail_times``. The
raise is a real activity failure, so Temporal's compiled retry policy
(``maximum_attempts=3``, ADR 054 D9) re-runs THIS activity only; the
already-completed step-one activity's result replays from history. The one
``{system: pipeline, action: step2}`` row is recorded ONLY on the invocation
that succeeds. So with ``fail_times: 1`` the ledger reads: step1 x1,
step2_attempt x2, step2 x1, step3 x1 — the recovery proof.

SELF-CONTAINED ON PURPOSE (ADR 097 D2). The deployed temporal-worker image
bakes ``src/`` + ``workflows/`` + ``agents/`` only — ``certification/`` (and
its ``certification.harness.sim_systems`` module) is NOT importable there. So
this impl re-implements the harness ledger minimally: same table, same DDL +
insert shape, same backend resolution (``MOVATE_DB_URL``/``MOVATE_PG_URL`` →
Postgres via asyncpg — already a shipped dependency; otherwise the SQLite
file at ``MOVATE_DB``, default ``~/.movate/local.db``). The certification
driver's side-effect asserts read the rows back by ``run_id``.

The python skill backend puts this skill dir's PARENT on ``sys.path``
(``movate/core/skill_backend/python.py::_resolve``), so the
``entry: sim-step-flaky.impl:run`` in skill.yaml resolves this file via a
PEP 420 namespace package — hyphenated dir names are fine through
``importlib.import_module``.

Contract: ``run(input_payload, ctx) -> dict`` — the validated skill input
plus a ``SkillExecutionContext``. ``ctx.run_id`` is the workflow run id (both
``call_skill_activity`` and the native runner's ``_run_tool`` thread it), so
the attempt count is scoped to ONE run; an explicit ``run_id`` in the input
wins when provided (the harness facades' convention). ``ctx.mock``
short-circuits to a success stub without touching any DB and without ever
raising (the documented convention for externally-recording backends), so
``mdk run --mock`` stays hermetic.
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
_SYSTEM = "pipeline"
_ATTEMPT_ACTION = "step2_attempt"
_SUCCESS_ACTION = "step2"

# Same DDL as certification/harness/sim_systems.py — the rows must land in
# the identical table the harness reads.
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


def _count(run_id: str, action: str) -> int:
    """Count this run's rows for ``action`` — the attempt number."""
    url = _pg_url()
    if url:

        async def _do() -> int:
            import asyncpg  # noqa: PLC0415 — shipped dep; deferred like sim_systems

            conn = await asyncpg.connect(url)
            try:
                await conn.execute(_PG_DDL)
                count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE run_id=$1 AND system=$2 AND action=$3",
                    run_id,
                    _SYSTEM,
                    action,
                )
                return int(count or 0)
            finally:
                await conn.close()

        with ThreadPoolExecutor(max_workers=1) as ex:
            return int(ex.submit(lambda: asyncio.run(_do())).result())
    conn = sqlite3.connect(_sqlite_path())
    try:
        conn.execute(_SQLITE_DDL)
        row = conn.execute(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE run_id=? AND system=? AND action=?",
            (run_id, _SYSTEM, action),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def _reference(run_id: str) -> str:
    """A stable synthetic step reference — same run ⇒ same ref, so the
    eventually-successful attempt confirms one consistent step id."""
    digest = hashlib.sha256(f"{run_id}:step2".encode()).hexdigest()[:6].upper()
    return f"STEP-{digest}"


def run(input_payload: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Record this attempt; raise while attempts <= fail_times; record the
    step2 success row ONLY on the invocation that completes."""
    request = str(input_payload.get("request", ""))
    fail_times = int(input_payload.get("fail_times", 0) or 0)
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    ref = _reference(run_id)
    if getattr(ctx, "mock", False):
        # Hermetic stub: no DB, no raise — `mdk run --mock` always succeeds.
        return {
            "step_result": f"[mock] Completed step2 for {request or 'request'} (reference {ref})."
        }
    _record(run_id, _ATTEMPT_ACTION, {"request": request, "fail_times": fail_times})
    attempts = _count(run_id, _ATTEMPT_ACTION)
    if attempts <= fail_times:
        raise RuntimeError(
            f"simulated step2 failure for {request or 'request'} "
            f"(attempt {attempts} of fail_times={fail_times}) — "
            "the activity retry policy should re-run ONLY this step"
        )
    _record(run_id, _SUCCESS_ACTION, {"request": request, "attempts": attempts})
    return {
        "step_result": (
            f"Completed step2 for {request or 'request'} on attempt {attempts} (reference {ref})."
        )
    }
