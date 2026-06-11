"""Simulated observability-facts pull for the `sim-fetch-facts` skill.

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
``entry: sim-fetch-facts.impl:run`` in skill.yaml resolves this file via a
PEP 420 namespace package — hyphenated dir names are fine through
``importlib.import_module``.

Contract: ``run(input_payload, ctx) -> dict`` — the validated skill input
plus a ``SkillExecutionContext``. ``ctx.run_id`` is the workflow run id (both
``call_skill_activity`` and the native runner's ``_run_tool`` thread it), so
the ledger row is attributable to its run; an explicit ``run_id`` in the
input wins when provided (the harness facades' convention). ``ctx.mock``
short-circuits to the SAME canned data without touching any DB (the
documented convention for externally-recording backends), so ``mdk run
--mock`` stays hermetic.

THE FACT SHAPE. Each canned row mirrors the real
``observability_facts`` row (``movate.core.models.ObservabilityFact``, the
flat reporting surface ``GET /api/v1/observability/facts`` serves — ADR 096):
``fact_id``/``kind``/``source_id``/``tenant_id``/``workflow``/``agent``/
``node_id``/``status``/``runtime``/``route``/``cost_usd``/``tokens_in``/
``tokens_out``/``latency_ms``/``governance_effect``/``error_type`` — so the
summarize agent downstream reads exactly what a real endpoint pull would
give it. The optional ``facts_endpoint`` input documents the real endpoint;
the sim NEVER does network IO — it returns the canned rows and echoes the
endpoint it would have queried in ``facts_source``.
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
_SYSTEM = "observability"
_ACTION = "fetch_facts"

#: The real reporting surface this sim stands in for (ADR 096) — echoed in
#: ``facts_source`` when the caller passes no explicit ``facts_endpoint``.
_DEFAULT_ENDPOINT = "/api/v1/observability/facts"

# Canned, replay-identical fact rows keyed by the `profile` knob. steady =
# every row succeeded (the clean/direct-report path, failure_count 0);
# degraded = the steady rows PLUS one failed workflow_run, one failed run,
# and a governance warn — the summarize agent must count exactly 2 failures.
_STEADY_FACTS: list[dict[str, Any]] = [
    {
        "fact_id": "workflow_run:wfr-cert-3001",
        "kind": "workflow_run",
        "source_id": "wfr-cert-3001",
        "tenant_id": "default",
        "workflow": "expense-approval",
        "agent": None,
        "node_id": None,
        "status": "success",
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": 0,
        "governance_effect": "allow",
        "error_type": None,
    },
    {
        "fact_id": "workflow_run:wfr-cert-3002",
        "kind": "workflow_run",
        "source_id": "wfr-cert-3002",
        "tenant_id": "default",
        "workflow": "itsm-request",
        "agent": None,
        "node_id": None,
        "status": "success",
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": 0,
        "governance_effect": "allow",
        "error_type": None,
    },
    {
        "fact_id": "run:run-cert-7101",
        "kind": "run",
        "source_id": "run-cert-7101",
        "tenant_id": "default",
        "workflow": None,
        "agent": "notify",
        "node_id": "notify",
        "status": "success",
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.0042,
        "tokens_in": 152,
        "tokens_out": 27,
        "latency_ms": 1730,
        "governance_effect": "allow",
        "error_type": None,
    },
    {
        "fact_id": "run:run-cert-7102",
        "kind": "run",
        "source_id": "run-cert-7102",
        "tenant_id": "default",
        "workflow": None,
        "agent": "digest",
        "node_id": "digest",
        "status": "success",
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.0117,
        "tokens_in": 644,
        "tokens_out": 188,
        "latency_ms": 4210,
        "governance_effect": "allow",
        "error_type": None,
    },
]
_DEGRADED_EXTRA: list[dict[str, Any]] = [
    {
        "fact_id": "workflow_run:wfr-cert-3003",
        "kind": "workflow_run",
        "source_id": "wfr-cert-3003",
        "tenant_id": "default",
        "workflow": "pii-detection",
        "agent": None,
        "node_id": "quarantine",
        "status": "error",
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": 0,
        "governance_effect": "allow",
        "error_type": "SkillError",
    },
    {
        "fact_id": "run:run-cert-7103",
        "kind": "run",
        "source_id": "run-cert-7103",
        "tenant_id": "default",
        "workflow": None,
        "agent": "erp-poster",
        "node_id": "post-erp",
        "status": "error",
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.0,
        "tokens_in": 512,
        "tokens_out": 0,
        "latency_ms": 30000,
        "governance_effect": "warn",
        "error_type": "timeout",
    },
]
_PROFILES: dict[str, list[dict[str, Any]]] = {
    "steady": _STEADY_FACTS,
    "degraded": [*_STEADY_FACTS, *_DEGRADED_EXTRA],
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
    """Record the simulated facts pull; return the canned rows + the echo.

    A missing ``profile`` falls back to ``steady``; an unknown explicit
    profile is rejected by the input schema upstream — the ``KeyError`` here
    is the fail-loud backstop, never a silent default bucket. The ledger
    payload carries the pull's parameters + row count, never the rows
    themselves.
    """
    window = str(input_payload.get("window") or "24h")
    profile = str(input_payload.get("profile") or "steady")
    endpoint = str(input_payload.get("facts_endpoint") or _DEFAULT_ENDPOINT)
    facts = _PROFILES[profile]
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    if not getattr(ctx, "mock", False):
        _record(
            run_id,
            {"window": window, "profile": profile, "endpoint": endpoint, "rows": len(facts)},
        )
    return {"facts": [dict(row) for row in facts], "facts_source": endpoint}
