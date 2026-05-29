"""NL query + troubleshoot (ADR 047) — grounding, citations, SQL-safety.

The critical test here is the SQL-SAFETY CONTRACT: the detail path is
text-to-PARAMETERIZED-TEMPLATE, never text-to-arbitrary-SQL. We assert:

* the template registry is a CLOSED set; an unknown name is ignored (no run).
* there is NO ``execute_sql`` / ``raw_query`` style escape hatch in the module
  and no template f-string-interpolates a param into a query.
* params are typed + clamped (window bounded, agent coerced).

Plus the grounding guarantees: every answer carries non-empty evidence when
there is data, confidence is surfaced, and an empty store degrades to a
low-confidence fallback that still cites what it looked at.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from movate.core.models import ErrorInfo, JobStatus, Metrics, RunRecord, TokenUsage
from movate.core.observability import query
from movate.core.observability.models import ObservabilityInsight
from movate.providers.base import CompletionRequest, CompletionResponse
from movate.testing import InMemoryStorage


@dataclass
class _StubLLM:
    """Records prompts; returns a scripted reply per call (plan, then answer)."""

    name: str = "stub"
    version: str = "1"
    replies: list[str] = field(default_factory=list)
    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        idx = min(len(self.calls) - 1, len(self.replies) - 1) if self.replies else 0
        text = self.replies[idx] if self.replies else "{}"
        return CompletionResponse(text=text, tokens=TokenUsage(input=50, output=20))


def _run(
    *, run_id: str, agent: str = "triage", status=JobStatus.SUCCESS, cost=0.01, when
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id=f"j-{run_id}",
        tenant_id="t1",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="h",
        provider="openai/gpt-4o-mini",
        provider_version="v1",
        pricing_version="2026-05",
        status=status,
        input={"q": "x"},
        output={"a": "y"} if status == JobStatus.SUCCESS else None,
        error=ErrorInfo(type="Timeout", message="boom") if status != JobStatus.SUCCESS else None,
        metrics=Metrics(cost_usd=cost, latency_ms=120, tokens=TokenUsage(input=10, output=5)),
        created_at=when,
    )


async def _seed(storage: InMemoryStorage) -> None:
    await storage.init()
    now = datetime.now(UTC)
    for i in range(4):
        await storage.save_run(_run(run_id=f"ok{i}", cost=0.02, when=now - timedelta(hours=i)))
    await storage.save_run(
        _run(run_id="bad1", status=JobStatus.ERROR, when=now - timedelta(hours=1))
    )
    await storage.save_insight(
        ObservabilityInsight(
            tenant_id="t1",
            project_id="default",
            date=now.date(),
            health_score=72.0,
            anomalies=[
                {
                    "metric": "cost",
                    "severity": "warning",
                    "note": "cost 3.1 sigma above",
                    "value": 1.0,
                    "baseline": 0.3,
                    "z": 3.1,
                }
            ],
            top_failures=[
                {"signature": "Timeout", "count": 1, "sample_message": "boom", "agent": "triage"}
            ],
            usage_rollup={"runs": 5, "errors": 1, "error_rate": 0.2, "cost_usd": 0.09},
            trends={},
            narrative_digest="Yesterday: 5 runs. Watch: a timeout.",
        )
    )


# ---------------------------------------------------------------------------
# SQL-SAFETY CONTRACT
# ---------------------------------------------------------------------------


def test_template_registry_is_a_closed_set() -> None:
    # The exact CLOSED set the LLM may pick from. Adding to this is a reviewed
    # change to the dict, never a runtime/LLM decision.
    assert set(query.QUERY_TEMPLATES) == {
        "cost_by_agent",
        "failed_runs",
        "latency_percentiles",
        "usage_by_provider",
    }


async def test_unknown_template_name_is_not_executed() -> None:
    storage = InMemoryStorage()
    await storage.init()
    # An unknown / injected name returns None (no execution path).
    result = await query.run_template(
        "DROP TABLE runs; --", {"window": 7}, storage=storage, tenant_id="t1"
    )
    assert result is None
    result2 = await query.run_template(
        "totally_made_up", {"window": 7}, storage=storage, tenant_id="t1"
    )
    assert result2 is None


def test_no_arbitrary_sql_escape_hatch_exists() -> None:
    """There must be NO raw-SQL entry point the LLM output could reach."""
    forbidden = {"execute_sql", "raw_query", "run_sql", "exec_sql", "query_sql"}
    assert not (forbidden & set(vars(query)))
    # And no module-level callable accepts a raw `sql` parameter.
    for name, obj in vars(query).items():
        if inspect.isfunction(obj):
            params = set(inspect.signature(obj).parameters)
            assert "sql" not in params, f"{name} exposes a raw sql param"


def test_template_functions_do_not_string_format_params_into_sql() -> None:
    """Each template calls typed Protocol methods — no f-string SQL building.

    A coarse but effective guard: the template source must not contain an
    f-string with 'SELECT'/'WHERE' (the LLM's params never reach raw SQL).
    """
    for name, fn in query.QUERY_TEMPLATES.items():
        src = inspect.getsource(fn)
        lowered = src.lower()
        assert "select " not in lowered, f"{name} appears to build raw SQL"
        assert 'f"select' not in lowered and "f'select" not in lowered


async def test_params_are_typed_and_clamped() -> None:
    storage = InMemoryStorage()
    await _seed(storage)
    # window above the cap is clamped to MAX_WINDOW_DAYS; a non-int falls back.
    result = await query.run_template(
        "cost_by_agent",
        {"window": 10_000, "agent": "x", "evil": "1; DROP"},
        storage=storage,
        tenant_id="t1",
    )
    assert result is not None
    assert result.params["window"] == query.MAX_WINDOW_DAYS
    # The unknown 'evil' param was dropped (not forwarded).
    assert "evil" not in result.params


async def test_template_caps_rows() -> None:
    storage = InMemoryStorage()
    await _seed(storage)
    result = await query.run_template(
        "failed_runs", {"window": 90}, storage=storage, tenant_id="t1"
    )
    assert result is not None
    assert len(result.rows) <= query.MAX_ROWS


# ---------------------------------------------------------------------------
# ask — grounded evidence + citations mandatory
# ---------------------------------------------------------------------------


async def test_ask_returns_grounded_evidence() -> None:
    storage = InMemoryStorage()
    await _seed(storage)
    # First LLM reply = plan (pick a template); second = synthesized answer.
    llm = _StubLLM(
        replies=[
            '{"templates": [{"name": "cost_by_agent", "params": {"window": 7}}]}',
            json.dumps(
                {
                    "answer": "Cost is concentrated in triage.",
                    "confidence": 0.8,
                    "suggested_action": "review triage",
                }
            ),
        ]
    )
    ans = await query.ask(
        "where is my cost going?",
        tenant_id="t1",
        project_id="default",
        storage=storage,
        llm=llm,
        budget_usd=1.0,
    )
    # Citations are mandatory + non-empty when there's data.
    assert ans.evidence, "ask must cite evidence when data exists"
    kinds = {e.kind for e in ans.evidence}
    assert "insight" in kinds  # the fast-path source
    assert "query" in kinds  # the chosen template
    assert ans.confidence > 0.0
    assert ans.answer


async def test_ask_empty_store_low_confidence_fallback() -> None:
    storage = InMemoryStorage()
    await storage.init()
    # No LLM, no insights → deterministic fallback, capped confidence.
    ans = await query.ask(
        "how are things?", tenant_id="t1", project_id="default", storage=storage, llm=None
    )
    assert ans.confidence <= 0.25
    assert "No observability insights" in ans.answer
    assert ans.cost_usd == 0.0


async def test_ask_no_llm_still_grounds_in_insights() -> None:
    storage = InMemoryStorage()
    await _seed(storage)
    ans = await query.ask(
        "summary?", tenant_id="t1", project_id="default", storage=storage, llm=None
    )
    # Even without an LLM, the fast-path insight is cited.
    assert any(e.kind == "insight" for e in ans.evidence)


async def test_ask_is_tenant_scoped() -> None:
    storage = InMemoryStorage()
    await _seed(storage)  # seeds tenant t1
    # A different tenant sees no insights → fallback, no t1 leakage.
    ans = await query.ask(
        "status?", tenant_id="intruder", project_id="default", storage=storage, llm=None
    )
    assert not any(e.kind == "insight" for e in ans.evidence)


# ---------------------------------------------------------------------------
# troubleshoot — correlates failures into a root-cause narrative
# ---------------------------------------------------------------------------


async def test_troubleshoot_cites_failures_and_anomalies() -> None:
    storage = InMemoryStorage()
    await _seed(storage)
    llm = _StubLLM(
        replies=[
            json.dumps(
                {
                    "answer": "Likely a timeout cluster.",
                    "confidence": 0.7,
                    "suggested_action": "raise timeout",
                }
            )
        ]
    )
    ans = await query.troubleshoot(
        "agent keeps timing out",
        7,
        tenant_id="t1",
        project_id="default",
        storage=storage,
        llm=llm,
        budget_usd=1.0,
    )
    kinds = {e.kind for e in ans.evidence}
    # The failure cluster + anomaly from the latest insight are cited.
    assert "failure" in kinds
    assert "event" in kinds  # anomalies surface as event evidence
    assert ans.answer


async def test_troubleshoot_empty_is_low_confidence() -> None:
    storage = InMemoryStorage()
    await storage.init()
    ans = await query.troubleshoot(
        "weird latency", tenant_id="t1", project_id="default", storage=storage, llm=None
    )
    assert ans.confidence <= 0.25
