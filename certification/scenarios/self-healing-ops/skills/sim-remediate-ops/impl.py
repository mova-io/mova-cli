"""Simulated remediation attempt #1 for the `sim-remediate-ops` skill.

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

WHY THE DIRECTORY IS NOT NAMED ``sim-remediate`` (cert run ar11boj, 2026-06):
the python skill backend imports a skill as ``<dir-name>.impl`` with the dir's
PARENT on ``sys.path``, and caches the resolved callable PER ENTRY STRING for
the worker-process lifetime. ``workflows/incident-response/skills/sim-remediate``
already claims the ``sim-remediate.impl:run`` entry on the shared certification
worker — whichever scenario dispatched first poisoned the other's dispatch
(the incident-response impl returns ``remediation_status``, not ``r1_status``,
so this workflow's output validation failed and Temporal's retry policy
re-ran the activity, duplicating ledger rows). A skill directory name must be
unique across every workflow a single worker serves; ``sim-remediate-ops`` is.
``tests/test_b9_scenarios.py`` pins this with a repo-wide duplicate-dir guard.

The attempt-1 outcome is a PURE PREDICATE over the fault, so the verification
decision downstream replays identically on Temporal: a fault containing
"hardware" can never be fixed by software remediation, and a fault containing
"stuck" is TRANSIENT — the first attempt races the condition and fails, only
the retry (``sim-remediate-retry``, attempt #2) lands it. Everything else
applies on the first attempt. The triage agent's ``remediation_action`` is
recorded for the audit trail but never decides the outcome.

The ledger write is FAIL-SOFT and IDEMPOTENT (the other ar11boj lesson): the
predicate result is the skill's contract, the row is observability — a ledger
failure is logged and swallowed, never raised, so a flaky DB cannot turn
Temporal's retry policy into a duplicate-row storm. And a retried attempt
(e.g. a timeout AFTER the commit) rebuilds the byte-identical payload, so the
insert is suppressed when the same ``(run_id, system, action, payload)`` row
already exists — the certification ``times:`` counts stay honest.

The python skill backend puts this skill dir's PARENT on ``sys.path``
(``movate/core/skill_backend/python.py::_resolve``), so the
``entry: sim-remediate-ops.impl:run`` in skill.yaml resolves this file via a
PEP 420 namespace package — hyphenated dir names are fine through
``importlib.import_module``.

Contract: ``run(input_payload, ctx) -> dict`` — the validated skill input
plus a ``SkillExecutionContext``. ``ctx.run_id`` is the workflow run id (both
``call_skill_activity`` and the native runner's ``_run_tool`` thread it), so
the ledger row is attributable to its run; an explicit ``run_id`` in the
input wins when provided (the harness facades' convention). ``ctx.mock``
short-circuits the ledger write (the outcome predicate itself is pure), so
``mdk run --mock`` stays hermetic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_TABLE = "sim_side_effects"
_SYSTEM = "ops"
_ACTION = "remediate"
_ATTEMPT = 1

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

# Idempotent append: insert ONLY when no identical (run_id, system, action,
# payload) row exists — a Temporal activity retry re-runs the pure predicate,
# rebuilds the byte-identical payload, and lands on the existing row instead
# of doubling the certification count. (`ts` is excluded on purpose: it is
# the only per-attempt-varying column.) Rows without a run_id have no safe
# dedupe key and are appended unconditionally.
_PG_INSERT = (
    f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) "
    "SELECT $1,$2,$3,$4,$5 WHERE NOT EXISTS ("
    f"SELECT 1 FROM {_TABLE} "
    "WHERE run_id=$2 AND system=$3 AND action=$4 AND payload=$5)"
)
_PG_INSERT_NO_RUN = (
    f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) VALUES ($1,$2,$3,$4,$5)"
)
_SQLITE_INSERT = (
    f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) "
    "SELECT ?,?,?,?,? WHERE NOT EXISTS ("
    f"SELECT 1 FROM {_TABLE} WHERE run_id=? AND system=? AND action=? AND payload=?)"
)
_SQLITE_INSERT_NO_RUN = (
    f"INSERT INTO {_TABLE} (ts, run_id, system, action, payload) VALUES (?,?,?,?,?)"
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
    """Best-effort idempotent ledger append — Postgres when configured, else
    SQLite. FAIL-SOFT BY CONTRACT: the predicate result is the skill's output,
    the row is observability — any failure here is logged and swallowed so the
    activity (and Temporal's retry policy) never sees it."""
    ts, body = time.time(), json.dumps(payload, default=str)
    try:
        url = _pg_url()
        if url:

            async def _do() -> None:
                import asyncpg  # noqa: PLC0415 — shipped dep; deferred like sim_systems

                conn = await asyncpg.connect(url)
                try:
                    await conn.execute(_PG_DDL)
                    if run_id:
                        await conn.execute(_PG_INSERT, ts, run_id, _SYSTEM, _ACTION, body)
                    else:
                        await conn.execute(_PG_INSERT_NO_RUN, ts, run_id, _SYSTEM, _ACTION, body)
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
                if run_id:
                    conn.execute(
                        _SQLITE_INSERT,
                        (ts, run_id, _SYSTEM, _ACTION, body, run_id, _SYSTEM, _ACTION, body),
                    )
                else:
                    conn.execute(_SQLITE_INSERT_NO_RUN, (ts, run_id, _SYSTEM, _ACTION, body))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        _log.warning(
            "sim ledger write failed (run_id=%s system=%s action=%s) — continuing: "
            "the ledger is observability, the predicate result is the contract",
            run_id,
            _SYSTEM,
            _ACTION,
            exc_info=True,
        )


def attempt_outcome(fault: str) -> str:
    """The pure attempt-1 predicate: hardware never applies, transient
    ("stuck") faults need the retry, everything else applies first try."""
    lowered = fault.lower()
    if "hardware" in lowered or "stuck" in lowered:
        return "failed"
    return "applied"


def run(input_payload: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Apply remediation attempt #1; record the attempt-1 ledger row."""
    fault = str(input_payload.get("fault", ""))
    component = str(input_payload.get("component", ""))
    remediation_action = str(input_payload.get("remediation_action", ""))
    status = attempt_outcome(fault)
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    if not getattr(ctx, "mock", False):
        _record(
            run_id,
            {
                "component": component,
                "remediation_action": remediation_action,
                "attempt": _ATTEMPT,
                "status": status,
            },
        )
    return {"r1_status": status}
