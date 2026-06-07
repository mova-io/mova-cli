"""``mdk skills remote`` + ``mdk contexts remote`` CLI verbs (ADR 060 D3).

Drives the actual Typer commands end-to-end through a TestClient-backed
runtime: the CLI resolves a configured ``--target`` → ``MovateClient`` →
(monkeypatched ASGI transport) → the real runtime handlers → InMemoryStorage.
Mirrors ``tests/test_cli_workflow_signal.py``'s ``cli_env`` harness.

The commands call ``asyncio.run`` internally, so the tests are sync (a running
pytest-asyncio loop would otherwise clash) and seed via a one-shot
``asyncio.run``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.client import MovateClient
from movate.core.models import AgentBundleRecord
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
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="sc-cli", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    monkeypatch.setenv("MOVATE_TEST_KEY", minted.full_key)
    await storage.save_agent_bundle(
        AgentBundleRecord(
            name="faq-bot",
            tenant_id=tenant_id,
            version="v1",
            created_by="seed",
            content_hash="seed-hash",
            files={"agent.yaml": "name: faq-bot\nversion: v1\n"},
            created_at=datetime.now(UTC),
        )
    )

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


def _write_skill_dir(tmp_path: Path, version: str = "1.0.0") -> Path:
    d = tmp_path / "web-search"
    d.mkdir()
    (d / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: web-search\n"
        f"version: {version}\n"
        "description: Search the web.\n"
        "schema:\n"
        "  input:\n    query: string\n"
        "  output:\n    result: string\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: myproject.skills.search:run\n",
        encoding="utf-8",
    )
    return d


@pytest.mark.unit
def test_cli_skills_remote_create_list_get(cli_env, tmp_path: Path) -> None:
    skill_dir = _write_skill_dir(tmp_path)
    r = runner.invoke(cli_app, ["skills", "remote", "create", str(skill_dir), "-o", "json"])
    assert r.exit_code == 0, r.stdout + r.stderr

    rl = runner.invoke(cli_app, ["skills", "remote", "list", "-o", "json"])
    assert rl.exit_code == 0, rl.stdout + rl.stderr
    payload = json.loads(rl.stdout)
    assert payload["count"] == 1
    assert payload["skills"][0]["name"] == "web-search"

    rg = runner.invoke(cli_app, ["skills", "remote", "get", "web-search", "-o", "json"])
    assert rg.exit_code == 0
    assert json.loads(rg.stdout)["version"] == "1.0.0"


@pytest.mark.unit
def test_cli_skills_remote_attach(cli_env, tmp_path: Path) -> None:
    skill_dir = _write_skill_dir(tmp_path)
    runner.invoke(cli_app, ["skills", "remote", "create", str(skill_dir)])
    r = runner.invoke(cli_app, ["skills", "remote", "attach", "web-search", "--agent", "faq-bot"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert any(b.version == "v1+skill-web-search" for b in cli_env.storage.agent_bundles)


@pytest.mark.unit
def test_cli_contexts_remote_create_get_versions(cli_env, tmp_path: Path) -> None:
    md = tmp_path / "tone.md"
    md.write_text("# Tone\nBe concise.\n", encoding="utf-8")
    r = runner.invoke(
        cli_app,
        ["contexts", "remote", "create", "company-tone", "-f", str(md), "-o", "json"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["version"] == "v1"

    md2 = tmp_path / "tone2.md"
    md2.write_text("# Tone v2\n", encoding="utf-8")
    r2 = runner.invoke(
        cli_app,
        ["contexts", "remote", "update", "company-tone", "-f", str(md2), "--version", "v2"],
    )
    assert r2.exit_code == 0, r2.stdout + r2.stderr

    rv = runner.invoke(cli_app, ["contexts", "remote", "versions", "company-tone", "-o", "json"])
    assert rv.exit_code == 0
    assert [v["version"] for v in json.loads(rv.stdout)["versions"]] == ["v2", "v1"]


@pytest.mark.unit
def test_cli_contexts_remote_attach(cli_env, tmp_path: Path) -> None:
    md = tmp_path / "policy.md"
    md.write_text("# Policy\n", encoding="utf-8")
    runner.invoke(cli_app, ["contexts", "remote", "create", "policy", "-f", str(md)])
    r = runner.invoke(cli_app, ["contexts", "remote", "attach", "policy", "--agent", "faq-bot"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert any(b.version == "v1+context-policy" for b in cli_env.storage.agent_bundles)
