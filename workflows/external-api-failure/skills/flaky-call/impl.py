"""Simulated flaky external-API call for the `flaky-call` skill.

THE RETRY-OBSERVABILITY TRICK. Temporal's activity retry policy
(``_RETRY_POLICY = RetryPolicy(maximum_attempts=3)`` in the compiled module,
ADR 054 D9) is invisible from the outside: a retried activity looks like one
successful node. This skill makes it auditable: every invocation FIRST
appends one ``{system: external-api, action: attempt}`` row to the shared
``sim_side_effects`` ledger keyed by the workflow run id, THEN counts its own
rows for that run — and RAISES while ``attempts <= fail_times``. The raise
becomes a real activity failure, Temporal re-runs the activity, and the next
invocation sees one more row. So:

* ``fail_times: 0`` — 1 attempt row, success on the first try (provider
  ``primary``).
* ``fail_times: 1`` — 2 attempt rows; the FIRST attempt failed, Temporal's
  durable retry succeeded (provider ``fallback`` — the fail-over served it).
  This is the certification suite's retry proof.
* ``fail_times >= 3`` — exactly 3 attempt rows (the policy's cap), then the
  activity error propagates and the workflow lands a terminal ERROR fact.
  The ledger shows the budget was spent: rows = attempts.

SELF-CONTAINED ON PURPOSE (ADR 097 D2). The deployed temporal-worker image
bakes ``src/`` + ``workflows/`` + ``agents/`` only — ``certification/`` (and
its ``certification.harness.sim_systems`` module) is NOT importable there. So
this impl re-implements the harness ledger minimally: same table, same DDL +
insert shape, same backend resolution (``MOVATE_DB_URL``/``MOVATE_PG_URL`` →
Postgres via asyncpg — already a shipped dependency; otherwise the SQLite
file at ``MOVATE_DB``, default ``~/.movate/local.db``). The certification
driver's side-effect asserts read the attempt rows back by ``run_id``.

The python skill backend puts this skill dir's PARENT on ``sys.path``
(``movate/core/skill_backend/python.py::_resolve``), so the
``entry: flaky-call.impl:run`` in skill.yaml resolves this file via a
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
_SYSTEM = "external-api"
_ACTION = "attempt"

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


def _record_attempt_and_count(run_id: str, payload: dict[str, Any]) -> int:
    """Append THIS invocation's attempt row, then return the run's attempt
    count (rows = activity attempts — the retry-observability contract).

    One connection per call: Temporal retries are sequential per activity, so
    insert-then-count is race-free within a run.
    """
    ts, body = time.time(), json.dumps(payload, default=str)
    url = _pg_url()
    if url:

        async def _do() -> int:
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
                count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE run_id=$1 AND system=$2 AND action=$3",
                    run_id,
                    _SYSTEM,
                    _ACTION,
                )
                return int(count or 0)
            finally:
                await conn.close()

        # Sync skill code may already sit inside a running event loop (a
        # Temporal activity) — run the async op on its own thread + loop,
        # the sim_systems pattern.
        with ThreadPoolExecutor(max_workers=1) as ex:
            return int(ex.submit(lambda: asyncio.run(_do())).result())
    conn = sqlite3.connect(_sqlite_path())
    try:
        conn.execute(_SQLITE_DDL)
        conn.execute(
            f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) VALUES (?,?,?,?,?)",
            (ts, run_id, _SYSTEM, _ACTION, body),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE run_id=? AND system=? AND action=?",
            (run_id, _SYSTEM, _ACTION),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def _reference(run_id: str, request: str) -> str:
    """A stable synthetic API reference — same (run, request) ⇒ same ref, so
    the eventually-successful attempt confirms one consistent call id."""
    digest = hashlib.sha256(f"{run_id}:{request}".encode()).hexdigest()[:6].upper()
    return f"API-{digest}"


def run(input_payload: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Record this attempt; raise while attempts <= fail_times; then succeed.

    The raise is the whole point: it surfaces as a SkillError → the activity
    fails → Temporal's retry policy re-runs it (max 3 attempts). The success
    reports which provider served — ``primary`` on a clean first attempt,
    ``fallback`` when the durable retry (the fail-over) carried it.
    """
    request = str(input_payload.get("request", ""))
    fail_times = int(input_payload.get("fail_times", 0) or 0)
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    ref = _reference(run_id, request)
    if getattr(ctx, "mock", False):
        # Hermetic stub: no DB, no raise — `mdk run --mock` always succeeds.
        return {
            "provider_ok": True,
            "provider": "primary",
            "api_result": f"[mock] External call for {request or 'request'} ok (reference {ref}).",
        }
    attempts = _record_attempt_and_count(run_id, {"request": request, "fail_times": fail_times})
    if attempts <= fail_times:
        raise RuntimeError(
            f"simulated external-API failure for {request or 'request'} "
            f"(attempt {attempts} of fail_times={fail_times}) — "
            "the activity retry policy should re-run this call"
        )
    provider = "primary" if attempts == 1 else "fallback"
    return {
        "provider_ok": True,
        "provider": provider,
        "api_result": (
            f"External call for {request or 'request'} succeeded on attempt {attempts} "
            f"via the {provider} provider (reference {ref})."
        ),
    }
