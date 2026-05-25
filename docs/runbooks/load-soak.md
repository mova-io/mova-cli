# Load / soak testing the job-queue (`scripts/loadtest.py`)

> **🔒 Production-readiness gate.** A *real* soak runs against a **deployed**
> runtime with a worker pool draining the queue (and, on Azure, KEDA scaling
> those workers). This runbook's `--local` mode exercises the harness itself in
> CI without Azure — it is **not** a substitute for a real soak. Sign-off on
> production readiness requires a captured baseline from a deployed target.

`scripts/loadtest.py` drives the **async submit → queue → worker-drain →
terminal-status** path under controlled concurrency, captures a throughput +
latency baseline, prints a summary table, and writes a machine-readable JSON
report. It is the harness an operator runs before calling the platform
production-ready.

It uses only the runtime's HTTP API (`httpx`, already a shipped dependency) — no
new dependencies, no access to the runtime internals.

---

## Two modes

| Mode | Flag | Hits | Needs | Use |
|---|---|---|---|---|
| **Deployed** | `--target-url <url>` | `POST /api/v1/agents/{name}/runs` then polls `GET /api/v1/jobs/{id}` (falls back to the unversioned `/jobs/{id}` alias) | a live runtime + worker pool + bearer token | the **real soak** 🔒 |
| **Local** | `--local` | in-process `InMemoryStorage` enqueue → claim → terminal loop | nothing (no server, keys, or Azure) | exercising the harness in CI |

Both modes produce the **same report shape**, so the measurement + reporting
code is validated by the local path before you point it at a deployed target.

---

## Running it

### Local smoke (CI, no server)

```bash
uv run python scripts/loadtest.py --local --total 200 --concurrency 16 \
    --out /tmp/loadtest-local.json
```

This enqueues 200 jobs against an in-memory queue, drains them, and writes the
report. The process exits non-zero if the **accounting doesn't balance** (every
submitted job must end as a terminal status, a timeout, or a poll error) — so
you can gate CI on it.

### Real soak against a deployed runtime

```bash
# Register / resolve a target + key the usual way, then:
uv run python scripts/loadtest.py \
    --target-url https://movate-dev-api.<...>.azurecontainerapps.io \
    --agent faq-bot \
    --api-key "$MDK_API_KEY" \
    --concurrency 32 \
    --total 2000 \
    --mock \
    --input-json '{"text": "ping"}' \
    --out soak-baseline.json
```

Duration-bounded soak (submit at steady concurrency for a fixed window):

```bash
uv run python scripts/loadtest.py \
    --target-url https://... --agent faq-bot --api-key "$MDK_API_KEY" \
    --concurrency 32 --duration 600 --mock
```

---

## Arguments

| Arg | Default | Meaning |
|---|---|---|
| `--target-url` | — | Base URL of a deployed runtime (deployed mode). Mutually exclusive with `--local`. |
| `--local` | off | In-process mode (no server). |
| `--api-key` | `$MDK_API_KEY` / `$MOVATE_API_KEY` | Bearer token for the deployed target. |
| `--agent` | `loadtest-agent` | Agent name to submit runs to. |
| `--concurrency` | `8` | Max in-flight submits. |
| `--total` | `100` | Total runs to submit. Mutually exclusive with `--duration`. |
| `--duration` | — | Soak window in seconds (deployed mode only). |
| `--input-json` | `{"text": "loadtest ping"}` | Agent input payload as a JSON object. **Must satisfy the agent's input schema.** |
| `--mock` | off | Set `mock:true` on each submission (no provider keys/cost). |
| `--timeout` | `120` | Per-job seconds from submit→terminal before counting a timeout. |
| `--poll-interval` | `1.0` | Seconds between job-status polls. |
| `--request-timeout` | `30` | Per-HTTP-request timeout. |
| `--out` | `./loadtest-report.json` | JSON report path. |

> **`--mock` caveat.** Server-side, `mock:true` is only honoured on the inline
> `?wait=true` execution path; the async/worker path uses the worker's own
> provider configuration. `--mock` keeps the *submission* cheap and avoids
> provider keys, but the agent's input must still satisfy its declared schema.
> For a true zero-cost queue-drain soak, point at an agent whose worker is
> itself configured for the mock provider, or accept that runs execute against
> the deployed model.

---

## Reading the report

The summary table prints submit + drain stats; the JSON (`--out`) carries the
full detail. Schema (`schema_version: 1`):

```jsonc
{
  "schema_version": 1,
  "config": { "mode": "...", "agent": "...", "concurrency": N,
              "total": N, "duration_s": null, "mock": true,
              "timeout_s": 120.0, "poll_interval_s": 1.0,
              "input_keys": ["text"], "target_url": "..." },
  "wall_clock_s": 12.34,
  "submit": {
    "attempted": N, "succeeded": N, "failed": N,
    "rate_per_s": 162.3,                       // submit throughput
    "latency_s": { "count", "min", "max", "mean", "p50", "p95", "p99" },
    "sample_errors": [ "submit HTTP 503", ... ] // first ~10 distinct failures
  },
  "drain": {
    "terminal_total": N,
    "timeouts": N,                             // jobs that never went terminal
    "poll_errors": N,                          // status endpoint kept failing
    "status_histogram": { "success": N, "error": N, "safety_blocked": N,
                          "dead_letter": N, "cancelled": N },
    "end_to_end_latency_s": { ... }            // submit -> terminal (queue drain)
  },
  "accounting": {
    "submitted_ok": N, "accounted": N, "balanced": true
  },
  "azure_keda_watchlist": [ "...", ... ]
}
```

Key signals:

- **`submit.rate_per_s`** — how fast the API accepts work (202s). This is the
  *ingest* ceiling, independent of how fast workers drain.
- **`drain.end_to_end_latency_s.p95/p99`** vs **`submit.latency_s.p95/p99`** — a
  large gap is **queue wait**, not submit slowness. The end-to-end p99 is your
  real "time to result under load".
- **`drain.status_histogram`** — watch `error` / `safety_blocked` /
  `dead_letter`. A growing `dead_letter` tally under sustained load means jobs
  are exhausting their retry budget (provider errors, per-job timeouts).
- **`drain.timeouts`** — jobs that never reached terminal within `--timeout`.
  Non-zero means the worker pool isn't keeping up (or jobs are genuinely
  hung — cross-check `mdk jobs list --status running`).
- **`accounting.balanced`** — must be `true`. The process exits non-zero if not.

---

## What to watch on Azure (KEDA autoscale)

The harness only drives the API; on a deployed Azure Container Apps target,
watch the worker side in parallel (the report echoes this list under
`azure_keda_watchlist`):

- **KEDA replica scale-up.** As queue depth rises, the worker ScaledObject
  should add replicas. Watch `az containerapp replica list` (or KEDA metrics) —
  replica count should climb with queue depth and shrink as it drains.
- **Drain rate vs submit rate.** If submit rate stays high but `terminal_total`
  lags far behind, the worker pool is under-provisioned — raise `maxReplicas`
  (or the per-replica concurrency) and re-baseline.
- **Dead-letter under sustained load.** A nonzero, *growing* `dead_letter`
  count is the canary for systemic failure (provider throttling, downstream
  timeouts). Triage with `mdk jobs list --status dead_letter`.
- **Scale-to-zero / cold start.** After an idle period, the first soak burst
  pays a cold-start latency tax as KEDA scales the worker pool up from zero —
  expect a fat end-to-end p99 on the first run, recovering as replicas warm.

---

## Baseline capture procedure (production-readiness sign-off 🔒)

1. **Deploy** the target runtime and confirm the worker pool is draining
   (`mdk jobs list` shows jobs moving to terminal).
2. **Warm up** with a small run so KEDA isn't scaling from zero during the
   measured pass:
   `uv run python scripts/loadtest.py --target-url <url> --agent <a> --total 50`.
3. **Capture** a steady-state baseline at your target concurrency:
   `... --concurrency 32 --total 2000 --mock --out baseline-$(date +%F).json`.
4. **Record** in your release notes: `submit.rate_per_s`,
   `drain.end_to_end_latency_s.p95/p99`, the `status_histogram`, peak KEDA
   replica count, and the wall-clock. These are the numbers a future regression
   is measured against.
5. **Soak** for an extended window to surface leaks / drift:
   `... --concurrency 16 --duration 1800` and confirm latency + dead-letter
   counts stay flat (no upward creep).
6. **Sign off** only if: `accounting.balanced` is `true`, `timeouts == 0`,
   `dead_letter` is flat, and end-to-end p99 is within your SLO.
