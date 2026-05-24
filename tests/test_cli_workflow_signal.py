"""``mdk workflow runs`` + ``mdk workflow signal`` CLI (ADR 017 D5, PR 2).

Hermetic: a tmp config + a registered target + a monkeypatched MovateClient
routed through the FastAPI app via ASGITransport (no real socket). Mirrors the
``cli_env`` pattern in tests/test_client_and_remote_cli.py.

Asserts: ``runs --paused`` lists paused runs (showing the gate prompt);
``signal`` posts the decision and prints the continuation job id (and the
continuation job is enqueued with resume_workflow_run_id set).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.auth import mint_api_key
from movate.core.client import MovateClient
from movate.core.models import ApiKeyEnv, WorkflowRunRecord, WorkflowStatus
from movate.core.user_config import TargetConfig, UserConfig, save_user_config
from movate.runtime import build_app
from movate.testing import InMemoryStorage

runner = CliRunner(mix_stderr=False)


@dataclass
class _CliEnv:
    storage: InMemoryStorage
    tenant_id: str


@pytest.fixture
async def cli_env(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("MOVATE_TEST_KEY", "placeholder")

    storage = InMemoryStorage()
    await storage.init()
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="test")
    await storage.save_api_key(minted.record)
    monkeypatch.setenv("MOVATE_TEST_KEY", minted.full_key)

    save_user_config(
        UserConfig(
            targets={"test": TargetConfig(url="http://test", key_env="MOVATE_TEST_KEY")},
            active="test",
        )
    )

    transport = httpx.ASGITransport(app=build_app(storage))
    real_init = MovateClient.__init__

    def _patched_init(self, *, base_url, api_key, timeout=30.0, transport=None):
        real_init(
            self,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            transport=transport or globals()["_test_transport"],
        )

    globals()["_test_transport"] = transport
    monkeypatch.setattr(MovateClient, "__init__", _patched_init)
    return _CliEnv(storage=storage, tenant_id=tenant_id)


def _seed_paused_sync(storage: InMemoryStorage, tenant_id: str, rid: str = "wf-1") -> None:
    """Seed a PAUSED run via a one-shot asyncio.run so the test stays sync.

    Sync on purpose (see tests/test_client_and_remote_cli.py): ``runner.invoke``
    calls ``asyncio.run`` inside the typer command, which fails if a
    pytest-asyncio loop is already running for an ``async def`` test.
    """
    import asyncio  # noqa: PLC0415

    async def _seed() -> None:
        await storage.save_workflow_run(
            WorkflowRunRecord(
                workflow_run_id=rid,
                tenant_id=tenant_id,
                workflow="approval-flow",
                workflow_version="0.1.0",
                status=WorkflowStatus.PAUSED,
                initial_state={"text": "seed"},
                final_state={"text": "seed", "step1": "done"},
                paused_node_id="gate",
                paused_state={"text": "seed", "step1": "done"},
                human_task={"prompt": "Approve this refund?", "output_contract": ["decision"]},
            )
        )

    asyncio.run(_seed())


@pytest.mark.unit
def test_cli_workflow_runs_paused_lists_gate_prompt(cli_env) -> None:
    _seed_paused_sync(cli_env.storage, cli_env.tenant_id)

    result = runner.invoke(cli_app, ["workflow", "runs", "--paused", "-o", "json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    row = payload["workflow_runs"][0]
    assert row["status"] == "paused"
    assert row["human_task"]["prompt"] == "Approve this refund?"


@pytest.mark.unit
def test_cli_workflow_signal_prints_continuation_job(cli_env) -> None:
    import asyncio  # noqa: PLC0415

    _seed_paused_sync(cli_env.storage, cli_env.tenant_id)

    result = runner.invoke(
        cli_app,
        ["workflow", "signal", "wf-1", "--decision", '{"decision": "approve"}', "-o", "json"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "queued"
    job_id = payload["job_id"]

    # The continuation job was enqueued carrying the resume id.
    async def _get():
        return await cli_env.storage.get_job(job_id, tenant_id=cli_env.tenant_id)

    job = asyncio.run(_get())
    assert job is not None
    assert job.resume_workflow_run_id == "wf-1"


@pytest.mark.unit
def test_cli_workflow_signal_bad_decision_json_exits_2(cli_env) -> None:
    """A non-JSON --decision is a clean exit-2 (no traceback)."""
    result = runner.invoke(cli_app, ["workflow", "signal", "wf-1", "--decision", "not-json"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_workflow_signal_unknown_run_nonzero(cli_env) -> None:
    """A 404 from the runtime surfaces as a non-zero exit (4xx → exit 4)."""
    result = runner.invoke(
        cli_app,
        ["workflow", "signal", "ghost", "--decision", '{"decision": "approve"}'],
    )
    assert result.exit_code != 0
