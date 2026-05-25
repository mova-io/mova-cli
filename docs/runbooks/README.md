# movate operator runbooks

How to **configure, operate, and troubleshoot** a deployed movate (`mdk`),
written from the operator's point of view. Each runbook is grounded in the
actual CLI commands, `/api/v1` endpoints, env vars, scopes, and bicep params —
verified against the source.

## Start here

| Runbook | Covers |
|---|---|
| [`orchestration.md`](orchestration.md) | **Scheduler** (`mdk schedule`, `mdk scheduler-tick`, the cron ACA Job), **event/webhook triggers** (`mdk trigger`, the HMAC fire endpoint), **durable + HITL** workflow pause/resume (`mdk workflow runs/signal`). ADR 017. |
| [`improvement-loop.md`](improvement-loop.md) | **Harvest** (`mdk eval harvest`), **continuous eval + drift** (`mdk eval-schedule`, `mdk eval-scheduler-tick`, per-dimension drift), **canary** champion/challenger (`mdk canary`). ADR 016. |
| [`serving-and-keys.md`](serving-and-keys.md) | **Batch** (`mdk batch`), **SSE streaming** (`mdk run --stream`), **job/run polling** (`/api/v1/jobs` + unversioned alias), **auth** (scopes, `mdk auth create-key/rotate-key/revoke-all`), **rate limits**, and the **proposed** per-tenant BYOK surface (ADR 018, not yet shipped). ADR 012/013/018. |
| [`load-soak.md`](load-soak.md) | **Load / soak harness** (`scripts/loadtest.py`) — drive the job-queue + worker-drain (and KEDA autoscale) path under load, capture a throughput/latency baseline, read the JSON report. The production-readiness sign-off gate (🔒 real soak needs a deployed target). |
| [`dr-backup.md`](dr-backup.md) | **Disaster recovery** — **Azure Postgres PITR** (the primary DR: automated backups + point-in-time restore + the restore procedure) and the **`mdk export` / `mdk import`** control-plane escape hatch (agent registry, api keys, canary, schedules, BYOK provider keys), plus a full restore drill. (🔒 real PITR needs a deployed server). |

## Deploying in the first place

These runbooks assume a runtime is already deployed. For provisioning Azure and
minting the first key, see:

* [`../azure-bootstrap.md`](../azure-bootstrap.md) — end-to-end Azure Container
  Apps bootstrap (RG + service principal + bicep + first runtime key +
  auto-deploy on `release/*`).
* [`../azure-credentials-setup.md`](../azure-credentials-setup.md) — credential
  setup detail.

## Conventions across the runbooks

* **Targets:** CLI commands that hit a deployed runtime resolve a target via
  per-command `--target`/`-t` > top-level `-t`/`MOVATE_TARGET` > the active
  target in `~/.movate/config.yaml`. Register one with
  `mdk config add-target <name> --url <url> --key-env <ENV_VAR>`.
* **Additive + default-off:** schedules, triggers, canary, continuous eval, and
  the scheduler ACA Job all do nothing until an operator opts in per agent/env.
* **Everything is a job:** schedules, triggers, batch rows, and workflow
  continuations all enqueue ordinary `JobKind.AGENT`/`WORKFLOW`/`EVAL` jobs, so
  they're observable + retryable through the same worker — and a worker must be
  draining the queue for any of them to execute.
* **Scopes (ADR 013):** `read` / `run` / `eval` / `kb:write` / `admin` /
  `fleet-admin`, least-privilege, checked per endpoint. See the scope table in
  [`serving-and-keys.md`](serving-and-keys.md).

## ADRs behind these surfaces

* [`../adr/016-continuous-improvement-loop.md`](../adr/016-continuous-improvement-loop.md)
* [`../adr/017-agent-orchestration.md`](../adr/017-agent-orchestration.md)
* [`../adr/018-tenant-provider-keys.md`](../adr/018-tenant-provider-keys.md) (proposed)
* [`../adr/012-runtime-auth-resilience.md`](../adr/012-runtime-auth-resilience.md),
  [`../adr/013-end-to-end-identity.md`](../adr/013-end-to-end-identity.md)
