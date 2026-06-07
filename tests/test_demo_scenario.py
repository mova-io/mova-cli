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

from movate.cli.dev_key import DEV_TENANT_ID
from movate.cli.main import app
from movate.core.auth import TENANT_PREFIX_LEN, mint_api_key, parse_api_key
from movate.core.demo import (
    DEMO_GRAPH_AGENT,
    DEMO_MARKER_KEY,
    DEMO_PROJECT_ID,
    DEMO_TENANT_ID,
    generate_scenario,
)
from movate.core.demo.scenario import _demo_embedding
from movate.core.models import AgentSpec, ApiKeyEnv
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
def test_scenario_everything_under_scenario_tenant() -> None:
    """Safety invariant: every scenario record carries the single scenario tenant.

    The scenario tenant is the dash-free serve --dev tenant (NOT a ``demo-``
    prefix) so the live browser graph viewer — which authenticates as that
    tenant — can see the agents + graph. The demo-marker sentinel in metadata
    is what keeps the rows unambiguously synthetic.
    """
    sc = generate_scenario()
    for ag in sc.agents:
        assert ag.tenant_id == DEMO_TENANT_ID
    for wf in sc.workflows:
        assert wf.tenant_id == DEMO_TENANT_ID
    for e in sc.entities:
        assert e.tenant_id == DEMO_TENANT_ID
        assert e.metadata and e.metadata.get(DEMO_MARKER_KEY) is True
    for r in sc.relations:
        assert r.tenant_id == DEMO_TENANT_ID


@pytest.mark.unit
def test_scenario_tenant_is_dash_free_serve_dev_tenant() -> None:
    """The scenario tenant MUST equal serve --dev's tenant and be a valid API key prefix.

    Gap-1 regression guard. ``mdk serve --dev`` authenticates the live viewer as
    ``DEV_TENANT_ID``; if the scenario seeds under a different tenant the viewer
    queries the wrong scope and renders empty. The tenant must also be a *valid*
    API key tenant-prefix: dash-free and ≥ TENANT_PREFIX_LEN chars, since a
    ``demo-`` prefix (``demo-acm``) breaks the ``[a-zA-Z0-9]{8}`` key regex → 401.
    """
    # Aligned with the serve --dev tenant the browser viewer authenticates as.
    assert DEMO_TENANT_ID == DEV_TENANT_ID
    # Dash-free + long enough → a usable, parseable API key tenant-prefix.
    assert "-" not in DEMO_TENANT_ID
    assert len(DEMO_TENANT_ID) >= TENANT_PREFIX_LEN
    minted = mint_api_key(tenant_id=DEMO_TENANT_ID, env=ApiKeyEnv.TEST)
    parsed = parse_api_key(minted.full_key)  # must not raise (would be a 401)
    assert parsed.tenant_prefix == DEMO_TENANT_ID[:TENANT_PREFIX_LEN]


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


# ---------------------------------------------------------------------------
# 4. Demo-prep correctness — gaps fixed in fix/demo-tenant-and-purge
# ---------------------------------------------------------------------------


async def _count_rows(db: str, table: str, where: str = "") -> int:
    storage = SqliteProvider(db_path=db)
    await storage.init()
    try:
        conn = storage._conn
        assert conn is not None
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table} {where}")
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        await storage.close()


@pytest.mark.unit
def test_seed_writes_scenario_under_serve_dev_tenant(tmp_path, monkeypatch) -> None:
    """Gap 1: agents + graph land under the serve --dev tenant (dash-free), not demo-acme."""
    db = str(tmp_path / "dev-tenant.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    assert runner.invoke(app, ["demo", "seed", "--days", "2"]).exit_code == 0

    async def _check() -> None:
        storage = SqliteProvider(db_path=db)
        await storage.init()
        try:
            # The read path the browser viewer uses: scoped to the serve --dev
            # tenant. It must find the agents + a non-trivial graph.
            agents = await storage.list_agents(tenant_id=DEV_TENANT_ID, limit=20)
            assert {a.name for a in agents} >= {"support-triage", "voice-concierge"}
            ents = await storage.list_entities(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEV_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            rels = await storage.list_relations(
                agent=DEMO_GRAPH_AGENT, tenant_id=DEV_TENANT_ID, project_id=DEMO_PROJECT_ID
            )
            assert len(ents) >= 8 and len(rels) >= 8
            # And nothing scenario-shaped leaked under the old demo-acme tenant.
            stale = await storage.list_entities(
                agent=DEMO_GRAPH_AGENT, tenant_id="demo-acme", project_id=DEMO_PROJECT_ID
            )
            assert stale == []
        finally:
            await storage.close()

    asyncio.run(_check())


@pytest.mark.unit
def test_reseed_then_clear_leaves_zero_orphan_insights(tmp_path, monkeypatch) -> None:
    """Gap 2: insights don't accumulate across re-seeds, and clear purges them all."""
    db = str(tmp_path / "insights.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)

    assert runner.invoke(app, ["demo", "seed", "--days", "3"]).exit_code == 0
    first = asyncio.run(_count_rows(db, "observability_insights"))
    assert first > 0, "analyzer should have written insights on the first seed"

    # Re-seed WITH --clear-first: insights must not grow unbounded (no orphans).
    assert runner.invoke(app, ["demo", "seed", "--days", "3", "--clear-first"]).exit_code == 0
    second = asyncio.run(_count_rows(db, "observability_insights"))
    assert second == first, f"re-seed left orphan insights: {first} -> {second}"

    # clear purges every insight row.
    assert runner.invoke(app, ["demo", "clear", "--yes"]).exit_code == 0
    assert asyncio.run(_count_rows(db, "observability_insights")) == 0


@pytest.mark.unit
def test_clear_purges_scenario_tenant_rows(tmp_path, monkeypatch) -> None:
    """Gap 1/2: clear purges the scenario rows under the dash-free serve --dev tenant."""
    db = str(tmp_path / "purge-scenario.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    assert runner.invoke(app, ["demo", "seed", "--days", "2"]).exit_code == 0
    # Pre-clear: scenario rows exist under the exact scenario tenant.
    where = f"WHERE tenant_id = '{DEMO_TENANT_ID}'"
    assert asyncio.run(_count_rows(db, "agent_bundles", where)) > 0
    assert asyncio.run(_count_rows(db, "kb_entities", where)) > 0

    assert runner.invoke(app, ["demo", "clear", "--yes"]).exit_code == 0
    for table in ("agent_bundles", "workflow_bundles", "kb_entities", "kb_relations"):
        assert asyncio.run(_count_rows(db, table, where)) == 0, f"{table} not purged"


@pytest.mark.unit
def test_with_voice_summary_does_not_claim_persisted_turns(tmp_path, monkeypatch) -> None:
    """Gap 3: --with-voice summary marks the count as generated-not-stored.

    Voice turns aren't persisted yet (no voice_turns table), so the summary must
    not print a bare "N voice turns" that maps to no queryable data.
    """
    db = str(tmp_path / "voice.db")
    monkeypatch.setenv("MOVATE_DB", db)
    if "MOVATE_DB_URL" in os.environ:
        monkeypatch.delenv("MOVATE_DB_URL")
    runner = CliRunner(mix_stderr=False)
    res = runner.invoke(app, ["demo", "seed", "--days", "2", "--with-voice"])
    assert res.exit_code == 0, res.stdout + (res.stderr or "")
    # The row is present but explicitly flagged as not stored.
    assert "not stored" in res.stdout
    # No voice_turns table is created by the seed (nothing persisted).
    exists = asyncio.run(
        _count_rows(
            db,
            "sqlite_master",
            "WHERE type='table' AND name='voice_turns'",
        )
    )
    assert exists == 0
