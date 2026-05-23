"""CLI — ``mdk agent history`` + ``mdk agent revert`` (ADR 014 D3).

Exercises the local-via-storage path (the same pattern as ``mdk
tenants``): a per-test sqlite file via ``MOVATE_DB``, seeded through the
storage layer, then driven through the Typer CLI.

* ``mdk agent history`` lists the versions newest-first, marks the
  current one, and shows ``created_by``.
* ``mdk agent revert --to`` re-publishes a prior version forward as the
  new latest, non-destructively (the originals survive), and confirms
  via the prompt (bypassed with ``--yes``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.models import AgentBundleRecord
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)

_TENANT = "local"  # the CLI default tenant


@pytest.fixture
def cli_db(tmp_path: Path, monkeypatch) -> Path:
    """Point the CLI's ``build_storage()`` at a per-test sqlite file."""
    db = tmp_path / "movate.db"
    monkeypatch.setenv("MOVATE_DB", str(db))
    return db


def _seed(db: Path, *, name: str, version: str, created_by: str, hours_ago: int) -> None:
    """Append one published version to the sqlite registry."""

    async def _go() -> None:
        storage = build_storage()
        await storage.init()
        try:
            rec = AgentBundleRecord(
                name=name,
                tenant_id=_TENANT,
                version=version,
                created_by=created_by,
                content_hash=f"hash-{version}",
                files={"agent.yaml": f"name: {name}\nversion: {version}\n"},
                created_at=datetime.now(UTC) - timedelta(hours=hours_ago),
            )
            await storage.save_agent_bundle(rec)
        finally:
            await storage.close()

    asyncio.run(_go())


def _versions(db: Path, name: str) -> list[str]:
    async def _go() -> list[str]:
        storage = build_storage()
        await storage.init()
        try:
            rows = await storage.list_agent_versions(name, tenant_id=_TENANT, limit=100)
            return [r.version for r in rows]
        finally:
            await storage.close()

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_history_lists_versions_newest_first(cli_db: Path) -> None:
    _seed(cli_db, name="faq", version="0.1.0", created_by="alice", hours_ago=2)
    _seed(cli_db, name="faq", version="0.2.0", created_by="bob", hours_ago=1)

    r = runner.invoke(cli_app, ["agent", "history", "faq"])
    assert r.exit_code == 0, r.stdout + r.stderr
    out = r.stdout
    assert "0.1.0" in out
    assert "0.2.0" in out
    # Newest (0.2.0) appears before 0.1.0 in the table.
    assert out.index("0.2.0") < out.index("0.1.0")
    # created_by audit surfaced.
    assert "alice" in out
    assert "bob" in out


@pytest.mark.unit
def test_history_json(cli_db: Path) -> None:
    _seed(cli_db, name="faq", version="0.1.0", created_by="alice", hours_ago=2)
    _seed(cli_db, name="faq", version="0.2.0", created_by="bob", hours_ago=1)

    r = runner.invoke(cli_app, ["agent", "history", "faq", "--json"])
    assert r.exit_code == 0, r.stdout + r.stderr

    body = json.loads(r.stdout)
    assert body["count"] == 2
    assert [v["version"] for v in body["versions"]] == ["0.2.0", "0.1.0"]
    assert body["versions"][0]["is_current"] is True


@pytest.mark.unit
def test_history_empty(cli_db: Path) -> None:
    r = runner.invoke(cli_app, ["agent", "history", "nope"])
    assert r.exit_code == 0, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_revert_republishes_non_destructively(cli_db: Path) -> None:
    _seed(cli_db, name="faq", version="0.1.0", created_by="alice", hours_ago=2)
    _seed(cli_db, name="faq", version="0.2.0", created_by="bob", hours_ago=1)

    r = runner.invoke(cli_app, ["agent", "revert", "faq", "--to", "0.1.0", "--yes"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "reverted" in r.stderr.lower()

    versions = _versions(cli_db, "faq")
    # Originals intact + a new revert row appended (non-destructive).
    assert "0.1.0" in versions
    assert "0.2.0" in versions
    assert len(versions) == 3


@pytest.mark.unit
def test_revert_unknown_version_errors(cli_db: Path) -> None:
    _seed(cli_db, name="faq", version="0.1.0", created_by="alice", hours_ago=1)
    r = runner.invoke(cli_app, ["agent", "revert", "faq", "--to", "9.9.9", "--yes"])
    assert r.exit_code != 0
    assert "9.9.9" in r.stderr
