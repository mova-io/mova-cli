# Changelog

All notable changes to movate. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [CalVer](https://calver.org/) `YYYY.M.D.N` (date-based, auto-bumped
per commit). Releases prior to 2026-05 used SemVer (`v0.x`); those tags remain as history.

## [Unreleased]

### Added — `movate watch` hot-reload for the dev inner loop (post-v1.0)

**TDD-style feedback while iterating on a prompt.** Saves the file
→ sees lint + cost-forecast + validate result in <1s. No more
manual `movate validate` after every edit.

- **`movate watch <agent>`** — polls the agent's files (agent.yaml,
  prompt, both schemas, dataset, judge config if present) every
  0.5s and re-runs `movate validate` whenever any of them changes.
  Each re-run prints with a timestamp so you can correlate save →
  result.
- **Stdlib polling, not `watchdog`/`watchfiles`.** Adding a runtime
  dep for one dev-loop command isn't worth it; mtime polling at
  500ms is fast enough for human keystrokes, works identically on
  every platform (no FSEvents vs inotify quirks), and pulls zero
  extra deps.
- **Resilient to broken-mid-save state.** Editors that do
  write-then-rename can briefly produce an unparseable `agent.yaml`.
  The watcher catches `AgentLoadError` on each re-derive and keeps
  going with the previous file set — fix the file and the watcher
  picks up the next valid state.
- **200ms debounce** after detecting a change so an
  atomic-write-then-rename doesn't dispatch twice for one save.
- **Flags:**
  * `--poll-interval N` (default 0.5s; raise for slow shared FS)
  * `--strict` — pass-through to validate (promote lint warnings
    to errors)
- **Initial dispatch on entry** — operator sees current state
  without having to make a no-op edit.
- **Ctrl-C** exits cleanly. The watcher is intentionally
  foreground-blocking; no daemon mode.
- **Dispatch is split from the loop** (`dispatch_once(agent_dir,
  *, strict)`) so tests can drive it deterministically without
  spinning up real `time.sleep` machinery.
- 8 new tests in `tests/test_watch.py` covering: path-discovery
  includes all expected files (yaml + prompt + schemas + dataset)
  + excludes missing optional files (judge), dispatch returns 0
  for clean / 2 for broken agent, --strict promotes warnings,
  CLI help renders, end-to-end file-change → dispatch via
  short-poll, broken YAML doesn't crash the watcher.

**Operator effect:** prompt-iterating engineer keeps `movate
watch ./agents/faq-agent` running in one terminal, edits prompt.md
in their editor, sees the lint + cost-forecast results scroll by
on save. The "did I break the schema?" check shrinks from a
deliberate command to a passive feedback loop.

### Added — Cost forecast on `movate validate` (post-v1.0)

**Catches "this eval would cost $3" before running it.** Validate
now prints an estimated cost for running ``movate eval`` against
the agent's dataset — char/4 token approximation × dataset size ×
the model's pricing. Quick gut-check before burning real money.

- **`core/cost_forecast.py`** — pure-Python ``estimate_eval_cost``
  + ``CostForecast`` dataclass. Renders each case's prompt with
  Jinja (microseconds per case) so the per-case interpolation is
  captured, not just the template length.
- **Math:** `tokens ≈ chars / 4` (well-established for GPT/Anthropic
  families, ±20% for English). Output budget = `model.params.max_tokens`
  if set, else 500. Cost = `input_tokens/1k × input_per_1k +
  output_tokens/1k × output_per_1k`, summed across cases.
- **Returns `None` (silent skip)** when: no dataset configured,
  dataset file missing, model not in pricing table, dataset empty,
  or every case fails to render. The right UX is absence, not a
  "couldn't estimate" warning that ops learn to ignore.
- **`movate validate`** prints a single dim line on the happy path:
  > `eval cost: ~$0.0045 (30 cases x ~120 in + ~1024 out tokens)`
  
  Hidden when None. No flag needed — it's free info on every
  validate.
- **Cases with invalid inputs are skipped, not crashed.** A case
  whose input refs a missing schema field would raise Jinja's
  `UndefinedError` mid-render; the forecast eats that exception
  and continues so a half-broken dataset still gets a partial
  estimate. The `UNDECLARED_INPUT_REF` prompt-linter rule is the
  right tool for diagnosing that bug; the forecast just keeps
  shipping a number.
- 10 new tests in `tests/test_cost_forecast.py`: None-when-missing
  (dataset / pricing / empty), exact-math on a known pricing
  table, default vs override of `max_tokens`, linear scaling
  (10 cases = 2x 5 cases), invalid-input case skipping, CLI
  integration (prints forecast on scaffold, hides when no dataset).

**Operator effect:** before this, engineer runs `movate eval`
without thinking, lands a $4 cloud bill on a 200-case dataset
with `gpt-4o-2024-08-06`. After this, they see `eval cost: ~$4.12`
at validate time and either swap to `gpt-4o-mini` or accept the
cost knowingly.

### Added — Prompt linter in `movate validate` (post-v1.0)

**Catches real prompt bugs at validate time, not at the first
provider call.** Four rules cover the most common ways an
``agent.yaml`` works in scaffolding but fails in production.

- **`core/prompt_linter.py`** — pure-function rules + ``LintIssue``
  dataclass + ``lint_prompt(bundle)`` orchestrator. Each rule is
  isolated so adding a new one is one function + one test.
- **Rules shipped:**
  * ``UNDECLARED_INPUT_REF`` (error) — the template references
    ``{{ input.X }}`` but ``X`` isn't in the input schema's
    ``properties``. Renders to ``StrictUndefined`` at runtime → the
    real `render_prompt` raises. Catches it before deploy via
    Jinja2 AST analysis (only matches actual Getattr nodes, not
    string literals).
  * ``MISSING_JSON_INSTRUCTION`` (warning) — output schema is an
    object but prompt doesn't mention "json" anywhere. Models wrap
    JSON in prose without an explicit instruction. Case-insensitive
    match.
  * ``NO_OUTPUT_SCHEMA_REFERENCE`` (warning) — prompt mentions NONE
    of the output schema's field names. Models hallucinate field
    names when the expected shape isn't visible in the prompt.
  * ``EMPTY_PROMPT`` (error) — whitespace-only prompt. Scaffolding
    leftover that somehow shipped.
  * ``TINY_PROMPT`` (warning) — under 40 chars of non-whitespace
    content. Scaffolding stub.
- **`movate validate <agent>`** runs lint automatically. Errors
  always exit 2; warnings print but don't fail by default.
  * ``--strict`` — promote warnings to errors (CI gate setting).
  * ``--no-lint`` — skip the linter (schema + policy checks still
    run). Escape hatch for half-baked WIP agents.
- **Output format:** errors first (red ✗ + code + message + dim
  hint line), then warnings (yellow ! + same shape). Each issue
  carries a stable ``code`` so CI annotations can filter or
  suppress specific rules. On a clean pass, validate prints a
  ``lint: ✓ clean`` row to confirm the linter ran.
- 19 new tests in ``tests/test_prompt_linter.py``: each rule gets
  a happy-path + finding test; the default scaffold passes every
  rule (critical — if the scaffold tripped the linter, every
  `movate init` would surface confusing warnings); CLI exit-code
  + flag tests for ``--strict`` / ``--no-lint`` / errors-vs-warnings
  semantics.

**Operator effect:** an engineer copies the scaffold, renames the
input schema field from ``text`` → ``question`` but forgets to
update the prompt's ``{{ input.text }}``. Before this:
``movate validate`` passes; first real ``movate run`` raises
``UndefinedError`` mid-render. After this: `movate validate`
reports `UNDECLARED_INPUT_REF: prompt references {{ input.text }}
but 'text' is not in the input schema's properties` and exits 2.
The bug is caught before commit.

### Added — Per-tenant monthly cost ceiling (v1.0)

**Closes the runaway-cost gap.** Before this, a misbehaving agent or a
customer's prompt that loops through tool calls could rack up
hundreds of dollars before anyone noticed. Now each tenant gets a
monthly USD cap; runs auto-pause when current-month spend hits the
limit; operators triage via the new `movate tenants` CLI.

- **`TenantBudget`** Pydantic model — one row per tenant with
  `monthly_usd_limit: float | None` (None = explicitly unlimited).
  Absent row = unlimited by default (v0.x-compat — no policy
  change for projects that don't opt in).
- **`tenant_budgets` table** added to sqlite + postgres + InMemoryStorage.
  Sqlite via idempotent `CREATE TABLE IF NOT EXISTS` migration;
  postgres natively idempotent; new partial index
  `idx_runs_tenant_created` covers the current-month aggregation
  so the `SUM(metrics->>'cost_usd')` is an index range scan, not a
  table scan.
- **`StorageProvider` gained four methods** (Protocol + all three
  backends):
  * `get_tenant_budget(tenant_id) -> TenantBudget | None` — PK
    lookup; sub-millisecond.
  * `upsert_tenant_budget(budget)` — preserves `created_at` on
    update, refreshes `updated_at` server-side so operators see
    "first set" and "last touched" separately.
  * `list_tenant_budgets() -> list[TenantBudget]` — operator only.
  * `sum_tenant_cost_current_month(tenant_id) -> float` — sums
    `runs.metrics.cost_usd` for rows created since the 1st of
    the current calendar month (UTC). 0.0 if no runs.
- **`Executor._check_tenant_budget`** runs FIRST at execute() entry
  (before the model-policy check + schema validation). No provider
  call fires if budget is breached — zero cost incurred on the
  aborted run. Failure persisted to the `failures` table with
  `tenant_budget_exceeded` type for audit.
- **Self-fixing error message** — surfaces both numbers + the exact
  CLI command to fix it (matches v1.0 stages 3/4 pattern):
  > "tenant 'abc' has spent $5.00 of $1.00 this month; runs are
  > paused. Operator can raise the budget with `movate tenants
  > set-budget abc --monthly-usd <new>` or wait for next-month
  > rollover."
- **`TenantBudgetExceededError`** + `FailureType.TENANT_BUDGET_EXCEEDED`
  + entry in `DEFAULT_RETRY` (no retry, no fallback — a cheaper
  model wouldn't help; the cap is the cap).
- **`movate tenants` CLI** with four subcommands:
  * `set-budget <tenant> --monthly-usd <amount>` — set or update.
  * `clear-budget <tenant>` — sets `monthly_usd_limit = NULL`
    (row stays for audit history; cap becomes unlimited).
  * `show <tenant>` — Rich table with budget, spent this month,
    remaining, color-coded status (green / yellow ≥80% / red
    when paused), audit timestamps.
  * `list` — every configured budget oldest-first with the same
    status column.
- 24 new tests in `tests/test_tenant_budget.py` covering: storage
  round-trip (no-row default, upsert preserves `created_at`,
  clear-via-None, list ordering, sum-zero-when-empty, sum-only-
  this-month-and-this-tenant), executor enforcement (no-row
  allows, None-limit allows, spend-meets-budget blocks before
  provider call, operator pointer in error message, under-budget
  proceeds), and CLI integration (set persists + show reads back,
  clear flips to unlimited, show for unknown tenant reports
  no-row, list enumerates with color status, set rejects
  negative).

**Race window:** under high concurrency two simultaneous runs can
both observe "under budget" and both succeed, pushing combined cost
past the cap. The overrun is bounded by the in-flight call count
(typically <10 for a single tenant). Operators should set the cap
slightly below the hard ceiling they actually want to enforce.
Stronger guarantees (SELECT FOR UPDATE on the budget row + a
ledger table) lands post-v1.0 if a customer asks.

**Operator workflow when a budget is breached:**

```
$ movate tenants show <tenant-id>
# → paused (over budget). spent $523.40 of $500.00.

$ movate tenants set-budget <tenant-id> --monthly-usd 1000
# → budget raised; next run for this tenant proceeds.

# OR: wait for the 1st of next month — sum_tenant_cost_current_month
# returns 0 again, the tenant un-pauses automatically.
```

### Changed — Worker autoscaling: CPU → KEDA Postgres queue-depth (post-v1.0)

**Leading-indicator scaling.** Before this, the worker Container App
scaled on CPU utilization — a *lagging* indicator (CPU rises only
after a backlog has built up + been claimed). Now it scales on
**queue depth** via the KEDA postgresql scaler — the load is
visible BEFORE any pod's CPU rises.

- **`containerapp-worker.bicep`** scale rule replaced:
  * Old: ``type: 'cpu', metadata: {type: 'Utilization', value: '70'}``
  * New: ``type: 'postgresql'`` with the query
    ``SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND
    (next_retry_at IS NULL OR next_retry_at <= NOW())``.
  * Filters on the same claimable-set as ``claim_next_job`` — so
    re-queued jobs awaiting backoff don't artificially inflate the
    scale-up signal.
- **`queueDepthPerReplica` param** (default 5 in the module, set
  to 10 in prod / 3 in dev via ``main.bicep``). Desired replicas =
  ``ceil(queryResult / queueDepthPerReplica)``, clamped to
  ``[minReplicas, maxReplicas]``. KEDA evaluates ~every 30s.
- **New KV secret ``pg-connection-string``** — full libpq DSN for
  the KEDA scaler. Required because KEDA runs in ACA's environment
  sidecar (outside the worker container) and needs a self-contained
  connection string. Distinct from ``pg-password`` (which the
  worker uses via PGPASSWORD). The operator runbook
  (``docs/azure-bootstrap.md`` step 4 + ``infra/azure/README.md``
  KV-population block) walks through setting this during the
  two-pass deploy.
- **`KEDA_PG_CONNECTION_STRING` env var** on the worker container —
  references the new KV secret. Consumed by KEDA's
  ``connectionFromEnv`` field, not by the worker process itself.

**Operator effect:** when 50 jobs hit the queue, the worker scales
up within ~30s (next KEDA evaluation cycle) instead of waiting for
CPU to register the backlog. For an agent that's I/O-bound on a
provider call, CPU might never rise enough to trigger the old
rule — KEDA catches that case correctly.

**What's NOT covered:** scale-to-zero. ACA + KEDA support it, but
the worker keeps ``minReplicas >= 1`` so a job submitted in the
first 30s after a quiet period doesn't wait for a cold-start.
Operators can opt in by setting ``minReplicas: 0`` if cost matters
more than first-job latency.

### Added — Per-API-key rate limiting (post-v1.0)

**Protects the deployed runtime from runaway clients.** Before this,
a single misbehaving consumer could flood ``POST /run`` and starve
every other tenant's quota. Now each API key gets its own
token-bucket budget; overflow returns 429 + ``Retry-After`` so
well-behaved clients recover automatically.

- **`core/rate_limit.py`** — pluggable rate limiter:
  * ``RateLimiter`` Protocol — single ``check(key)`` method
    returning a ``RateLimitDecision`` (allowed, limit, remaining,
    reset_at_unix, retry_after_seconds).
  * ``InProcessRateLimiter`` — token bucket per key, dict-backed,
    single-process. Default for v1.x. Memory grows linearly with
    distinct keys (~tens of bytes per key).
  * ``NoOpRateLimiter`` — always-allow fallback. Used when limit is
    disabled. Headers still attach with sentinel ``Limit: 0`` so
    operators see "rate limiting OFF" at a glance.
  * Future ``RedisRateLimiter`` slots in against the same Protocol
    when multi-replica shared state is actually needed (post-v1.x).
- **Algorithm:** token bucket (NOT leaky bucket) to tolerate
  realistic bursts. A client quiet for a minute can spend the full
  60-token budget in one go; steady-state still averages to
  ``limit_per_minute``. ``time.monotonic`` for rate windows (immune
  to NTP corrections) + ``time.time`` for the reset-at header (real
  Unix timestamp clients expect).
- **`build_app(storage, *, rate_limit_per_minute=60)`** — default
  60 req/min/key, matching the BACKLOG plan. Pass ``0`` (or
  ``None``) to disable.
- **Middleware integration** — the rate-limit check runs AFTER
  successful auth (so anonymous/invalid-key floods get 401 cheaply
  before touching the limiter). Bucket key is ``record.key_id``
  (stable across token refreshes for the same logical key).
  ``/healthz`` and ``/ready`` are unauthed → bypass the limiter
  entirely so ACA's 10-second readiness probe + 30-second liveness
  probe never burn a budget.
- **Response headers** (every authenticated response, success or
  429):
  * ``X-RateLimit-Limit`` — bucket capacity
  * ``X-RateLimit-Remaining`` — tokens left (integer floor)
  * ``X-RateLimit-Reset`` — Unix timestamp when bucket will be full
  
  429 responses additionally carry ``Retry-After`` (RFC 7231
  delta-seconds, integer ceiling).
- **`ErrorCode.RATE_LIMITED`** + ``rate_limited()`` helper in
  ``runtime/errors.py`` — matches the existing 401/404 envelope
  shape with stable code, human-readable message.
- **`movate serve --rate-limit-per-minute`** + env var
  ``MOVATE_RATE_LIMIT_PER_MINUTE``. Startup banner surfaces the
  configured value (or ``DISABLED`` in yellow when off).
- 16 new tests in ``tests/test_rate_limit.py``:
  * Pure-math (clock-mocked): bucket starts full, drains, refills
    with elapsed time, capacity caps refill at idle, per-key
    isolation, ``retry_after`` ceiling math, ``limit_per_minute<1``
    raises at construction, ``NoOpRateLimiter`` always allows.
  * Middleware integration: auth'd responses carry headers, 4th
    request after a 3-token drain returns 429 + Retry-After,
    unauthenticated floods aren't rate-limited (auth fails first),
    ``/healthz`` + ``/ready`` not rate-limited, per-key isolation
    at the HTTP layer, ``rate_limit_per_minute=0`` returns the
    sentinel zero-limit headers, end-to-end recovery after the
    retry window elapses (via the clock-monkeypatch).

**Operator effect:** a single tenant flooding ``POST /run`` at 600
req/min stops getting 5xx-amplification at the worker — they get
clean 429s with a ``Retry-After`` telling them when to back off.
Other tenants' quotas are unaffected (per-key buckets are
independent).

### Added — `/ready` endpoint with deep checks (post-v1.0)

**Stops ACA from routing traffic to broken pods.** Before this, ACA's
readiness probe hit ``/healthz`` (unconditional 200) — meaning a pod
whose Postgres connection was dead still received traffic and 5xx'd
every request. Now ``/ready`` runs deep checks (storage ping); 503
when anything's broken so ACA pulls the pod out of rotation
WITHOUT restarting it (a restart wouldn't help if the DB is the
problem). The pod returns to the load balancer once the dependency
recovers.

- **`GET /ready`** — unauthed readiness probe with per-check status.
  Returns 200 + ``{"status": "ready", "checks": {...}}`` when every
  check passes; 503 + ``{"status": "not_ready", "checks":
  {"storage": "<error type + truncated message>"}}`` when any
  fails. ACA reads the HTTP status; the JSON body is for human
  triage via curl. Truncates error messages to 120 chars so we
  don't leak DSNs or internal context.
- **`StorageProvider.ping()`** — new Protocol method. Sqlite does
  ``SELECT 1``; postgres does ``SELECT 1`` against the pool
  (exercises the same path real queries take, catching
  pool-exhausted on top of DB-down). `InMemoryStorage.ping()` is a
  no-op (tests that exercise the failure path use a custom
  subclass that overrides ping to raise).
- **`ReadyView`** schema — separate from `HealthView` so the two
  probes have distinct contracts. `/healthz` stays minimal
  (`status` + `version`); `/ready` carries the per-check map for
  triage.
- **`/healthz` stays unconditional 200.** Deliberately doesn't gate
  on storage because a DB blip would otherwise trigger pod
  restarts that don't help. Liveness checks "is this process
  alive?"; readiness checks "should this process get traffic?"
  Separate concerns.
- **Bicep `containerapp-api.bicep`** — readinessProbe path flipped
  from `/healthz` to `/ready`. Liveness probe unchanged (stays on
  `/healthz`). Cadence unchanged (10s readiness, 30s liveness).
- 3 new tests in `tests/test_runtime_app.py`: 200 happy path, 503
  with the right error info when storage ping fails (via a
  `FailingStorage` subclass that raises on `ping()`), and unauthed
  access works (ACA hits without bearer).

**Operator effect:** during a planned Postgres failover window
(~30s), ACA will mark every API pod NotReady → stop routing →
client retries succeed once Postgres comes back. Without this,
clients would see 30s of 500s instead.

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

## [0.8.0] — 2026-05-19

**movate now has a Knowledge Base.** v0.8 ships a full RAG pipeline —
ingest → embed → hybrid search → rerank — integrated end-to-end into
the agent executor. Agents can retrieve from their own KB on every
run without any custom code.

| PR | what shipped |
|---|---|
| A | `kb_chunks` storage layer (SQLite FTS5 + Postgres GIN); `mdk kb ingest` CLI |
| B | `citation_accuracy` eval dimension; automated grounding scorecard |
| C | Hybrid BM25 + vector search with Reciprocal Rank Fusion |
| D | KB upload via Chainlit playground (`mdk playground`) |
| E | Query rewriter + fan-out retrieval (multi-query expansion) |
| F | LLM rerank stage — cross-encoder scoring over top-k candidates |
| G | PDF parser for KB ingest |
| H | Multi-hop retrieval loop; agent can issue follow-up queries |
| I | Per-agent retrieval config in `agent.yaml` (`retrieval:` stanza) |
| J | `mdk validate` warns on orphan retrieval config |
| K | Retrieval observability: per-stage latency + chunk counts in trace |
| L | DOCX parser (python-docx) |
| M | HTML parser (BeautifulSoup) |

### Added — Knowledge Base storage and ingestion

- **`movate.kb.storage`** — `kb_chunks` table with FTS5 (SQLite) and
  GIN (Postgres) full-text indexes. Supports per-agent, per-source,
  and per-tenant isolation. `delete_kb_chunks` for clean re-ingest.
- **`mdk kb ingest <path>`** — recursively ingest PDF, DOCX, HTML,
  Markdown, and plain-text files into the agent's KB. Env-var tunable:
  `MOVATE_BM25_K1`, `MOVATE_BM25_B`, `MOVATE_RRF_K`. Options:
  `--clean-source` (delete old chunks before re-ingest),
  `--ocr-lang`, `--ocr-backend` (Tesseract or EasyOCR).
- **`mdk kb search <agent> <query>`** — interactive retrieval CLI;
  shows raw hybrid-search output with BM25 + vector + RRF scores.
- **`mdk kb stats [--by-source]`** — chunk counts and size breakdown.

### Added — Retrieval pipeline internals

- **Hybrid search** (`movate.kb.lexical` + `movate.kb.search`):
  BM25 scores from FTS5/GIN merged with cosine-similarity vector
  scores via Reciprocal Rank Fusion. All three constants are
  env-var tunable for operator A/B testing.
- **Query rewriter** (`movate.kb.rewrite`): expands the user query
  into up to N variants; fan-out retrieves from each; results
  deduplicated before rerank.
- **LLM rerank** (`movate.kb.rerank`): sends the top-k retrieval
  candidates back to the model for cross-encoder-style scoring;
  returns a re-ordered slice the executor injects as context.
- **Multi-hop loop**: executor runs up to `retrieval.max_hops`
  retrieval rounds when the model signals it needs more evidence.
- **File-size guard**: files above `MOVATE_MAX_FILE_MB` (default 50)
  are skipped with a warning rather than silently timing out the
  PDF parser.

### Added — Document parsers

- **PDF** (`movate.kb.parsers.pdf`): pdfminer.six text extraction
  with Tesseract OCR fallback for scanned/image-only pages.
  Per-page mixed mode, DPI 300, `--oem 1 --psm 6`, whitespace normalisation.
  EasyOCR backend selectable via `MOVATE_OCR_BACKEND=easyocr`.
- **DOCX** (`movate.kb.parsers.docx`): python-docx; preserves
  paragraph structure and table cells as separate chunks.
- **HTML** (`movate.kb.parsers.html`): BeautifulSoup; strips
  scripts/styles, preserves heading hierarchy as chunk metadata.
- **Image OCR** (`movate.kb.parsers.image`): standalone Tesseract
  / EasyOCR wrapper for PNG/JPG KB assets.

### Added — Eval: citation accuracy dimension

- **`citation_accuracy`** scorecard category: checks that every
  factual claim in the agent output is supported by a retrieved
  chunk. Rubric is configurable per agent in `evals/dataset.jsonl`
  (`contexts` field). Grounding enforcement modes M2-M6 let operators
  gate deploys on minimum citation scores.

---

## [0.7.0] — 2026-05-13

**movate grows a Teams front door and a native provider layer.** v0.7
ships native Anthropic and OpenAI providers (bypassing LiteLLM for
features LiteLLM doesn't yet surface), a full Microsoft Teams bot
integration, and the Runtime API that the Angular `mova.io` front end
consumes.

### Added — Native LLM providers

- **`runtime: native_anthropic`** (`movate.providers.anthropic`):
  calls the official `anthropic` SDK directly. Supports `to_tool_spec`
  in Anthropic's flat `{name, description, input_schema}` shape;
  translates the executor's OpenAI-style message history (tool_calls +
  tool results) into Anthropic content blocks transparently.
  Prompt caching, thinking blocks, and vision are architecturally
  ready; deferred to follow-ups.
- **`runtime: native_openai`** (`movate.providers.openai_native`):
  calls the official `openai` SDK directly. Same executor contract
  as LiteLLM; `tools=` passthrough; `tool_calls` parsing. `pricing_key`
  bridges bare model ids (`gpt-4o-mini-2024-07-18`) to the
  `openai/...` pricing-table keys.
- Both providers implement full exception translation
  (`AuthError`, `RateLimitError`, `MovateTimeoutError`, etc.) matching
  the LiteLLM taxonomy so the executor's retry/fallback layer is
  provider-agnostic.

### Added — Microsoft Teams bot (Slices 3.1.a–e)

- **Slice 3.1.a** — Bot Framework webhook skeleton: POST `/api/messages`
  receives Activity objects, dispatches text messages to the executor.
- **Slice 3.1.b** — Adaptive Cards: agent responses render as rich
  cards with collapsible source citations.
- **Slice 3.1.c** — Per-user identity binding: `/movate connect`,
  `/whoami`, `/disconnect` slash commands map Teams user ids to
  movate tenant identities.
- **Slice 3.1.d** — File attachment handling: users can upload
  documents directly in Teams; files are ingested into the agent's KB.
- **Slice 3.1.e** — Azure Bot Service manifest + registration Bicep;
  UAI for ACA. `mdk teams deploy` one-command bot provisioning.

### Added — Runtime API (Groups G + H)

- **Group G** — Angular cross-cutting: CORS headers, `/api/v1` prefix
  mount, OpenAPI client generation docs.
- **Agent CRUD**: `POST /api/v1/agents` (create from wizard payload),
  `GET /api/v1/agents`, `GET /api/v1/agents/{name}` (profile detail).
- **Run + eval endpoints**: `POST /api/v1/agents/{name}/runs`,
  `POST /api/v1/agents/{name}/validate` (shippability gate),
  `GET /api/v1/runs/{run_id}/trace` (replay for trace-viewer).
- **Group H** — Eval endpoints: kickoff, retrieval, list.
- **GitHub integration** (ADR 007): `POST /agents/{name}/publish`
  (feature-flagged), `GET /agents/{name}/history` (commit log).
- **`mdk rename`** — rename an agent in place (updates agent.yaml,
  directory, and all cross-references).

---

## [0.6.0] — 2026-05-13

**movate gains a skills system and ships to Azure.** v0.6 extends
the agent model with callable skills (Python functions, HTTP
endpoints, MCP servers), adds an eval gating system for CI, and ships
the first full Azure production deployment.

### Added — Skills system (PRs 1–7)

- **Python skill backend** (`implementation.kind: python`): entry-point
  import path resolves at load time; callable is invoked with
  `(input: dict, ctx: SkillExecutionContext)`.
- **HTTP skill backend** (`implementation.kind: http`): calls an
  external REST endpoint; supports Bearer auth, timeout, schema
  validation of the response.
- **MCP skill backend** (`implementation.kind: mcp`): wraps an MCP
  server tool call; the executor manages the MCP session lifecycle.
- **`SkillPolicy`**: per-skill call budget, timeout, and allowed-input
  schema enforcement — violations surface as typed `SkillError`.
- **`mdk skills list`** — show all skills in scope with backend type,
  version, and cost.
- **`mdk skills scaffold <name>`** — generate a new skill directory
  with `skill.yaml` + implementation stub.
- **`mdk skills run <name> --input '{"k":"v"}'`** — invoke a skill
  directly from the CLI for local iteration without running a full agent.

### Added — Eval and validation improvements

- **`mdk eval <url>`** — run an eval against a remotely-served agent;
  no local project required.
- **Four-dimension eval reporting**: `accuracy`, `completeness`,
  `format`, `safety` — each dimension scored separately, displayed in
  a colour-coded table.
- **Per-objective gating**: `evals/dataset.jsonl` entries can specify
  `min_score` per dimension; `mdk eval --gate 0.8` blocks on any
  failing objective.
- **`mdk doctor --explain`** — each diagnostic prints a remediation
  hint rather than a bare pass/fail.
- **`mdk import json <file>`** — convert a JSON array of
  `{input, expected}` objects into a `dataset.jsonl` eval file.

### Added — Agent config and templates

- **Inline YAML schema shorthand**: `input: {field: type}` instead of
  full JSON Schema — the loader expands it automatically.
- **Canonical config split**: `agent.yaml` holds the agent declaration;
  `policy.yaml` holds guardrails + budget limits (optional, merged at
  load time).
- **`mdk` binary alias**: `mdk` is now the preferred short alias
  alongside `movate`.
- **FAQ agent template** (`mdk add faq`): pre-wired with a confidence
  rubric, grounding enforcement, and a 15-case eval dataset.
- **Lyzr adapter** (`runtime: lyzr`): thin shim routing to Lyzr's
  hosted agent API; maps the response to `CompletionResponse`.

### Added — Production deployment

- **Azure Bicep IaC**: Azure Container Apps + Postgres Flex Server +
  ACR + Key Vault + Log Analytics — full production stack in one
  `az deployment` invocation.
- **`movate deploy`** (`mdk deploy`): builds image, pushes to ACR,
  triggers ACA revision.
- **Per-tenant monthly cost ceiling**: `budget.monthly_usd_ceiling`
  in `policy.yaml`; the executor soft-blocks new runs once the limit
  is reached (returns `cost_limit_exceeded`).
- **Per-API-key rate limiting**: token bucket, `429 + Retry-After`
  headers, configurable per key.
- **Job retry policy**: exponential backoff + dead-letter after max
  attempts; worker continues on adjacent jobs after a failing one.
- **`movate watch`**: polls agent files every 0.5s; re-runs
  `movate validate` (with cost forecast + prompt lint) on any change.
  200ms debounce, resilient to mid-save broken YAML.

---

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

[0.5.0]: https://github.com/mova-io/mova-cli/releases/tag/v0.5.0

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

[0.4.0]: https://github.com/mova-io/mova-cli/releases/tag/v0.4.0

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

[0.3.1]: https://github.com/mova-io/mova-cli/releases/tag/v0.3.1

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

[0.3.0]: https://github.com/mova-io/mova-cli/releases/tag/v0.3.0

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

[0.2.0]: https://github.com/mova-io/mova-cli/releases/tag/v0.2.0
