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
    configure_activities,
)
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
