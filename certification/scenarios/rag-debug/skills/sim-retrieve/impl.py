"""Deterministic simulated retrieval for the `sim-retrieve` skill — keyword
scoring over an inline knowledge base, no embeddings, no network, no LLM.

THE DEBUGGING POINT OF THIS SKILL: retrieval quality must be inspectable as a
first-class, replayable step. A real vector store hides the scoring inside a
service; this simulation makes every stage auditable — the query tokens, the
per-document score, and the ranked result all derive from a pure stdlib
function, replay identically on Temporal, and are unit-tested value by value
(tests/test_b7_scenarios.py). The downstream decision node routes on the
returned ``top_score``; the diagnose path exists precisely because a low
score is a DEBUGGABLE outcome, not an error.

Scoring (fixed + deterministic): lowercase-alphanumeric tokens, drop a small
stopword list + single characters, then per document
``score = |query_tokens ∩ doc_tokens| / |query_tokens|`` rounded to 4 places.
Documents scoring 0 are dropped, the rest sort by ``(-score, id)`` and the
top 3 are returned; ``top_score`` is the best of them (0.0 when nothing
matched or the query had no content tokens).

SELF-CONTAINED ON PURPOSE (ADR 097 D2). The deployed temporal-worker image
bakes ``src/`` + ``workflows/`` + ``agents/`` only — ``certification/`` (and
its ``certification.harness.sim_systems`` module) is NOT importable there. So
this impl re-implements the harness ledger minimally: it appends one
``{system: vectorstore, action: retrieve}`` row to the SAME
``sim_side_effects`` table, with the SAME DDL + insert shape and the SAME
backend resolution (``MOVATE_DB_URL``/``MOVATE_PG_URL`` → Postgres via
asyncpg — already a shipped dependency; otherwise the SQLite file at
``MOVATE_DB``, default ``~/.movate/local.db``). The certification driver's
side-effect asserts (``certification/harness/asserts.py``) read the row back
from the shared DB by ``run_id``.

The python skill backend puts this skill dir's PARENT on ``sys.path``
(``movate/core/skill_backend/python.py::_resolve``), so the
``entry: sim-retrieve.impl:run`` in skill.yaml resolves this file via a
PEP 420 namespace package — hyphenated dir names are fine through
``importlib.import_module``.

Contract: ``run(input_payload, ctx) -> dict`` — the validated skill input
plus a ``SkillExecutionContext``. ``ctx.run_id`` is the workflow run id (both
``call_skill_activity`` and the native runner's ``_run_tool`` thread it), so
the ledger row is attributable to its run; an explicit ``run_id`` in the
input wins when provided (the harness facades' convention). ``ctx.mock``
skips ONLY the ledger write (the retrieval itself is pure and runs for
real), so ``mdk run --mock`` exercises the true routing hermetically.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_TABLE = "sim_side_effects"
_SYSTEM = "vectorstore"
_ACTION = "retrieve"

#: The inline knowledge base — a tiny IT-helpdesk corpus. Small ON PURPOSE:
#: the scenario certifies the retrieval *pipeline* (score → route → answer /
#: diagnose), not corpus scale. Swap these for your real store's documents
#: (or the whole skill for a real retriever) without touching the workflow.
_DOCS: tuple[dict[str, str], ...] = (
    {
        "id": "kb-001",
        "title": "Resetting your corporate password",
        "text": (
            "To reset your corporate password open the self-service portal, choose "
            "reset password, and confirm the code sent to your phone. The new "
            "password must be at least twelve characters."
        ),
    },
    {
        "id": "kb-002",
        "title": "Requesting VPN access",
        "text": (
            "Request VPN access through the service portal. After approval install "
            "the VPN client, sign in with your corporate credentials, and connect "
            "to the nearest gateway."
        ),
    },
    {
        "id": "kb-003",
        "title": "Submitting an expense report",
        "text": (
            "Submit an expense report in the finance portal within thirty days of "
            "the purchase. Attach receipts for every item above twenty five "
            "dollars and pick the correct cost center."
        ),
    },
    {
        "id": "kb-004",
        "title": "Joining the office wifi",
        "text": (
            "Join the office wifi by selecting the corp network, entering your "
            "corporate credentials, and accepting the device certificate when "
            "prompted."
        ),
    },
    {
        "id": "kb-005",
        "title": "Printer troubleshooting",
        "text": (
            "If the office printer fails, restart the print spooler, check the "
            "paper tray, and reinstall the printer driver from the software "
            "center."
        ),
    },
)

#: Tokens carrying no retrieval signal — kept SMALL and fixed so the scoring
#: stays explainable; single-character tokens are dropped separately.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "can",
        "do",
        "does",
        "for",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "you",
        "your",
    }
)

_WORD = re.compile(r"[a-z0-9]+")
_TOP_K = 3


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric content tokens (stopwords + 1-char dropped)."""
    return {t for t in _WORD.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS}


def retrieve(query: str) -> tuple[list[dict[str, Any]], float]:
    """Score every doc against ``query``; return (top-k hits, top_score).

    Each hit is ``{id, title, score, text}``; hits are sorted by
    ``(-score, id)`` so ties break deterministically. A query with no
    content tokens (or no overlap) returns ``([], 0.0)`` — the diagnose
    route's input, not an error.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return [], 0.0
    hits: list[dict[str, Any]] = []
    for doc in _DOCS:
        matched = len(query_tokens & _tokens(doc["title"] + " " + doc["text"]))
        score = round(matched / len(query_tokens), 4)
        if score > 0:
            hits.append(
                {"id": doc["id"], "title": doc["title"], "score": score, "text": doc["text"]}
            )
    hits.sort(key=lambda d: (-d["score"], d["id"]))
    top = hits[:_TOP_K]
    return top, (float(top[0]["score"]) if top else 0.0)


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
    """Retrieve from the inline KB; record the auditable retrieval row."""
    query = str(input_payload.get("query", ""))
    run_id = str(input_payload.get("run_id") or getattr(ctx, "run_id", "") or "")
    retrieved_docs, top_score = retrieve(query)
    if not getattr(ctx, "mock", False):
        _record(
            run_id,
            {
                "query": query,
                "top_score": top_score,
                "doc_ids": [d["id"] for d in retrieved_docs],
            },
        )
    return {"retrieved_docs": retrieved_docs, "top_score": top_score}
