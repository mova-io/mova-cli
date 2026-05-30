"""Tests for the demo *scenario* (sample agents + workflow + knowledge graph)
and the ``mdk demo doctor`` readiness check.

Three layers:

1. **Pure scenario generator** (:mod:`movate.core.demo.scenario`) —
   determinism, the demo-tagging invariant, valid ``AgentSpec`` bundles, a
   voice-capable agent, a workflow bundle, and a connected (no-dangling-edge)
   graph.
2. **CLI seed → graph queryable** — a real SQLite ``mdk demo seed`` persists
   agents + workflow + graph that read back through the StorageProvider
   Protocol (``list_agents`` / ``list_entities`` / ``expand_neighbors``).
3. **demo doctor** — GO on a seeded env, NO-GO (exit 1) on an empty one.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.demo import (
    DEMO_GRAPH_AGENT,
    DEMO_MARKER_KEY,
    DEMO_PROJECT_ID,
    DEMO_TENANT_ID,
    generate_scenario,
)
from movate.core.demo.scenario import _demo_embedding
from movate.core.models import AgentSpec
from movate.storage.sqlite import SqliteProvider

# ---------------------------------------------------------------------------
# 1. Pure generator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scenario_is_deterministic() -> None:
    """Same generator call → byte-identical content hashes (reproducible demo)."""
    a = generate_scenario()
    b = generate_scenario()
    assert [e.entity_id for e in a.entities] == [e.entity_id for e in b.entities]
    assert [r.relation_id for r in a.relations] == [r.relation_id for r in b.relations]
    assert [ag.content_hash for ag in a.agents] == [ag.content_hash for ag in b.agents]


@pytest.mark.unit
def test_scenario_everything_demo_tagged() -> None:
    """Safety invariant: every record carries the demo tenant prefix."""
    sc = generate_scenario()
    for ag in sc.agents:
        assert ag.tenant_id == DEMO_TENANT_ID
        assert ag.tenant_id.startswith("demo-")
    for wf in sc.workflows:
        assert wf.tenant_id.startswith("demo-")
    for e in sc.entities:
        assert e.tenant_id.startswith("demo-")
        assert e.metadata and e.metadata.get(DEMO_MARKER_KEY) is True
    for r in sc.relations:
        assert r.tenant_id.startswith("demo-")


@pytest.mark.unit
def test_sample_agents_parse_as_agentspec() -> None:
    """Every seeded agent.yaml validates as AgentSpec (doctor relies on this)."""
    sc = generate_scenario()
    for ag in sc.agents:
        spec = AgentSpec.model_validate(yaml.safe_load(ag.files["agent.yaml"]))
        assert spec.name == ag.name


@pytest.mark.unit
def test_scenario_has_voice_and_workflow() -> None:
    sc = generate_scenario()
    voice_agents = [a for a in sc.agents if "voice:" in a.files["agent.yaml"]]
    assert len(voice_agents) == 1, "exactly one voice-capable sample agent expected"
    # The voice block parses + opts in.
    spec = AgentSpec.model_validate(yaml.safe_load(voice_agents[0].files["agent.yaml"]))
    assert spec.voice is not None and spec.voice.enabled is True
    assert spec.voice.stt and spec.voice.tts
    # A workflow bundle is present and published.
    assert sc.workflows and sc.workflows[0].published is True


@pytest.mark.unit
def test_graph_has_no_dangling_edges() -> None:
    """Every relation's endpoints exist as entities (storage won't auto-create)."""
    sc = generate_scenario()
    ids = {e.entity_id for e in sc.entities}
    assert len(sc.entities) >= 8
    assert len(sc.relations) >= 8
    for r in sc.relations:
        assert r.src_entity_id in ids
        assert r.dst_entity_id in ids


@pytest.mark.unit
def test_demo_embedding_is_unit_norm_and_stable() -> None:
    v1 = _demo_embedding("Pro Tier")
    v2 = _demo_embedding("Pro Tier")
    assert v1 == v2  # deterministic
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-9  # L2-normalized → cosine well-defined
    assert _demo_embedding("Pro Tier") != _demo_embedding("Free Tier")


# ---------------------------------------------------------------------------
# 2. CLI seed → graph + registry queryable through the Protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_full_seed_persists_scenario(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "scenario.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    res = runner.invoke(app, ["demo", "seed", "--agents", "3", "--tenants", "2", "--days", "3"])
    assert res.exit_code == 0, res.stdout + (res.stderr or "")

    async def _check() -> None:
        storage = SqliteProvider(db_path=db)
        await storage.init()
        try:
            agents = await storage.list_agents(tenant_id=DEMO_TENANT_ID, limit=20)
            assert {a.name for a in agents} >= {"support-triage", "voice-concierge"}
            ents = await storage.list_entities(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEMO_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            rels = await storage.list_relations(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEMO_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            assert len(ents) >= 8 and len(rels) >= 8
            # Drill-down: a node has neighbors.
            root = ents[0]
            sub = await storage.expand_neighbors(
                agent=DEMO_GRAPH_AGENT,
                tenant_id=DEMO_TENANT_ID,
                entity_ids=[root.entity_id],
                hops=2,
                limit=50,
                project_id=DEMO_PROJECT_ID,
            )
            assert len(sub.entities) >= 1
        finally:
            await storage.close()

    asyncio.run(_check())


@pytest.mark.unit
def test_telemetry_only_skips_scenario(tmp_path, monkeypatch) -> None:
    """--telemetry-only keeps the historical behavior: no agents/graph."""
    db = str(tmp_path / "telemetry-only.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    res = runner.invoke(
        app, ["demo", "seed", "--telemetry-only", "--agents", "2", "--tenants", "1", "--days", "2"]
    )
    assert res.exit_code == 0, res.stdout + (res.stderr or "")

    async def _check() -> None:
        storage = SqliteProvider(db_path=db)
        await storage.init()
        try:
            agents = await storage.list_agents(tenant_id=DEMO_TENANT_ID, limit=20)
            assert agents == []
            ents = await storage.list_entities(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEMO_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            assert ents == []
        finally:
            await storage.close()

    asyncio.run(_check())


@pytest.mark.unit
def test_clear_purges_scenario_tables(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "purge.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    assert runner.invoke(app, ["demo", "seed", "--days", "2"]).exit_code == 0
    assert runner.invoke(app, ["demo", "clear", "--yes"]).exit_code == 0

    async def _check() -> None:
        storage = SqliteProvider(db_path=db)
        await storage.init()
        try:
            agents = await storage.list_agents(tenant_id=DEMO_TENANT_ID, limit=20)
            ents = await storage.list_entities(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEMO_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            assert agents == [] and ents == []
        finally:
            await storage.close()

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# 3. demo doctor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doctor_go_on_seeded_env(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "doctor-go.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    assert runner.invoke(app, ["demo", "seed", "--days", "3"]).exit_code == 0

    res = runner.invoke(app, ["demo", "doctor"])
    assert res.exit_code == 0, res.stdout + (res.stderr or "")
    assert "Demo is GO" in res.stdout
    assert "hard_fail=0" in res.stdout


@pytest.mark.unit
def test_doctor_not_ready_on_empty_env(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "doctor-empty.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    res = runner.invoke(app, ["demo", "doctor"])
    assert res.exit_code == 1
    out = res.stdout + (res.stderr or "")
    assert "Demo NOT ready" in out
