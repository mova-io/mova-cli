"""Glass-box trace rendering for the Chainlit playground.

Three layers, mirroring the implementation seam:

1. ``build_explain_steps`` — the PURE transform from an explain decision-chain
   payload to render-ready :class:`ExplainStep`s (Chainlit-free, so it runs on
   a no-extras install). Asserts that tool / retrieval / decision Steps are
   built from the payload, and that a missing / empty / malformed payload
   yields no Steps (→ the app degrades to today's plain message).
2. ``PlaygroundClient.get_explain`` — the client call that reuses the read-only
   ``GET /api/v1/runs/{id}/explain?steps=true`` surface, including its
   graceful-degrade-to-``None`` on 404 / transport error.
3. ``app._render_glassbox`` / ``_emit_step`` — the Chainlit render path
   (gated on the extra): asserts Steps are emitted from an explain payload and
   that a missing run id / unavailable chain / raising client never errors.
"""

from __future__ import annotations

import sys
from typing import ClassVar

import httpx
import pytest

from movate.playground.client import PlaygroundClient, PlaygroundClientConfig
from movate.playground.explain_steps import ExplainStep, build_explain_steps

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Pure transform: build_explain_steps
# ---------------------------------------------------------------------------


def _payload(**over: object) -> dict[str, object]:
    """A representative explain payload (steps=true shape)."""
    base: dict[str, object] = {
        "run_id": "run-1",
        "agent": "demo",
        "status": "success",
        "turns": [
            {
                "index": 1,
                "model": "openai/gpt-4o",
                "input_tokens": 100,
                "output_tokens": 20,
                "latency_ms": 800,
                "finish_reason": "tool_use",
            },
            {
                "index": 2,
                "model": "openai/gpt-4o",
                "input_tokens": 150,
                "output_tokens": 40,
                "latency_ms": 600,
                "finish_reason": "final",
            },
        ],
        "skill_calls": [
            {
                "step": 1,
                "turn": 1,
                "skill": "weather",
                "input": {"city": "Paris"},
                "output": {"temp_c": 18},
                "latency_ms": 42.0,
            },
            {
                "step": 2,
                "turn": 1,
                "skill": "kb_search",
                "input": {"query": "refund policy"},
                "output": {
                    "chunks": [
                        {"score": 0.91, "source": "docs/refunds.md", "content": "Refunds in 30d."},
                        {"score": 0.80, "source": "docs/terms.md", "content": "Terms apply."},
                    ]
                },
                "latency_ms": 30.0,
            },
        ],
    }
    base.update(over)
    return base


def test_build_steps_emits_tool_retrieval_and_decision() -> None:
    steps = build_explain_steps(_payload())
    # Turn 1 (tool_use) becomes a decision Step parenting its two calls; turn 2
    # (final, no children) is dropped as noise.
    assert len(steps) == 1
    decision = steps[0]
    assert decision.kind == "decision"
    assert "turn 1" in decision.name
    kinds = [c.kind for c in decision.children]
    assert kinds == ["tool", "retrieval"]
    # The decision body names the model + that it called tools.
    assert "called tools" in decision.body
    assert "openai/gpt-4o" in decision.body


def test_tool_step_carries_input_and_output() -> None:
    decision = build_explain_steps(_payload())[0]
    tool = next(c for c in decision.children if c.kind == "tool")
    assert "weather" in tool.name
    assert "Paris" in tool.body
    assert "temp_c" in tool.body
    assert "42 ms" in tool.body


def test_retrieval_step_lists_chunks_with_scores() -> None:
    decision = build_explain_steps(_payload())[0]
    retrieval = next(c for c in decision.children if c.kind == "retrieval")
    assert "kb_search" in retrieval.name
    assert "Retrieved 2 chunk(s)" in retrieval.body
    assert "refunds.md" in retrieval.body  # source filename only
    assert "0.91" in retrieval.body
    assert "refund policy" in retrieval.body  # the query


def test_tool_error_renders_error_not_output() -> None:
    payload = _payload(
        skill_calls=[
            {
                "step": 1,
                "turn": 1,
                "skill": "broken",
                "input": {"x": 1},
                "error": "SkillError: boom",
                "latency_ms": 5.0,
            }
        ],
        turns=[{"index": 1, "model": "m", "finish_reason": "tool_use"}],
    )
    decision = build_explain_steps(payload)[0]
    tool = decision.children[0]
    assert "SkillError: boom" in tool.body


def test_orphan_calls_without_turn_linkage_surface_top_level() -> None:
    # Legacy record: skill calls with no matching turn record still render.
    payload = {
        "turns": [],
        "skill_calls": [
            {"step": 1, "turn": 0, "skill": "legacy", "input": {}, "output": {}, "latency_ms": 1.0}
        ],
    }
    steps = build_explain_steps(payload)
    assert len(steps) == 1
    assert steps[0].kind == "tool"
    assert "legacy" in steps[0].name


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"turns": [], "skill_calls": []},
        # A plain single-shot answer: one final turn, no tool calls → no Steps.
        {"turns": [{"index": 1, "model": "m", "finish_reason": "final"}], "skill_calls": []},
        # Malformed: wrong types must not raise.
        {"turns": "nope", "skill_calls": 42},
        "not-a-dict",
        [1, 2, 3],
    ],
)
def test_empty_or_malformed_payload_yields_no_steps(payload: object) -> None:
    assert build_explain_steps(payload) == []  # type: ignore[arg-type]


def test_explain_step_is_chainlit_free() -> None:
    # Sanity: the pure transform module is importable without chainlit loaded
    # and exposes the dataclass the app renders. (Import hygiene at large is
    # asserted in test_playground_logic.)
    from movate.playground import explain_steps  # noqa: PLC0415

    assert explain_steps.__name__ in sys.modules
    step = ExplainStep(name="x", kind="tool", body="b")
    assert step.children == []


# ---------------------------------------------------------------------------
# 2. Client: get_explain reuses /runs/{id}/explain and degrades to None
# ---------------------------------------------------------------------------


def _client_with(handler: object) -> PlaygroundClient:
    client = PlaygroundClient(
        PlaygroundClientConfig(runtime_url="http://runtime.example", api_key="tok")
    )
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client._client = httpx.AsyncClient(base_url="http://runtime.example", transport=transport)
    return client


async def test_get_explain_calls_endpoint_with_steps_true() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"run_id": "r1", "skill_calls": []})

    client = _client_with(handler)
    result = await client.get_explain("r1")
    assert result == {"run_id": "r1", "skill_calls": []}
    assert captured["url"] == "http://runtime.example/api/v1/runs/r1/explain?steps=true"


async def test_get_explain_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"detail": "not found"})

    client = _client_with(handler)
    assert await client.get_explain("missing") is None


async def test_get_explain_returns_none_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("boom")

    client = _client_with(handler)
    assert await client.get_explain("r1") is None


# ---------------------------------------------------------------------------
# 3. App render path (Chainlit-gated)
# ---------------------------------------------------------------------------

pytest.importorskip("chainlit")


class _FakeStep:
    """Captures the cl.Step calls the app makes (name + type + output)."""

    recorded: ClassVar[list[dict[str, object]]] = []

    def __init__(self, name: str | None = None, type: str = "undefined", **_: object) -> None:
        self.name = name
        self.type = type
        self.output = ""
        self.id = "step-id"

    async def __aenter__(self) -> _FakeStep:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        _FakeStep.recorded.append({"name": self.name, "type": self.type, "output": self.output})
        return False


class _FakeClient:
    def __init__(self, payload: dict[str, object] | None, *, raises: bool = False) -> None:
        self._payload = payload
        self._raises = raises
        self.calls: list[str] = []

    async def get_explain(self, run_id: str) -> dict[str, object] | None:
        self.calls.append(run_id)
        if self._raises:
            raise RuntimeError("explain blew up")
        return self._payload


@pytest.fixture
def app_mod(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from movate.playground import app  # noqa: PLC0415

    _FakeStep.recorded = []
    monkeypatch.setattr(app.cl, "Step", _FakeStep)
    return app


async def test_render_glassbox_emits_steps_from_payload(app_mod, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _FakeClient(_payload())
    await app_mod._render_glassbox(client, "run-1")  # type: ignore[arg-type]
    assert client.calls == ["run-1"]
    names = [r["name"] for r in _FakeStep.recorded]
    # A decision Step plus its two child Steps (tool + retrieval) were emitted.
    assert any("decision" in str(n) for n in names)
    assert any("weather" in str(n) for n in names)
    assert any("kb_search" in str(n) for n in names)
    types = {r["type"] for r in _FakeStep.recorded}
    assert {"run", "tool", "retrieval"} <= types  # decision maps to "run"


async def test_render_glassbox_no_run_id_is_noop(app_mod) -> None:  # type: ignore[no-untyped-def]
    client = _FakeClient(_payload())
    await app_mod._render_glassbox(client, None)  # type: ignore[arg-type]
    assert client.calls == []
    assert _FakeStep.recorded == []


async def test_render_glassbox_unavailable_chain_degrades(app_mod) -> None:  # type: ignore[no-untyped-def]
    # Endpoint absent / older run → client returns None → no Steps, no error.
    client = _FakeClient(None)
    await app_mod._render_glassbox(client, "run-1")  # type: ignore[arg-type]
    assert client.calls == ["run-1"]
    assert _FakeStep.recorded == []


async def test_render_glassbox_swallows_client_error(app_mod) -> None:  # type: ignore[no-untyped-def]
    # A raising explain call must never propagate into the chat.
    client = _FakeClient(None, raises=True)
    await app_mod._render_glassbox(client, "run-1")  # type: ignore[arg-type]
    assert _FakeStep.recorded == []
