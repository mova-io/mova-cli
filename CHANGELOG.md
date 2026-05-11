# Changelog

All notable changes to movate. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added — Job retry policy with exponential backoff + dead-letter (post-v1.0)

**Closes the production-readiness reliability gap.** Before this, every
``ERROR`` was terminal — a single transient blip (network, provider 5xx,
rate-limit) killed the job permanently. Now transient failures re-queue
with exponential backoff, persistent failures stay terminal, and jobs
that exhaust their retry budget land in ``DEAD_LETTER`` for operator
triage.

- **`JobStatus.DEAD_LETTER`** — new terminal status. Distinct from
  ``ERROR`` ("failed once, won't retry") — ``DEAD_LETTER`` means "we
  tried N times and gave up." Operators triage with
  ``movate jobs list --status dead_letter`` (already works via the
  existing ``list_jobs`` filter).
- **`core/job_retry.py`** — pure policy module:
  * ``JobRetryPolicy`` dataclass (max_attempts, base_seconds, factor,
    cap_seconds, jitter). Default = 3 attempts (initial + 2 retries),
    5s base, 3x factor, 5min cap, ±25% jitter.
  * ``should_retry(retryable, attempt_count)`` → bool. The retry
    decision.
  * ``compute_next_retry_at(attempt_count)`` → datetime. Exponential
    backoff with jitter; floors at 0 so jitter can't schedule a
    retry in the past.
  * ``is_exhausted(attempt_count)`` → bool. Distinguishes
    "retryable-but-budget-spent" (→ ``DEAD_LETTER``) from
    "not retryable at all" (→ ``ERROR``).
- **`JobRecord` schema additions:** ``attempt_count: int = 0`` and
  ``next_retry_at: datetime | None = None``. Sqlite via idempotent
  ``ALTER TABLE … ADD COLUMN``; postgres via ``ADD COLUMN IF NOT
  EXISTS``. Existing rows from before this migration get default
  values (attempt_count=0, next_retry_at=NULL) so they're treated as
  fresh jobs with a full retry budget — safe default.
- **`StorageProvider.requeue_job(job_id, *, tenant_id, next_retry_at,
  attempt_count)`** — new Protocol method. Flips ``RUNNING`` →
  ``QUEUED``, clears ``claimed_at``, stamps the new
  attempt_count + next_retry_at. Tenant-scoped in WHERE (v1.0 stage 4
  defense-in-depth). Implemented in all three backends.
- **`claim_next_job` is retry-aware** — sqlite + postgres + memory
  now skip rows whose ``next_retry_at`` is in the future. The
  ``next_retry_at IS NULL`` branch is the common case (fresh jobs);
  ``<= now`` covers re-queued jobs whose backoff has elapsed. New
  partial index ``idx_jobs_retry_at`` on both backends keeps the
  filter cheap.
- **`update_job` accepts ``DEAD_LETTER``** as a terminal status
  (previously only ``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED``).
- **Worker integration:** new ``_resolve_outcome(job, outcome)``
  helper centralizes the three-way decision (retry / dead-letter /
  terminal-error). After dispatch, the worker calls either
  ``requeue_job`` (with the new attempt_count + computed
  next_retry_at) or ``update_job`` (with the resolved final status).
  Notifications are SKIPPED on the retry path — the run isn't done
  yet; the dispatcher fires only when the job lands in a true
  terminal status (avoids spam on flaky jobs).
- **`WorkerConfig.retry_policy`** — workers can override the default
  policy. Set ``max_attempts=1`` for the strict "fail fast" mode
  (every retryable error → ``DEAD_LETTER`` immediately).
- 23 new tests across `tests/test_job_retry.py` covering: pure-math
  edge cases (retryable=False short-circuit, budget boundary,
  jitter band, never-in-past floor), storage round-trip
  parametrized over memory + sqlite + postgres (requeue_job,
  claim respects next_retry_at, claim picks up after retry elapsed,
  update_job accepts DEAD_LETTER, save_job persists retry fields),
  and worker integration (requeues transient, keeps non-retryable
  terminal, dead-letters at budget exhaustion, 3-attempt
  fail-fail-succeed happy path, max_attempts=1 disables retries,
  notifier skipped on retry path but fires on DEAD_LETTER).

**Operator triage flow:** when a job lands in DEAD_LETTER, the
``error`` field on the ``JobRecord`` carries the structured error
info from the LAST attempt (type, message, retryable=true), the
``attempt_count`` shows how many times we tried, and
``completed_at`` is set. The standard ``movate jobs show <id>``
displays all of this. Operators investigate the root cause, fix
the underlying issue (e.g. bump a provider quota), and either
manually re-queue (post-v1.1) or accept the loss.

Total: **555 passing** (532 → 555, +23 retry tests).

### Added — Azure deploy onboarding (`scripts/azure-bootstrap.sh` + `movate doctor --target`)

**Closes the manual-toil gap between "you have an Azure subscription"
and "`git push release/<env>` deploys."** v1.0 stages 1-4 shipped the
deploy code path; this is the operator runbook + tooling that makes
the first deploy painless.

- **`scripts/azure-bootstrap.sh <env>`** — idempotent one-shot per-env
  setup. Creates the resource group, the service principal for
  GitHub Actions, the federated OIDC credential pinning to
  `refs/heads/release/<env>`, and the Contributor / AcrPush role
  assignments. Defers AcrPush if the ACR doesn't exist yet (Bicep
  creates it) with a warning; re-running after Bicep locks it in.
  Prints the values to paste into the GitHub Environment secrets —
  the manual UI step that genuinely can't be scripted. Safe to
  re-run after fixing a typo or to re-print the secrets list.
- **`movate doctor --target <name>`** extends the existing
  environment-check command with an Azure preflight section: walks
  `az` installed → logged in → subscription match → resource group
  → ACR → both Container Apps → `/healthz`. Each row reports the
  finding + an operator pointer (`run scripts/azure-bootstrap.sh`,
  `az account set --subscription ...`, etc.) so failures are
  self-fixing. First thing to run when `movate deploy` is acting up.
- **`docs/azure-bootstrap.md`** — 8-step end-to-end runbook from
  "you have a subscription" to "auto-deploy via release/*". Spells
  out what's automated (the two new tools), what isn't (sub
  provisioning, GitHub Environment UI, the Key Vault chicken-and-egg
  on first Bicep run), cost expectations per env, and a
  troubleshooting table indexed on symptom.
- 10 new tests in `tests/test_doctor_azure.py` covering: no `az` on
  PATH short-circuits, no `az login` short-circuits, missing Azure
  config on target short-circuits, subscription mismatch
  short-circuits, missing RG with bootstrap pointer, happy-path
  every-layer green with image tag surfaced, `/healthz` unreachable
  reported distinctly from missing, and CLI integration
  (`movate doctor` unchanged when no `--target`, `--target` renders
  the Azure table, unknown target reports cleanly without crashing).

### Security — Tenant isolation audit (v1.0 stage 4)

**Closes the v1.0 deploy loop.** Every storage read / mutate path that
touches per-tenant rows now filters by ``tenant_id`` at the SQL layer.
Even if a future HTTP handler forgets the cross-tenant check (or a
buggy worker is misconfigured), the storage backend enforces tenant
boundary in the WHERE clause — defense in depth.

**Audit findings (now fixed):**

* ``get_run`` / ``get_workflow_run`` / ``get_eval`` / ``get_job`` —
  previously did SELECT by id only. Now require ``tenant_id`` kwarg
  and add ``AND tenant_id = ?`` to the WHERE clause. Cross-tenant
  lookups return ``None`` (NOT ``403`` — leaking 403 vs 404 lets a
  caller probe whether an id exists in another tenant).
* ``update_job`` — previously updated by ``job_id`` only. Now scoped
  to ``tenant_id`` so even a misconfigured worker can't mutate
  another tenant's job. Silently no-ops on tenant mismatch.
* ``revoke_api_key`` / ``touch_api_key`` — previously mutated by
  ``key_id`` only. Now require ``tenant_id``. A tenant who learns
  another tenant's key_id (8-char random suffix) still can't revoke
  it or pollute its ``last_used_at`` audit trail.
* ``list_evals`` / ``list_workflow_runs`` — previously took no
  ``tenant_id`` param. Now accept an optional ``tenant_id`` filter
  that the HTTP layer will pass; ``tenant_id=None`` remains the
  operator drain-mode path, never exposed on HTTP.

**Surface that already enforced (verified, no changes needed):**

* ``list_runs`` / ``list_jobs`` / ``list_api_keys`` / ``claim_next_job``
  already filtered by ``tenant_id``.
* ``get_api_key`` looks up by ``key_id`` without a tenant filter — by
  design. The auth middleware's ``check_record`` cross-checks the
  presented key's tenant prefix against ``record.tenant_id`` before
  the request proceeds; that's the boundary, not the storage method.

**Call sites updated:** HTTP ``GET /jobs/{id}`` handler, auth middleware
``touch_api_key`` (now passes tenant from the verified record), worker
``update_job`` + ``get_job`` (passes the claimed job's tenant), CLI
``movate auth revoke-key`` (looks up the key first to derive its
tenant for operator-friendly UX), local trace replay (defaults to
``tenant_id="local"`` matching the CLI Executor's tenant stamp).

**Test:** new ``tests/test_tenant_isolation.py`` — 15 cases
parametrized over memory + sqlite + postgres backends (45 invocations
when PG configured). Each populates parallel rows in two tenants
(``alpha``, ``beta``) then sweeps every cross-tenant read path
asserting Beta can never see Alpha's ids and vice versa, plus a
combined sweep covering all 5 tables at once so any future schema
addition that forgets the filter fails this test.

Total: **522 passing** (492 → 522, +30 from isolation tests). All
existing tests pass after threading the new ``tenant_id`` kwarg
through ~25 call sites in the test suite.

**v1.0 is now feature-complete.** Stages 1 (Bicep IaC), 2 (``movate
deploy`` + GH Actions), 3 (model policy enforcement), and 4 (tenant
isolation audit) all done.

### Added — Model policy enforcement (v1.0 stage 3)

**Production-grade governance for which providers / models / cost
ceilings an agent may use.** The `policy:` block on `movate.yaml`
declares the rules; movate enforces them at two concentric layers so
a bundle can't slip past the gate.

- **`policy:` block on `movate.yaml`** — three optional fields, all
  permissive by default (an absent or empty block = no restrictions,
  preserving v0.x behavior for projects that haven't opted in):
  * `allowed_providers: [openai, azure, anthropic]` — provider
    *prefixes* (the part before `/` in a LiteLLM model string).
    Empty list = no restriction.
  * `deny_models: [openai/gpt-3.5-turbo]` — explicit full-model
    blocklist. Takes precedence over `allowed_providers` so an
    operator can pin out specific revisions even within an allowed
    provider (e.g. deny `openai/gpt-4-0314` while keeping
    `openai/gpt-4o-mini`).
  * `max_cost_per_run_usd: 0.50` — hard ceiling on per-run cost. The
    runtime enforces `min(agent.budget.max_cost_usd_per_run, policy)`
    so an agent's authored budget can never relax the org cap.
- **`ModelPolicy.check_model(provider)`** — returns `None` (allowed)
  or a human-readable violation string. Pure function; the rest of
  the integration composes it.
- **`ModelPolicy.check_agent(spec)`** — aggregates violations across
  primary + every fallback + budget in a single pass. Operator fixes
  everything at once instead of playing whack-a-mole.
- **`movate validate <agent>`** — static check on every agent.yaml
  before merge. Exits 2 with a per-violation list (`primary model:
  ...`, `fallback 'X': ...`, `budget=Y exceeds policy ceiling Z`)
  plus a pointer back to `movate.yaml: policy`. Compliant agents see
  a `policy: ✓ compliant` line in the validate output so the operator
  knows the check actually ran.
- **`Executor.execute()` entry** — runtime re-check at every
  invocation (the bundle loaded by `movate serve` over HTTP never hit
  `validate`, so the runtime layer is the actual security boundary).
  Denied models raise `PolicyViolationError` BEFORE any provider call
  — zero cost incurred for a forbidden model. The failure surfaces
  as terminal `policy_violation` status and is persisted to the
  `failures` table for audit.
- **`PolicyViolationError`** + `FailureType.POLICY_VIOLATION` —
  typed error, no retry, no fallback (the fallback chain is itself
  policy-checked, so falling back to another denied model would just
  hit the same wall). New entry in `DEFAULT_RETRY` for completeness.
- **`bench`-friendly** — when `model_override` is passed to
  `execute()` (the bench / compare flow), only the override is
  checked, not the agent's fallbacks (which are already disabled in
  override mode). Aligns the policy semantics with the existing
  fallback-disabling behavior.
- **`movate.yaml` example** — the repo's own `movate.yaml` ships
  with a commented `policy:` block as a copy-paste template.
- 21 new tests across `tests/test_policy.py` covering: permissive
  default, allowed_providers prefix matching, deny_models precedence,
  multi-violation aggregation, budget-ceiling check, `effective_max_cost`
  min math, `movate.yaml` round-trip, executor enforcement
  (denied-primary short-circuits provider call, denied fallback,
  allowed override skips fallback policy check, budget ceiling
  tightens), and `movate validate` CLI integration (compliant exits
  0, three violation types each exit 2 with the right pointer).

**What's left for v1.0:** stage 4 — tenant isolation audit. With
stages 1 (Bicep), 2 (`movate deploy`), and 3 (model policy) done,
v1.0 is one focused audit pass away from feature-complete.

### Added — `movate deploy` + GitHub Actions deploy workflow (v1.0 stage 2)

**Closes the `git push release/* → ACA-deployed service` loop.** Stage 1
provisioned the infrastructure (Bicep); stage 2 makes deploying a code
change one command: `movate deploy --target prod`.

- **`movate deploy`** — wraps `az acr build` (cloud-side Docker build,
  no local Docker needed) + `az containerapp update` for both the API
  and worker Container Apps, then polls `GET /healthz` until the new
  revision's `version` field matches the just-built image. Image tag
  default is `movate:<version>-<git-sha-short>` for traceability.
  Flags:
  * `--target <name>` — pulls Azure config from the target's
    `azure_subscription` / `azure_resource_group` / `azure_acr_name`
    / `azure_env` fields
  * `--image-tag <tag>` — override (e.g. for rollbacks)
  * `--skip-build` — redeploy an existing image (rollback flow:
    pair with `--image-tag movate:<prev>`)
  * `--only api` / `--only worker` — partial update for code changes
    confined to one component
  * `--dry-run` — print the plan + the exact `az` commands without
    running them
  * `--no-wait` — fire-and-forget mode for CI
  * `--wait-timeout` — `/healthz` poll budget (exit 124 on timeout)
- **`TargetConfig` extended** with four optional Azure deploy fields
  (`azure_subscription`, `azure_resource_group`, `azure_acr_name`,
  `azure_env`). A target without these fields can still be used for
  `movate submit` / `movate jobs` (read-only access to a runtime),
  but `movate deploy` errors with a clean pointer back to
  `movate config add-target`. `add-target` now accepts `--azure-*`
  flags and surfaces "deploy enabled" or "deploy NOT enabled" at
  registration time so the operator sees the capability gap immediately.
- **`.github/workflows/deploy.yml`** — push to `release/<env>` (or
  manual `workflow_dispatch` with `target_env` input) → Azure federated
  OIDC login (no stored client secrets) → hydrate `~/.movate/config.yaml`
  from per-environment GitHub secrets → run `movate deploy`. The
  workflow scopes itself to a matching GitHub *Environment* so prod
  deploys can require approval and per-env secret sets can't leak
  across envs. A small `resolve` job extracts the env name from the
  branch (`release/prod` → `prod`) before the deploy job picks up the
  right scoped secrets.
- **Integration surface = `az` CLI shell-out, not Azure SDKs.** Adds
  zero new runtime deps; operators already have `az` for everything
  else. Cost = subprocess management; benefit = clean rollback /
  retry / debug story (an operator can re-run the printed command
  by hand if anything looks off).
- 23 new tests across plan-building (image-tag composition, only-api /
  only-worker filtering, every missing-Azure-field branch with helpful
  pointer), CLI integration (dry-run no-subprocess, full run fires
  3 `az` commands, `--skip-build` skips the build, `--only` filters
  apps, missing-`az` exits 2, missing-Azure-config exits 2, `az`
  failure surfaces as exit 1), and the async `/healthz` poll loop
  (version-match return, exit-124 timeout, transient network errors
  swallowed and retried via `httpx.MockTransport`).

**What's left for v1.0:** stage 3 (model policy enforcement at
executor entry) and stage 4 (tenant-isolation audit). With stages 1
and 2 done, the deploy story is complete — a developer can scaffold
an agent locally, run evals, push to `release/dev`, and have it
serving traffic in Azure ~3 minutes later with no manual `az`
invocations.

### Added — Server-side email notifications (post-v1.0)

**Per-job email when work finishes.** Closes the "kick off a long
remote job, get pinged when done" loop without requiring the user to
keep their laptop awake polling. Server-side: the runtime workers
fire SMTP after each terminal status transition.

- **Schema:** `notify_email TEXT` column on `jobs`. Sqlite via
  idempotent `ALTER TABLE` in `_MIGRATIONS`; postgres via
  `ADD COLUMN IF NOT EXISTS` in `_SCHEMA` (PG-native idempotency).
  `JobRecord` Pydantic + `RunSubmission` wire type + `JobView`
  response all surface the field. The HTTP handler threads it from
  the request body into the persisted record.
- **`core/notify.py`** — pluggable `NotificationDispatcher` Protocol:
  * `ConsoleBackend` — logs the intent at INFO. Default; safe in
    dev / tests / misconfigured deployments. Operators see what
    would have been sent if SMTP were wired up.
  * `SmtpEmailBackend` — sends via stdlib `smtplib`. Vendor-agnostic:
    ACS Email, SendGrid, Mailgun, AWS SES, Gmail all speak SMTP. The
    operator picks via env vars (`MOVATE_SMTP_HOST`, `_PORT`, `_USER`,
    `_PASSWORD`, `_FROM`, `_USE_SSL`, `_TIMEOUT_SECONDS`). STARTTLS
    upgrade on port 587, full SSL on port 465. Constructor takes
    explicit args so tests don't depend on env state.
- **`build_dispatcher()`** factory — env-driven backend selection.
  `MOVATE_SMTP_HOST` unset → `ConsoleBackend`. Set → `SmtpEmailBackend`.
  Bad config (non-int port, etc.) falls back to console with a
  warning instead of crashing the worker.
- **Worker integration** — `Worker.__init__` now accepts an optional
  `notifier: NotificationDispatcher`. After each terminal
  `update_job`, the worker re-fetches the post-update view (so the
  email sees the final status, not the RUNNING snapshot from
  `claim_next_job`) and fires the dispatcher. Wrapped in
  try/except so a buggy dispatcher can't sink the loop —
  notification is courtesy, never load-bearing.
- **`movate submit --notify-email <addr>`** — threads through
  `MovateClient.submit_job(..., notify_email=...)` → `RunSubmission`
  → handler → `JobRecord` → worker → dispatcher → SMTP. The worker
  prints `notifications: smtp backend` (or `console`) at startup so
  operators see immediately which path is active.
- 14 new tests across `build_dispatcher` env selection, both backends
  (ConsoleBackend logs / SmtpEmailBackend sends via a faked
  `smtplib.SMTP`), STARTTLS-skip-on-SSL, SMTP error swallowing,
  worker fires dispatcher on terminal, worker skips dispatcher when
  no email, worker swallows dispatcher exceptions, schema round-trip
  preserves the column.

**Subject line example:**
> `[movate] ✓ agent/faq-agent — success`

**Body:** job id, kind, target, tenant, run id, elapsed time, error
info if applicable. Plain text — works in every mail client without
HTML rendering quirks.

**SMS deferred.** Phone-number provisioning + carrier registration
(A2P 10DLC for US numbers, equivalents elsewhere) is multi-week
business setup. Code shape is identical (`notify_sms` column +
Twilio/ACS SMS backend); skipping until a customer specifically asks.

### Added — Remote-runtime CLI: targets, `submit`, `jobs`

**The dev-team intuitive workflow for deployed runtimes.** Stop typing
`curl http://... -H "Authorization: Bearer ..." -d '{...}'`; start
typing `movate submit alpha '{...}'`. Targets, bearer tokens, and
fire-and-forget vs --wait modes all bundled.

- **`core/user_config.py`** — `~/.movate/config.yaml` schema:
  ```yaml
  targets:
    local: {url: http://127.0.0.1:8000, key_env: MOVATE_LOCAL_KEY}
    prod:  {url: https://..., key_env: MOVATE_PROD_KEY}
  active: local
  ```
  Bearer tokens NEVER in the file — only the name of the env var that
  holds them. Config file is dotfile-safe to commit. Path overrideable
  via `MOVATE_CONFIG_PATH` for tests + CI.
- **`core/client.py`** — `MovateClient` async httpx wrapper with
  `submit_job`, `get_job`, `list_agents`, `healthz`, `wait_for_terminal`.
  Translates non-2xx responses into structured `MovateClientError`
  with `status_code` + `code` + `message`. Accepts an optional
  `transport` kwarg so tests can route through `httpx.ASGITransport`
  for hermetic in-process testing — no real network, no port.
- **`movate config add-target | list-targets | use | show | remove-target`**
  — manage the user-level config. First add auto-promotes to active
  for first-run UX.
- **`movate submit <agent> [INPUT]`** — queue a job at the active
  (or `--target`-named) runtime. Default is fire-and-forget: bare
  JSON `{job_id, status}` to stdout, "queued + how to poll" hint to
  stderr. `--wait` polls with a Rich spinner until terminal; `--notify`
  pops a desktop notification (macOS osascript / Linux notify-send /
  no-op on Windows). `--output json` for scripting. Exit code 1 on
  terminal-but-failed, 124 on `--wait` timeout (conventional
  `timeout` exit code so bash scripts can branch).
- **`movate jobs show <id>` / `wait <id>` / `list-agents`** — inspect
  job state on a deployed runtime. Distinct from `movate logs` (which
  reads the LOCAL sqlite for post-mortem). Same `--target` / `--output`
  conventions as submit.
- 29 new tests cover: user-config round-trip, MovateClient over
  ASGITransport (auth, 401 / 404 / timeout paths, poll-until-terminal),
  CLI integration (config CRUD, submit fire-and-forget + show
  round-trip, error UX for unset bearer-token env vars).
- End-to-end real-binary smoke validated: scaffold agent → start
  `movate serve` + `movate worker` → `movate config add-target` →
  `movate submit --wait --output json` round-trips through the wire
  in ~135ms.

The 90% dev-team case for "kick off a long eval, get notified when
it's done" is now `movate submit ... --wait --notify`. **Server-side
SMS/email notifications** are tracked in BACKLOG for post-v1.0; that
needs an ACS / Twilio / SendGrid decision and per-job `notify_target`
column on the `jobs` table.

### Added — Azure Bicep IaC (v1.0 stage 1)

**Foundation for `git push release/* → ACA-deployed service`.** Stage 1
provisions; stages 2-4 (deploy CLI, model policy, tenant isolation
audit) close the v1.0 loop.

- **`infra/azure/main.bicep`** orchestrator at `resourceGroup` scope.
  Per-env defaults (dev/staging/prod) drive SKU tiers, replica
  counts, and retention without parameter sprawl.
- **`infra/azure/modules/`** — seven focused modules, each with
  `@description` on every param and `output` for what the next
  module needs:
  * `loganalytics.bicep` — workspace + retention
  * `acr.bicep` — registry (Basic for dev, Standard for prod)
  * `keyvault.bicep` — RBAC mode, soft-delete + purge protection
  * `postgres.bicep` — Flex Server + database + Azure-services
    firewall rule
  * `containerapp-env.bicep` — ACA Environment wired to Log
    Analytics; prod adds a Dedicated workload profile alongside
    Consumption
  * `containerapp-api.bicep` — `movate serve` with external
    ingress, /healthz liveness + readiness probes, KV secret refs
    via system-assigned managed identity
  * `containerapp-worker.bicep` — `movate worker` with no ingress;
    CPU-utilization scale rule (v1.1 will swap to a KEDA Postgres
    scaler keyed on queue depth)
- **Role assignments at top level** (not inside modules) — keeps the
  dependency edges from ACA managed identity → ACR (AcrPull) and
  ACA managed identity → Key Vault (Key Vault Secrets User) explicit
  in `main.bicep` where the assignee + scope cross module boundaries.
- **`infra/azure/main.bicepparam.example`** — parameter template with
  inline guidance on the Key Vault chicken-and-egg (Container Apps
  reference secrets that must exist in KV at deploy time; two-pass
  or bootstrap-vault options documented).
- **`Dockerfile`** — multi-stage Python 3.11 + uv build with two
  final targets sharing the same base layers: `runtime` (CMD =
  `movate serve`) and `worker` (CMD = `movate worker`). Non-root
  user, baked default tracer = `stdout` (Log Analytics captures it
  via the ACA Env), `MOVATE_AGENTS_PATH=/app/agents`.
- **`.dockerignore`** — excludes tests, docs, build artifacts, dev
  DBs, and `infra/` from the image context. Smaller, faster builds;
  zero risk of leaking secrets.
- **CI `bicep` job** — installs the Bicep CLI and runs
  `bicep build infra/azure/main.bicep` + `bicep lint` on every PR.
  No Azure subscription needed; catches syntax errors / unknown
  resource types / param mismatches before an operator hits them.
- **Operator walkthrough** at
  [infra/azure/README.md](infra/azure/README.md) — end-to-end
  recipe from `az login` to verified `/healthz`, including the
  KV-secret-population dance and the first `movate auth create-key`
  call against the deployed DB.

Design decisions (naming convention, region default, per-env SKU
choices, secret strategy, no-VNet-in-v1.0) locked in
[docs/v1.0-azure-design.md](docs/v1.0-azure-design.md).

**Out of scope for stage 1** (lands later): `movate deploy` CLI
binding `az acr build` + `az containerapp update`, GH Actions
deploy.yml, custom domain + TLS, VNet integration, multi-region
failover.

### Added — Progress UI for long-running CLI ops

The dev team's intuition for "is this still working?" is now backed
by visible feedback. Three commands that used to run silently for 30s
to several minutes now show what's happening.

- **`cli/_progress.py`** — three reusable helpers, all writing to
  **stderr** so stdout JSON pipes stay clean:
  * `progress_bar(description, total)` — known-length loop with
    moving bar, mof-N count, elapsed time, and side-suffix support
    (e.g. running mean score)
  * `spinner(message)` — indeterminate-duration single operation
  * `print_event(message, style)` — one-line stderr print for
    streaming feeds
  All auto-degrade on non-TTY (CI logs, redirected output, captured
  test runs). Rich does this natively; the helpers verify the
  contract via `Console.is_terminal`.
- **`movate eval`** — case-by-case progress bar with running mean
  score in the side-suffix. Suppressed for `-o json` / `-o markdown`
  / `--mock` so automation paths and quick tests stay clean.
- **`movate bench`** — model-by-model progress bar showing the
  just-finished model name in the suffix.
- **`movate worker`** — streaming feed: one line per completed job
  with status icon (✓ / ⊘ / ✗), kind/target, duration, short job_id.
  At-a-glance throughput + failure visibility for operators tailing
  the worker process.
- Engine hooks (`EvalEngine.on_case_complete`,
  `BenchEngine.on_model_complete`, `Worker.on_job_complete`) are
  optional callbacks; engines call them in a `contextlib.suppress`
  block so a buggy UI callback can never sink the run. Tests
  explicitly assert this contract.
- Seven new tests covering JSON-output stays clean, markdown stays
  clean, callback exceptions are swallowed, non-TTY produces no ANSI
  escapes. Real-binary smoke validated the worker live feed against
  three queued jobs.

## [0.5.0] — 2026-05-09

**movate is now a service.** v0.5 takes the framework from "library +
local CLI" through queue → auth → HTTP → worker → Postgres, in five
incremental stages:

| stage | what shipped |
|---|---|
| 1 | Job queue data layer (`JobRecord` + `jobs` table + claim semantics) |
| 2 | API key auth crypto + storage + `movate auth create-key | list-keys | revoke-key` |
| 3a | FastAPI runtime with `/healthz`, `POST /run`, `GET /jobs/{id}` + auth middleware |
| 3b | Agent registry + `GET /agents` + `movate serve` (uvicorn binding) |
| 4 | Worker claim loop + `movate worker` — climactic deliverable; movate stops being a queue and becomes a runtime |
| 5 | PostgresProvider port — production-ready storage with `SELECT ... FOR UPDATE SKIP LOCKED` for true worker parallelism |

**121 new tests across the release** (412 unit + 3 smoke when PG is
configured; 391/3 without). End-to-end binary smoke validated against
both backends: `movate serve` + `movate worker` in two real processes,
job lifecycles QUEUED → RUNNING → SUCCESS in ~12-100ms.

### Added — PostgresProvider (v0.5 stage 5)

**v0.5 capabilities are now feature-complete on both backends.**

- **`storage/postgres.py`** — full Protocol parity with
  `SqliteProvider`, against `asyncpg`. Schema uses `JSONB` (queryable,
  indexable) instead of TEXT-with-JSON, `TIMESTAMPTZ` instead of ISO
  strings, `BOOLEAN` instead of `INTEGER`. Per-connection pool init
  registers a `json.dumps`/`json.loads` codec for `jsonb` so handlers
  pass and receive plain dicts.
- **`claim_next_job` uses `SELECT ... FOR UPDATE SKIP LOCKED`** —
  superior to sqlite's `BEGIN IMMEDIATE`. Multiple workers truly
  run in parallel: each takes a row-level lock on a different row,
  no global serialization. New test
  `test_postgres_claim_skip_locked_runs_concurrent` proves both
  workers grab two distinct rows concurrently (sqlite would block
  one of them).
- **`build_storage()` switches on `MOVATE_DB_URL`** —
  `postgres://` / `postgresql://` URLs route to `PostgresProvider`;
  otherwise falls back to `SqliteProvider`. `asyncpg` is imported
  lazily so sqlite-only deployments don't need it installed.
- **`tests/conftest.py`** now provides a shared `storage` fixture
  parametrized over `(memory, sqlite, postgres)`. PG params skip
  automatically when `MOVATE_PG_TEST_URL` is unset, so devs without
  a local PG see clean test runs and CI can wire a service-container
  job to exercise that branch. Per-test truncation of the PG state
  keeps tests hermetic without re-creating the schema.
- 21 new test invocations: 16 conformance tests now run against
  Postgres in addition to sqlite + memory; one new PG-specific
  concurrent-claim test for SKIP LOCKED.

### Fixed — Two real bugs surfaced by the PG smoke walk-through

- **asyncpg pool was created on the wrong event loop.**
  `cli/serve.py` was doing `asyncio.run(storage.init())` (creates
  pool on a temporary loop, which then exits), then `uvicorn.run(app, ...)`
  (creates a different loop). asyncpg connections are bound to
  their creation loop; this manifested as "another operation is in
  progress" 500s on the first request. Restructured to do
  `asyncio.run(_run_serve(...))` where `_run_serve` is async and
  uses `uvicorn.Server.serve()` so init + serve share one loop.
- **Fire-and-forget `touch_api_key` raced asyncpg pool RESET.**
  The auth middleware was scheduling `asyncio.create_task(_safe_touch(...))`
  after a successful auth. Under asyncpg pool semantics, this could
  re-acquire the same connection that was mid-RESET (called by pool
  release after the previous `get_api_key`), triggering the same
  "another operation is in progress" error. Moved to inline
  `await _safe_touch(...)` — the latency cost is sub-millisecond
  vs the cost of a flaky service. Also made the corresponding test
  deterministic (no skip-on-race).

### Added — Worker claim loop + `movate worker` (v0.5 stage 4)

**movate is now a runtime, not just a queue.** The full HTTP →
queue → claim → execute → terminal-state lifecycle works end-to-end
between two real processes.

- **`runtime/dispatch.py`** — `WorkerDispatch.execute_job(job) →
  DispatchOutcome`. Pure logic, no async loop. Agent + workflow
  paths; unknown target / executor crash both → terminal ERROR with
  structured error info. The split keeps tests deterministic
  (assert each branch with one call) and makes the loop trivial.
- **`runtime/worker.py`** — `Worker.run_one_cycle()` (deterministic;
  one claim+dispatch+update; tests call this directly) and
  `Worker.run_forever(stop_event)` (CLI loop, sleeps the configured
  poll interval when the queue is empty, exits promptly on event
  set even mid-poll). Never crashes on a single bad job: dispatch
  errors and storage update failures both get logged and the loop
  continues.
- **`runtime/registry.scan_workflows(path)`** — mirrors
  `scan_agents`. Returns name → `WorkflowGraph`; one broken
  workflow.yaml warns and skips rather than crashing startup.
- **`movate worker`** CLI replaces the stub. Flags: `--tenant-id`
  (drain a single tenant; default is all), `--agents-path` (env:
  `MOVATE_AGENTS_PATH`), `--workflows-path` (env:
  `MOVATE_WORKFLOWS_PATH`), `--poll-interval`, `--mock`. Registers
  SIGINT/SIGTERM handlers that flip the stop event so in-flight
  jobs finish before exit.
- **`RunResponse` gained `run_id`** — populated by `Executor.execute`
  for both success and error paths. The worker reads it to mirror
  into `JobRecord.result_run_id`. Backwards compatible: empty
  string default.
- **`runtime/worker.WorkerConfig`** — `poll_interval_seconds` and
  optional `tenant_id`. Workers without a `tenant_id` drain all
  queues (operator/dev mode); tenant-bound workers are the
  production pattern.
- **End-to-end binary smoke** validated: scaffold an agent, start
  `movate serve --port 8766` and `movate worker --mock` in
  separate processes, mint a key, POST /run → 202 queued, poll
  /jobs/{id} → `status: success`, `result_run_id` matches the
  persisted `RunRecord.run_id`, total lifecycle ~112ms (claim
  ~106ms after submission, completed ~6ms after claim).
- 10 new tests across dispatch (agent success/error/unknown,
  workflow with real one-node yaml on disk, executor crash →
  internal error) and worker (claim/empty, drain one job, unknown
  target → ERROR, tenant scoping, run_forever exits on stop event
  even with long poll interval).

### Added — Agent registry + `movate serve` (v0.5 stage 3b)

- **`runtime/registry.py`** — `scan_agents(root)` walks one level
  deep for directories containing `agent.yaml`, loads each via the
  existing `load_agent`, sorts by spec name. Invalid agents (broken
  YAML, unknown api_version, etc.) are skipped with a warning log
  rather than crashing — one bad agent shouldn't blackhole the
  catalog at runtime startup.
- **`GET /agents`** endpoint returns name/version/description
  metadata only. Auth-required for consistency. Per-tenant agent
  visibility is post-v0.5 — every authenticated tenant currently
  sees the same catalog (sufficient for a single-team deployment).
- **`movate serve`** replaces the v0.5 stub with a real uvicorn
  binding. Flags: `--host` (default `127.0.0.1`), `--port` (default
  `8000`), `--agents-path` (env: `MOVATE_AGENTS_PATH`, default
  `./agents`), `--log-level`. Storage is pre-init'd on the parent
  loop so aiosqlite connections aren't bound to a dead loop;
  registry is scanned once at startup so each `/agents` request is
  a constant-time list lookup.
- 11 new tests: 8 registry edge cases (missing/file/empty roots,
  one-level walk, sibling-skip, partial-failure tolerance),
  3 `/agents` endpoint cases (empty registry, metadata-only
  response, auth required).
- **End-to-end binary smoke** validated against the real `movate`
  binary: `serve` boots → `/healthz` returns 200 → `auth
  create-key` mints a key → `/agents` lists scaffolded agents →
  `POST /run` returns 202 with job_id → `GET /jobs/{id}` returns
  the queued state. The full HTTP→storage→auth chain works.

### Added — FastAPI runtime (v0.5 stage 3a)

- **`runtime/`** package — thin HTTP layer over the storage Protocol
  and `core/auth`. Wire schemas (`runtime/schemas.py`) live separately
  from `core/models.py` so API and DB can evolve independently.
- **`build_app(storage)`** factory — `runtime/app.py` returns a
  FastAPI app bound to a given storage backend. Tests pass an
  `InMemoryStorage`; `movate serve` (lands stage 3b) will pass a
  `SqliteProvider`. The factory pattern means there's no global
  app object and no env-var gymnastics.
- **Endpoints:** `GET /healthz` (unauthed liveness), `POST /run`
  (queue a job → 202 with `job_id`), `GET /jobs/{id}` (poll; returns
  the current JobRecord state minus `api_key_id`).
- **Auth middleware** (`runtime/middleware.py`) composes the stage-2
  primitives: `parse_api_key` → `storage.get_api_key` →
  `check_record`. Every failure mode collapses to a uniform `401`
  with `{"error": {"code": "auth_required", "message": "..."}}` —
  the discriminator is logged but never echoed (timing-oracle
  defense). Successful auth fires-and-forgets `touch_api_key` so
  `last_used_at` reflects calls without blocking responses.
- **`AuthContext`** dataclass — what handlers receive after a
  successful auth. Carries `tenant_id`, `api_key_id`, `env`. Handlers
  MUST NOT reach back to the underlying `ApiKeyRecord` (no plaintext
  secret on the wire ever).
- **Tenant scoping:** `GET /jobs/{id}` returns 404 (not 403) for
  cross-tenant lookups. 403 would let an attacker probe whether a
  `job_id` exists in another tenant.
- **`runtime/errors.py`** — single error envelope shape; codes are
  stable enums (`AUTH_REQUIRED`, `NOT_FOUND`, `BAD_REQUEST`,
  `INTERNAL`); messages may change between releases but codes are
  contract.
- 14 tests via `fastapi.TestClient` + `InMemoryStorage`: every auth
  failure mode → 401, /run persists tenant + key attribution onto
  the JobRecord, /jobs/{id} cross-tenant safety, request validation
  (422 on missing fields / unknown JobKind / empty target).

### Added — API key auth (v0.5 stage 2)

- **`core/auth.py`** — pure crypto, no I/O. `mint_api_key` produces a
  `mvt_<env>_<tenant_prefix>_<key_id>_<secret>` string with 256 bits
  of entropy in the secret. `parse_api_key` validates shape via regex
  (rejects malformed / wrong env / wrong tenant prefix length).
  `hash_secret` uses SHA-256 of `salt || secret`; `verify_secret` is
  constant-time via `hmac.compare_digest`. `check_record` is the
  decision tree for verification — returns `None` on success or a
  `VerificationFailure(reason=...)` for not_found / revoked /
  tenant_mismatch / env_mismatch / bad_secret. Each branch is unit
  tested in isolation.
- **`ApiKeyEnv` enum** — `live` | `test`, hard separation enforced at
  parse time before any DB hit. **`ApiKeyRecord`** Pydantic model
  carries `secret_hash`, `salt`, `created_at`, `last_used_at`,
  `revoked_at`, optional `label`. The plaintext secret is never
  stored.
- **`api_keys` table** added via SQLite migrations (idempotent). One
  partial index: `WHERE revoked_at IS NULL` — keeps `list_api_keys`
  fast as the table grows with revocations. Storage methods
  (`save_api_key`, `get_api_key`, `list_api_keys`, `revoke_api_key`
  idempotent, `touch_api_key` for last-used bump) on the Protocol +
  both backends.
- **`movate auth create-key | list-keys | revoke-key`** CLI surface.
  `create-key` prints the full key once on stdout (pipe into a
  vault) with a "save this now" warning on stderr. `--quiet`
  inverts the output streams for shell capture (`KEY=$(... --quiet)`).
  `list-keys` defaults to active keys; `--include-revoked` shows the
  full audit history. End-to-end smoked against the real binary
  with `MOVATE_DB=/tmp/...`: mint → list → revoke → list.
- 37 tests across pure crypto / storage round-trip / CLI integration.

### Added — Job queue data layer (v0.5 stage 1)

- **`JobRecord` + `JobKind`** in `core/models.py` — queue entry with
  agent/workflow discriminator, lifecycle status, optional
  `result_run_id` mirror back to the produced run, and `api_key_id`
  for audit. Re-uses the existing `JobStatus` enum so queue and run
  share one status vocabulary.
- **`jobs` table** added to the SQLite schema via `_MIGRATIONS` (so
  upgraders pick it up cleanly). Two indexes: `idx_jobs_queue_head`
  (partial, `WHERE status = 'queued'`) keeps `claim_next_job` O(queued);
  `idx_jobs_tenant_created` covers the `/jobs` listing path.
- **`save_job` / `get_job` / `list_jobs` / `claim_next_job` /
  `update_job`** on `StorageProvider` Protocol; implemented in
  `SqliteProvider` and `InMemoryStorage`.
- **Claim semantics:** FIFO oldest-first, status-guard (only
  `QUEUED` rows ever claimed), tenant-scoped, atomic via sqlite
  `BEGIN IMMEDIATE`. The Postgres provider (stage 5) uses
  `SELECT ... FOR UPDATE SKIP LOCKED` instead. 25 conformance tests
  (parametrized over both backends) cover CRUD round-trip, FIFO,
  status guard, tenant isolation, terminal-only `update_job`, and
  concurrent-claim no-double-dispatch on sqlite with two
  connections.
- **`update_job`** rejects non-terminal status transitions —
  `QUEUED`/`RUNNING` are owned by `save_job`/`claim_next_job`, so
  passing them is a programming error.
- Design decisions locked in
  [docs/v0.5-design.md](docs/v0.5-design.md) (queue claim model,
  multi-tenant isolation, API key format, workflow-in-queue
  dispatch, job→run linkage).

### Added — File-based eval baselines (CI integration)

- **`movate eval --baseline-file <path>`** — load an `EvalRecord` from a
  JSON file instead of looking up an `eval_id` in sqlite. Unblocks CI
  use: GitHub Actions runners are ephemeral, sqlite isn't. With this
  flag, the baseline can be a git-tracked artifact in the consumer's
  repo. Mutually exclusive with `--baseline`.
- **`movate eval --output-baseline <path>`** — after running, write the
  current run's `EvalRecord` to disk as JSON. Pair with the
  `refresh-baseline` job in CI on main-branch merge to keep the
  committed baseline current. Creates parent directories so users can
  drop the file at `.movate/<agent>/baseline.json` without pre-creating
  the dir.
- Example workflow at
  [.github/workflows/eval-gate.example.yml](.github/workflows/eval-gate.example.yml)
  with `gate-pr` (PR-time regression check) and `refresh-baseline`
  (main-branch refresh) jobs. Docs at
  [docs/ci-eval-gate.md](docs/ci-eval-gate.md). Six new tests cover
  load, write, mutual exclusion, missing/malformed JSON.

[0.5.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.5.0

## [0.4.0] — 2026-05-08

Observability + regression-detection. Closes both halves of the
"something changed; what?" loop: `eval --baseline` flags aggregate
score regressions; `run --replay` lets you re-execute the exact recorded
input against the current code. Plus a full tracing stack — Langfuse,
OTel, fan-out — and trace replay for post-mortem reconstruction.

**89 new tests this release** (288 unit + 3 smoke = 291 total). `ruff
format`, `ruff check`, `mypy src` (strict) all clean.

### Added — Tracing backends

- **LangfuseTracer** (`tracing/langfuse.py`) — wraps the Langfuse v2 SDK
  behind our `Tracer` Protocol. Optional dep (`pip install movate[langfuse]`).
  Fail-soft: tracer errors never break a run.
- **OtelTracer** (`tracing/otel.py`) — OTLP-HTTP exporter; span hierarchy
  workflow → node → provider call. Optional dep (`movate[otel]`). Lazy import.
- **CompositeTracer** (`tracing/composite.py`) — fan-out to N delegate
  tracers with per-delegate `SpanCtx` mapping; one bad backend can't kill
  siblings. Each delegate call wrapped in try/except.

### Added — Trace replay

- **`movate trace replay <id>`** (`cli/trace.py` + `core/replay.py`) —
  auto-detects agent vs workflow run by id, renders Rich tables (status,
  agent/workflow, latency, cost, tokens, error) plus per-node breakdown
  and initial/final state for workflows. `-v` shows full input/output JSON;
  `-o json` is pipe-friendly for diffs. New `get_run(run_id)` and
  `get_workflow_run(id)` lookups on `StorageProvider`.

### Added — Eval baseline diff

- **`movate eval --baseline <eval-id>`** (`core/baseline.py` + `cli/eval.py`)
  — closes the regression-detection loop. Diffs current eval vs a stored
  `EvalRecord` (mean_score, pass_rate, sample_count, cost). Renders a Rich
  diff table after the main eval output; `-o json` includes a `baseline`
  block. Exits 1 on regression past `--regression-tolerance` (default 0.0,
  strict). Asserts agent identity matches across baseline ↔ current.
  New `get_eval(eval_id)` storage method. 21 tests in `tests/test_baseline.py`.
- Per-case diff deferred to v0.4.1+ when datasets are big enough that
  aggregate isn't enough.

### Added — Run replay

- **`movate run <agent> --replay <run-id>`** (`core/run_replay.py` +
  `cli/run.py`) — single best regression-debug tool. Re-executes a
  recorded `RunRecord` through the *current* agent bundle (prompt, model,
  schemas, pricing all reload from disk; only the input is pinned).
  `AgentReplayDiff` surfaces `output_changed`, `status_changed`,
  `changed_keys` (top-level keys whose values diverge), cost delta, and
  latency delta. `-o json` for piping; `-o text` renders a Rich summary
  table to stderr with a slim diff JSON on stdout.
- Output changes are *not* failures — surfacing the diff IS the goal,
  exit 0 even when the agent now produces different output. Only a
  current-run error trips exit 1. Mismatch (run-id missing, agent name
  doesn't match the bundle) → exit 2.
- `--replay` is mutually exclusive with positional INPUT / `--input`.
  Workflow replay deferred; single-agent debug covers the 80% case.
  14 tests in `tests/test_run_replay.py`.

[0.4.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.4.0

## [0.3.1] — 2026-05-09

Patch release. Fixes a `RunRecord` double-save bug in `WorkflowRunner` that
would have inflated per-workflow cost / latency reports by ~2×. Surfaced by
a throwaway IR→LangGraph prototype (now deleted; findings preserved in
[docs/v0.3-langgraph-prototype.md](docs/v0.3-langgraph-prototype.md)).

### Fixed

- `Executor.execute(...)` now accepts `workflow_run_id` + `node_id` kwargs
  and stamps them onto the persisted `RunRecord`. `WorkflowRunner` no
  longer saves a second copy with a fresh UUID after every node.
- `WorkflowRunner._stamp_workflow_link` deleted. Per-node summaries are
  still synthesized in-memory for the `WorkflowResult.runs` view; on
  failure the runner persists a single `ERROR`-status row so
  `list_runs(workflow_run_id=…)` joins remain complete.

### Added

- [docs/v0.3-langgraph-prototype.md](docs/v0.3-langgraph-prototype.md) —
  four findings from the IR-vs-LangGraph seam check. Locks in the v1.1
  compiler design (`merge_dicts` state reducer, HITL via checkpointer,
  parallel-fan-out enum redundancy).

[0.3.1]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.1

## [0.3.0] — 2026-05-09

Sequential workflows: declarative `workflow.yaml` → IR → runner. Linear
chains of agent nodes only in v0.3; the IR is forward-aware (`NodeType` /
`EdgeKind` enums include conditional, parallel, HITL, sub-workflow variants)
so v1.1's LangGraph compiler can target it without a schema break.

**42 new tests** (196 unit + 3 smoke = 199 total). `ruff format`,
`ruff check`, `mypy src` (strict) all clean.

### Added — Workflow IR + compiler

- `WorkflowSpec` Pydantic model for `workflow.yaml` ([src/movate/core/workflow/spec.py](src/movate/core/workflow/spec.py)) — `kind: Workflow`, `state_schema`, `entrypoint`, `nodes`, `edges`. Node `type` constrained to `Literal["agent"]` so typos fail at parse time.
- `WorkflowGraph` IR ([src/movate/core/workflow/ir.py](src/movate/core/workflow/ir.py)) — nodes, edges, topology helpers (`successors`, `predecessors`, `sources`, `sinks`, `is_linear`, `topological_order`). Future-aware enums: `NodeType` ∈ {AGENT, TOOL, HUMAN, FUNCTION, SUB_WORKFLOW}, `EdgeKind` ∈ {SEQUENTIAL, CONDITIONAL, PARALLEL_FAN_OUT, PARALLEL_FAN_IN}.
- Two-pass compiler ([src/movate/core/workflow/compiler.py](src/movate/core/workflow/compiler.py)):
  - `compile_workflow(spec, dir)` — structural validation: duplicate ids, dangling edges, self-loops, cycles, orphan reachability, state-schema parsing.
  - `validate_linear(graph)` — v0.3 phase gate. Rejects branches, joins, conditional edges, non-agent node types with phase-aware error messages naming when each feature lands.

### Added — Workflow runner + storage

- `WorkflowRunner` ([src/movate/core/workflow/runner.py](src/movate/core/workflow/runner.py)) walks the IR in topological order. State plumbing: initial state validated against `state_schema`; per node, state is *projected* onto the agent's input schema (keys in `properties` only) and the agent's output is shallow-merged back. Mid-pipeline failures stop the run, retain the pre-merge state, and stamp the failed `node_id`.
- `WorkflowStatus` enum + `WorkflowRunRecord` Pydantic. `RunRecord` extended with optional `workflow_run_id` + `node_id` so per-node history joins back to the parent run.
- New sqlite `workflow_runs` table + idempotent `ALTER TABLE runs ADD COLUMN` migrations for existing v0.2 DBs.
- `InMemoryStorage` (in `movate.testing`) updated to match the new protocol.

### Added — Workflow CLI integration

- `is_workflow_path()` auto-detects workflow vs agent by presence of `workflow.yaml`.
- `movate validate <path>` — workflow branch prints topology chain, exits 0/2.
- `movate show <path>` — workflow branch renders Rich tables + ASCII chain + Mermaid `flowchart LR` block (paste into a PR for a live diagram).
- `movate run <path>` — workflow branch parses `INPUT` as JSON / file / stdin (no auto-wrap), executes through `WorkflowRunner`, prints per-node summary + `final_state`. `--output {table|json}`.

### Architecture decisions locked

- **IR is the contract; validators are policy.** The IR's enum members include v1.1+ variants. The compiler validators decide which variants are allowed *per phase*. v1.1 swaps `validate_linear` for `validate_dag` (and adds a `LangGraphCompiler` emitting LangGraph from the same `WorkflowGraph`) without touching the IR or the structural compiler.
- **State plumbing v0.3 = projection + shallow merge.** Explicit `inputs:` / `outputs:` mappings deferred to v0.4 when real workflows demand finer control.

[0.3.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.0

## [0.2.0] — 2026-05-08

First tagged release. Single-agent loop is production-ready; eval + bench
engines support exact-match and LLM-as-judge with cross-family enforcement;
`movate.testing` package is the consumer-facing test surface.

**157 tests** (154 unit, 3 live-API smoke). `ruff format`, `ruff check`, `mypy
src` (strict) all clean.

### Added — Agent loop (Phase 1 / v0.1)

- Typer + Rich CLI grouped by intent (`Develop`, `Run & evaluate`, `Diagnose`,
  `Deploy & operate`, `Manage`).
- `agent.yaml` schema (`api_version: movate/v1`) — Pydantic-validated; rejects
  floating tags, bad semver, wrong api_version, extra fields.
- Loader → `AgentBundle` (YAML + Jinja2 prompt + I/O JSON Schemas + sha256
  prompt hash).
- Linear executor with typed retry policy + provider fallback chain.
- `BaseLLMProvider` Protocol behind which the LiteLLM adapter lives — direct
  LiteLLM imports are forbidden in user code.
- `LiteLLMProvider` with typed exception mapping (auth, rate-limit, timeout,
  context-length, content-filter, model-unavailable). LiteLLM's own retry
  layer disabled (`num_retries=0`) so movate's policy owns retries.
- `MockProvider` — deterministic, network-free; judge-aware (returns a JSON
  score when the prompt contains `Rubric:`).
- Versioned pricing table at `providers/pricing.yaml`; cost-drift detection
  vs LiteLLM's reported cost (>5% logs loud).
- Per-run budget enforcement (`max_cost_usd_per_run` aborts with a typed
  error before billing).
- SQLite storage for runs, failures, and evals; idempotent `init()`.
- Stdout tracer (JSON-on-stderr, OTel-shaped span schema).
- Commands: `init`, `validate`, `show`, `run`, `doctor` (with API-key,
  pricing, and config presence checks).
- Agent template (`movate init`) ships with prompt, schemas, and a 2-case
  eval dataset.

### Added — Evals & bench (Phase 2 / v0.2)

- **Eval engine** (`movate.core.eval`) — dataset loader (sha256-stamped),
  judge config loader (auto-discovers `<agent>/evals/judge.yaml`), exact-match
  + LLM-as-judge scorers, N runs per case, aggregation modes
  (`mean | min | p10`).
- **Cross-family enforcement** (`assert_cross_family`) — Azure OpenAI is
  treated as the same family as OpenAI (shared weights → shared blind spots).
  Configs that share a family between agent and judge are rejected at
  parse time, not run time.
- **`movate eval`** — `--gate`, `--gate-mode`, `--runs`, `--mock`,
  `-o table | json | markdown`. Persists `EvalRecord` to sqlite for v0.4
  baseline diffing.
- **`movate bench`** — multi-model comparison with `--model` (repeatable)
  and `--judge`. Reuses `Executor.execute(model_override=…)` so each row
  tests exactly one model with no fallback contamination. Cross-family
  judge skips per-row with a stderr note rather than failing the whole
  bench. Reports cost (mean), latency (p50, p95), aggregated score, errors,
  and a sample output per model.
- **`movate pricing`** — Rich table + `-o json` + `-p <prefix>` filter.
- **Markdown reporter** — `render_eval_markdown` + `render_bench_markdown`
  in `movate.core.reporters`. GFM-safe (pipe-escape, backtick → `&#96;`,
  60-char input truncation). Suitable for `gh pr comment -F -`.

### Added — Templates

- Three new packaged templates beyond `default`:
  - **`faq`** — `{question}` → `{answer, confidence}`. Ships with
    `judge.yaml.example` (semantic-correctness rubric).
  - **`summarizer`** — `{text, max_words}` → `{summary, word_count}`. Ships
    with `judge.yaml.example` (faithfulness/coverage/brevity rubric).
  - **`classifier`** — `{text, labels[]}` → `{label}`. Exact-match-friendly
    (finite label set; no judge needed).
- Template registry at `src/movate/templates/__init__.py` — `TEMPLATES`
  dict + `get_template_path()` + `list_templates()`. New templates =
  drop a directory + add a line.
- `movate init -t {default|faq|summarizer|classifier}`.

### Added — `movate.testing` (consumer surface)

- Public package at [`src/movate/testing/`](src/movate/testing/):
  - **Doubles** — `InMemoryStorage` (StorageProvider conformance with
    filters), `NullTracer` (capture spans + events), `JudgeStubProvider`
    (splits agent vs judge prompts; captures bodies for assertion),
    re-exported `MockProvider`.
  - **Scaffolding** — `scaffold_agent(dst, name, template=…)` clones a
    packaged template; `build_test_executor(...)` wires test doubles into
    a ready-to-use Executor.
  - **Pytest fixtures** — `mock_provider`, `in_memory_storage`,
    `null_tracer`, `pricing`, `temp_agent_dir`, `build_executor`.
    Activate by adding `pytest_plugins = ["movate.testing.fixtures"]`
    to a consumer's `conftest.py`.

### Added — CI + smoke

- `.github/workflows/ci.yml` runs ruff, mypy, and `pytest -m "not smoke"`
  on every PR.
- Live-API smoke at `tests/test_smoke_litellm.py` — 3 tests (OpenAI direct,
  Anthropic direct, full executor against real OpenAI), gated by both
  `MOVATE_SMOKE=1` and the relevant API key. CI excludes the marker.
- `scripts/smoke.sh` — one-command invocation that sources `.env` and
  reports key presence before running.

### Architecture decisions locked

- **No LangGraph in v0.x.** Workflow orchestration (Phase 3 / v0.3) ships
  on a homegrown `WorkflowGraph` IR; LangGraph slots in as an alternative
  compiler at v1.1+ when conditional routing / parallel / HITL / checkpointing
  are actually needed.
- **LiteLLM is implementation detail.** All user code goes through
  `BaseLLMProvider`. The seam keeps room for v1.1+ provider routing.
- **Pricing is canonical, not inferred.** LiteLLM's reported cost is logged
  for drift detection but never used for billing.

[0.2.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.2.0
