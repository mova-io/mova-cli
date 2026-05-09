"""Test doubles: in-memory storage, null tracer, scripted judge provider.

These mirror the real implementations' protocols closely enough that they
satisfy mypy strict against ``StorageProvider`` / ``Tracer`` /
``BaseLLMProvider`` without copying production code into ``tests/``.
"""

from __future__ import annotations

from typing import Any

from movate.core.models import EvalRecord, FailureRecord, RunRecord
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.tracing.base import SpanCtx, Tracer


class InMemoryStorage:
    """In-memory implementation of :class:`movate.storage.base.StorageProvider`.

    Records are kept in plain lists for direct assertion in tests
    (``assert len(storage.runs) == 1``). ``init`` and ``close`` are no-ops.
    """

    name = "in_memory"

    def __init__(self) -> None:
        self.runs: list[RunRecord] = []
        self.failures: list[FailureRecord] = []
        self.evals: list[EvalRecord] = []

    async def init(self) -> None:
        return None

    async def save_run(self, run: RunRecord) -> None:
        self.runs.append(run)

    async def save_failure(self, f: FailureRecord) -> None:
        self.failures.append(f)

    async def save_eval(self, e: EvalRecord) -> None:
        self.evals.append(e)

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]:
        rows = self.runs
        if agent:
            rows = [r for r in rows if r.agent == agent]
        if tenant_id:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        if status:
            rows = [r for r in rows if r.status.value == status]
        return list(rows)[:limit]

    async def list_evals(self, *, agent: str | None = None, limit: int = 20) -> list[EvalRecord]:
        rows = self.evals
        if agent:
            rows = [e for e in rows if e.agent == agent]
        return list(rows)[:limit]

    async def close(self) -> None:
        return None


class NullTracer(Tracer):
    """Tracer that captures spans + events in lists for assertion.

    Use ``tracer.events`` to assert observability hooks fired (e.g.
    ``fallback_triggered``, ``cost_drift``).
    """

    name = "null"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.ended_status: list[str] = []

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        return SpanCtx(
            trace_id="trace-x",
            name=name,
            attributes=dict(attrs or {}),
            parent_id=parent.span_id if parent else None,
        )

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        self.ended_status.append(status)

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        self.events.append(event)

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        span.attributes[key] = value


class JudgeStubProvider(BaseLLMProvider):
    """Provider double that splits behavior by prompt content.

    * If the prompt contains ``Rubric:`` (i.e. an LLM-as-judge call), returns
      a JSON object with the configured ``judge_score`` + a ``"stub"`` rationale.
    * Otherwise returns the configured ``agent_response`` verbatim.

    Captures every provider string seen in ``calls`` and every judge prompt
    body in ``judge_prompts`` so tests can assert which path ran and what
    rubric was used.
    """

    name = "judge_stub"
    version = "0.0.1"

    def __init__(self, *, agent_response: str, judge_score: float) -> None:
        self._agent_response = agent_response
        self._judge_score = judge_score
        self.calls: list[str] = []
        self.judge_prompts: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request.provider)
        body = request.messages[0].content if request.messages else ""
        if "Rubric:" in body:
            self.judge_prompts.append(body)
            return CompletionResponse(
                text=f'{{"score": {self._judge_score}, "rationale": "stub"}}',
            )
        return CompletionResponse(text=self._agent_response)

    async def stream(self, request: CompletionRequest) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError
