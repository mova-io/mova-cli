"""langfuse_trace_url + eval_sync edge helpers (ADR 031 D1).

No real Langfuse needed: the URL helper is pure (env → string), and the
eval-sync helpers are exercised with a stub tracer mirroring the
``score_eval_summary`` / ``sync_dataset`` extension surface.
"""

from __future__ import annotations

from typing import Any

import pytest

from movate.tracing.eval_sync import (
    build_dataset_items,
    push_eval_scores,
    sync_eval_dataset,
)
from movate.tracing.langfuse_link import langfuse_trace_url

# ---------------------------------------------------------------------------
# langfuse_trace_url
# ---------------------------------------------------------------------------


def _clear_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LANGFUSE_HOST",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PROJECT_ID",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.unit
def test_trace_url_with_explicit_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_langfuse_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com/")
    assert langfuse_trace_url("abc123") == "https://lf.example.com/trace/abc123"


@pytest.mark.unit
def test_trace_url_with_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_langfuse_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com")
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "proj-1")
    assert langfuse_trace_url("abc123") == "https://lf.example.com/project/proj-1/traces/abc123"


@pytest.mark.unit
def test_trace_url_base_url_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_langfuse_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://alias.example.com")
    assert langfuse_trace_url("t1") == "https://alias.example.com/trace/t1"


@pytest.mark.unit
def test_trace_url_secret_key_defaults_to_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_langfuse_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-set")
    assert langfuse_trace_url("t1") == "https://cloud.langfuse.com/trace/t1"


@pytest.mark.unit
def test_trace_url_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_langfuse_env(monkeypatch)
    assert langfuse_trace_url("t1") is None


@pytest.mark.unit
def test_trace_url_none_without_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_langfuse_env(monkeypatch)
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com")
    assert langfuse_trace_url("") is None
    assert langfuse_trace_url(None) is None


# ---------------------------------------------------------------------------
# eval_sync edge helpers
# ---------------------------------------------------------------------------


class _StubMetrics:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id


class _StubResponse:
    def __init__(self, trace_id: str) -> None:
        self.metrics = _StubMetrics(trace_id)
        self.trace_id = trace_id


class _StubRun:
    def __init__(self, trace_id: str) -> None:
        self.response = _StubResponse(trace_id)


class _StubCase:
    def __init__(self, input_obj: Any, expected: Any, objective: str | None = None) -> None:
        self.input = input_obj
        self.expected = expected
        self.objective = objective
        self.tags: list[str] = []


class _StubCaseSummary:
    def __init__(self, trace_id: str, case: _StubCase) -> None:
        self.runs = [_StubRun(trace_id)]
        self.case = case


class _StubDimMeans:
    def as_dict(self) -> dict[str, float]:
        return {"Accuracy": 0.9}


class _StubSummary:
    def __init__(self, trace_id: str, cases: list[_StubCaseSummary]) -> None:
        self.agent = "demo"
        self.pass_rate = 0.8
        self.mean_score = 0.7
        self.dimensional_means = _StubDimMeans()
        self.cases = cases


class _RecordingTracer:
    """Stub tracer exposing the Langfuse extension surface."""

    def __init__(self) -> None:
        self.score_calls: list[dict[str, Any]] = []
        self.dataset_calls: list[dict[str, Any]] = []

    async def score_eval_summary(self, **kwargs: Any) -> None:
        self.score_calls.append(kwargs)

    async def sync_dataset(self, **kwargs: Any) -> int:
        self.dataset_calls.append(kwargs)
        return len(kwargs.get("items", []))


class _PlainTracer:
    """A non-Langfuse tracer — no extension methods."""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_eval_scores_forwards_to_tracer() -> None:
    tracer = _RecordingTracer()
    summary = _StubSummary(
        "trace-1", [_StubCaseSummary("trace-1", _StubCase({"q": "1"}, {"a": "x"}))]
    )
    await push_eval_scores(tracer, summary, drift_deltas={"mean_score": -0.1})
    assert len(tracer.score_calls) == 1
    call = tracer.score_calls[0]
    assert call["trace_id"] == "trace-1"
    assert call["pass_rate"] == 0.8
    assert call["mean_score"] == 0.7
    assert call["dimension_means"] == {"Accuracy": 0.9}
    assert call["drift_deltas"] == {"mean_score": -0.1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_eval_scores_picks_last_trace_id() -> None:
    tracer = _RecordingTracer()
    summary = _StubSummary(
        "trace-A",
        [
            _StubCaseSummary("trace-A", _StubCase({"q": "1"}, None)),
            _StubCaseSummary("trace-B", _StubCase({"q": "2"}, None)),
        ],
    )
    await push_eval_scores(tracer, summary)
    assert tracer.score_calls[0]["trace_id"] == "trace-B"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_eval_scores_noop_on_plain_tracer() -> None:
    # No score_eval_summary method → silent no-op, no error.
    summary = _StubSummary("trace-1", [_StubCaseSummary("trace-1", _StubCase({}, None))])
    await push_eval_scores(_PlainTracer(), summary)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_eval_scores_noop_without_trace_id() -> None:
    tracer = _RecordingTracer()
    # Every case has an empty trace id (tracing off) → nothing pushed.
    summary = _StubSummary("", [_StubCaseSummary("", _StubCase({}, None))])
    await push_eval_scores(tracer, summary)
    assert tracer.score_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_eval_scores_best_effort_on_raise() -> None:
    class _Raising(_RecordingTracer):
        async def score_eval_summary(self, **kwargs: Any) -> None:
            raise RuntimeError("down")

    summary = _StubSummary("trace-1", [_StubCaseSummary("trace-1", _StubCase({}, None))])
    # Does not propagate.
    await push_eval_scores(_Raising(), summary)


@pytest.mark.unit
def test_build_dataset_items_shape_and_stable_ids() -> None:
    cases = [
        _StubCase({"q": "1"}, {"a": "x"}, objective="obj1"),
        _StubCase({"q": "2"}, None),
    ]
    items = build_dataset_items("demo", cases)
    assert len(items) == 2
    assert items[0]["input"] == {"q": "1"}
    assert items[0]["expected_output"] == {"a": "x"}
    assert items[0]["metadata"] == {"objective": "obj1"}
    assert items[1]["expected_output"] is None
    # ids stable + deterministic across rebuilds.
    again = build_dataset_items("demo", cases)
    assert [i["id"] for i in items] == [i["id"] for i in again]
    # ids unique within a dataset.
    assert items[0]["id"] != items[1]["id"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_eval_dataset_forwards() -> None:
    tracer = _RecordingTracer()
    cases = [_StubCase({"q": "1"}, {"a": "x"})]
    synced = await sync_eval_dataset(tracer, agent="demo", cases=cases)
    assert synced == 1
    call = tracer.dataset_calls[0]
    assert call["name"] == "mdk-eval-demo"
    assert len(call["items"]) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_eval_dataset_noop_on_plain_tracer() -> None:
    assert await sync_eval_dataset(_PlainTracer(), agent="demo", cases=[_StubCase({}, None)]) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_eval_dataset_noop_without_cases() -> None:
    tracer = _RecordingTracer()
    assert await sync_eval_dataset(tracer, agent="demo", cases=[]) == 0
    assert tracer.dataset_calls == []
