"""Runtime resolve-from-registry (ADR 014 step 2) — closes #109.

Covers the resolver, the publish dual-write, the one-time filesystem
import, and — the headline — that an agent published via
``POST /api/v1/agents`` is runnable by the **worker dispatch** from a
fresh worker that has NO filesystem ``agents_path`` for that agent. That
last test is the #109 regression: before this change an agent created on
the API pod was invisible to the worker pod; now both resolve through the
shared durable registry.

Requires the runtime extras (fastapi) — skipped automatically where only
the core package is installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")  # skip whole module if runtime extras absent

from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import AgentBundleRecord, JobKind, JobRecord, JobStatus
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.runtime import build_app
from movate.runtime.agent_resolver import (
    bundle_files_from_dir,
    content_hash,
    import_filesystem_agents,
    materialize_bundle,
    resolve_agent_bundle,
)
from movate.runtime.dispatch import WorkerDispatch
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

# ---------------------------------------------------------------------------
# Canonical bundle bytes (path-form schemas), shared by the API tests.
# ---------------------------------------------------------------------------

_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: registry-bot
version: 0.1.0
description: demo for registry-resolve tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""
_PROMPT = b"Reply to {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()
_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
).encode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="resolve-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id


def _publish_agent(client: TestClient, headers: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=headers,
    )
    assert r.status_code == 201, r.text


def _make_record(
    *,
    name: str = "regbot",
    tenant_id: str = "tenant-a",
    version: str = "0.1.0",
    files: dict[str, str] | None = None,
) -> AgentBundleRecord:
    files = files or {
        "agent.yaml": (
            "api_version: movate/v1\n"
            "kind: Agent\n"
            f"name: {name}\n"
            f"version: {version}\n"
            "model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n  input:\n    text: string\n  output:\n    message: string\n"
        ),
        "prompt.md": "Reply to {{ input.text }}\n",
    }
    return AgentBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by="tester",
        content_hash=content_hash(files),
        files=files,
    )


# ---------------------------------------------------------------------------
# Resolver unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resolve_registry_hit_materializes_and_loads(
    storage: InMemoryStorage,
) -> None:
    """Registry hit → materialize the files → load_agent → AgentBundle."""
    record = _make_record(name="regbot", tenant_id="t1")
    await storage.save_agent_bundle(record)

    bundle = await resolve_agent_bundle(storage, "regbot", tenant_id="t1")

    assert bundle is not None
    assert bundle.spec.name == "regbot"
    assert bundle.spec.version == "0.1.0"
    # The agent dir is the per-pod materialization cache, not the repo.
    assert "mdk-agents" in str(bundle.agent_dir)
    assert (bundle.agent_dir / "agent.yaml").is_file()


@pytest.mark.unit
async def test_resolve_version_keyed_cache_is_reused(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second resolve of the same (tenant, name, version) reuses the
    materialized dir — it does NOT re-write the files."""
    record = _make_record(name="cachebot", tenant_id="t1", version="0.1.0")
    await storage.save_agent_bundle(record)

    first = await resolve_agent_bundle(storage, "cachebot", tenant_id="t1")
    assert first is not None
    cache_dir = first.agent_dir

    # If a second resolve re-materialized, _write_files would run again.
    # Spy on it: a cache hit must NOT call it.
    import movate.runtime.agent_resolver as resolver_mod  # noqa: PLC0415

    calls = {"n": 0}
    original = resolver_mod._write_files

    def _spy(root: Path, files: dict[str, str]) -> None:
        calls["n"] += 1
        original(root, files)

    monkeypatch.setattr(resolver_mod, "_write_files", _spy)

    second = await resolve_agent_bundle(storage, "cachebot", tenant_id="t1")
    assert second is not None
    assert second.agent_dir == cache_dir
    assert calls["n"] == 0  # warm cache: no re-materialization


@pytest.mark.unit
async def test_resolve_registry_miss_falls_back_to_filesystem(
    storage: InMemoryStorage, tmp_path: Path
) -> None:
    """Empty registry → resolver returns the filesystem fallback bundle
    byte-for-byte (this is what keeps `mdk serve --agents` working)."""
    agent_dir = scaffold_agent(tmp_path / "fsbot", name="fsbot")
    fs_bundle = load_agent(agent_dir)

    # Registry has nothing for this tenant → fallback wins.
    resolved = await resolve_agent_bundle(storage, "fsbot", tenant_id="t1", fallback=[fs_bundle])
    assert resolved is fs_bundle


@pytest.mark.unit
async def test_resolve_tenant_isolation(storage: InMemoryStorage) -> None:
    """A bundle published under tenant-a is NOT resolvable for tenant-b
    (no cross-tenant leak); falls through to the (empty) fallback → None."""
    await storage.save_agent_bundle(_make_record(name="secret", tenant_id="tenant-a"))

    same_tenant = await resolve_agent_bundle(storage, "secret", tenant_id="tenant-a")
    assert same_tenant is not None

    other_tenant = await resolve_agent_bundle(storage, "secret", tenant_id="tenant-b")
    assert other_tenant is None


@pytest.mark.unit
async def test_resolve_registry_preferred_over_filesystem(
    storage: InMemoryStorage, tmp_path: Path
) -> None:
    """When BOTH the registry and the fallback have the agent, the
    durable registry wins (it's the source of truth for deployed pods)."""
    agent_dir = scaffold_agent(tmp_path / "dupbot", name="dupbot")
    fs_bundle = load_agent(agent_dir)
    # Registry copy at a DIFFERENT version so we can tell which one resolved.
    await storage.save_agent_bundle(_make_record(name="dupbot", tenant_id="t1", version="9.9.9"))

    resolved = await resolve_agent_bundle(storage, "dupbot", tenant_id="t1", fallback=[fs_bundle])
    assert resolved is not None
    assert resolved.spec.version == "9.9.9"  # registry, not the FS 0.1.0


@pytest.mark.unit
async def test_materialize_then_loadable() -> None:
    """materialize_bundle writes the files and load_agent succeeds on the
    resulting dir (the resolver's two private steps, asserted directly)."""
    record = _make_record(name="matbot", tenant_id="t1", version="0.2.0")
    agent_dir = materialize_bundle(record)
    bundle = load_agent(agent_dir)
    assert bundle.spec.name == "matbot"


# ---------------------------------------------------------------------------
# Dual-write — POST/PUT /agents also writes to the durable registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_post_agents_dual_writes_to_registry(
    storage: InMemoryStorage, auth_setup, tmp_path: Path
) -> None:
    """POST /api/v1/agents lands in the durable registry (tenant-scoped),
    in addition to the filesystem persist."""
    headers, tenant_id = auth_setup
    agents_path = tmp_path / "agents"
    agents_path.mkdir()
    app = build_app(storage, agents_path=agents_path, rate_limit_per_minute=None)
    client = TestClient(app)

    _publish_agent(client, headers)

    # Filesystem copy still exists (back-compat).
    assert (agents_path / "registry-bot" / "agent.yaml").is_file()

    # AND the durable registry row exists, scoped to the caller's tenant.
    record = await storage.get_agent_bundle("registry-bot", tenant_id=tenant_id)
    assert record is not None
    assert record.version == "0.1.0"
    assert "agent.yaml" in record.files
    assert "prompt.md" in record.files
    # Not visible to a different tenant.
    assert await storage.get_agent_bundle("registry-bot", tenant_id="other") is None


@pytest.mark.unit
async def test_put_agents_dual_writes_new_version(
    storage: InMemoryStorage, auth_setup, tmp_path: Path
) -> None:
    """PUT /api/v1/agents/{name} writes the updated bundle into the
    registry as a new version (history)."""
    headers, tenant_id = auth_setup
    agents_path = tmp_path / "agents"
    agents_path.mkdir()
    app = build_app(storage, agents_path=agents_path, rate_limit_per_minute=None)
    client = TestClient(app)

    _publish_agent(client, headers)

    bumped = _AGENT_YAML.replace(b"version: 0.1.0", b"version: 0.2.0")
    r = client.put(
        "/api/v1/agents/registry-bot",
        files=[
            ("agent_yaml", ("agent.yaml", bumped, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=headers,
    )
    assert r.status_code == 200, r.text

    versions = await storage.list_agent_versions("registry-bot", tenant_id=tenant_id)
    seen = {v.version for v in versions}
    assert {"0.1.0", "0.2.0"} <= seen
    latest = await storage.get_agent_bundle("registry-bot", tenant_id=tenant_id)
    assert latest is not None and latest.version == "0.2.0"


# ---------------------------------------------------------------------------
# One-time filesystem → registry import
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_import_filesystem_agents_is_idempotent(
    storage: InMemoryStorage, tmp_path: Path
) -> None:
    """Filesystem agents seed into the registry; a re-run imports nothing
    (idempotent on (name, version))."""
    a1 = load_agent(scaffold_agent(tmp_path / "one", name="one"))
    a2 = load_agent(scaffold_agent(tmp_path / "two", name="two"))

    imported = await import_filesystem_agents(storage, [a1, a2], tenant_id="t1", created_by="seed")
    assert imported == 2
    assert await storage.get_agent_bundle("one", tenant_id="t1") is not None
    assert await storage.get_agent_bundle("two", tenant_id="t1") is not None

    # Second run is a no-op (rows already exist).
    again = await import_filesystem_agents(storage, [a1, a2], tenant_id="t1")
    assert again == 0


@pytest.mark.unit
async def test_startup_seeds_filesystem_agents_when_import_tenant_set(
    storage: InMemoryStorage, tmp_path: Path
) -> None:
    """build_app(import_tenant_id=...) seeds FS agents into the registry
    on lifespan startup."""
    agents = [load_agent(scaffold_agent(tmp_path / "seed", name="seed"))]
    app = build_app(
        storage,
        agents=agents,
        import_tenant_id="deploy-tenant",
        rate_limit_per_minute=None,
    )
    # Entering the TestClient context manager runs the lifespan startup.
    with TestClient(app):
        pass

    seeded = await storage.get_agent_bundle("seed", tenant_id="deploy-tenant")
    assert seeded is not None
    assert seeded.version == "0.1.0"


@pytest.mark.unit
async def test_startup_no_import_without_tenant(storage: InMemoryStorage, tmp_path: Path) -> None:
    """Default (no import tenant) → startup imports nothing; the resolver
    fallback alone serves FS agents (local `mdk serve` behavior)."""
    agents = [load_agent(scaffold_agent(tmp_path / "noseed", name="noseed"))]
    app = build_app(storage, agents=agents, rate_limit_per_minute=None)
    with TestClient(app):
        pass

    # No registry rows were written (the FS fallback covers resolution).
    assert storage.agent_bundles == []


@pytest.mark.unit
async def test_bundle_files_from_dir_roundtrips(tmp_path: Path) -> None:
    """The dir → files reader captures the agent's text files; a record
    built from them materializes back to a loadable agent."""
    agent_dir = scaffold_agent(tmp_path / "rt", name="rt")
    files = bundle_files_from_dir(agent_dir)
    assert "agent.yaml" in files
    assert "prompt.md" in files

    record = _make_record(name="rt", tenant_id="t1", files=files)
    materialized = materialize_bundle(record)
    assert load_agent(materialized).spec.name == "rt"


# ---------------------------------------------------------------------------
# THE #109 regression test — worker runs an API-published agent
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_worker_runs_agent_published_via_api_from_registry(
    storage: InMemoryStorage, auth_setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#109 regression (the headline).

    Publish an agent via ``POST /api/v1/agents`` (it lands in the durable
    registry, dual-written alongside the API pod's filesystem). Then drive
    the WORKER dispatch path with a fresh worker that has NO filesystem
    ``agents_path`` for that agent (``agents=[]``) — only the shared
    storage. The worker must resolve the agent from the registry and run
    it. Before ADR 014 step 2 this returned ``unknown_agent``; now it runs.
    """
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "from the worker"}')
    headers, tenant_id = auth_setup

    # --- API pod: publish the agent (its agents_path is API-pod-local) ---
    api_agents_path = tmp_path / "api-pod-agents"
    api_agents_path.mkdir()
    api_app = build_app(storage, agents_path=api_agents_path, rate_limit_per_minute=None)
    api_client = TestClient(api_app)
    _publish_agent(api_client, headers)

    # Confirm the durable row exists for the caller's tenant.
    assert await storage.get_agent_bundle("registry-bot", tenant_id=tenant_id) is not None

    # --- Worker pod: a FRESH dispatch with NO filesystem agents at all ---
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="worker-default",
    )
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[])

    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,  # the tenant the API published under
        kind=JobKind.AGENT,
        target="registry-bot",
        status=JobStatus.QUEUED,
        input={"text": "hello"},
    )
    await storage.save_job(job)

    outcome = await dispatch.execute_job(job)

    # Resolved from the registry + ran — the #109 fix.
    assert outcome.status == JobStatus.SUCCESS, outcome.error
    assert outcome.result_run_id is not None
    assert outcome.error is None
    # The produced RunRecord is stored under the JOB's tenant (so the
    # caller's GET /runs/<id> finds it) and ran the published agent.
    run = await storage.get_run(outcome.result_run_id, tenant_id=tenant_id)
    assert run is not None
    assert run.agent == "registry-bot"


@pytest.mark.unit
async def test_worker_unknown_agent_when_neither_registry_nor_fs(
    storage: InMemoryStorage,
) -> None:
    """A worker with no FS agents AND no registry row → terminal
    unknown_agent ERROR (the genuine not-published case)."""
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="worker-default",
    )
    dispatch = WorkerDispatch(storage=storage, executor=executor, agents=[])
    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id="t1",
        kind=JobKind.AGENT,
        target="ghost",
        status=JobStatus.QUEUED,
        input={"text": "hi"},
    )
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.ERROR
    assert outcome.error is not None
    assert outcome.error["type"] == "unknown_agent"
