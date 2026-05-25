"""Tests for ``scripts/loadtest.py`` — the load/soak harness.

Coverage:

* The pure ``percentile`` helper against a known sample list.
* A ``--local`` run end-to-end: the report JSON has the expected keys, the
  submitted count equals the sum of terminal statuses + timeouts + poll errors
  (the conservation/accounting invariant), and the status mix is honoured.
* The deployed polling path counts a bad/slow response instead of raising,
  using an ``httpx.MockTransport`` — no real network.

Everything is fast + deterministic: in-memory storage or a mocked transport,
no sleeps that depend on wall-clock beyond the harness's own bounded polling.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "loadtest.py"

_spec = importlib.util.spec_from_file_location("loadtest", SCRIPT)
assert _spec and _spec.loader
lt = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve `cls.__module__` during load.
sys.modules["loadtest"] = lt
_spec.loader.exec_module(lt)


# ---------------------------------------------------------------------------
# percentile — pure logic
# ---------------------------------------------------------------------------


def test_percentile_known_list() -> None:
    # 1..100 → nearest-rank: p50 == 50, p95 == 95, p99 == 99.
    samples = [float(i) for i in range(1, 101)]
    assert lt.percentile(samples, 50) == 50.0
    assert lt.percentile(samples, 95) == 95.0
    assert lt.percentile(samples, 99) == 99.0
    # Edges clamp to min/max.
    assert lt.percentile(samples, 0) == 1.0
    assert lt.percentile(samples, 100) == 100.0


def test_percentile_empty_is_zero() -> None:
    assert lt.percentile([], 95) == 0.0


def test_percentile_single_sample() -> None:
    assert lt.percentile([7.0], 50) == 7.0
    assert lt.percentile([7.0], 99) == 7.0


def test_expand_status_mix_sums_to_total() -> None:
    schedule = lt._expand_status_mix({"success": 0.8, "error": 0.2}, 20)
    assert len(schedule) == 20
    assert schedule.count("success") == 16
    assert schedule.count("error") == 4


def test_expand_status_mix_zero_total() -> None:
    assert lt._expand_status_mix({"success": 1.0}, 0) == []


# ---------------------------------------------------------------------------
# --local run — full harness, in-memory, deterministic
# ---------------------------------------------------------------------------


def test_local_run_report_keys_and_accounting(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    rc = lt.main(
        [
            "--local",
            "--total",
            "20",
            "--concurrency",
            "4",
            "--agent",
            "demo-agent",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    report = json.loads(out.read_text())

    # Top-level shape.
    assert report["schema_version"] == 1
    for key in ("config", "wall_clock_s", "submit", "drain", "accounting"):
        assert key in report

    # Config echo.
    assert report["config"]["mode"] == "local"
    assert report["config"]["agent"] == "demo-agent"
    assert report["config"]["concurrency"] == 4
    assert report["config"]["total"] == 20

    # All 20 submitted ok against the in-memory queue.
    assert report["submit"]["succeeded"] == 20
    assert report["submit"]["failed"] == 0
    assert report["submit"]["latency_s"]["count"] == 20

    # Conservation: submitted_ok == terminal + timeouts + poll_errors.
    drn = report["drain"]
    assert drn["terminal_total"] == 20
    assert drn["timeouts"] == 0
    assert drn["poll_errors"] == 0
    assert report["accounting"]["balanced"] is True
    assert report["accounting"]["accounted"] == report["accounting"]["submitted_ok"] == 20

    # Default mix is all-success.
    assert drn["status_histogram"] == {"success": 20}
    # End-to-end latency block populated.
    assert drn["end_to_end_latency_s"]["count"] == 20

    # KEDA watchlist present for the operator running a real soak.
    assert report["azure_keda_watchlist"]
    assert any("KEDA" in note for note in report["azure_keda_watchlist"])


def test_local_run_status_mix_histogram() -> None:
    agg = asyncio.run(
        lt.run_local(
            agent="a",
            payload={"text": "x"},
            concurrency=4,
            total=10,
            status_mix={"success": 0.6, "error": 0.4},
        )
    )
    report = lt.build_report(agg, config={"mode": "local"}, wall_clock_s=0.5)
    hist = report["drain"]["status_histogram"]
    assert hist == {"error": 4, "success": 6}
    assert report["accounting"]["balanced"] is True
    assert report["drain"]["terminal_total"] == 10


# ---------------------------------------------------------------------------
# Deployed polling path — bad/slow responses are COUNTED, never raised
# ---------------------------------------------------------------------------


def test_poll_counts_repeated_failures_not_raises() -> None:
    """A status endpoint that keeps 500-ing is counted as a poll error."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Every status GET returns a 500 → _fetch_job_status yields None.
        return httpx.Response(500, json={"detail": "boom"})

    async def run() -> lt.JobResult:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            import time  # noqa: PLC0415

            return await lt._poll_to_terminal(
                client,
                job_id="j1",
                submitted_at=time.perf_counter(),
                timeout_s=30.0,
                poll_interval_s=0.0,
            )

    result = asyncio.run(run())
    # Did not raise; surfaced as a poll error (terminal None, not timed out).
    assert result.terminal_status is None
    assert result.timed_out is False
    assert result.error == "repeated poll failures"

    # And the aggregator counts it.
    agg = lt.Aggregator()
    agg.record_job(result)
    assert agg.poll_errors == 1
    assert agg.status_histogram == {}


def test_poll_times_out_when_never_terminal() -> None:
    """A job stuck in 'running' past the timeout is counted as a timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "running"})

    async def run() -> lt.JobResult:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            import time  # noqa: PLC0415

            # Already-elapsed deadline → first non-terminal poll trips timeout.
            return await lt._poll_to_terminal(
                client,
                job_id="j2",
                submitted_at=time.perf_counter() - 100.0,
                timeout_s=1.0,
                poll_interval_s=0.0,
            )

    result = asyncio.run(run())
    assert result.timed_out is True
    assert result.terminal_status is None

    agg = lt.Aggregator()
    agg.record_job(result)
    assert agg.timeouts == 1


def test_poll_reaches_terminal_via_v1_then_falls_back_to_alias() -> None:
    """404 on /api/v1/jobs falls through to the unversioned /jobs alias."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.startswith("/api/v1/jobs"):
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"status": "success"})

    async def run() -> lt.JobResult:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            import time  # noqa: PLC0415

            return await lt._poll_to_terminal(
                client,
                job_id="j3",
                submitted_at=time.perf_counter(),
                timeout_s=5.0,
                poll_interval_s=0.0,
            )

    result = asyncio.run(run())
    assert result.terminal_status == "success"
    assert result.drain_s is not None
    # Tried the versioned route first, then the alias.
    assert any(p.startswith("/api/v1/jobs") for p in calls)
    assert any(p == "/jobs/j3" for p in calls)


def test_submit_failure_is_counted_not_raised() -> None:
    """A non-202 submit response is counted as a submit failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    async def run() -> lt.SubmitResult:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            return await lt._submit_one(client, agent="a", payload={"text": "x"}, mock=True)

    result = asyncio.run(run())
    assert result.ok is False
    assert result.job_id is None
    assert "503" in (result.error or "")

    agg = lt.Aggregator()
    agg.record_submit(result)
    assert agg.submit_failures == 1


def test_run_deployed_end_to_end_with_mock_transport() -> None:
    """Full deployed path against a mock transport: submit + poll + report."""
    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/runs"):
            jid = f"job-{len(submitted)}"
            submitted.append(jid)
            return httpx.Response(202, json={"job_id": jid, "status": "queued"})
        # Status poll → immediately terminal.
        return httpx.Response(200, json={"status": "success"})

    async def run() -> lt.Aggregator:
        # Patch the AsyncClient inside run_deployed to use our transport.
        real_async_client = httpx.AsyncClient

        def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_async_client(
                base_url=str(kwargs.get("base_url", "http://test")),
                transport=httpx.MockTransport(handler),
            )

        # Monkeypatch via attribute on the module's httpx reference.
        orig = lt.httpx.AsyncClient
        lt.httpx.AsyncClient = factory  # type: ignore[assignment, misc]
        try:
            return await lt.run_deployed(
                target_url="http://test",
                api_key="k",
                agent="demo",
                payload={"text": "hi"},
                mock=True,
                concurrency=4,
                total=12,
                duration_s=None,
                timeout_s=5.0,
                poll_interval_s=0.0,
                request_timeout_s=5.0,
            )
        finally:
            lt.httpx.AsyncClient = orig  # type: ignore[misc]

    agg = asyncio.run(run())
    report = lt.build_report(agg, config={"mode": "deployed"}, wall_clock_s=1.0)
    assert report["submit"]["succeeded"] == 12
    assert report["submit"]["failed"] == 0
    assert report["drain"]["status_histogram"] == {"success": 12}
    assert report["accounting"]["balanced"] is True


# ---------------------------------------------------------------------------
# CLI arg validation
# ---------------------------------------------------------------------------


def test_main_requires_a_mode() -> None:
    with pytest.raises(SystemExit):
        lt.main(["--total", "5"])


def test_input_json_must_be_object() -> None:
    with pytest.raises(SystemExit):
        lt._resolve_payload("[1, 2, 3]")


def test_input_json_bad_json() -> None:
    with pytest.raises(SystemExit):
        lt._resolve_payload("{not json")


def test_local_rejects_duration() -> None:
    with pytest.raises(SystemExit):
        lt.main(["--local", "--duration", "5"])
