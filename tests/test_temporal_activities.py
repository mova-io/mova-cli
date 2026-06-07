"""Tests for the Phase 1 Temporal activity wrappers (ADR 054 Track C).

The four activities (``call_agent_activity`` / ``call_skill_activity`` /
``call_gate_activity`` / ``call_judge_activity``) are the call targets the
Track-B compiler (``movate.core.workflow.compilers.temporal``) emits by name.
These tests assert:

* import-safety without the ``[temporal]`` extra (ADR 054 D7) — copying the
  compiler suite's ``sys.meta_path``-blocking pattern,
* the DI contract (``configure_activities`` / ``_get_context``),
* that each activity is a thin shim forwarding to the Executor / SkillBackend
  (ADR 054 D3) — exercised with fakes so no network / LLM is hit.

Hermetic + fast: no ``temporal server``, no real provider.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

import movate.core.workflow.temporal_activities as ta
from movate.core.models import ErrorInfo, Metrics, RunResponse
from movate.core.workflow.judge import (
    build_judge_state_value,
    derive_terminate,
    verdict_from_response_data,
)
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Fakes — no network, no LLM, no temporal server.
# ---------------------------------------------------------------------------


class _FakeBundle:
    """Minimal stand-in for an AgentBundle / SkillBundle the activities touch."""

    def __init__(self, name: str = "fake-agent", properties: dict | None = None) -> None:
        class _Spec:
            pass

        self.spec = _Spec()
        self.spec.name = name  # type: ignore[attr-defined]
        # ``input_schema`` is read by the activities' state projection.
        self.input_schema = {"properties": properties} if properties is not None else {}


class _FakeExecutor:
    """Records the ``execute`` call + returns a canned RunResponse."""

    def __init__(self, response: RunResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def execute(self, bundle: Any, request: Any, **kwargs: Any) -> RunResponse:
        self.calls.append(
            {
                "agent": request.agent,
                "input": dict(request.input),
                "workflow_run_id": kwargs.get("workflow_run_id"),
                "node_id": kwargs.get("node_id"),
                "tenant_id_override": kwargs.get("tenant_id_override"),
            }
        )
        return self._response


def _ok_response(data: dict[str, Any]) -> RunResponse:
    return RunResponse(status="success", data=data, metrics=Metrics())


def _err_response(message: str) -> RunResponse:
    return RunResponse(
        status="error",
        data={},
        metrics=Metrics(),
        error=ErrorInfo(type="internal", message=message),
    )


@pytest.fixture(autouse=True)
def _reset_context() -> Any:
    """Each test starts with no configured context (module global)."""
    ta._CONTEXT = None
    yield
    ta._CONTEXT = None


def _configure(monkeypatch: pytest.MonkeyPatch, tenant_id: str = "local") -> None:
    """Install a context without building a real LiteLLM provider."""
    ta.configure_activities(
        storage=InMemoryStorage(),
        pricing=object(),  # never read by the fakes
        tracer=NullTracer(),
        provider=object(),  # never read — _executor_for is monkeypatched
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# 1: Import-safety (ADR 054 D7) — module imports with temporalio blocked.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_import_safe_without_temporalio(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activities module imports cleanly even when ``temporalio`` is absent.

    Copies the compiler suite's ``sys.meta_path``-blocking pattern: hide
    temporalio, re-import fresh, assert the four activities are present +
    callable and that ``_require_temporalio`` raises the install hint.
    """
    blocked = [m for m in sys.modules if m == "temporalio" or m.startswith("temporalio.")]
    saved = {m: sys.modules[m] for m in blocked}
    for m in blocked:
        del sys.modules[m]

    class _BlockTemporalioFinder:
        def find_module(self, name: str, path: Any = None) -> Any:
            return self if name == "temporalio" or name.startswith("temporalio.") else None

        def load_module(self, name: str) -> Any:
            raise ImportError(f"hidden by test: {name}")

    finder = _BlockTemporalioFinder()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    mod_name = "movate.core.workflow.temporal_activities"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    try:
        import movate.core.workflow.temporal_activities as fresh  # noqa: PLC0415

        # The four activities exist and are plain (async) callables.
        for fn_name in (
            "call_agent_activity",
            "call_skill_activity",
            "call_gate_activity",
            "call_judge_activity",
        ):
            assert callable(getattr(fresh, fn_name))

        # The worker gate raises a clear install hint when temporalio is gone.
        assert fresh._HAVE_TEMPORALIO is False
        with pytest.raises(RuntimeError) as ei:
            fresh._require_temporalio()
        assert "[temporal] extra is not installed" in str(ei.value)
        assert "uv tool install" in str(ei.value)
    finally:
        for m, mod in saved.items():
            sys.modules[m] = mod
        if mod_name in sys.modules:
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# 2: DI contract — _get_context raises before configure_activities.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_context_unconfigured_raises() -> None:
    assert ta._CONTEXT is None
    with pytest.raises(RuntimeError) as ei:
        ta._get_context()
    assert "not configured" in str(ei.value)


@pytest.mark.unit
def test_configure_then_get_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, tenant_id="acme")
    ctx = ta._get_context()
    assert ctx.tenant_id == "acme"
    assert isinstance(ctx.storage, InMemoryStorage)


@pytest.mark.unit
def test_configure_defaults_pricing_and_tracer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitted pricing/tracer fall back to load_pricing()/build_tracer()."""
    sentinel_pricing = object()
    sentinel_tracer = object()
    monkeypatch.setattr("movate.providers.pricing.load_pricing", lambda *a, **k: sentinel_pricing)
    monkeypatch.setattr("movate.tracing.build_tracer", lambda: sentinel_tracer)
    ta.configure_activities(storage=InMemoryStorage(), provider=object())
    ctx = ta._get_context()
    assert ctx.pricing is sentinel_pricing
    assert ctx.tracer is sentinel_tracer


# ---------------------------------------------------------------------------
# 3: _require_temporalio raises the install hint when the extra is absent.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_require_temporalio_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ta, "_HAVE_TEMPORALIO", False)
    with pytest.raises(RuntimeError) as ei:
        ta._require_temporalio()
    assert "[temporal] extra is not installed" in str(ei.value)
    assert "uv tool install" in str(ei.value)


# ---------------------------------------------------------------------------
# 4: call_agent_activity forwards to the Executor + returns response.data.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_agent_activity_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, tenant_id="tenantA")

    bundle = _FakeBundle(name="echoer", properties={"text": {"type": "string"}})
    fake_exec = _FakeExecutor(_ok_response({"step1": "done"}))

    monkeypatch.setattr(ta, "_executor_for", lambda ctx, state: fake_exec)
    monkeypatch.setattr("movate.core.loader.load_agent", lambda ref, *, defaults=None: bundle)

    state = {"text": "hello", "unused": "drop-me"}
    out = await ta.call_agent_activity("node-1", "/agents/echoer", state, "run-xyz")

    assert out == {"step1": "done"}
    assert len(fake_exec.calls) == 1
    call = fake_exec.calls[0]
    # State is projected to the agent's input schema (only "text").
    assert call["input"] == {"text": "hello"}
    assert call["workflow_run_id"] == "run-xyz"
    assert call["node_id"] == "node-1"
    assert call["tenant_id_override"] == "tenantA"


@pytest.mark.unit
async def test_call_agent_activity_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-success RunResponse surfaces as an exception (Temporal retries)."""
    _configure(monkeypatch)
    bundle = _FakeBundle()
    monkeypatch.setattr(
        ta, "_executor_for", lambda ctx, state: _FakeExecutor(_err_response("boom"))
    )
    monkeypatch.setattr("movate.core.loader.load_agent", lambda ref, *, defaults=None: bundle)

    with pytest.raises(RuntimeError) as ei:
        await ta.call_agent_activity("node-1", "/agents/x", {}, "run-1")
    assert "boom" in str(ei.value)


@pytest.mark.unit
async def test_call_agent_activity_tenant_from_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant_id in state overrides the context default."""
    _configure(monkeypatch, tenant_id="ctx-default")
    bundle = _FakeBundle()
    fake_exec = _FakeExecutor(_ok_response({}))
    monkeypatch.setattr(ta, "_executor_for", lambda ctx, state: fake_exec)
    monkeypatch.setattr("movate.core.loader.load_agent", lambda ref, *, defaults=None: bundle)

    await ta.call_agent_activity("n", "/a", {"tenant_id": "from-state"}, "r")
    assert fake_exec.calls[0]["tenant_id_override"] == "from-state"


# ---------------------------------------------------------------------------
# 5: call_gate_activity runs the classifier + returns its decision dict.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_gate_activity_returns_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    bundle = _FakeBundle(name="turn-judge", properties={"text": {"type": "string"}})
    fake_exec = _FakeExecutor(_ok_response({"label": "resolved"}))
    monkeypatch.setattr(ta, "_executor_for", lambda ctx, state: fake_exec)
    monkeypatch.setattr("movate.core.loader.load_agent", lambda ref, *, defaults=None: bundle)

    decision = await ta.call_gate_activity(
        "turn-gate-1",
        "/agents/turn-judge",
        {"transcript": "the dialogue"},
        "run-1",
        "",
        "transcript",
        ["continue", "resolved"],
    )
    # The classifier's decision dict (carrying "label") is returned verbatim.
    assert decision == {"label": "resolved"}
    # The classifier sees the native-runner input shape: the input_field value
    # mapped to "text" + the route labels (NOT a raw state projection).
    assert fake_exec.calls[0]["input"] == {
        "text": "the dialogue",
        "labels": ["continue", "resolved"],
    }


@pytest.mark.unit
async def test_call_gate_activity_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    bundle = _FakeBundle()
    monkeypatch.setattr(
        ta, "_executor_for", lambda ctx, state: _FakeExecutor(_err_response("nope"))
    )
    monkeypatch.setattr("movate.core.loader.load_agent", lambda ref, *, defaults=None: bundle)
    with pytest.raises(RuntimeError) as ei:
        await ta.call_gate_activity("g", "/clf", {}, "r")
    assert "nope" in str(ei.value)


# ---------------------------------------------------------------------------
# 6: call_skill_activity dispatches through dispatch_skill + returns output.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_skill_activity_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, tenant_id="tnt")
    skill = _FakeBundle(name="my-skill", properties={"q": {"type": "string"}})

    captured: dict[str, Any] = {}

    async def _fake_dispatch(skill_arg: Any, input_arg: dict, ctx_arg: Any) -> dict:
        captured["input"] = dict(input_arg)
        captured["tenant_id"] = ctx_arg.tenant_id
        captured["run_id"] = ctx_arg.run_id
        return {"answer": 42}

    monkeypatch.setattr("movate.core.skill_loader.load_skill", lambda ref: skill)
    monkeypatch.setattr("movate.core.skill_backend.base.dispatch_skill", _fake_dispatch)

    out = await ta.call_skill_activity(
        "skill-node", "/skills/my-skill", {"q": "x", "z": 1}, "run-7"
    )

    assert out == {"answer": 42}
    # Input narrowed to the skill's declared schema ("q" only).
    assert captured["input"] == {"q": "x"}
    assert captured["tenant_id"] == "tnt"
    assert captured["run_id"] == "run-7"


# ---------------------------------------------------------------------------
# 7: call_judge_activity RUNS the judge through the Executor (ADR 056 D5).
#
# This resolves the ADR 054 §11 state-interpreter caveat: the activity now
# loads the judge bundle (ref or inline criteria) + forwards to the Executor,
# returning the canonical D2 verdict {verdict, score, feedback, terminate}.
# ---------------------------------------------------------------------------


def _patch_judge_bundle(monkeypatch: pytest.MonkeyPatch, *, properties: dict | None = None) -> None:
    bundle = _FakeBundle(name="judge-agent", properties=properties or {"text": {"type": "string"}})
    monkeypatch.setattr(
        "movate.core.workflow.judge.load_judge_bundle",
        lambda *, judge_ref, criteria, defaults=None: bundle,
    )


@pytest.mark.unit
async def test_call_judge_activity_categorical_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    _patch_judge_bundle(monkeypatch)
    fake_exec = _FakeExecutor(_ok_response({"verdict": "accept", "feedback": ""}))
    monkeypatch.setattr(ta, "_executor_for", lambda ctx, state: fake_exec)

    out = await ta.call_judge_activity(
        "judge-1", "/agents/judge", {"input_field": "answer"}, {"answer": "good"}, "run-1"
    )
    assert out == {"verdict": "accept", "score": None, "feedback": "", "terminate": True}
    # The judge ran through the Executor (one execution model) with the artifact.
    assert fake_exec.calls[0]["input"] == {"text": "good"}
    assert fake_exec.calls[0]["node_id"] == "judge-1"


@pytest.mark.unit
async def test_call_judge_activity_threshold_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """pass_threshold ⇒ score gates terminate (the eval-gate form)."""
    _configure(monkeypatch)
    _patch_judge_bundle(monkeypatch)
    fake_exec = _FakeExecutor(_ok_response({"verdict": "revise", "score": 0.85, "feedback": "x"}))
    monkeypatch.setattr(ta, "_executor_for", lambda ctx, state: fake_exec)

    out = await ta.call_judge_activity(
        "judge-1", "/agents/judge", {"pass_threshold": 0.7}, {"answer": "ok"}, "run-1"
    )
    # score 0.85 >= 0.7 ⇒ terminate even though the categorical verdict is revise.
    assert out["terminate"] is True
    assert out["score"] == 0.85


@pytest.mark.unit
async def test_call_judge_activity_custom_input_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    _patch_judge_bundle(monkeypatch)
    fake_exec = _FakeExecutor(_ok_response({"verdict": "revise", "feedback": "fix"}))
    monkeypatch.setattr(ta, "_executor_for", lambda ctx, state: fake_exec)

    await ta.call_judge_activity(
        "judge-1", "/agents/judge", {"input_field": "draft"}, {"draft": "the draft"}, "run-1"
    )
    assert fake_exec.calls[0]["input"] == {"text": "the draft"}


@pytest.mark.unit
async def test_call_judge_activity_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed judge run surfaces as an exception (Temporal retries)."""
    _configure(monkeypatch)
    _patch_judge_bundle(monkeypatch)
    monkeypatch.setattr(
        ta, "_executor_for", lambda ctx, state: _FakeExecutor(_err_response("nope"))
    )

    with pytest.raises(RuntimeError) as ei:
        await ta.call_judge_activity("judge-1", "/agents/judge", {}, {"answer": "x"}, "run-1")
    assert "nope" in str(ei.value)


@pytest.mark.unit
async def test_judge_activity_unconfigured_raises() -> None:
    """The judge still enforces the configure contract (D3)."""
    assert ta._CONTEXT is None
    with pytest.raises(RuntimeError):
        await ta.call_judge_activity("j", "/agents/judge", {}, {"answer": "x"}, "r")


# ---------------------------------------------------------------------------
# 8: native ↔ Temporal verdict equivalence (ADR 056 D5 / ADR 055 D7).
#
# The native runner and the Temporal activity must derive the SAME verdict +
# terminate for the SAME judge output — they share core.workflow.judge.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_judge_activity_matches_native_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    judge_output = {"verdict": "revise", "score": 0.9, "feedback": "tighten it"}

    # What the native runner would compute (eval-gate, threshold 0.7).
    v, s, f = verdict_from_response_data(judge_output)
    native = build_judge_state_value(
        verdict=v,
        score=s,
        feedback=f,
        terminate=derive_terminate(verdict=v, score=s, pass_threshold=0.7),
    )

    # What the Temporal activity computes for the same output.
    _configure(monkeypatch)
    _patch_judge_bundle(monkeypatch)
    monkeypatch.setattr(
        ta, "_executor_for", lambda ctx, state: _FakeExecutor(_ok_response(judge_output))
    )
    temporal = await ta.call_judge_activity(
        "judge-1", "/agents/judge", {"pass_threshold": 0.7}, {"answer": "x"}, "run-1"
    )

    assert temporal == native
    assert temporal["terminate"] is True  # 0.9 >= 0.7
