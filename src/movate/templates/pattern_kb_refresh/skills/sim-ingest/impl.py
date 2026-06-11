"""Deterministic simulated KB ingestion for the `sim-ingest` skill.

THE REFRESH POINT OF THIS SKILL: ingestion must be countable and replayable
before anything judges it. The chunking rule is FIXED — per document,
``ceil(word_count / 40)`` chunks for non-empty text, 0 chunks for an
empty/whitespace document (counted in ``empty_docs``) — so the same input
always yields the same ``ingest_result``, replays identically on Temporal,
and is unit-tested at the boundaries (tests/test_b7_scenarios.py). The
downstream validate agent judges the SUMMARY (counts), never the documents.

SELF-CONTAINED ON PURPOSE (ADR 097 D2). The deployed temporal-worker image
bakes ``src/`` + ``workflows/`` + ``agents/`` only — ``certification/`` (and
its ``certification.harness.sim_systems`` module) is NOT importable there. So
this impl re-implements the harness ledger minimally: it appends one
``{system: kb, action: ingest}`` row to the SAME ``sim_side_effects`` table,
with the SAME DDL + insert shape and the SAME backend resolution
(``MOVATE_DB_URL``/``MOVATE_PG_URL`` → Postgres via asyncpg — already a
shipped dependency; otherwise the SQLite file at ``MOVATE_DB``, default
``~/.movate/local.db``). The certification driver's side-effect asserts
(``certification/harness/asserts.py``) read the row back from the shared DB
by ``run_id``.

The python skill backend puts this skill dir's PARENT on ``sys.path``
(``movate/core/skill_backend/python.py::_resolve``), so the
``entry: sim-ingest.impl:run`` in skill.yaml resolves this file via a PEP 420
namespace package — hyphenated dir names are fine through
``importlib.import_module``.

Contract: ``run(input_payload, ctx) -> dict`` — the validated skill input
plus a ``SkillExecutionContext``. ``ctx.run_id`` is the workflow run id (both
``call_skill_activity`` and the native runner's ``_run_tool`` thread it), so
the ledger row is attributable to its run; an explicit ``run_id`` in the
input wins when provided (the harness facades' convention). ``ctx.mock``
skips ONLY the ledger write (the counting itself is pure and runs for real),
so ``mdk run --mock`` exercises the true routing hermetically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_TABLE = "sim_side_effects"
_SYSTEM = "kb"
_ACTION = "ingest"

#: The fixed chunking rule: one chunk per 40 words (ceil). Fixed ON PURPOSE —
#: the scenario certifies the refresh *pipeline* (ingest → validate → publish
#: | escalate), not a chunking strategy.
_CHUNK_WORDS = 40


def ingest(documents: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Count the ingest deterministically; return (doc_count, chunk_count,
    empty_docs). A document with empty/whitespace ``text`` contributes 0
    chunks and counts in ``empty_docs`` — the validate agent's failure
    signal, never an exception here."""
    doc_count = len(documents)
    chunk_count = 0
    empty_docs = 0
    for doc in documents:
        words = str(doc.get("text", "")).split()
        if not words:
            empty_docs += 1
            continue
        chunk_count += math.ceil(len(words) / _CHUNK_WORDS)
    return doc_count, chunk_count, empty_docs


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


def _reference(run_id: str, doc_count: int, chunk_count: int) -> str:
    """A stable synthetic ingest reference — same (run, counts) ⇒ same ref,
    so a Temporal activity retry confirms the same ingest id."""
    digest = hashlib.sha256(f"{run_id}:{doc_count}:{chunk_count}".encode()).hexdigest()[:6].upper()
    return f"KB-ING-{digest}"


def run(input_payload: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Count the simulated ingest; record the auditable kb ingest row."""
    raw = input_payload.get("documents")
    documents = [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    doc_count, chunk_count, empty_docs = ingest(documents)
    if not getattr(ctx, "mock", False):
        _record(
            run_id,
            {"doc_count": doc_count, "chunk_count": chunk_count, "empty_docs": empty_docs},
        )
    return {
        "ingest_result": {
            "doc_count": doc_count,
            "chunk_count": chunk_count,
            "empty_docs": empty_docs,
            "reference": _reference(run_id, doc_count, chunk_count),
        }
    }
