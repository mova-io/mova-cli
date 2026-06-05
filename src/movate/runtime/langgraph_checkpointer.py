"""LangGraph checkpoint saver backed by mdk's StorageProvider (ADR 030 D4).

Enables durable graph state for HITL workflows, long-running agents, and
state that survives container restarts. Checkpoints are persisted via the
same storage backend the rest of mdk uses (SQLite for local dev, Postgres
in production).

Import safety: ``langgraph`` is imported LAZILY inside methods, never at
module scope. A runtime without ``mdk[langgraph]`` installed can still
import this module.

Usage::

    from movate.runtime.langgraph_checkpointer import MdkCheckpointSaver

    saver = await MdkCheckpointSaver.from_storage(storage)
    compiled = builder.compile(checkpointer=saver)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL schemas — kept here (not in storage/sqlite.py or storage/postgres.py)
# because the checkpointer owns its own tables independently of the main
# StorageProvider schema. ``setup()`` is idempotent.
# ---------------------------------------------------------------------------

_SQLITE_SETUP = """
CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    parent_id      TEXT,
    data           TEXT NOT NULL,
    metadata       TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS idx_lgcp_thread_ns_created
    ON langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC);

CREATE TABLE IF NOT EXISTS langgraph_writes (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    task_path      TEXT NOT NULL DEFAULT '',
    write_idx      INTEGER NOT NULL,
    channel        TEXT NOT NULL,
    data           TEXT NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx)
);

CREATE TABLE IF NOT EXISTS langgraph_blobs (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    channel        TEXT NOT NULL,
    version        TEXT NOT NULL,
    type           TEXT NOT NULL,
    data           BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
"""

_POSTGRES_SETUP = """
CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    parent_id      TEXT,
    data           TEXT NOT NULL,
    metadata       TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS idx_lgcp_thread_ns_created
    ON langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC);

CREATE TABLE IF NOT EXISTS langgraph_writes (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    task_path      TEXT NOT NULL DEFAULT '',
    write_idx      INTEGER NOT NULL,
    channel        TEXT NOT NULL,
    data           TEXT NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx)
);

CREATE TABLE IF NOT EXISTS langgraph_blobs (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    channel        TEXT NOT NULL,
    version        TEXT NOT NULL,
    type           TEXT NOT NULL,
    data           BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
"""


class MdkCheckpointSaver:
    """LangGraph ``BaseCheckpointSaver`` backed by mdk's StorageProvider.

    Delegates persistence to an ``aiosqlite`` connection (local dev) or an
    ``asyncpg`` pool (production). The checkpointer owns three tables:

    * ``langgraph_checkpoints`` — serialised graph state per
      ``(thread_id, checkpoint_ns, checkpoint_id)``.
    * ``langgraph_writes`` — intermediate writes (pending sends) linked to
      a checkpoint.
    * ``langgraph_blobs`` — channel values stored separately for efficient
      partial reads.

    Use :meth:`from_storage` to construct from an initialised
    ``StorageProvider``; it auto-detects SQLite vs Postgres.
    """

    def __init__(self, *, _backend: str, _conn: Any) -> None:
        from langgraph.checkpoint.serde.jsonplus import (  # noqa: PLC0415
            JsonPlusSerializer,
        )

        self._backend = _backend  # "sqlite" | "postgres"
        self._conn = _conn  # aiosqlite.Connection | asyncpg.Pool
        self.serde = JsonPlusSerializer()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def from_storage(cls, storage: StorageProvider) -> MdkCheckpointSaver:
        """Build a checkpointer from an initialised ``StorageProvider``.

        Auto-detects SQLite vs Postgres from the provider's ``name``
        attribute and extracts the underlying connection / pool.

        Raises ``TypeError`` for unsupported backends (e.g. InMemory).
        """
        name = getattr(storage, "name", "")
        if name == "sqlite":
            conn: Any = storage._db  # type: ignore[attr-defined]
            saver = cls(_backend="sqlite", _conn=conn)
            await saver._setup_sqlite()
            return saver
        if name == "postgres":
            pool: Any = storage._db  # type: ignore[attr-defined]
            saver = cls(_backend="postgres", _conn=pool)
            await saver._setup_postgres()
            return saver
        msg = (
            f"MdkCheckpointSaver does not support the {name!r} storage "
            "backend. Use SqliteProvider or PostgresProvider."
        )
        raise TypeError(msg)

    # ------------------------------------------------------------------
    # Schema setup (idempotent)
    # ------------------------------------------------------------------

    async def _setup_sqlite(self) -> None:
        await self._conn.executescript(_SQLITE_SETUP)
        await self._conn.commit()

    async def _setup_postgres(self) -> None:
        async with self._conn.acquire() as conn:
            for raw_stmt in _POSTGRES_SETUP.strip().split(";"):
                cleaned = raw_stmt.strip()
                if cleaned:
                    await conn.execute(cleaned)

    # ------------------------------------------------------------------
    # BaseCheckpointSaver Protocol surface — sync stubs raise, async is
    # the real implementation. LangGraph's ``ainvoke`` path always calls
    # the ``a*`` variants.
    # ------------------------------------------------------------------

    @property
    def config_specs(self) -> list[Any]:
        return []

    def get(self, config: Any) -> Any:
        raise NotImplementedError("Use aget (async)")

    def get_tuple(self, config: Any) -> Any:
        raise NotImplementedError("Use aget_tuple (async)")

    def list(
        self,
        config: Any | None,
        *,
        filter: dict[str, Any] | None = None,
        before: Any | None = None,
        limit: int | None = None,
    ) -> Iterator[Any]:
        raise NotImplementedError("Use alist (async)")

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        raise NotImplementedError("Use aput (async)")

    def put_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("Use aput_writes (async)")

    def delete_thread(self, thread_id: str) -> None:
        raise NotImplementedError("Use adelete_thread (async)")

    def get_next_version(self, current: str | None, channel: Any) -> str:
        """Monotonically increasing version string (matches MemorySaver)."""
        import random  # noqa: PLC0415

        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    # ------------------------------------------------------------------
    # Async: aget / aget_tuple
    # ------------------------------------------------------------------

    async def aget(self, config: Any) -> Any:
        tup = await self.aget_tuple(config)
        return tup.checkpoint if tup else None

    async def aget_tuple(self, config: Any) -> Any:
        from langgraph.checkpoint.base import (  # noqa: PLC0415
            get_checkpoint_id,
        )

        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        if self._backend == "sqlite":
            return await self._aget_tuple_sqlite(thread_id, checkpoint_ns, checkpoint_id, config)
        return await self._aget_tuple_postgres(thread_id, checkpoint_ns, checkpoint_id, config)

    async def _aget_tuple_sqlite(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
        config: Any,
    ) -> Any:

        if checkpoint_id:
            sql = (
                "SELECT checkpoint_id, data, metadata, parent_id "
                "FROM langgraph_checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?"
            )
            params: tuple[Any, ...] = (thread_id, checkpoint_ns, checkpoint_id)
        else:
            sql = (
                "SELECT checkpoint_id, data, metadata, parent_id "
                "FROM langgraph_checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = ? "
                "ORDER BY checkpoint_id DESC LIMIT 1"
            )
            params = (thread_id, checkpoint_ns)

        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return await self._row_to_tuple_sqlite(thread_id, checkpoint_ns, row, config)

    async def _row_to_tuple_sqlite(
        self,
        thread_id: str,
        checkpoint_ns: str,
        row: Any,
        config: Any,
    ) -> Any:
        from langgraph.checkpoint.base import CheckpointTuple  # noqa: PLC0415

        cid = row["checkpoint_id"]
        checkpoint_data = self.serde.loads_typed(("json", row["data"].encode()))
        metadata = self.serde.loads_typed(("json", row["metadata"].encode()))
        parent_id = row["parent_id"]

        # Load channel blobs.
        channel_values: dict[str, Any] = {}
        versions = checkpoint_data.get("channel_versions", {})
        for ch, ver in versions.items():
            async with self._conn.execute(
                "SELECT type, data FROM langgraph_blobs "
                "WHERE thread_id = ? AND checkpoint_ns = ? "
                "AND channel = ? AND version = ?",
                (thread_id, checkpoint_ns, ch, str(ver)),
            ) as bcur:
                brow = await bcur.fetchone()
                if brow and brow["type"] != "empty":
                    channel_values[ch] = self.serde.loads_typed((brow["type"], brow["data"]))

        # Load pending writes.
        pending_writes: list[tuple[str, str, Any]] = []
        async with self._conn.execute(
            "SELECT task_id, channel, data FROM langgraph_writes "
            "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_ns, cid),
        ) as wcur:
            async for wrow in wcur:
                val = self.serde.loads_typed(("json", wrow["data"].encode()))
                pending_writes.append((wrow["task_id"], wrow["channel"], val))

        result_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": cid,
            }
        }
        parent_config = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )

        return CheckpointTuple(
            config=result_config,
            checkpoint={**checkpoint_data, "channel_values": channel_values},
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes if pending_writes else None,
        )

    async def _aget_tuple_postgres(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
        config: Any,
    ) -> Any:
        from langgraph.checkpoint.base import CheckpointTuple  # noqa: PLC0415

        async with self._conn.acquire() as conn:
            if checkpoint_id:
                row = await conn.fetchrow(
                    "SELECT checkpoint_id, data, metadata, parent_id "
                    "FROM langgraph_checkpoints "
                    "WHERE thread_id = $1 AND checkpoint_ns = $2 "
                    "AND checkpoint_id = $3",
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT checkpoint_id, data, metadata, parent_id "
                    "FROM langgraph_checkpoints "
                    "WHERE thread_id = $1 AND checkpoint_ns = $2 "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    thread_id,
                    checkpoint_ns,
                )
            if not row:
                return None

            cid = row["checkpoint_id"]
            checkpoint_data = self.serde.loads_typed(("json", row["data"].encode()))
            metadata = self.serde.loads_typed(("json", row["metadata"].encode()))
            parent_id = row["parent_id"]

            # Load channel blobs.
            channel_values: dict[str, Any] = {}
            versions = checkpoint_data.get("channel_versions", {})
            for ch, ver in versions.items():
                brow = await conn.fetchrow(
                    "SELECT type, data FROM langgraph_blobs "
                    "WHERE thread_id = $1 AND checkpoint_ns = $2 "
                    "AND channel = $3 AND version = $4",
                    thread_id,
                    checkpoint_ns,
                    ch,
                    str(ver),
                )
                if brow and brow["type"] != "empty":
                    channel_values[ch] = self.serde.loads_typed((brow["type"], bytes(brow["data"])))

            # Load pending writes.
            pending_writes: list[tuple[str, str, Any]] = []
            wrows = await conn.fetch(
                "SELECT task_id, channel, data FROM langgraph_writes "
                "WHERE thread_id = $1 AND checkpoint_ns = $2 "
                "AND checkpoint_id = $3",
                thread_id,
                checkpoint_ns,
                cid,
            )
            for wrow in wrows:
                val = self.serde.loads_typed(("json", wrow["data"].encode()))
                pending_writes.append((wrow["task_id"], wrow["channel"], val))

        result_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": cid,
            }
        }
        parent_config = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )

        return CheckpointTuple(
            config=result_config,
            checkpoint={**checkpoint_data, "channel_values": channel_values},
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes if pending_writes else None,
        )

    # ------------------------------------------------------------------
    # Async: alist
    # ------------------------------------------------------------------

    async def alist(
        self,
        config: Any | None,
        *,
        filter: dict[str, Any] | None = None,
        before: Any | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        from langgraph.checkpoint.base import get_checkpoint_id  # noqa: PLC0415

        thread_id: str | None = None
        checkpoint_ns: str | None = None
        if config:
            thread_id = config["configurable"]["thread_id"]
            checkpoint_ns = config["configurable"].get("checkpoint_ns")

        before_id: str | None = None
        if before:
            before_id = get_checkpoint_id(before)

        if self._backend == "sqlite":
            async for tup in self._alist_sqlite(thread_id, checkpoint_ns, filter, before_id, limit):
                yield tup
        else:
            async for tup in self._alist_postgres(
                thread_id, checkpoint_ns, filter, before_id, limit
            ):
                yield tup

    async def _alist_sqlite(
        self,
        thread_id: str | None,
        checkpoint_ns: str | None,
        meta_filter: dict[str, Any] | None,
        before_id: str | None,
        limit: int | None,
    ) -> AsyncIterator[Any]:

        clauses: list[str] = []
        params: list[Any] = []
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if checkpoint_ns is not None:
            clauses.append("checkpoint_ns = ?")
            params.append(checkpoint_ns)
        if before_id is not None:
            clauses.append("checkpoint_id < ?")
            params.append(before_id)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, data, "
            "metadata, parent_id "
            f"FROM langgraph_checkpoints{where} "
            "ORDER BY checkpoint_id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {limit}"

        count = 0
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                md = self.serde.loads_typed(("json", row["metadata"].encode()))
                if meta_filter and not all(md.get(k) == v for k, v in meta_filter.items()):
                    continue

                tup = await self._row_to_tuple_sqlite(
                    row["thread_id"],
                    row["checkpoint_ns"],
                    row,
                    {
                        "configurable": {
                            "thread_id": row["thread_id"],
                            "checkpoint_ns": row["checkpoint_ns"],
                            "checkpoint_id": row["checkpoint_id"],
                        }
                    },
                )
                if tup:
                    yield tup
                    count += 1
                    if limit is not None and count >= limit:
                        return

    async def _alist_postgres(
        self,
        thread_id: str | None,
        checkpoint_ns: str | None,
        meta_filter: dict[str, Any] | None,
        before_id: str | None,
        limit: int | None,
    ) -> AsyncIterator[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        idx = 1
        if thread_id is not None:
            clauses.append(f"thread_id = ${idx}")
            params.append(thread_id)
            idx += 1
        if checkpoint_ns is not None:
            clauses.append(f"checkpoint_ns = ${idx}")
            params.append(checkpoint_ns)
            idx += 1
        if before_id is not None:
            clauses.append(f"checkpoint_id < ${idx}")
            params.append(before_id)
            idx += 1

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, data, "
            "metadata, parent_id "
            f"FROM langgraph_checkpoints{where} "
            "ORDER BY checkpoint_id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {limit}"

        async with self._conn.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        count = 0
        for row in rows:
            md = self.serde.loads_typed(("json", row["metadata"].encode()))
            if meta_filter and not all(md.get(k) == v for k, v in meta_filter.items()):
                continue
            tup = await self._aget_tuple_postgres(
                row["thread_id"],
                row["checkpoint_ns"],
                row["checkpoint_id"],
                {
                    "configurable": {
                        "thread_id": row["thread_id"],
                        "checkpoint_ns": row["checkpoint_ns"],
                        "checkpoint_id": row["checkpoint_id"],
                    }
                },
            )
            if tup:
                yield tup
                count += 1
                if limit is not None and count >= limit:
                    return

    # ------------------------------------------------------------------
    # Async: aput
    # ------------------------------------------------------------------

    async def aput(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: dict[str, Any],
    ) -> Any:
        from langgraph.checkpoint.base import (  # noqa: PLC0415
            get_checkpoint_metadata,
        )

        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        parent_id: str | None = config["configurable"].get("checkpoint_id")
        cid: str = checkpoint["id"]

        # Separate channel_values from the checkpoint data for blob storage.
        c = dict(checkpoint)
        values: dict[str, Any] = c.pop("channel_values", {})

        effective_metadata = get_checkpoint_metadata(config, metadata)
        cp_data = self.serde.dumps_typed(c)
        md_data = self.serde.dumps_typed(effective_metadata)

        if self._backend == "sqlite":
            await self._aput_sqlite(
                thread_id,
                checkpoint_ns,
                cid,
                parent_id,
                cp_data,
                md_data,
                values,
                new_versions,
            )
        else:
            await self._aput_postgres(
                thread_id,
                checkpoint_ns,
                cid,
                parent_id,
                cp_data,
                md_data,
                values,
                new_versions,
            )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": cid,
            }
        }

    async def _aput_sqlite(
        self,
        thread_id: str,
        checkpoint_ns: str,
        cid: str,
        parent_id: str | None,
        cp_data: tuple[str, bytes],
        md_data: tuple[str, bytes],
        values: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> None:
        # Store channel blobs.
        for ch, ver in new_versions.items():
            blob = self.serde.dumps_typed(values[ch]) if ch in values else ("empty", b"")
            await self._conn.execute(
                "INSERT OR REPLACE INTO langgraph_blobs "
                "(thread_id, checkpoint_ns, channel, version, type, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, checkpoint_ns, ch, str(ver), blob[0], blob[1]),
            )

        # Store checkpoint.
        await self._conn.execute(
            "INSERT OR REPLACE INTO langgraph_checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_id, data, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                checkpoint_ns,
                cid,
                parent_id,
                cp_data[1].decode(),
                md_data[1].decode(),
            ),
        )
        await self._conn.commit()

    async def _aput_postgres(
        self,
        thread_id: str,
        checkpoint_ns: str,
        cid: str,
        parent_id: str | None,
        cp_data: tuple[str, bytes],
        md_data: tuple[str, bytes],
        values: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> None:
        async with self._conn.acquire() as conn, conn.transaction():
            # Store channel blobs.
            for ch, ver in new_versions.items():
                blob = self.serde.dumps_typed(values[ch]) if ch in values else ("empty", b"")
                await conn.execute(
                    "INSERT INTO langgraph_blobs "
                    "(thread_id, checkpoint_ns, channel, version, type, data) "
                    "VALUES ($1, $2, $3, $4, $5, $6) "
                    "ON CONFLICT (thread_id, checkpoint_ns, channel, version) "
                    "DO UPDATE SET type = EXCLUDED.type, data = EXCLUDED.data",
                    thread_id,
                    checkpoint_ns,
                    ch,
                    str(ver),
                    blob[0],
                    blob[1],
                )

            # Store checkpoint.
            await conn.execute(
                "INSERT INTO langgraph_checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_id, "
                "data, metadata) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) "
                "DO UPDATE SET parent_id = EXCLUDED.parent_id, "
                "data = EXCLUDED.data, metadata = EXCLUDED.metadata",
                thread_id,
                checkpoint_ns,
                cid,
                parent_id,
                cp_data[1].decode(),
                md_data[1].decode(),
            )

    # ------------------------------------------------------------------
    # Async: aput_writes
    # ------------------------------------------------------------------

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:

        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id: str = config["configurable"]["checkpoint_id"]

        if self._backend == "sqlite":
            await self._aput_writes_sqlite(
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                writes,
                task_id,
                task_path,
            )
        else:
            await self._aput_writes_postgres(
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                writes,
                task_id,
                task_path,
            )

    async def _aput_writes_sqlite(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str,
    ) -> None:
        from langgraph.checkpoint.base import WRITES_IDX_MAP  # noqa: PLC0415

        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            data = self.serde.dumps_typed(value)
            await self._conn.execute(
                "INSERT OR REPLACE INTO langgraph_writes "
                "(thread_id, checkpoint_ns, checkpoint_id, task_id, "
                "task_path, write_idx, channel, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    task_id,
                    task_path,
                    write_idx,
                    channel,
                    data[1].decode(),
                ),
            )
        await self._conn.commit()

    async def _aput_writes_postgres(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str,
    ) -> None:
        from langgraph.checkpoint.base import WRITES_IDX_MAP  # noqa: PLC0415

        async with self._conn.acquire() as conn, conn.transaction():
            for idx, (channel, value) in enumerate(writes):
                write_idx = WRITES_IDX_MAP.get(channel, idx)
                data = self.serde.dumps_typed(value)
                await conn.execute(
                    "INSERT INTO langgraph_writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, "
                    "task_path, write_idx, channel, data) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, "
                    "task_id, write_idx) "
                    "DO UPDATE SET channel = EXCLUDED.channel, "
                    "data = EXCLUDED.data",
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    task_id,
                    task_path,
                    write_idx,
                    channel,
                    data[1].decode(),
                )

    # ------------------------------------------------------------------
    # Async: adelete_thread
    # ------------------------------------------------------------------

    async def adelete_thread(self, thread_id: str) -> None:
        if self._backend == "sqlite":
            for table in (
                "langgraph_checkpoints",
                "langgraph_writes",
                "langgraph_blobs",
            ):
                await self._conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ?",
                    (thread_id,),
                )
            await self._conn.commit()
        else:
            async with self._conn.acquire() as conn, conn.transaction():
                for table in (
                    "langgraph_checkpoints",
                    "langgraph_writes",
                    "langgraph_blobs",
                ):
                    await conn.execute(
                        f"DELETE FROM {table} WHERE thread_id = $1",
                        thread_id,
                    )
