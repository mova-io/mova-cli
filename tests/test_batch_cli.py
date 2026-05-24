"""``mdk batch`` CLI — request/render helpers + client round-trip.

Two layers:

* **Client round-trip** drives the real ``MovateClient`` batch methods
  (``submit_batch`` / ``get_batch`` / ``list_batches`` / ``wait_for_batch``)
  through a TestClient-backed runtime via ``httpx.ASGITransport`` — same wire
  path the CLI uses, no socket. This exercises the exact request shapes the
  ``mdk batch submit/status/list`` commands send.
* **Render + parse helpers** unit-test the CLI's pure functions
  (``_read_jsonl`` dataset parsing, ``_emit_status`` / ``_emit_list``
  rendering) without needing a runtime.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from movate.cli._output import TableJson
from movate.cli.batch_cmd import _emit_list, _emit_status, _read_jsonl
from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.client import MovateClient
from movate.core.models import JobStatus
from movate.runtime import build_app
from movate.runtime.schemas import (
    BatchListItemView,
    BatchListView,
    BatchStatusCounts,
    BatchStatusView,
)
from movate.testing import InMemoryStorage

_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: cli-batch-demo
version: 0.1.0
description: demo
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""
_PROMPT = b"Hi {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()
_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
).encode()


@pytest.fixture
async def runtime(tmp_path: Path):
    """(storage, app, full_key, tenant_id) with one registered agent."""
    storage = InMemoryStorage()
    await storage.init()
    agents_path = tmp_path / "agents"
    agents_path.mkdir()
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="cli-batch", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    app = build_app(storage, agents_path=agents_path)

    # Register the agent through the runtime so resolve_agent_bundle finds it.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        base_url="http://test",
        transport=transport,
        headers={"Authorization": f"Bearer {minted.full_key}"},
    ) as raw:
        r = await raw.post(
            "/api/v1/agents",
            files=[
                ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
                ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
                ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
                ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
            ],
        )
        assert r.status_code == 201, r.text
    return storage, app, minted.full_key, tenant_id


def _client_for(app, key: str) -> MovateClient:
    return MovateClient(base_url="http://test", api_key=key, transport=httpx.ASGITransport(app=app))


# ---------------------------------------------------------------------------
# Client round-trip (the CLI's request path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cli_submit_then_status_then_list(runtime) -> None:
    storage, app, key, tenant_id = runtime
    async with _client_for(app, key) as client:
        accepted = await client.submit_batch(
            agent="cli-batch-demo", rows=[{"text": "a"}, {"text": "b"}, {"text": "c"}]
        )
        assert accepted.total == 3
        assert accepted.status == "queued"

        # status — all queued initially.
        status = await client.get_batch(accepted.batch_id)
        assert status.total == 3
        assert status.counts.queued == 3
        assert status.state == "running"

        # list — the batch shows up.
        listing = await client.list_batches()
        assert accepted.batch_id in {b.batch_id for b in listing.batches}

    # Flip every child terminal → status becomes complete.
    children = await storage.list_jobs(tenant_id=tenant_id, batch_id=accepted.batch_id, limit=100)
    for c in children:
        await storage.update_job(c.job_id, tenant_id=tenant_id, status=JobStatus.SUCCESS)
    async with _client_for(app, key) as client:
        final = await client.wait_for_batch(accepted.batch_id, max_wait_seconds=1.0)
        assert final.state == "complete"
        assert final.counts.success == 3


# ---------------------------------------------------------------------------
# Dataset parse helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_jsonl_parses_rows(tmp_path: Path) -> None:
    p = tmp_path / "d.jsonl"
    p.write_text('{"text": "a"}\n\n{"text": "b"}\n')
    rows = _read_jsonl(p)
    assert rows == [{"text": "a"}, {"text": "b"}]


@pytest.mark.unit
def test_read_jsonl_rejects_non_object(tmp_path: Path) -> None:
    p = tmp_path / "d.jsonl"
    p.write_text('{"text": "a"}\n[1,2]\n')
    with pytest.raises(ValueError, match="line 2 must be a JSON object"):
        _read_jsonl(p)


@pytest.mark.unit
def test_read_jsonl_rejects_malformed(tmp_path: Path) -> None:
    p = tmp_path / "d.jsonl"
    p.write_text("not json\n")
    with pytest.raises(ValueError, match="line 1 is not valid JSON"):
        _read_jsonl(p)


# ---------------------------------------------------------------------------
# Render helpers — table + json
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emit_status_table(capsys) -> None:
    view = BatchStatusView(
        batch_id="b1",
        agent="cli-batch-demo",
        total=3,
        counts=BatchStatusCounts(queued=1, success=2),
        state="running",
        created_at=datetime.now(UTC),
        job_ids=["j1", "j2", "j3"],
    )
    _emit_status(view, output_format=TableJson.TABLE)
    out = capsys.readouterr().out
    assert "b1" in out
    assert "running" in out


@pytest.mark.unit
def test_emit_status_json(capsys) -> None:
    view = BatchStatusView(
        batch_id="b1",
        agent="cli-batch-demo",
        total=2,
        counts=BatchStatusCounts(success=2),
        state="complete",
        created_at=datetime.now(UTC),
        job_ids=["j1", "j2"],
    )
    _emit_status(view, output_format=TableJson.JSON)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["batch_id"] == "b1"
    assert parsed["state"] == "complete"
    assert parsed["counts"]["success"] == 2


@pytest.mark.unit
def test_emit_list_empty(capsys) -> None:
    _emit_list(BatchListView(batches=[], count=0), output_format=TableJson.TABLE)
    assert "no batches" in capsys.readouterr().out


@pytest.mark.unit
def test_emit_list_table(capsys) -> None:
    view = BatchListView(
        batches=[
            BatchListItemView(
                batch_id="b1",
                agent="cli-batch-demo",
                total=3,
                created_at=datetime.now(UTC),
            )
        ],
        count=1,
    )
    _emit_list(view, output_format=TableJson.TABLE)
    out = capsys.readouterr().out
    assert "b1" in out
    assert "cli-batch-demo" in out
