# MDK Certification Suite

A validation suite that runs **real** MDK workflows to prove the platform is
production-ready — governance, Temporal durability, HITL, tracing, retries,
policy enforcement, cost tracking, audit — not just "it can run a chatbot".

## The testing contract (the real/simulated boundary)

- **Real:** the agents, the workflow, Temporal durable execution, the governance
  shadow + policy enforcement, guardrails, tracing, HITL pause/resume.
- **Simulated:** only the *external SaaS side-effects* (email, ServiceNow, SAP,
  ERP, Slack, identity provisioning). Each call records an auditable row to a
  SQLite ledger (`harness/sim_systems.py`) instead of hitting a live system — so
  scenarios are repeatable, have no external creds, and can assert *what the
  workflow did to the world*. The ledger IS an audit trail.
- **Deterministic:** agent outputs are seeded so platform assertions are stable
  (we assert "the >$5000 branch triggered Director approval", not LLM prose).
  A `--live` mode runs the same bundle against a real LLM for demos.

## Layout

```
harness/
  sim_systems.py   # the simulated-systems ledger + per-system skill entrypoints
  asserts.py       # platform-capability assertions (governance/HITL/cost/side-effects)
  cert_metrics.py  # certify() → the mdk.certification.scenario metric
  driver.py        # the suite driver: cases.yaml → API runs → capability verdicts
run_suite.py       # entrypoint: python -m certification.run_suite
scenarios/
  <name>/          # a real MDK project bundle + cases.yaml (the case spec)
```

## Running the suite

The driver runs each scenario's `cases.yaml` against the **deployed dev
runtime** end-to-end (submit → poll job → resume HUMAN pauses → read the
terminal fact back from the ADR 096 observability surface):

```bash
MDK_DEV_KEY=<dev bearer token> uv run python -m certification.run_suite --target dev
```

Options: `--scenario <name>` (filter), `--json` (machine-readable summary),
`--base-url` / env `MDK_DEV_API_URL` (override the dev API), `--fact-timeout`
(seconds to wait for a terminal fact per case, default 180). `--target local`
is **deferred** — it errors loudly instead of half-simulating a worker +
Temporal stack. Exit code: `0` = no capability failed, `1` = at least one
failure, `2` = configuration error.

Every case the driver submits is stamped, at submit time, with a
`certification: {case, scenario}` key merged into the workflow input (the
state schemas are `additionalProperties: true`, and agents only ever see
their input-schema projection of state, so the key changes nothing about
execution). It lands in the run's `initial_state`, so you can **filter test
traffic by searching the input for `certification`** — in the Temporal UI's
workflow input, in `workflow_runs.initial_state`, and in Langfuse.

The driver deliberately reads results back from
`GET /api/v1/observability/facts?kind=workflow_run` — dogfooding the one
integration surface the platform exposes (ADR 096) rather than internal
endpoints. (The single-run `GET /api/v1/workflow-runs/{id}` is not used; the
HITL queue is found via the `?status=paused` list.)

## What each capability asserts

Each capability verdict is one `cert_metrics.certify(scenario, capability)`
block per case:

| capability | asserts |
| --- | --- |
| `durable-execution` | a terminal `workflow_run` fact with the expected status appeared within the timeout (across pause/resume on Temporal) |
| `decision-routing` | the fact's `route` equals the expectation — honestly `null` for the expense workflow, whose decision node routes without writing `tier`/`route` into state — plus `final_state_has`/`final_state_lacks` markers from the workflow-runs list (e.g. `erp_result` present only on approve paths, absent on reject) |
| `hitl` | the run paused **at the expected node** (the `?status=paused` queue) and the signalled decision resumed it; cases with no gate record an honest skip. A step with `wait_timeout: true` (ADR 062 D4 — the approval-timeout scenario) instead observes the pause and deliberately does **not** signal: the gate's durable timer must fire and route the run itself, and the next pause/fact poll waits it out. Such cases set a per-case `timeout_s` to budget the real wall clock above the suite defaults |
| `decision-routing` | the fact's `route` equals the expectation — honestly `null` for the expense workflow, whose decision node routes without writing `tier`/`route` into state — plus `final_state_has`/`final_state_lacks` markers from the workflow-runs list (e.g. `erp_result` present only on approve paths, absent on reject), and value-level `final_state_contains`/`final_state_omits` substring markers scoped to one state key (the B2 redaction scenarios prove `[EMAIL]`/`[SSN]` tokens present and raw PII absent in `redacted_text`) |
| `hitl` | the run paused **at the expected node** (the `?status=paused` queue) and the signalled decision resumed it; cases with no gate record an honest skip |
| `cost` | `fact.cost_usd > 0` — only when a case opts in. The expense cases do **not**: workflow_run facts carry `cost_usd=0` by design (per-node rollup is a reader-side join, ADR 096) and the Temporal path emits no per-node `run` facts yet, so the column shows SKIP rather than a green-washed pass |
| `governance` | the terminal fact's `governance_effect` (ADR 096) is **non-null** — a governance gate actually evaluated on the run, proving the deployed worker loaded the bundled policy (`workflows/expense-approval/policy.yaml`, baked to the image WORKDIR as `project.yaml`) — and equals the case's `expect.governance` (`allow` / `warn`; `deny` is excluded because an enforced deny never produces a terminal-success fact). The expense cases expect `allow`: the MODEL allowlist matches the agents' providers and the COST ceiling sits above the default per-run budget, so the gates evaluate without warning. Cases without `expect.governance` record an honest skip |
| `side-effects` | sim-ledger expectations (`asserts.assert_side_effect` / `assert_no_side_effect`) against `sim_side_effects` in the **shared** DB — evaluated only when `MOVATE_PG_URL`/`MOVATE_DB_URL` points at the deployed Postgres; SKIPPED (with a note) otherwise. Positive ERP expectations are also not yet authored for expense-approval: the deployed `erp-poster` agent returns an LLM confirmation and does not call the `sim-erp` skill, so only the reject case's *no-erp-row* expectation is honest today |

A capability's scenario verdict folds over its cases: any fail → FAIL, else
any pass → PASS, else SKIP. Skips never emit a metric datapoint — they are a
local-matrix verdict only.

## Metrics → the Grafana matrix

`run_suite` calls `movate.tracing.metrics.init_metrics()` at startup, so every
verdict emits `mdk.certification.scenario{scenario, capability, status}` —
**when an OTLP sink is configured** (`OTEL_EXPORTER_OTLP_*`); otherwise it is
a fail-soft no-op. A laptop cannot reach the internal collector, so the
**mdk - certification** Grafana dashboard only fills when the suite runs
in-env — the follow-up is an ACA job that runs the driver on a schedule. The
printed matrix + exit code are the local source of truth either way.

## Capability coverage (the certification matrix)

Each scenario maps to capabilities it proves. The suite is "certified" when the
matrix (governance · temporal · HITL · tracing · retries · parallelism ·
approvals · policy · KB · evals · cost · audit) is fully green across scenarios.
