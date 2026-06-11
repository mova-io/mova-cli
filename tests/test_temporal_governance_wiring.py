"""The Temporal backend threads project policy into its per-activity Executor
(task #44 — governance was dormant on the durable path).

Before this fix, ``_executor_for`` built the Executor with no
policy/runtime_policy/skill_policy, so the ADR 093 governance shadow (and the
executor's policy enforcement) were silently inactive on Temporal — only the
local/native path (via ``build_local_runtime``) wired them. These tests pin the
wiring at the seam: a non-permissive policy ⇒ the durable-path executor's
governance engine is live; a permissive one ⇒ it stays a no-op (zero regression).

No Temporal SDK needed — ``temporal_activities`` is import-isolated, so this is a
plain unit test of ``configure_activities`` → ``_executor_for``.
"""

from __future__ import annotations

import pytest

from movate.core.config import ModelPolicy, RuntimePolicy, SkillPolicy
from movate.core.workflow.temporal_activities import (
    _executor_for,
    _get_context,
    call_human_activity,
    configure_activities,
    persist_workflow_result_activity,
)
from movate.governance import consume_run_effect, peek_run_effect, record_run_effect
from movate.governance.effects import RUN_EFFECT_STATE_KEY
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer


async def _ctx_executor(**policy_kwargs: object):
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=load_pricing(),
        tracer=NullTracer(),
        provider=MockProvider(),
        tenant_id="local",
        **policy_kwargs,  # type: ignore[arg-type]
    )
    return _executor_for(_get_context(), {"tenant_id": "local"})


@pytest.mark.unit
async def test_temporal_executor_governance_active_with_policy() -> None:
    # A non-permissive ModelPolicy must reach the durable-path Executor and
    # activate the ADR 093 governance shadow — the wiring that was missing.
    ex = await _ctx_executor(
        policy=ModelPolicy(allowed_providers=["azure"]),
        runtime_policy=RuntimePolicy(),
        skill_policy=SkillPolicy(),
    )
    assert ex._governance is not None
    # The threaded policy is the one the activity context carries.
    assert ex._policy.allowed_providers == ["azure"]


@pytest.mark.unit
async def test_temporal_executor_permissive_when_no_policy() -> None:
    # Explicit permissive policies ⇒ the shadow stays a no-op — byte-for-byte
    # the prior (and deployed-without-config) behavior.
    ex = await _ctx_executor(
        policy=ModelPolicy(),
        runtime_policy=RuntimePolicy(),
        skill_policy=SkillPolicy(),
    )
    assert ex._governance is None


# ---------------------------------------------------------------------------
# ADR 096 — the per-run governance effect crosses activity boundaries via the
# process-local registry and lands on the persist/pause activities' facts.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_persist_activity_stamps_and_consumes_run_effect() -> None:
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=load_pricing(),
        tracer=NullTracer(),
        provider=MockProvider(),
        tenant_id="local",
        policy=ModelPolicy(),
        runtime_policy=RuntimePolicy(),
        skill_policy=SkillPolicy(),
    )

    run_id = "wfr-gov-1"
    # Simulate what call_agent_activity records around Executor.execute: the
    # node-level effects fold severity-wise into the run's registry entry.
    record_run_effect(run_id, "allow")
    record_run_effect(run_id, "warn")

    await persist_workflow_result_activity(
        run_id,
        "success",
        {"text": "in"},
        {"text": "out", "tenant_id": "local"},
        None,
        "expense-approval",
        "0.1.0",
    )

    facts = await storage.list_observability_facts(tenant_id="local")
    assert len(facts) == 1
    assert facts[0].fact_id == f"workflow_run:{run_id}"
    assert facts[0].governance_effect == "warn"
    # Terminal persist CONSUMES the registry slot (no leak per completed run).
    assert peek_run_effect(run_id) is None


@pytest.mark.unit
async def test_pause_activity_peeks_run_effect_without_consuming() -> None:
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=load_pricing(),
        tracer=NullTracer(),
        provider=MockProvider(),
        tenant_id="local",
        policy=ModelPolicy(),
        runtime_policy=RuntimePolicy(),
        skill_policy=SkillPolicy(),
    )

    run_id = "wfr-gov-2"
    record_run_effect(run_id, "allow")

    await call_human_activity(
        "manager-approval",
        {"text": "x", "tenant_id": "local"},
        run_id,
        "Approve?",
        ["decision"],
        [],
        "expense-approval",
        "0.1.0",
    )

    facts = await storage.list_observability_facts(tenant_id="local")
    assert len(facts) == 1
    assert facts[0].status == "paused"
    assert facts[0].governance_effect == "allow"
    # The run resumes after the pause — the registry entry must survive for
    # the terminal persist (peek, not pop).
    assert consume_run_effect(run_id) == "allow"


# ---------------------------------------------------------------------------
# ADR 096 cross-process regression: the run-effect registry is process-LOCAL,
# but the shared task queue hands a run's activities to ANY polling worker
# (the dispatch path's ephemeral in-process worker + the long-lived
# `mdk worker --backend temporal`). When the activity that recorded the
# effect and the persist activity land on different processes, the registry
# is empty where the terminal fact is written — the observed
# governance_effect=NULL on fast tool-chain runs. The effect now ALSO rides
# workflow state (RUN_EFFECT_STATE_KEY, folded into governed activities'
# returned deltas), which travels through Temporal history to whichever
# process persists.
# ---------------------------------------------------------------------------


async def _configured_storage():
    storage = InMemoryStorage()
    await storage.init()
    configure_activities(
        storage=storage,
        pricing=load_pricing(),
        tracer=NullTracer(),
        provider=MockProvider(),
        tenant_id="local",
        policy=ModelPolicy(),
        runtime_policy=RuntimePolicy(),
        skill_policy=SkillPolicy(),
    )
    return storage


@pytest.mark.unit
async def test_persist_activity_reads_state_carried_effect_when_registry_empty() -> None:
    """THE incident regression (fast path): the last node's agent gate ran on
    another worker process — this process's registry is empty — but the
    effect arrived in final_state, so the terminal fact must carry it."""
    storage = await _configured_storage()

    run_id = "wfr-gov-xproc-1"
    assert peek_run_effect(run_id) is None  # simulate the other-process persist

    await persist_workflow_result_activity(
        run_id,
        "success",
        {"request": "nightly-batch"},
        {"summary": "done", "tenant_id": "local", RUN_EFFECT_STATE_KEY: "allow"},
        None,
        "partial-failure-recovery",
        "0.1.0",
    )

    facts = await storage.list_observability_facts(tenant_id="local")
    assert len(facts) == 1
    assert facts[0].governance_effect == "allow"
    # The reserved key is observability plumbing — never part of the durable
    # record's user-visible final_state.
    record = await storage.get_workflow_run(run_id, tenant_id="local")
    assert record is not None
    assert RUN_EFFECT_STATE_KEY not in record.final_state
    assert record.final_state["summary"] == "done"


@pytest.mark.unit
async def test_persist_activity_folds_registry_and_state_effects() -> None:
    """Severity-wins across the two sources: a warn recorded in THIS process's
    registry beats an allow carried in state (and vice versa)."""
    storage = await _configured_storage()

    run_id = "wfr-gov-xproc-2"
    record_run_effect(run_id, "warn")

    await persist_workflow_result_activity(
        run_id,
        "success",
        {"text": "in"},
        {"text": "out", "tenant_id": "local", RUN_EFFECT_STATE_KEY: "allow"},
        None,
        "expense-approval",
        "0.1.0",
    )

    facts = await storage.list_observability_facts(tenant_id="local")
    assert facts[0].governance_effect == "warn"
    assert peek_run_effect(run_id) is None  # still consumed


@pytest.mark.unit
async def test_pause_activity_reads_state_carried_effect_when_registry_empty() -> None:
    """Same cross-process coverage on the pause fact — the EFFECT lands on the
    fact, while the reserved key is stripped from the persisted pause record
    (its state surfaces through the HITL API; the LIVE workflow state keeps
    the key, so the resumed segment still accumulates from it)."""
    storage = await _configured_storage()

    run_id = "wfr-gov-xproc-3"
    assert peek_run_effect(run_id) is None

    await call_human_activity(
        "manager-approval",
        {"text": "x", "tenant_id": "local", RUN_EFFECT_STATE_KEY: "warn"},
        run_id,
        "Approve?",
        ["decision"],
        [],
        "expense-approval",
        "0.1.0",
    )

    facts = await storage.list_observability_facts(tenant_id="local")
    assert len(facts) == 1
    assert facts[0].status == "paused"
    assert facts[0].governance_effect == "warn"
    record = await storage.get_workflow_run(run_id, tenant_id="local")
    assert record is not None
    assert record.paused_state is not None
    # Plumbing, not user-visible state: stripped from the persisted record.
    assert RUN_EFFECT_STATE_KEY not in record.paused_state
    assert record.paused_state["text"] == "x"
