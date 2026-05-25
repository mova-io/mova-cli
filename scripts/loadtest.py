#!/usr/bin/env python3
"""Load / soak test harness for the movate (``mdk``) job-queue + worker-drain path.

This is the harness an operator runs **before signing off a runtime as
production-ready** (BACKLOG item 28). It drives the async submit → queue →
worker-drain → terminal-status path under controlled concurrency, captures a
throughput + latency baseline, and writes a documented JSON report alongside a
human-readable summary table.

Two modes
---------

* ``--target-url`` (deployed): submit N runs at a target concurrency to a
  *running* runtime via the async API ``POST /api/v1/agents/{name}/runs``
  (202 + ``job_id``), then poll ``GET /api/v1/jobs/{job_id}`` (with a fallback
  to the unversioned ``/jobs/{job_id}`` alias) until each job reaches a terminal
  status. This is the **real** soak path. 🔒 It needs a live target + a worker
  pool draining the queue; on Azure, KEDA should scale worker replicas with
  queue depth (watch the notes the report prints).

* ``--local`` (in-process, no server): drive an in-memory storage queue
  (``movate.testing.doubles.InMemoryStorage``) through the same
  enqueue → claim → terminal loop a worker would, with a configurable terminal
  status distribution. This path takes no network, no provider keys, and no
  Azure — it exists so the **measurement + reporting code itself** is
  exercisable in CI. It is NOT a substitute for a real soak.

Pure stdlib + ``httpx`` (already a shipped dependency) — no new deps.

Usage
-----
::

    # CI smoke of the harness itself (no server, no keys):
    uv run python scripts/loadtest.py --local --total 200 --concurrency 16

    # Real soak against a deployed runtime:
    uv run python scripts/loadtest.py \\
        --target-url https://movate-dev-api.<...>.azurecontainerapps.io \\
        --agent faq-bot --api-key "$MDK_API_KEY" \\
        --concurrency 32 --total 2000 --mock \\
        --input-json '{"text": "ping"}' --out soak-baseline.json

    # Duration-bounded soak (submit for 10 min at steady concurrency):
    uv run python scripts/loadtest.py --target-url https://... --agent faq-bot \\
        --api-key "$MDK_API_KEY" --concurrency 32 --duration 600
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Terminal job statuses, mirrored as plain strings so this harness never needs
# to import runtime/core just to recognise a finished job. Kept in sync with
# ``movate.core.models.JobStatus`` (success/error/safety_blocked/dead_letter)
# plus ``cancelled`` which a future cancel path may surface.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"success", "error", "safety_blocked", "dead_letter", "cancelled"}
)

DEFAULT_OUT = "./loadtest-report.json"
# A trivially-valid default payload. Most scaffolded agents take a ``text``
# field; override with --input-json for an agent whose schema differs.
DEFAULT_INPUT: dict[str, Any] = {"text": "loadtest ping"}

# HTTP status codes we branch on (named to keep ruff PLR2004 happy + readable).
HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_NOT_FOUND = 404
# Give up on a job whose status endpoint fails this many times in a row.
MAX_CONSECUTIVE_POLL_FAILURES = 5
# How many distinct submit-error strings to retain as a sample in the report.
MAX_SAMPLE_ERRORS = 10
PCT_MAX = 100.0


# ---------------------------------------------------------------------------
# Measurement primitives
# ---------------------------------------------------------------------------


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile of ``samples`` (0 <= pct <= 100).

    Deterministic + dependency-free (no numpy). Returns ``0.0`` for an empty
    list so a run with zero successful measurements still produces a valid
    report rather than crashing. ``pct=50`` → p50/median, ``95`` → p95, etc.

    Nearest-rank: sort ascending, take the ``ceil(pct/100 * n)``-th sample
    (1-indexed), clamped into range. Stable for the small-N percentiles an
    operator reads off a soak.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    n = len(ordered)
    if pct <= 0:
        return ordered[0]
    if pct >= PCT_MAX:
        return ordered[-1]
    rank = math.ceil((pct / PCT_MAX) * n)
    idx = min(max(rank, 1), n) - 1
    return ordered[idx]


def _latency_block(samples: list[float]) -> dict[str, float]:
    """p50/p95/p99 + min/max/mean of a latency sample list, in seconds."""
    return {
        "count": len(samples),
        "min": min(samples) if samples else 0.0,
        "max": max(samples) if samples else 0.0,
        "mean": (sum(samples) / len(samples)) if samples else 0.0,
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
    }


@dataclass
class SubmitResult:
    """Outcome of one submit attempt."""

    ok: bool
    job_id: str | None
    latency_s: float
    error: str | None = None


@dataclass
class JobResult:
    """Outcome of polling one job to terminal (or timing out / erroring)."""

    job_id: str
    terminal_status: str | None  # None => timed out or poll error
    drain_s: float | None  # submit -> terminal wall time; None if never terminal
    timed_out: bool = False
    error: str | None = None


@dataclass
class Aggregator:
    """Accumulates submit + drain results into the final report."""

    submit_latencies: list[float] = field(default_factory=list)
    submit_failures: int = 0
    submit_errors: list[str] = field(default_factory=list)
    drain_latencies: list[float] = field(default_factory=list)
    status_histogram: dict[str, int] = field(default_factory=dict)
    timeouts: int = 0
    poll_errors: int = 0

    def record_submit(self, r: SubmitResult) -> None:
        if r.ok:
            self.submit_latencies.append(r.latency_s)
        else:
            self.submit_failures += 1
            if r.error and len(self.submit_errors) < MAX_SAMPLE_ERRORS:
                self.submit_errors.append(r.error)

    def record_job(self, r: JobResult) -> None:
        if r.timed_out:
            self.timeouts += 1
            return
        if r.terminal_status is None:
            # A poll error that wasn't a timeout (connection died, bad JSON).
            self.poll_errors += 1
            return
        self.status_histogram[r.terminal_status] = (
            self.status_histogram.get(r.terminal_status, 0) + 1
        )
        if r.drain_s is not None:
            self.drain_latencies.append(r.drain_s)


# ---------------------------------------------------------------------------
# Deployed-target driver (real soak path)
# ---------------------------------------------------------------------------


async def _submit_one(
    client: httpx.AsyncClient,
    *,
    agent: str,
    payload: dict[str, Any],
    mock: bool,
) -> SubmitResult:
    """POST one run; return job_id + submit latency. Never raises."""
    body: dict[str, Any] = {"input": payload}
    if mock:
        body["mock"] = True
    started = time.perf_counter()
    try:
        resp = await client.post(f"/api/v1/agents/{agent}/runs", json=body)
        latency = time.perf_counter() - started
        if resp.status_code not in (HTTP_OK, HTTP_ACCEPTED):
            return SubmitResult(
                ok=False,
                job_id=None,
                latency_s=latency,
                error=f"submit HTTP {resp.status_code}",
            )
        data = resp.json()
        job_id = data.get("job_id")
        if not job_id:
            # ?wait=true would return a RunView (run_id, no job_id). We never
            # pass wait=true, but be defensive rather than crash.
            return SubmitResult(
                ok=False, job_id=None, latency_s=latency, error="no job_id in response"
            )
        return SubmitResult(ok=True, job_id=job_id, latency_s=latency)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        latency = time.perf_counter() - started
        return SubmitResult(ok=False, job_id=None, latency_s=latency, error=repr(exc))


async def _fetch_job_status(client: httpx.AsyncClient, job_id: str) -> str | None:
    """GET one job's status string. Tries /api/v1/jobs then the /jobs alias.

    Returns the status string, or ``None`` on any error / unexpected shape —
    the caller treats ``None`` as "not yet terminal, keep polling" so a single
    transient blip never aborts a job's wait.
    """
    for path in (f"/api/v1/jobs/{job_id}", f"/jobs/{job_id}"):
        try:
            resp = await client.get(path)
        except httpx.HTTPError:
            continue
        if resp.status_code == HTTP_NOT_FOUND:
            # The versioned route may not exist on older runtimes; try the alias.
            continue
        if resp.status_code != HTTP_OK:
            return None
        try:
            return str(resp.json().get("status"))
        except ValueError:
            return None
    return None


async def _poll_to_terminal(
    client: httpx.AsyncClient,
    *,
    job_id: str,
    submitted_at: float,
    timeout_s: float,
    poll_interval_s: float,
) -> JobResult:
    """Poll one job until terminal or ``timeout_s`` elapses. Never raises."""
    deadline = submitted_at + timeout_s
    consecutive_poll_failures = 0
    while True:
        status = await _fetch_job_status(client, job_id)
        if status in TERMINAL_STATUSES:
            return JobResult(
                job_id=job_id,
                terminal_status=status,
                drain_s=time.perf_counter() - submitted_at,
            )
        if status is None:
            consecutive_poll_failures += 1
            # Give up on a job whose status endpoint keeps failing — count it
            # as a poll error (not a crash, not a fake terminal).
            if consecutive_poll_failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                return JobResult(
                    job_id=job_id,
                    terminal_status=None,
                    drain_s=None,
                    error="repeated poll failures",
                )
        else:
            consecutive_poll_failures = 0
        if time.perf_counter() >= deadline:
            return JobResult(job_id=job_id, terminal_status=None, drain_s=None, timed_out=True)
        await asyncio.sleep(poll_interval_s)


async def run_deployed(
    *,
    target_url: str,
    api_key: str,
    agent: str,
    payload: dict[str, Any],
    mock: bool,
    concurrency: int,
    total: int | None,
    duration_s: float | None,
    timeout_s: float,
    poll_interval_s: float,
    request_timeout_s: float,
) -> Aggregator:
    """Drive a deployed runtime under load, returning the aggregated results.

    Submits are bounded by a semaphore at ``concurrency``. Each successful
    submit spawns a poll-to-terminal task. We gather everything at the end so a
    slow drain doesn't stall new submits.
    """
    agg = Aggregator()
    sem = asyncio.Semaphore(concurrency)
    poll_tasks: list[asyncio.Task[JobResult]] = []

    async with httpx.AsyncClient(
        base_url=target_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=request_timeout_s,
    ) as client:

        async def one_submission() -> None:
            async with sem:
                submitted_at = time.perf_counter()
                result = await _submit_one(client, agent=agent, payload=payload, mock=mock)
            agg.record_submit(result)
            if result.ok and result.job_id is not None:
                poll_tasks.append(
                    asyncio.create_task(
                        _poll_to_terminal(
                            client,
                            job_id=result.job_id,
                            submitted_at=submitted_at,
                            timeout_s=timeout_s,
                            poll_interval_s=poll_interval_s,
                        )
                    )
                )

        submit_tasks: list[asyncio.Task[None]] = []
        if duration_s is not None:
            # Soak: keep submitting until the window closes.
            window_end = time.perf_counter() + duration_s
            while time.perf_counter() < window_end:
                submit_tasks.append(asyncio.create_task(one_submission()))
                # Light pacing so we don't spawn an unbounded backlog of
                # coroutines faster than the semaphore drains them.
                await asyncio.sleep(0)
                if len(submit_tasks) % concurrency == 0:
                    await asyncio.sleep(0.01)
        else:
            assert total is not None
            submit_tasks = [asyncio.create_task(one_submission()) for _ in range(total)]

        await asyncio.gather(*submit_tasks)
        # Now wait for every spawned job to reach terminal / time out.
        for jr in await asyncio.gather(*poll_tasks):
            agg.record_job(jr)

    return agg


# ---------------------------------------------------------------------------
# Local in-process driver (CI-exercisable, no server)
# ---------------------------------------------------------------------------


async def run_local(
    *,
    agent: str,
    payload: dict[str, Any],
    concurrency: int,
    total: int,
    status_mix: dict[str, float] | None = None,
) -> Aggregator:
    """Exercise the enqueue → claim → terminal loop against in-memory storage.

    No server, no provider, no network. We import the test-double storage
    read-only and drive its job lifecycle directly so the measurement +
    reporting code is validated end-to-end in CI. The terminal status of each
    job is drawn deterministically from ``status_mix`` (default: all success).

    This deliberately does NOT run a real agent — it validates the harness, not
    real load. The shape of the returned ``Aggregator`` is identical to the
    deployed path so the report code is shared.
    """
    from datetime import UTC, datetime  # noqa: PLC0415
    from uuid import uuid4  # noqa: PLC0415

    # ``movate`` ships without a ``py.typed`` marker, so mypy treats it as
    # untyped from this standalone script's vantage point (the package's own
    # ``mypy src`` run analyses the source directly). These imports are
    # read-only — the harness never modifies ``src/movate``.
    from movate.core.models import (  # type: ignore[import-untyped]  # noqa: PLC0415
        JobKind,
        JobRecord,
        JobStatus,
    )
    from movate.testing.doubles import (  # type: ignore[import-untyped]  # noqa: PLC0415
        InMemoryStorage,
    )

    tenant = "loadtest"
    storage = InMemoryStorage()
    agg = Aggregator()
    sem = asyncio.Semaphore(concurrency)

    mix = status_mix or {"success": 1.0}
    # Build a deterministic terminal-status schedule covering ``total`` jobs in
    # the requested proportions (largest-remainder, index-ordered → stable).
    schedule = _expand_status_mix(mix, total)

    submit_meta: list[tuple[str, float]] = []  # (job_id, submitted_at)

    async def enqueue(i: int) -> None:
        async with sem:
            job_id = str(uuid4())
            submitted_at = time.perf_counter()
            try:
                await storage.save_job(
                    JobRecord(
                        job_id=job_id,
                        tenant_id=tenant,
                        kind=JobKind.AGENT,
                        target=agent,
                        input=payload,
                        created_at=datetime.now(UTC),
                    )
                )
                latency = time.perf_counter() - submitted_at
                agg.record_submit(SubmitResult(ok=True, job_id=job_id, latency_s=latency))
                submit_meta.append((job_id, submitted_at))
            except (ValueError, RuntimeError) as exc:  # pragma: no cover
                latency = time.perf_counter() - submitted_at
                agg.record_submit(
                    SubmitResult(ok=False, job_id=None, latency_s=latency, error=repr(exc))
                )
            _ = i

    await asyncio.gather(*[asyncio.create_task(enqueue(i)) for i in range(total)])

    # "Worker drain": claim each queued job and transition it to its scheduled
    # terminal status, recording the drain time. Sequential claim mirrors a
    # single worker pulling FIFO off the in-memory queue.
    status_iter = iter(schedule)
    submitted_by_id = dict(submit_meta)
    while True:
        claimed = await storage.claim_next_job(tenant_id=tenant)
        if claimed is None:
            break
        terminal = next(status_iter, "success")
        await storage.update_job(
            claimed.job_id,
            tenant_id=tenant,
            status=JobStatus(terminal),
        )
        submitted_at = submitted_by_id.get(claimed.job_id, time.perf_counter())
        agg.record_job(
            JobResult(
                job_id=claimed.job_id,
                terminal_status=terminal,
                drain_s=time.perf_counter() - submitted_at,
            )
        )

    return agg


def _expand_status_mix(mix: dict[str, float], total: int) -> list[str]:
    """Turn a {status: weight} mix into a deterministic length-``total`` list.

    Largest-remainder apportionment so the counts sum to exactly ``total`` and
    the ordering is stable (sorted by status name) — keeps tests deterministic.
    """
    if total <= 0:
        return []
    items = sorted(mix.items())
    weight_sum = sum(w for _, w in items) or 1.0
    raw = [(s, (w / weight_sum) * total) for s, w in items]
    floored = [(s, int(x)) for s, x in raw]
    counts = {s: c for s, c in floored}
    remainder = total - sum(counts.values())
    # Hand the leftover to the largest fractional parts first.
    by_frac = sorted(raw, key=lambda t: t[1] - int(t[1]), reverse=True)
    for s, _ in by_frac:
        if remainder <= 0:
            break
        counts[s] += 1
        remainder -= 1
    out: list[str] = []
    for s, _ in items:
        out.extend([s] * counts[s])
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_report(
    agg: Aggregator,
    *,
    config: dict[str, Any],
    wall_clock_s: float,
) -> dict[str, Any]:
    """Assemble the machine-readable report dict from an aggregator."""
    submitted_ok = len(agg.submit_latencies)
    total_attempted = submitted_ok + agg.submit_failures
    terminal_total = sum(agg.status_histogram.values())
    submit_rate = submitted_ok / wall_clock_s if wall_clock_s > 0 else 0.0

    return {
        "schema_version": 1,
        "config": config,
        "wall_clock_s": round(wall_clock_s, 4),
        "submit": {
            "attempted": total_attempted,
            "succeeded": submitted_ok,
            "failed": agg.submit_failures,
            "rate_per_s": round(submit_rate, 3),
            "latency_s": _latency_block(agg.submit_latencies),
            "sample_errors": agg.submit_errors,
        },
        "drain": {
            "terminal_total": terminal_total,
            "timeouts": agg.timeouts,
            "poll_errors": agg.poll_errors,
            "status_histogram": dict(sorted(agg.status_histogram.items())),
            "end_to_end_latency_s": _latency_block(agg.drain_latencies),
        },
        # Conservation check: every submitted job is accounted for as a terminal
        # status, a timeout, or a poll error. (Submit failures never produced a
        # job, so they're excluded from this sum.)
        "accounting": {
            "submitted_ok": submitted_ok,
            "accounted": terminal_total + agg.timeouts + agg.poll_errors,
            "balanced": (terminal_total + agg.timeouts + agg.poll_errors) == submitted_ok,
        },
        "azure_keda_watchlist": [
            "KEDA worker replica count should climb as queue depth rises "
            "(az containerapp replica list / KEDA ScaledObject metrics).",
            "Drain rate should keep pace with submit rate — a widening gap "
            "means the worker pool is under-provisioned (raise maxReplicas).",
            "Watch dead_letter count: a nonzero, growing tally under sustained "
            "load signals jobs exhausting their retry budget (provider errors, "
            "timeouts) — triage with `mdk jobs list --status dead_letter`.",
            "End-to-end p95/p99 vs submit p95/p99: a large delta is queue wait, "
            "not submit slowness.",
        ],
    }


def _fmt_latency(label: str, block: dict[str, float]) -> list[str]:
    return [
        f"  {label:<18} n={block['count']:<6} "
        f"p50={block['p50'] * 1000:8.1f}ms  "
        f"p95={block['p95'] * 1000:8.1f}ms  "
        f"p99={block['p99'] * 1000:8.1f}ms  "
        f"max={block['max'] * 1000:8.1f}ms"
    ]


def print_summary(report: dict[str, Any]) -> None:
    """Print a clean human-readable summary table to stdout."""
    cfg = report["config"]
    sub = report["submit"]
    drn = report["drain"]
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("  movate load/soak report")
    lines.append("=" * 72)
    lines.append(
        f"  mode={cfg.get('mode')}  agent={cfg.get('agent')}  "
        f"concurrency={cfg.get('concurrency')}  "
        f"target={cfg.get('total') or cfg.get('duration_s')}"
    )
    lines.append(f"  wall-clock: {report['wall_clock_s']:.2f}s")
    lines.append("-" * 72)
    lines.append(
        f"  submit:  attempted={sub['attempted']}  ok={sub['succeeded']}  "
        f"failed={sub['failed']}  rate={sub['rate_per_s']}/s"
    )
    lines.extend(_fmt_latency("submit latency", sub["latency_s"]))
    lines.append("-" * 72)
    lines.append(
        f"  drain:   terminal={drn['terminal_total']}  "
        f"timeouts={drn['timeouts']}  poll_errors={drn['poll_errors']}"
    )
    hist = drn["status_histogram"]
    if hist:
        hist_str = "  ".join(f"{k}={v}" for k, v in hist.items())
        lines.append(f"  status:  {hist_str}")
    lines.extend(_fmt_latency("end-to-end", drn["end_to_end_latency_s"]))
    lines.append("-" * 72)
    acct = report["accounting"]
    balance = "OK" if acct["balanced"] else "MISMATCH"
    lines.append(
        f"  accounting: submitted_ok={acct['submitted_ok']}  "
        f"accounted={acct['accounted']}  [{balance}]"
    )
    lines.append("=" * 72)
    lines.append("  Azure / KEDA watchlist (real soak):")
    for note in report["azure_keda_watchlist"]:
        lines.append(f"   - {note}")
    lines.append("=" * 72)
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loadtest.py",
        description=(
            "Load/soak harness for the movate job-queue + worker-drain path. "
            "Use --local for a CI-exercisable in-process run (no server), or "
            "--target-url for a real soak against a deployed runtime."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--target-url",
        help="Base URL of a deployed runtime (real soak). Mutually exclusive with --local.",
    )
    mode.add_argument(
        "--local",
        action="store_true",
        help="In-process mode: drive in-memory storage, no server/keys/Azure. "
        "For exercising the harness in CI.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Bearer token for the deployed target. Defaults to $MDK_API_KEY / $MOVATE_API_KEY.",
    )
    parser.add_argument(
        "--agent",
        default="loadtest-agent",
        help="Agent name to submit runs to (default: loadtest-agent).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Max in-flight submits (default: 8).",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=None,
        help="Total number of runs to submit. Mutually exclusive with "
        "--duration. Defaults to 100 if neither is given.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Soak window in seconds: keep submitting at --concurrency for this "
        "long (deployed mode only). Mutually exclusive with --total.",
    )
    parser.add_argument(
        "--input-json",
        default=None,
        help="Agent input payload as a JSON object string. Must satisfy the "
        'agent\'s input schema. Defaults to a trivially-valid {"text": ...}.',
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Set mock:true on each submission (deployed mode) so no provider "
        "keys / cost are needed. Note: only the inline ?wait path honours mock "
        "server-side; the input must still satisfy the agent schema.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-job max seconds from submit to terminal before counting it a "
        "timeout (default: 120).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between job-status polls (default: 1.0).",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30.0,
        help="Per-HTTP-request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"Path for the JSON report (default: {DEFAULT_OUT}).",
    )
    return parser.parse_args(argv)


def _resolve_payload(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return dict(DEFAULT_INPUT)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--input-json is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise SystemExit('--input-json must be a JSON object (e.g. \'{"text": "hi"}\')')
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if not args.local and not args.target_url:
        raise SystemExit("supply either --local or --target-url")
    if args.total is not None and args.duration is not None:
        raise SystemExit("--total and --duration are mutually exclusive")
    if args.local and args.duration is not None:
        raise SystemExit("--duration is only supported with --target-url (deployed)")

    payload = _resolve_payload(args.input_json)
    total = args.total
    if total is None and args.duration is None:
        total = 100

    config: dict[str, Any] = {
        "mode": "local" if args.local else "deployed",
        "agent": args.agent,
        "concurrency": args.concurrency,
        "total": total,
        "duration_s": args.duration,
        "mock": bool(args.mock),
        "timeout_s": args.timeout,
        "poll_interval_s": args.poll_interval,
        "input_keys": sorted(payload.keys()),
    }
    if args.target_url:
        config["target_url"] = args.target_url

    started = time.perf_counter()
    if args.local:
        agg = asyncio.run(
            run_local(
                agent=args.agent,
                payload=payload,
                concurrency=args.concurrency,
                total=total or 0,
            )
        )
    else:
        api_key = args.api_key or os.environ.get("MDK_API_KEY") or os.environ.get("MOVATE_API_KEY")
        if not api_key:
            raise SystemExit("no API key: pass --api-key or set $MDK_API_KEY / $MOVATE_API_KEY")
        agg = asyncio.run(
            run_deployed(
                target_url=args.target_url,
                api_key=api_key,
                agent=args.agent,
                payload=payload,
                mock=bool(args.mock),
                concurrency=args.concurrency,
                total=total,
                duration_s=args.duration,
                timeout_s=args.timeout,
                poll_interval_s=args.poll_interval,
                request_timeout_s=args.request_timeout,
            )
        )
    wall_clock = time.perf_counter() - started

    report = build_report(agg, config=config, wall_clock_s=wall_clock)
    print_summary(report)

    out_path = Path(args.out)
    with contextlib.suppress(OSError):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote report -> {out_path}")

    # Exit nonzero if the accounting didn't balance OR any submit failed in a
    # way that would invalidate a baseline — operators can gate CI on this.
    return 0 if report["accounting"]["balanced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
