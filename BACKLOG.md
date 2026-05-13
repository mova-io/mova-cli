# movate — Feature Backlog

A ranked, checkable list of features for movate. Each item is sized to "thing a user could notice or test." This is the working backlog — the high-level phasing lives in the [implementation roadmap](../../.claude/plans/want-to-take-inspiration-stateful-swan.md).

## How to read this

```
- [ ] **Feature name** `[LEVERAGE] [PHASE] [STATUS] [EFFORT]` — one-line description.
```

| Tag | Meaning |
|---|---|
| `[HIGH]` | Big unlock per unit effort. Ship these first. |
| `[MED]` | Worth doing but not urgent. |
| `[LOW]` | Nice-to-have or stop-energy. Defer or drop. |
| `[v0.1]…[v1.1+]` | Target release phase. |
| `[done]` | Already in repo. |
| `[next]` | Top-of-stack — pick this up next. |
| `[blocked:X]` | Waiting on X. |
| `[idea]` | Captured but no commitment. |
| `≤2h / ≤1d / 2-3d / 1w / 2w+` | Effort estimate. |

**Leverage** = (value to a movate user) ÷ (engineering effort + ongoing maintenance burden). When in doubt, prefer items with strong leverage even if their phase is later — you'll re-evaluate when those phases land.

---

## 🎯 Top 10 highest-leverage shortlist

> _Sessions are recorded most-recent first. Today's section (2026-05-13)
> closed three major lines: **ADR 002 (skills × every runtime)**, **the
> 4-dim eval reporting line item from Tier 7**, and **the first half of
> the Teams demo loop**. Full per-PR detail in
> [docs/progress/2026-05-13.md](docs/progress/2026-05-13.md)._

### Session 2026-05-13 — Teams demo loop + ADR 002 closeout

**Teams Slice 3.1.c — per-user identity binding shipped this session.** 33 new tests (1230 → 1263 total). Each Teams user binds their own Movate API key via DM (`/movate connect <api-key>`) so every `@movate run` they trigger uses THEIR key, with `RunRecord.created_by` audit attribution correct in multi-user deployments. Keys encrypted at rest via Fernet (`cryptography` package, AES-128-CBC + HMAC-SHA256); operator supplies the key via `MOVATE_TEAMS_ENCRYPTION_KEY` env, bot fails fast at boot if missing. New modules: `src/movate/teams_bot/crypto.py` (Fernet wrapper, typed errors), `storage.py` (aiosqlite `teams_users` table — sqlite-only for alpha, Postgres tracked as follow-up), `identity.py` (LRU-cached per-user MovateClient resolver). New DM-only commands: `connect`, `whoami`, `disconnect` — channel posts rejected with a "DM me" card so keys never leak. `run` routes through resolver first (bound user) → fleet client (unbound, default) → reject card (unbound + `--require-binding`). Three modes: default (alpha), strict (`--require-binding`), no-identity (smoke-test). KMS swap is interface-compatible. **PR #83. Closes [#67](https://github.com/jeremyyuAWS/movate-cli/issues/67).**

**Teams Slice 3.1.b — Adaptive Cards + MovateClient integration shipped this session.** 30 new tests. Replaced the 3.1.a echo with real agent execution + a rendered scorecard. New `src/movate/teams_bot/cards/` package (pure functions `RunResponse → dict`): `run_result.py` (response body, cost, latency, optional Langfuse trace link), `error.py` (failure card with category + one-line hint, no stack traces — those live in Langfuse). New `src/movate/teams_bot/client.py` with `execute_run()` wrapping `MovateClient.submit_job` → `wait_for_terminal` → `get_run`, returning a discriminated `RunOutcome`. Per-call timeout configurable via `MOVATE_TEAMS_RUN_TIMEOUT_S` (default 25s, under Teams' channel timeout). Handler dispatches on the variant — success / terminal_failure / timeout / client_failure / parse_error → distinct cards. Hint lookup table for operator-friendly suggestions on common error codes. New `Attachment` model on `ReplyActivity` for delivering Adaptive Cards inline. `build_app()` now accepts `runtime_url` / `fleet_api_key` / `langfuse_public_host` / `runtime_client` args; holds a long-lived MovateClient so the connection pool stays warm across requests. **PR #82. Closes [#66](https://github.com/jeremyyuAWS/movate-cli/issues/66).**

**Teams Slice 3.1.a — Bot Framework webhook skeleton shipped this session.** 28 new tests. First half-day slice of [ADR 003](docs/adr/003-teams-integration.md) — `mdk teams-bot serve` opens a FastAPI app on port 3978 (Bot Framework Emulator default), parses `@movate <command>` mentions, replies with plain text for `ping` / `help` / `run` (3.1.a echoes parsed args; 3.1.b wires real execution). Hand-rolled the Activity protocol (Pydantic models for Activity, ChannelAccount, ConversationAccount, Mention, ReplyActivity) instead of pulling in `botbuilder-core` (~30MB of azure-core transitive deps + JWT validation we don't need in the local-dev skeleton). New `[teams]` extra: `fastapi` + `uvicorn`. New `mdk teams-bot serve` Typer subcommand under Deploy & operate panel. Hardening PR for JWT validation lands separately before public exposure. **PR #63. Closes the skeleton portion of [#66](https://github.com/jeremyyuAWS/movate-cli/issues/66) and lays the foundation for slices 3.1.b through 3.1.e.**

**ADR 003 — Microsoft Teams as a self-serve front door drafted this session.** 360-line design doc capturing the CEO ask: sales team should do the first demo themselves. Cardinal rule: Teams is a *client* of the existing v0.5 HTTP runtime — no new execution surface, no forked storage. Three slices: 3.1 run-existing → 3.2 eval-with-upload → 3.3 saved-configs + scheduled. Auth model: bot-fleet API key for admin ops + per-user Movate API keys bound via DM (3.1.c, shipped). Four falsification tests documented. Status: **Proposed** (open questions on multi-tenant prospects, Langfuse trace linking, mobile UX, streaming responses called out for follow-up). **PR #60.** Establishes the v0.7 milestone.

**Skills PR 6 — native tool-use across every runtime shipped this session.** 30 new tests across two PRs (#61 Anthropic, #62 OpenAI). **Closes [ADR 002](docs/adr/002-skills-and-contexts.md) completely** — every skill backend (Python / HTTP / MCP) now runs under every runtime (LiteLLM / native_anthropic / native_openai). Native Anthropic (PR #61): `to_tool_spec` override emitting Anthropic's flat `{name, description, input_schema}` shape; `complete()` parses `tool_use` content blocks → `CompletionResponse(kind="tool_use", ...)`; new `_translate_messages` helper folds the executor's OpenAI-style history (`role="assistant"` with `tool_calls` + `role="tool"` results) into Anthropic content blocks (`tool_use` on assistant messages, `tool_result` on user messages, consecutive tool_results coalesced into one user message). Native OpenAI (PR #62): smaller diff because the OpenAI SDK accepts the same flat-message + nested-tool-spec format the LiteLLM path uses — just `tools=` passthrough + response-shape parsing. First-wins on parallel tool calls (matches LiteLLM PR 1 decision; parallel-dispatch is tracked as [#74](https://github.com/jeremyyuAWS/movate-cli/issues/74)). End-to-end integration tests with scripted FakeClients + real Python skills assert wire-payload correlation. **PRs #61 and #62.**

**Four-dimension eval reporting shipped this session.** 34 new tests + 2 reporter tests (1166 → 1200 total). **Flips the Tier 7 line item to done.** Every successful eval run now scores up to four dims per case: `accuracy` (existing v0.5 logic, exact-match OR LLM judge), `faithfulness` (LLM judge against optional `grounding` context — new dataset field), `coverage` (deterministic substring match against optional `expected_coverage` — new dataset field), `latency` (1.0 within budget, linear decay to 0.0 at 2x budget — uses agent's `timeouts.call_ms` or per-case `latency_budget_ms` override). New types: `Dimension` StrEnum, `DimensionScore`, `DimensionScores`, `DimensionalMeans`. Engine refactor with `_score_dimensions` orchestrator + per-dim methods. Critical back-compat: `CaseRun.score` (gate input) stays **accuracy-only** — `--gate 0.7` still means "70% accuracy across cases"; the other three dims are reporting-only. Rich table + JSON + markdown reporters all render the dimensional breakdown — but only when the dataset opts in to `faithfulness` or `coverage` (legacy datasets see the v0.5 view byte-for-byte). **PR #59.**

**Skill side_effects policy gate shipped this session.** 16 new tests. New `SkillPolicy` model in `core/config.py` with `allowed_side_effects: list[SkillSideEffects] | None`. Enforced at both `mdk validate` time (operators see policy violations before any execute attempt) AND executor entry (belt-and-braces — `mdk serve` can't bypass). Wired into `cli/_runtime.py` via `project_cfg.skills`. Closes the operator-confidence gap left after the canonical config split — operators can now declare "this project only runs `read-only` skills" and have it enforced uniformly. **PR #58.** Hardens the prospect-upload story for the eventual Teams slice 3.1.d.

**ADR 002 — Skills and shared contexts fully closed.** Today's PRs (#58 / #61 / #62) plus yesterday's #51-#57 close every slice in ADR 002. Skills work end-to-end across Python / HTTP / MCP backends and across LiteLLM / native_anthropic / native_openai runtimes; side_effects policy gates the inputs; shared contexts prepend automatically to prompts. Closing the ADR moves the Tier 1 / Tier 2 Deva-flagged demand to "done for the foreseeable future."

### Earlier sessions (most-recent first below)

**`movate watch` hot-reload shipped this session.** 8 new tests (635 unit + 3 smoke = 638 total). New `cli/watch.py` polls the agent's files (agent.yaml, prompt, both schemas, dataset, judge.yaml) every 0.5s via stdlib mtime checks (no `watchdog`/`watchfiles` dep needed). On change, re-runs `_validate_agent` (which prints lint + cost forecast + validate output). 200ms debounce for editor write-then-rename. Catches broken-mid-save `AgentLoadError` and keeps polling. `--poll-interval` + `--strict` flags. **TDD-style feedback loop: save the prompt, see results in <1s.**

**Cost forecast shipped this session.** 10 new tests (627 unit + 3 smoke = 630 total). New `core/cost_forecast.py` with `estimate_eval_cost(bundle, *, pricing) -> CostForecast | None`. Renders each case's prompt with Jinja, estimates tokens via chars/4 (well-established for GPT/Anthropic), multiplies by the agent's model's pricing. Prints `eval cost: ~$0.045 (30 cases x ~120 in + ~1024 out tokens)` on every `movate validate` when both a dataset + pricing entry exist; silent skip otherwise. Cases whose inputs miss schema fields get skipped (the prompt linter is the right tool for THAT diagnostic). **Catches "$4 surprise" bills BEFORE running the eval.**

**Prompt linter shipped this session.** 19 new tests (617 unit + 3 smoke = 620 total). New `core/prompt_linter.py` with four rules: `UNDECLARED_INPUT_REF` (error — Jinja2 AST analysis catches `{{ input.X }}` refs not in the input schema), `EMPTY_PROMPT` (error), `MISSING_JSON_INSTRUCTION` (warning), `NO_OUTPUT_SCHEMA_REFERENCE` (warning — prompt should mention at least one output field name), `TINY_PROMPT` (warning). Wired into `movate validate`: errors exit 2 always; warnings print but don't fail by default; `--strict` promotes warnings to errors (CI gate); `--no-lint` escape hatch. Each issue carries a stable `code` for CI annotation filtering + a `hint` pointing at the fix. Critical correctness check: default scaffold passes every rule (else every `movate init` would surface confusing warnings).

**Per-tenant monthly cost ceiling shipped this session.** 24 new tests (598 unit + 3 smoke = 601 total). New `TenantBudget` Pydantic model + `tenant_budgets` table on all 3 backends. Four new storage methods (`get_tenant_budget`, `upsert_tenant_budget`, `list_tenant_budgets`, `sum_tenant_cost_current_month`) with the perf-critical `(tenant_id, created_at)` index for the aggregation. `Executor._check_tenant_budget` runs FIRST at execute() entry — zero provider cost incurred on a budget-blocked run. New `TenantBudgetExceededError` + `FailureType.TENANT_BUDGET_EXCEEDED` (no retry, no fallback — the cap is the cap). New `movate tenants set-budget | clear-budget | show | list` CLI with color-coded status (green / yellow ≥80% / red paused). Self-fixing error messages include the exact CLI command to raise the cap. Absent row = unlimited (v0.x-compat default). **Closes the runaway-cost gap that v1.0 stages 1-4 left open.**

**KEDA queue-depth worker autoscaling shipped this session.** Bicep-only change in `containerapp-worker.bicep` — replaced the CPU-utilization scale rule with a KEDA `postgresql` scaler that counts claimable jobs (`status='queued' AND retry-window-elapsed`). Queue depth is a leading indicator (load visible before any pod's CPU rises); CPU was lagging. `queueDepthPerReplica` param (default 5; prod 10, dev 3 via main.bicep) controls scale-up aggression. New KV secret `pg-connection-string` for KEDA's sidecar (distinct from the worker's `pg-password`). Operator runbook (`infra/azure/README.md` + `docs/azure-bootstrap.md`) updated. `az bicep build` + `az bicep lint` clean locally.

**Per-API-key rate limiting shipped this session.** 16 new tests (574 unit + 3 smoke = 577 total). Token-bucket algorithm (tolerates bursts) keyed on `api_key_id`. `core/rate_limit.py` with `RateLimiter` Protocol + `InProcessRateLimiter` + `NoOpRateLimiter`. Middleware integration: rate-limit AFTER successful auth (anonymous floods get 401 cheaply); `/healthz` + `/ready` bypass the limiter so ACA probes don't burn budget. Every authenticated response carries `X-RateLimit-{Limit,Remaining,Reset}` headers; 429 adds `Retry-After`. New `ErrorCode.RATE_LIMITED`. `build_app(..., rate_limit_per_minute=60)` default; `movate serve --rate-limit-per-minute` flag + `MOVATE_RATE_LIMIT_PER_MINUTE` env var override. Pass `0`/`None` to disable (NoOp limiter, sentinel `Limit: 0` headers signal "OFF" to operators). Redis-backed shared state slots in as a future `RateLimiter` impl when multi-replica state-sharing becomes load-bearing.

**`/ready` endpoint with deep checks shipped this session.** 3 new tests (558 unit + 3 smoke = 561 total). New `GET /ready` runs storage ping; 503 + per-check failure info if anything's broken. New `StorageProvider.ping()` method on all 3 backends (sqlite + postgres do `SELECT 1`; in-memory is a no-op). `/healthz` stays unconditional 200 (liveness). Bicep `containerapp-api.bicep` flipped to use `/ready` for the readinessProbe so ACA pulls broken pods out of rotation without restarting them. **Prevents the "30s of 500s during Postgres failover" failure mode.**

**Job retry policy shipped this session — exponential backoff + dead-letter.** 23 new tests (555 unit + 3 smoke = 558 total). New `JobStatus.DEAD_LETTER` for retry-exhausted jobs (distinct from `ERROR`). New `core/job_retry.py` module with `should_retry` / `compute_next_retry_at` / `is_exhausted` pure functions + `JobRetryPolicy` dataclass (default: 3 attempts, 5s base, 3x factor, 5min cap, ±25% jitter). `JobRecord` gained `attempt_count` + `next_retry_at` columns (idempotent sqlite + postgres migrations). New `StorageProvider.requeue_job(...)` method flips `RUNNING` → `QUEUED` with the new attempt+backoff stamped; `claim_next_job` is retry-aware (skips rows whose `next_retry_at` is in the future via a new partial index). `update_job` accepts `DEAD_LETTER` as terminal. Worker's `_resolve_outcome` centralizes the three-way decision (retry / dead-letter / terminal-error). Notifications skipped on retry path so flaky jobs don't spam ops inboxes — they only fire on a true terminal landing. `WorkerConfig.retry_policy` lets workers override the default (set `max_attempts=1` for the strict fail-fast mode). **Production-readiness gap closed.**

**Azure onboarding tooling shipped this session — `scripts/azure-bootstrap.sh` + `movate doctor --target`.** 10 new tests (532 unit + 3 smoke = 535 total). Bash script idempotently creates RG + service principal + federated OIDC credential + role assignments per env (prints the GH Environment secrets to paste at the end). `movate doctor --target <name>` walks `az` install → login → subscription match → RG → ACR → both Container Apps → `/healthz` with operator pointers on every red. New `docs/azure-bootstrap.md` is the 8-step end-to-end runbook from "you have a subscription" to "auto-deploy via release/*", including cost expectations and a symptom-indexed troubleshooting table. The remaining unautomated work — Azure subscription provisioning, GitHub Environment UI secret-paste, the Key Vault chicken-and-egg on first Bicep run — is each documented inline. **Removes the operator-toil gap left after stages 1-4.**

**v1.0 stage 4 shipped this session — tenant isolation audit.** 30 new test invocations (522 unit + 3 smoke = 525 total). Audit found 9 gaps in storage methods that read or mutated per-tenant rows without filtering by ``tenant_id`` — every single one now enforces tenant boundary in the SQL WHERE clause. ``get_run`` / ``get_workflow_run`` / ``get_eval`` / ``get_job`` now require ``tenant_id`` kwarg and return ``None`` for cross-tenant lookups (NOT 403, which would leak id existence). ``update_job`` / ``revoke_api_key`` / ``touch_api_key`` likewise tenant-scope the WHERE clause so even a misconfigured worker can't mutate another tenant's row. ``list_evals`` / ``list_workflow_runs`` gained the optional ``tenant_id`` filter their siblings already had. New ``tests/test_tenant_isolation.py`` parametrized over memory + sqlite + postgres mints two tenants, populates parallel rows in every table, then sweeps every cross-tenant read path asserting tenant B can never see tenant A's data — single source of truth for the multi-tenant security boundary. **v1.0 is now feature-complete.**

**v1.0 stage 3 shipped this session — model policy enforcement.** 21 new tests (492 unit + 3 smoke = 495 total). New `policy:` block on `movate.yaml` with three optional fields (`allowed_providers`, `deny_models`, `max_cost_per_run_usd`); permissive default preserves v0.x behavior for projects that don't opt in. Enforced at TWO layers: `movate validate` (static gate on every agent.yaml — clean per-violation list with pointer back to movate.yaml) and `Executor.execute()` entry (runtime check, so bundles loaded via `movate serve` can't bypass — denied models short-circuit BEFORE any provider call so zero cost is incurred). Aggregated violation reporting (primary + every fallback + budget checked in one pass — operator fixes everything at once). `bench`-friendly: `model_override` runs only check the override, mirroring the existing fallback-disable semantics. `PolicyViolationError` + `FailureType.POLICY_VIOLATION` for typed handling; persisted to `failures` table for audit.

**v1.0 stage 2 shipped this session — `movate deploy` + GH Actions deploy.yml.** 23 new tests (464 unit + 3 smoke = 467 total). `cli/deploy.py` wraps `az acr build` (cloud-side image build) + `az containerapp update` for both API + worker, then polls `GET /healthz` until the new revision's `version` matches the just-built image. Image-tag default = `movate:<version>-<git-sha-short>` for traceability; `--image-tag` override for rollbacks (`--skip-build --image-tag movate:0.5.0-prev_sha`). `--only api`/`--only worker` for partial updates; `--dry-run` for plan inspection; `--no-wait` for CI fire-and-forget; `--wait-timeout` (default 300s, exit 124 on miss). `TargetConfig` extended with optional `azure_subscription` / `azure_resource_group` / `azure_acr_name` / `azure_env` fields; `add-target` accepts `--azure-*` flags and surfaces "deploy enabled / NOT enabled" at registration time. `.github/workflows/deploy.yml` uses Azure federated OIDC (no stored client secrets) + per-env GitHub Environments for scoped secrets + approval gates; triggers on push to `release/<env>` or manual `workflow_dispatch` with `target_env` input. Integration surface = `az` CLI shell-out, not Azure SDKs (zero new runtime deps; operators already have `az`).

**Server-side email notifications shipped this session.** 14 new tests (441 unit + 3 smoke = 444 total). `notify_email` column on `jobs` (sqlite ALTER + postgres `ADD COLUMN IF NOT EXISTS`); `JobRecord` + `RunSubmission` + `JobView` carry it through the wire. `core/notify.py` with `NotificationDispatcher` Protocol + `ConsoleBackend` (default; logs intent) + `SmtpEmailBackend` (production; vendor-agnostic — ACS Email / SendGrid / Mailgun / SES all speak SMTP). `build_dispatcher()` env-driven: `MOVATE_SMTP_HOST` unset → console; set → SMTP with STARTTLS or SSL based on `MOVATE_SMTP_USE_SSL`. Worker fires-and-awaits after terminal `update_job`; failure logs but never re-queues. `movate submit ... --notify-email <addr>` threads through `MovateClient.submit_job` → wire → worker → SMTP. SMS deferred: blocked on multi-week A2P 10DLC / phone-number provisioning, code is the small part.

**Remote-runtime CLI ergonomics shipped this session.** 29 new tests (427 unit + 3 smoke = 430 total). `core/user_config.py` for `~/.movate/config.yaml` (deployment targets + active pointer; bearer tokens stay in env vars). `core/client.py` with `MovateClient` httpx wrapper that supports `httpx.ASGITransport` for hermetic tests. Three new CLI surfaces: `movate config add-target | list-targets | use | show | remove-target`, `movate submit <agent> [--target] [--wait] [--notify] [--output]`, `movate jobs show | wait | list-agents`. `--wait` polls with a Rich spinner until terminal; `--notify` fires a desktop notification (macOS osascript / Linux notify-send / no-op on Windows). End-to-end binary smoke walked through cleanly: `config add-target` → `submit` fire-and-forget → `jobs show` → `submit --wait` returns terminal state in ~135ms.

**v1.0 stage 1 shipped this session — Azure Bicep IaC.** Locally compiles + lints clean via `az bicep build` / `az bicep lint`. Seven modular `.bicep` files (loganalytics, acr, keyvault, postgres, containerapp-env, containerapp-api, containerapp-worker) orchestrated by `infra/azure/main.bicep`. Role assignments (AcrPull, Key Vault Secrets User) wire the ACA managed identities to ACR + KV at top level. Per-env SKU defaults (`dev`/`staging`/`prod`) drive Postgres tier, ACA replicas, retention. Multi-stage `Dockerfile` with `runtime` (API) and `worker` final targets sharing one base. CI gained a `bicep` job running `bicep build` + `bicep lint` on every PR — no Azure subscription needed. Operator walkthrough at [infra/azure/README.md](infra/azure/README.md); design decisions locked in [docs/v1.0-azure-design.md](docs/v1.0-azure-design.md).

**Progress UI shipped this session.** 7 new tests (398 unit + 3 smoke = 401 total). `cli/_progress.py` with `progress_bar()`, `spinner()`, `print_event()` helpers — all stderr-only, auto-degrade on non-TTY. `EvalEngine` / `BenchEngine` / `Worker` gained optional progress callbacks that wrap user callbacks in `contextlib.suppress(Exception)` so buggy UI can never sink a run. `movate eval` shows case-by-case bar with running mean score; `movate bench` shows model-by-model bar; `movate worker` shows streaming feed (`07:59:09 ✓ agent/alpha (2ms · 5fdb30de)`). Suppressed for `-o json` / `-o markdown` / `--mock` so automation paths stay clean — JSON output validity verified by tests.

**v0.5.0 tagged + released this session.** [GitHub Release](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.5.0) with wheel + sdist attached (per RELEASING.md Option A). README capability matrix flipped from staged → shipped for HTTP runtime / worker / Postgres. Service-mode quickstart added to README so the v0.5 surface is discoverable. CI gained a `postgres` job using GHA's `services:` block (Postgres 16 container) — the parametrized storage conformance suite now runs against PG on every PR, closing the "regression only caught when a dev sets MOVATE_PG_TEST_URL locally" gap.

### Group A — Close the v1.0 deploy loop (highest leverage)

The Bicep is shipped; what's missing is the **one-command deploy** that
turns a code change into a running revision. Until this lands, every
deploy is a manual `az acr build && az containerapp update` chain.

1. [x] **v1.0 stage 1: Bicep IaC for Azure** `[HIGH] [v1.0] [done]` — modular `infra/azure/modules/*.bicep`; CI runs `bicep build` + `bicep lint` on every PR. Operator walkthrough at [infra/azure/README.md](infra/azure/README.md).
2. [x] **v1.0 stage 2: `movate deploy` CLI + GH-Actions deploy.yml** `[HIGH] [v1.0] [done]` — `movate deploy --target <name>` wraps `az acr build` + `az containerapp update` (both API + worker) + `/healthz` poll until version matches. `TargetConfig` carries optional Azure deploy fields (`add-target --azure-*`). `.github/workflows/deploy.yml` uses federated OIDC, scoped to per-env GH Environments for approval gates. Rollback via `--skip-build --image-tag <prev>`. 23 tests across plan-building, CLI integration with subprocess mocks, and the async `/healthz` poll loop with `httpx.MockTransport`.
3. [ ] **First Azure deployment validation** `[HIGH] [v1.0] [≤1d code, blocked on subscription access]` — operator runs the walkthrough against a real subscription; surface any IAM / region / SKU surprises that local Bicep compile can't catch. Onboarding tooling done this session: `scripts/azure-bootstrap.sh <env>` automates the RG + SP + federated-cred setup, `movate doctor --target prod` walks the deploy path with operator pointers on every red, `docs/azure-bootstrap.md` is the 8-step end-to-end runbook. **Only the Azure-side action remains** (get sub, run the script, run Bicep, paste 7 secrets, push `release/dev`).
4. [x] **v1.0 stage 3: Model policy enforcement** `[HIGH] [v1.0] [done]` — `policy:` block on `movate.yaml` (allowed_providers / deny_models / max_cost_per_run_usd); enforced at `movate validate` (static) + `Executor.execute()` entry (runtime, so bundles loaded by `movate serve` can't bypass). Denied models short-circuit before any provider call → zero cost incurred. 21 tests in [tests/test_policy.py](tests/test_policy.py).
5. [x] **v1.0 stage 4: Tenant isolation audit** `[HIGH] [v1.0] [done]` — every storage method that touches per-tenant rows now requires + filters by ``tenant_id`` at the SQL layer (9 audit gaps closed). New ``tests/test_tenant_isolation.py`` parametrized over all 3 backends sweeps every cross-tenant read path. **v1.0 is feature-complete.**

### Group B — Scale the worker (production-readiness for real load)

The runtime works for one user at a time. To handle bursty multi-tenant
load, the worker needs to scale on **queue depth** (not CPU), retry
transient failures, and bound runaway clients.

6. [x] **KEDA Postgres scaler for worker autoscaling** `[HIGH] [post-v1.0] [done]` — replaced the CPU-utilization scale rule in `containerapp-worker.bicep` with a KEDA `postgresql` scaler keyed on claimable-job count (`status='queued' AND retry-window-elapsed`). `queueDepthPerReplica` param tunes scale-up aggression (prod 10, dev 3). New KV secret `pg-connection-string` for the KEDA sidecar.
7. [x] **Job retry policy with exponential backoff + dead-letter** `[HIGH] [post-v1.0] [done]` — `core/job_retry.py` policy module + new `JobStatus.DEAD_LETTER` + `requeue_job` storage method + worker `_resolve_outcome` decision. Transient errors (retryable=true) re-queue with exponential backoff + ±25% jitter; persistent errors stay terminal `ERROR`; budget exhaustion lands in `DEAD_LETTER`. `WorkerConfig.retry_policy` lets workers override defaults. 23 tests in [tests/test_job_retry.py](tests/test_job_retry.py) covering pure-math, storage round-trip, and worker integration.
8. [x] **Rate limiting per API key** `[MED] [post-v1.0] [done]` — token-bucket (better burst tolerance than leaky-bucket) keyed on `api_key_id`. In-process v1.x via `InProcessRateLimiter`; Redis backend slots into the same Protocol post-v1.x. Default 60 req/min/key configurable via `--rate-limit-per-minute` flag or env var. 429 with `Retry-After` + `X-RateLimit-{Limit,Remaining,Reset}` headers. 16 tests in [tests/test_rate_limit.py](tests/test_rate_limit.py).
9. [x] **`/ready` endpoint with deep checks** `[MED] [post-v1.0] [done]` — new `GET /ready` runs storage ping (sqlite `SELECT 1`, postgres pool `SELECT 1`); 503 + per-check failure info if anything's broken. `/healthz` stays unconditional 200 (liveness). Bicep `containerapp-api` readinessProbe flipped to `/ready` so ACA pulls broken pods out of rotation without restarting them. 3 tests in [tests/test_runtime_app.py](tests/test_runtime_app.py).

### Group C — SMS notifications (parallel code + business work)

The email notifications shipped this session (item 11). SMS is broken
out so you can prioritize the code + business work independently.

10. [x] **Server-side email notifications** `[MED] [post-v1.0] [done]` — `notify_email` column + `NotificationDispatcher` Protocol + Console/SMTP backends + worker hook + CLI `--notify-email`. Vendor-agnostic via SMTP.
11. [ ] **SMS-1: vendor decision (Twilio vs Azure Communication Services SMS)** `[MED] [post-v1.0] [≤2h ops]` — pricing comparison: Twilio US SMS ~$0.0079/msg + $1.15/mo phone number; ACS SMS ~$0.0075/msg + $1/mo, integrates with KV + RBAC. Decision criteria: existing Movate Twilio account? cost sensitivity? Azure-native preference? **Output: locked vendor name in `docs/v1.0-azure-design.md`.**
12. [ ] **SMS-2: code path** `[MED] [post-v1.0] [~2d, post vendor pick]` — mirrors the email path: `notify_sms` column on `jobs` (sqlite + postgres idempotent migrations), `JobRecord` + `RunSubmission` + `JobView` carry it, CLI `movate submit ... --notify-sms +14155551212`, `SmsBackend` Protocol, `TwilioSmsBackend` or `AcsSmsBackend` depending on SMS-1, E.164 validation (regex + `phonenumbers` lib), `build_dispatcher()` composes email + SMS dispatchers if both env-configured. Tests via the same faked-vendor pattern used for SMTP.
13. [ ] **SMS-3: infra (phone-number resource + KV secrets)** `[MED] [post-v1.0] [≤0.5d, post vendor pick]` — if ACS picked: add `Microsoft.Communication/PhoneNumbers` to Bicep with E.911 address + KV secret for `ACS_CONNECTION_STRING`. If Twilio picked: just KV secrets for `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` + `TWILIO_FROM_NUMBER`. ACA worker container gets the env-var wiring.
14. [ ] **SMS-4: business setup (A2P 10DLC + sender ID approval)** `[BLOCKING] [post-v1.0] [2-3 WEEKS ops time, not code]` — **register Movate's brand + use case with The Campaign Registry (US A2P 10DLC)**; submit sender ID applications for non-US regions where customers exist. Carrier filtering can take 1-2 weeks AFTER registration approval. Should start **NOW in parallel** with code work if SMS is a real product need — code can ship and sit idle until the phone number is live; the carrier path can't be sped up. Cost: ~$50 one-time brand registration + ~$10/campaign vetting fee.
15. [ ] **SMS-5: real-SMS smoke test** `[LOW] [post-v1.0] [≤0.5d, post SMS-4 approval]` — submit a job with `--notify-sms <ops-phone>`, watch the SMS land. Out-of-pocket: ~$0.01 per smoke.

### Group D — Polish + nice-to-haves

16. [ ] **Workflow replay** `[LOW] [post-v1.0]` — `movate run --replay <workflow-run-id>`. Single-agent replay already covers 80% of debug cases; defer until a customer asks.
17. [ ] **More templates as customer engagements demand** `[MED] [post-v1.0]` — extractor, RAG, function-caller; trivial to add now that the registry exists.
18. [ ] **HTTP streaming for `POST /run?wait=true`** `[LOW] [post-v1.0]` — server-sent events for long jobs so the client streams instead of polling. Useful for interactive UIs; not needed for the current batch / dev-team workflows.

---

## 1. Foundation — single agent (Phase 1 / v0.1)

### Already shipped

- [x] **Repo skeleton + `pyproject.toml` + CI** `[HIGH] [v0.1] [done] [≤1d]` — `uv sync`, ruff, mypy strict, pytest, GH Actions.
- [x] **CLI panel structure (Typer + Rich)** `[HIGH] [v0.1] [done] [≤1d]` — Develop / Run & evaluate / Diagnose / Deploy & operate / Manage.
- [x] **`agent.yaml` schema (`movate/v1`)** `[HIGH] [v0.1] [done] [≤1d]` — Pydantic-validated; rejects floating tags, bad semver, wrong api_version.
- [x] **Loader → `AgentBundle`** `[HIGH] [v0.1] [done] [≤1d]` — YAML + prompt template + JSON schemas + sha256 prompt hash.
- [x] **Failure taxonomy + retry policy** `[HIGH] [v0.1] [done] [≤1d]` — typed errors with default rules per type; retry_after honored on rate-limit.
- [x] **`BaseLLMProvider` Protocol** `[HIGH] [v0.1] [done] [≤2h]` — single seam; LiteLLM is implementation detail.
- [x] **`LiteLLMProvider` (LiteLLM-backed adapter)** `[HIGH] [v0.1] [done] [≤1d]` — `num_retries=0` (movate owns retries); typed exception mapping.
- [x] **`MockProvider`** `[HIGH] [v0.1] [done] [≤2h]` — deterministic, network-free; every test depends on it.
- [x] **Pricing table (packaged YAML)** `[MED] [v0.1] [done] [≤2h]` — versioned, auditable; canonical for billing.
- [x] **Cost-drift detection (LiteLLM vs table > 5%)** `[MED] [v0.1] [done] [≤2h]` — logs loud when prices stale.
- [x] **Budget enforcement per run** `[HIGH] [v0.1] [done] [≤1h]` — `max_cost_usd_per_run` aborts with `BudgetExceededError`.
- [x] **Linear executor with fallback chain** `[HIGH] [v0.1] [done] [1d]` — validate → render → invoke (retry+fallback) → validate output → record.
- [x] **SQLite storage (runs + failures)** `[HIGH] [v0.1] [done] [≤1d]` — `~/.movate/local.db`; aiosqlite.
- [x] **Stdout tracer (stderr stream)** `[HIGH] [v0.1] [done] [≤2h]` — JSON spans; doesn't pollute stdout.
- [x] **Agent template (`movate init`-able)** `[HIGH] [v0.1] [done] [≤2h]` — `agent.yaml` + `prompt.md` + I/O schema + eval dataset stub.
- [x] **`movate init`** `[HIGH] [v0.1] [done] [≤2h]` — scaffold from packaged template.
- [x] **`movate validate`** `[HIGH] [v0.1] [done] [≤2h]` — strict early failure.
- [x] **`movate show`** `[MED] [v0.1] [done] [≤2h]` — print resolved spec for PR review.
- [x] **`movate doctor` (basic)** `[MED] [v0.1] [done] [≤2h]` — Python, version, dep check.

### Phase 1 shipped (in this iteration)

- [x] **`movate run` command (wiring)** `[HIGH] [v0.1] [done]` — string/JSON/file/stdin input coercion, mock + real provider via LiteLLM, JSON or text output.
- [x] **`movate doctor` (deep checks)** `[MED] [v0.1] [done]` — Python, version, required + optional deps, API-key presence, sqlite path, pricing-table version, `movate.yaml` discovery.
- [x] **Phase 0 smoke test refresh** `[MED] [v0.1] [done]` — only Phase 2+ commands remain in the parametrized stub list.
- [x] **Unit tests — models** `[HIGH] [v0.1] [done]` — 25 tests; rejects floating tags / bad semver / wrong api_version / extra fields.
- [x] **Unit tests — loader** `[HIGH] [v0.1] [done]` — 11 tests; missing files, malformed schema, prompt hash stability.
- [x] **Unit tests — retry** `[HIGH] [v0.1] [done]` — 7 tests; taxonomy + backoff; rate-limit retry_after honored.
- [x] **Unit tests — executor with `MockProvider`** `[HIGH] [v0.1] [done]` — 12 tests; happy path, schema failures (input/output/non-JSON), budget breach, fallback chain (full + partial recovery), auth = no-retry, content-filter = safety_blocked, model_override skips fallback, cost-drift warning.
- [x] **Unit tests — sqlite round-trip** `[MED] [v0.1] [done]` — 5 tests; save_run, save_failure, list_runs filters, init idempotency.
- [x] **End-to-end smoke** `[HIGH] [v0.1] [done]` — verified `init demo-agent → validate → show → run "hello" --mock` returns success with cost + tokens + pricing version recorded in SQLite.
- [x] **`.env.example` template** `[MED] [v0.1] [done]`
- [x] **`movate.yaml` example at repo root** `[LOW] [v0.1] [done]`

---

## 2. Evals & comparison (Phase 2 / v0.2)

### Shipped

- [x] **Eval engine — exact-match scorer** `[HIGH] [v0.2] [done]` — `EvalEngine` in [src/movate/core/eval.py](src/movate/core/eval.py); 30 unit tests.
- [x] **Eval engine — LLM-as-judge with cross-family enforcement** `[HIGH] [v0.2] [done]` — same module; `assert_cross_family()` raises at config time. Azure↔OpenAI treated as same family.
- [x] **`movate eval` with `--gate 0.7` exit-code semantics** `[HIGH] [v0.2] [done]` — Rich table + JSON output, exit 0/1 by gate.
- [x] **N runs per case + aggregation modes** `[HIGH] [v0.2] [done]` — `--runs N --gate-mode mean|min|p10`; mean default.
- [x] **Eval result persistence (sqlite `evals` table)** `[MED] [v0.2] [done]` — `EvalRecord` saved on every run; index on `(agent, created_at)`.
- [x] **Dataset hashing + `dataset_hash` on EvalRecord** `[MED] [v0.2] [done]` — sha256 of dataset bytes stamped per run.
- [x] **Judge config validation at parse time** `[MED] [v0.2] [done]` — `JudgeConfig` Pydantic + `EvalEngine._validate_judge` rejects same-family before any case runs.
- [x] **judge.yaml.example in template** `[MED] [v0.2] [done]` — dropped in `evals/judge.yaml.example`; rename to enable.

### More shipped

- [x] **`movate bench` (multi-model compare)** `[HIGH] [v0.2] [done]` — `BenchEngine` in [src/movate/core/bench.py](src/movate/core/bench.py); CLI in [src/movate/cli/bench.py](src/movate/cli/bench.py); 8 unit tests. Cost (mean), latency (p50/p95), score (aggregated per gate-mode), errors, sample. Cross-family skipping with stderr note. Reads defaults from `movate.yaml: bench`.
- [x] **`MockProvider` is judge-aware** `[MED] [v0.2] [done]` — detects "Rubric:" in prompt, returns `{"score": 0.5, "rationale": "mock judge"}`; both responses overridable via env vars.

### Open

- [x] **Markdown reporter for CI annotation** `[MED→HIGH] [v0.2] [done]` — `render_eval_markdown` + `render_bench_markdown` in [src/movate/core/reporters.py](src/movate/core/reporters.py); `--output markdown` on both `movate eval` and `movate bench`. GFM-safe escaping (pipes, backticks), input truncation, `<details>` block for per-case rows, judge-skipped rows annotated. 8 tests in [tests/test_reporters.py](tests/test_reporters.py).
- [x] **`movate pricing` (print table)** `[LOW→MED] [v0.2] [done]` — Rich table + `-o json` + `-p <prefix>` filter, in [src/movate/cli/pricing.py](src/movate/cli/pricing.py). 5 tests in [tests/test_pricing_cli.py](tests/test_pricing_cli.py).
- [ ] **Rubric library (3-5 standard rubrics)** `[MED] [v0.2] [≤1d]` — relevance, correctness, faithfulness, safety, tone. Imported by name from `evals/judge.yaml`.
- [ ] **`--parallel` flag for bench** `[MED] [v0.3] [≤1d]` — currently sequential; parallel respects per-provider rate limits.
- [ ] **Persist `BenchSummary` to sqlite** `[LOW] [v0.4] [≤1d]` — currently ephemeral; needed for trend tracking.
- [ ] **DeepEval integration** `[LOW] [v0.5+] [1w]` — defer until RAG-grounding metrics are actually needed.
- [ ] **Ragas integration** `[LOW] [v0.5+] [1w]` — same.
- [ ] **TruLens integration** `[LOW] [v0.7+] [1w]` — same.

---

## 3. Sequential workflows (Phase 3 / v0.3)

- [x] **`workflow.yaml` Pydantic spec** `[HIGH] [v0.3] [done]` — [src/movate/core/workflow/spec.py](src/movate/core/workflow/spec.py): `WorkflowSpec`, `NodeSpec`, `EdgeSpec`. `kind: Workflow`, `state_schema`, `entrypoint`, `nodes`, `edges`, semver+name validators.
- [x] **`WorkflowGraph` IR (internal)** `[HIGH] [v0.3] [done]` — [src/movate/core/workflow/ir.py](src/movate/core/workflow/ir.py): `WorkflowGraph`, `WorkflowNode`, `WorkflowEdge`, `NodeType` (AGENT, TOOL, HUMAN, FUNCTION, SUB_WORKFLOW), `EdgeKind` (SEQUENTIAL, CONDITIONAL, PARALLEL_FAN_OUT, PARALLEL_FAN_IN). Helpers: `successors`, `predecessors`, `sources`, `sinks`, `is_linear`, `topological_order`. Future-aware enums let v1.1's LangGraph compiler reuse the same IR without a schema break.
- [x] **Sequential compiler with strict validation** `[HIGH] [v0.3] [done]` — [src/movate/core/workflow/compiler.py](src/movate/core/workflow/compiler.py). Two-pass: `compile_workflow` (structural — duplicates, dangling edges, self-loops, cycles, orphans, state-schema validation) + `validate_linear` (v0.3 phase gate — rejects branches, joins, conditional edges, non-agent node types with phase-aware error messages). 27 tests in [tests/test_workflow.py](tests/test_workflow.py).
- [x] **Workflow runner — typed `WorkflowState` plumbing** `[HIGH] [v0.3] [done]` — [src/movate/core/workflow/runner.py](src/movate/core/workflow/runner.py). State projected onto each node's input schema; output shallow-merged back. State validated against `state_schema` at entry. 6 tests in [tests/test_workflow_runner.py](tests/test_workflow_runner.py).
- [x] **Per-node `RunRecord` linked by `workflow_run_id`** `[HIGH] [v0.3] [done]` — `RunRecord.workflow_run_id` + `node_id` fields; new `workflow_runs` sqlite table + `WorkflowRunRecord`; `list_runs(workflow_run_id=…)` filter; idempotent `ALTER` migrations.
- [x] **Partial-failure preservation** `[HIGH] [v0.3] [done]` — runner stops at the failing node, returns the pre-merge state, marks workflow `ERROR` with `error_node_id`. Per-node `RunRecord`s up to and including the failure are persisted.
- [x] **`movate run <workflow>` extension** `[HIGH] [v0.3] [done]` — `is_workflow_path()` auto-detect in [src/movate/cli/_workflow_path.py](src/movate/cli/_workflow_path.py); `cli/run.py`, `cli/validate.py`, `cli/show.py` all dispatch.
- [x] **`movate show workflow` topology render (ASCII / Mermaid)** `[MED] [v0.3] [done]` — Rich header + nodes table, ASCII chain (`first → second → third`), Mermaid `flowchart LR` block ready for PR descriptions. 9 tests in [tests/test_cli_workflow.py](tests/test_cli_workflow.py).
- [ ] **`--node-trace` flag** `[MED] [v0.3] [≤2h]` — surface intermediate states on stdout for debugging.
- [ ] **`workflow.yaml: runtime: <homegrown|langgraph>` field (parsed but warns on `langgraph`)** `[MED] [v0.3] [≤1h]` — future-proofs the YAML so v1.1 adds zero schema churn.
- [ ] **Throwaway IR→LangGraph prototype** `[HIGH] [v0.3] [1d]` — write it, prove the seam, **delete it** until v1.1. Mitigates the #1 risk in the plan.
- [ ] **Conditional edges** `[—] [v1.1] [—]` — explicitly OUT of v0.3.
- [ ] **Parallel fan-out** `[—] [v1.1] [—]` — out.
- [ ] **HITL nodes** `[—] [v1.1] [—]` — out.
- [ ] **Loops / iteration** `[—] [v1.1] [—]` — out.

---

## 4. Observability (Phase 4 / v0.4)

- [x] **Langfuse tracer** `[HIGH] [v0.4] [done]` — `LangfuseTracer` in [src/movate/tracing/langfuse.py](src/movate/tracing/langfuse.py); `build_tracer()` auto-selects via `MOVATE_TRACER=langfuse` or `LANGFUSE_SECRET_KEY` env. Falls back to stdout with a stderr warning if the package or keys are missing — never breaks a run. Client injectable so tests don't need the real SDK. `movate doctor` now surfaces resolved tracer + LANGFUSE_* env vars. 12 tests in [tests/test_tracing_langfuse.py](tests/test_tracing_langfuse.py).
- [x] **OTel tracer (OTLP exporter)** `[HIGH] [v0.4] [done]` — `OtelTracer` in [src/movate/tracing/otel.py](src/movate/tracing/otel.py); OTLP-HTTP exporter via `BatchSpanProcessor`. `OTEL_EXPORTER_OTLP_ENDPOINT` + optional `OTEL_SERVICE_NAME` env vars. Tracer + provider injectable for tests; SDK imported lazily so the module loads without `opentelemetry`. Attribute coercion via `_otel_value` so dict / tuple / list values become OTel-acceptable JSON strings.
- [x] **Tracer auto-select via `MOVATE_TRACER`** `[MED] [v0.4] [done]` — `stdout | langfuse | otel | composite`. Auto-detects on env vars when unset.
- [x] **Composite tracer (multi-fanout)** `[MED] [v0.4] [done]` — `CompositeTracer` in [src/movate/tracing/composite.py](src/movate/tracing/composite.py). Per-span mapping back to per-delegate `SpanCtx`s so end/event/attribute fan-out. Each delegate wrapped in try/except — one bad backend can't kill siblings. 26 tests in [tests/test_tracing_otel.py](tests/test_tracing_otel.py) covering OtelTracer + CompositeTracer + all dispatch paths.
- [x] **`movate trace replay <run-id>`** `[HIGH] [v0.4] [done]` — `core/replay.py` (engine) + `cli/trace.py` (rendering). Auto-detects agent vs workflow id, renders Rich tables + per-node breakdown for workflows, `--verbose` shows full input/output bodies, `--output json` is pipe-friendly. New `get_run(run_id)` + `get_workflow_run(id)` storage methods. 19 tests in [tests/test_replay.py](tests/test_replay.py) + [tests/test_cli_trace.py](tests/test_cli_trace.py).
- [ ] **`movate logs <run-id> --tail`** `[MED] [v0.4] [≤1d]` — read sqlite + tracer events, render Rich timeline.
- [x] **Drift baseline (`movate eval --baseline <eval-id>`)** `[HIGH] [v0.4] [done]` — `core/baseline.py` (`BaselineDiff` math, regression-detection) + `cli/eval.py` (`--baseline`, `--regression-tolerance`). Diffs mean_score, pass_rate, sample_count, cost; renders Rich diff table after eval output; includes `baseline` block in `-o json`; exits 1 on regression past tolerance. New `get_eval(eval_id)` storage method. 21 tests in [tests/test_baseline.py](tests/test_baseline.py). Per-case diff deferred to v0.4.1+ when datasets demand it.
- [ ] **Span attributes — token-level cost breakdown** `[MED] [v0.4] [≤2h]` — `cost_usd`, `pricing_version`, `cached_input_tokens` per provider call.
- [ ] **Privacy: redact prompt/output spans by config** `[MED] [v0.4] [≤1d]` — `tracer.redact_io: true` for tenants with PII.
- [ ] **Cost dashboards (Langfuse-side)** `[LOW] [v0.4] [—]` — delegated to Langfuse; just confirm dashboard exists.
- [ ] **Real-time event bus** `[LOW] [post-v1.0] [—]` — defer; tracing covers v0.4 needs.

---

## 5. Server + queue (Phase 5 / v0.5)

- [ ] **PostgresProvider (port + harden)** `[HIGH] [v0.5] [2-3d]` — asyncpg pool, `FOR UPDATE SKIP LOCKED`, JSONB.
- [ ] **`migrations/0001_init.sql` runs on startup** `[HIGH] [v0.5] [≤1h]` — idempotent.
- [ ] **`movate.runtime.app` (FastAPI)** `[HIGH] [v0.5] [2-3d]` — `/run`, `/jobs/{id}`, `/agents`, `/healthz`.
- [ ] **`movate.runtime.worker`** `[HIGH] [v0.5] [2-3d]` — claim-next-job loop; concurrency-safe; metrics/healthz.
- [ ] **API key issuance + bcrypt hash (`mvt_<env>_<tenant>_<keyid>_<secret>`)** `[HIGH] [v0.5] [2-3d]` — multi-tenant safety from day one.
- [ ] **`movate auth create-key|list-keys|revoke-key`** `[HIGH] [v0.5] [≤1d]` — operator UX.
- [x] **Tenant isolation audit (every query filtered by `tenant_id`)** `[HIGH] [v1.0] [done]` — every storage read / mutate path that touches per-tenant rows now filters by ``tenant_id`` in the SQL WHERE clause. 9 audit gaps closed (``get_run`` / ``get_workflow_run`` / ``get_eval`` / ``get_job`` / ``update_job`` / ``revoke_api_key`` / ``touch_api_key`` / ``list_evals`` / ``list_workflow_runs``). 30 cross-tenant fuzz test invocations in [tests/test_tenant_isolation.py](tests/test_tenant_isolation.py).
- [ ] **Idempotency on `/run` by `request_id`** `[HIGH] [v0.5] [≤1d]` — retry-safe; returns existing job.
- [ ] **`workflow_runs` table linking child runs** `[HIGH] [v0.5] [≤1d]` — needed once workflows are persistent.
- [ ] **`/run` rate limit (per tenant)** `[MED] [v0.5] [≤1d]` — prevents tenant from starving the queue.
- [ ] **Prom metrics endpoint** `[MED] [v0.5] [≤1d]` — `/metrics` for jobs, runs, latency, cost.
- [ ] **Redis** `[LOW] [post-v0.5] [—]` — defer; Postgres is enough through v1.0.
- [ ] **pgvector retrieval** `[—] [v1.2+] [—]` — deliberately out.

---

## 6. Deploy + CI gating (Phase 6 / v1.0)

- [x] **Bicep: ACA + Postgres Flex + Key Vault + ACR + Log Analytics** `[HIGH] [v1.0] [done]` — modular `infra/azure/modules/*.bicep` + `main.bicep` orchestrator; CI `bicep build` + `bicep lint`. Operator walkthrough at [infra/azure/README.md](infra/azure/README.md).
- [x] **`movate deploy <env>`** `[HIGH] [v1.0] [done]` — wraps `az acr build` + `az containerapp update` (both apps) + `/healthz` poll. Image tag = `movate:<version>-<git-sha-short>`. Rollback via `--skip-build --image-tag <prev>`. 23 tests in [tests/test_deploy.py](tests/test_deploy.py).
- [ ] **GH Actions `validate.yml`** `[HIGH] [v1.0] [≤1d]` — schema + topology validation on every PR.
- [x] **GH Actions `eval-gate.example.yml` (block on regression)** `[HIGH] [v1.0] [done]` — `cli/eval.py` gained `--baseline-file <path>` and `--output-baseline <path>` flags so baselines can be git-tracked instead of stuck in ephemeral runner sqlite. Example workflow at [.github/workflows/eval-gate.example.yml](.github/workflows/eval-gate.example.yml) ships a `gate-pr` job (PR runs `--baseline-file`, exits 1 on regression past tolerance) and a `refresh-baseline` job (main-merge re-runs eval with `--output-baseline` and auto-commits). Docs at [docs/ci-eval-gate.md](docs/ci-eval-gate.md). 6 tests covering load, write, mutual exclusion, malformed-JSON path.
- [x] **GH Actions `deploy.yml` (release branch → ACA)** `[HIGH] [v1.0] [done]` — push to `release/<env>` (or `workflow_dispatch` with `target_env`) → Azure federated OIDC login → hydrate `~/.movate/config.yaml` from env-scoped GH secrets → `uv run movate deploy`. Per-env GitHub Environments gate prod with approval rules.
- [ ] **GH Actions `security.yml`** `[MED] [v1.0] [≤1d]` — dependency + secret scan.
- [x] **Model policy enforcement** `[HIGH] [v1.0] [done]` — `policy:` block on `movate.yaml` (allowed_providers, deny_models, max_cost_per_run_usd). Enforced at `movate validate` (static) + `Executor.execute()` entry (runtime). 21 tests in [tests/test_policy.py](tests/test_policy.py).
- [ ] **Promotion semantics dev → staging → prod** `[MED] [v1.0] [≤1d]` — env profiles + revision tags.
- [ ] **Deployment health check + rollback** `[MED] [v1.0] [≤1d]` — `/healthz` poll + ACA revision pinning.
- [x] **Per-tenant cost ceiling enforcement** `[HIGH] [v1.0] [done]` — `TenantBudget` model + `tenant_budgets` table + `Executor._check_tenant_budget` at execute() entry. New `TenantBudgetExceededError` + CLI `movate tenants set-budget | clear-budget | show | list`. Storage methods on all 3 backends. 24 tests in [tests/test_tenant_budget.py](tests/test_tenant_budget.py).
- [ ] **Multi-region** `[—] [post-v1.0] [—]` — out.
- [ ] **Blue/green** `[LOW] [post-v1.0] [—]` — ACA revisions cover most of this.

---

## 7. LangGraph swap-in + advanced (Phase 7 / v1.1+)

- [ ] **`workflow/compilers/langgraph.py`** `[HIGH] [v1.1] [1w]` — alternative compiler from `WorkflowGraph` IR; gated by `runtime: langgraph`.
- [ ] **Conditional edges** `[HIGH] [v1.1] [2-3d]` — `edges: [{from: A, to: B, when: "$.score > 0.7"}]`.
- [ ] **Parallel fan-out** `[HIGH] [v1.1] [2-3d]` — `fan_out` nodes with deterministic merge.
- [ ] **HITL nodes (`type: human`)** `[HIGH] [v1.1] [1w]` — pause workflow, await external resolve via `/runs/{id}/resume`.
- [ ] **Checkpointing (LangGraph-native)** `[HIGH] [v1.1] [2-3d]` — resume from last successful node after failure.
- [ ] **Tool registry (`movate.tools`)** `[HIGH] [v1.1] [1w]` — Python decorator → JSON schema → injected into prompt + tool-calling loop.
- [ ] **Built-in tools — `kb_search`, `http_get`, `sql_query`** `[MED] [v1.1] [3-5d]` — high reuse across customer engagements.
- [ ] **Skill packs (composable rule + prompt bundles)** `[MED] [v1.2] [1w]` — `grounding`, `citation_enforcement`, `pii_redaction`.
- [ ] **Provider routing rules (cost / latency / region)** `[HIGH] [v1.1] [3-5d]` — `models/routing.yaml`; declarative, enforced at executor.
- [ ] **Memory provider (PRD §F)** `[MED] [v1.2] [1w]` — short-term + long-term; sqlite + Postgres backends.
- [ ] **Retrieval provider (pgvector)** `[HIGH] [v1.2] [1w]` — embed + ANN; canonical "grounding" implementation.
- [ ] **RBAC** `[MED] [v1.2] [1w]` — role-keyed scopes on `mvt_*` keys.
- [ ] **Azure AD SSO** `[MED] [v1.3] [1w]`.
- [ ] **Visual workflow editor** `[—] [post-v2] [—]` — explicitly out per PRD §2.
- [ ] **Marketplace / registry UI** `[—] [post-v2] [—]` — out.
- [ ] **Autonomous self-modifying agents** `[—] [post-v2] [—]` — out.

---

## 8. Cross-cutting / developer experience (HIGH leverage globally)

These pay back across every phase. Don't queue them after v1.0 — interleave them.

- [ ] **Shell tab-completion (`movate --install-completion`)** `[HIGH] [v0.1] [done]` — already wired by Typer.
- [ ] **`.env` auto-load** `[HIGH] [v0.1] [done]` — already wired.
- [x] **`movate.testing` fixtures package** `[HIGH] [v0.2] [done]` — public surface in [src/movate/testing/](src/movate/testing/): `InMemoryStorage`, `NullTracer`, `JudgeStubProvider`, `MockProvider`, `scaffold_agent`, `build_test_executor`. Pytest fixtures (`mock_provider`, `in_memory_storage`, `null_tracer`, `pricing`, `temp_agent_dir`, `build_executor`) auto-discovered via `pytest_plugins = ["movate.testing.fixtures"]`. 14 conformance tests in [tests/test_testing.py](tests/test_testing.py).
- [x] **`movate watch <agent>` (hot-reload on YAML change)** `[MED] [v0.2] [done]` — polls agent.yaml + prompt + schemas + dataset + judge for mtime changes; re-runs `movate validate` (with lint + cost forecast) on each change. Stdlib polling (zero new deps); 200ms debounce; resilient to broken-mid-save YAML. `--poll-interval` + `--strict` flags. 8 tests in [tests/test_watch.py](tests/test_watch.py).
- [x] **Templates beyond `agent_init` — `faq`, `summarizer`, `classifier`** `[HIGH] [v0.2] [done]` — registry at [src/movate/templates/__init__.py](src/movate/templates/__init__.py); `movate init -t faq` (and `summarizer`, `classifier`). FAQ + summarizer ship with a `judge.yaml.example`; classifier uses exact-match. 21 tests in [tests/test_templates.py](tests/test_templates.py).
- [x] **Live-API smoke tests (env-gated)** `[HIGH] [v0.2] [done]` — [tests/test_smoke_litellm.py](tests/test_smoke_litellm.py) + [scripts/smoke.sh](scripts/smoke.sh). 3 tests covering OpenAI direct, Anthropic direct, and full executor against real OpenAI. Module-level `pytestmark = pytest.mark.smoke`; CI filters with `-m "not smoke"`. Each test independently gated on the relevant API key.
- [ ] **Workflow templates — `returns-processing`, `triage-then-respond`** `[MED] [v0.3] [≤1d]`.
- [ ] **VS Code launch configs (debug a single agent run)** `[MED] [v0.2] [≤2h]` — port from MDK if useful.
- [x] **`movate run --replay <run-id>`** `[HIGH] [v0.4] [done]` — `core/run_replay.py` + `cli/run.py` flag. Re-executes a recorded `RunRecord` against the current agent bundle (prompt/model/schemas reload from disk). Surfaces `output_changed`, `status_changed`, `changed_keys`, cost + latency deltas. Output changes are not failures (debug tool); only a current-run error trips exit 1. Mutually exclusive with positional INPUT. Workflow replay deferred. 14 tests in [tests/test_run_replay.py](tests/test_run_replay.py).
- [ ] **`movate diff <agent-a> <agent-b>`** `[MED] [v0.2] [≤1d]` — show prompt-hash, model, schema deltas; great for PR review.
- [x] **Prompt linter** `[MED] [v0.2] [done]` — `core/prompt_linter.py` with 4 rules (`UNDECLARED_INPUT_REF`, `EMPTY_PROMPT`, `MISSING_JSON_INSTRUCTION`, `NO_OUTPUT_SCHEMA_REFERENCE`, `TINY_PROMPT`). Wired into `movate validate` with `--strict` (CI gate) and `--no-lint` flags. 19 tests in [tests/test_prompt_linter.py](tests/test_prompt_linter.py).
- [x] **Cost forecast on `validate`** `[MED] [v0.2] [done]` — `core/cost_forecast.py` with `estimate_eval_cost(bundle, *, pricing)`. Renders each case's prompt, estimates tokens via chars/4, multiplies by model's pricing. Prints a dim `eval cost:` line on every validate when dataset + pricing available; silent skip otherwise. 10 tests in [tests/test_cost_forecast.py](tests/test_cost_forecast.py).
- [ ] **`--dry-run` on `run`** `[MED] [v0.2] [≤2h]` — render prompt, show what *would* be sent, exit 0.
- [ ] **Structured logging (structlog) everywhere** `[MED] [v0.4] [≤1d]` — already a dep; standardize on it.
- [ ] **Docs site (mkdocs) — internal** `[LOW] [v0.6] [1w]` — defer; per-user decision is internal-only, README + `--help` is enough through v0.5.

---

## 9. MDK rebrand + canonical config (Deva's feedback, May 2026)

Strategic direction shift: position movate-cli as **MDK — Movate Development Kit**, an enterprise SDK with knowledge graphs, multi-tenancy, and adapter architecture. Some items below are already shipped (see notes); the new work is grouped in phases v0.6 → v1.0.

> **Resale-clean stack is the real constraint, not "make MDK sellable."**
> MDK itself isn't sold. But Movate-built customer solutions embed MDK's
> dependencies, so every dep's license must be permissive enough that the
> customer engagement can be resold without copyleft contamination, BSL
> service-competition clauses, or open-sourcing of proprietary work.
>
> Current stack is **100% permissively licensed** (MIT / Apache 2.0 / BSD) —
> see [docs/license-posture.md](docs/license-posture.md) (TBD).
> Future additions must clear the same bar.
>
> **Strategic answers from leadership (May 2026):**
> 1. **Multi-cloud (GCP/AWS): eventual.** Stay Azure-first; new infrastructure
>    decisions must be cloud-portable in principle. ADR captures the rule.
> 2. **Multi-tenant: both — SaaS AND self-hosted.** Most building blocks
>    already in place. Need Helm chart for K8s self-hosting; SaaS onboarding
>    flow. v1.0 work.
> 3. **Lyzr adapter: STRATEGIC + TIME-BOXED.** Most current customer agents
>    are on Lyzr. Build a read-only adapter + `mdk import lyzr <id>` →
>    `agent.yaml` migration tool. Bridge to migrate OFF Lyzr, not stay
>    integrated. Bump to v0.7-v0.8 priority (not v1.0). License-check first.
> 4. **Knowledge graph: aspirational, visualization-first.** Use Apache AGE
>    for storage (cheap, Postgres-native). The actual value is d3.js
>    interactive visualization of the graph + simple search/filter.
>    Becomes the demo "wow moment" in v0.9. Not a deep-query system.

### Tier 0 — Brand + naming (v0.6, ~1 week)

- [ ] **Rename `movate` CLI → `mdk`** `[HIGH] [v0.6] [2-3d]` — binary, package, env var prefix (`MOVATE_*` → `MDK_*`), config dir (`~/.movate/` → `~/.mdk/`). Mechanical but breaking. Keep `movate` as transitional alias for 2 releases.
- [ ] **Rename `movate.yaml` → `policy.yaml`** `[HIGH] [v0.6] [1d]` — loader checks both, prefers `policy.yaml`. Deprecation warning on old name.
- [ ] **Update demo repo + docs to use `mdk`** `[HIGH] [v0.6] [1d]` — README, ask.py subprocess calls, dev-loop.md, deck source files.

### Tier 1 — Canonical config files (v0.6, ~2-3 weeks)

- [ ] **`policy.yaml` (renamed from movate.yaml)** `[HIGH] [v0.6] [done-after-rename]` — already implements: allowed_providers, deny_models, max_cost_per_run_usd, tenant_budgets.
- [ ] **`runtime.yaml` — runtime + adapter selection + provider routing** `[HIGH] [v0.6] [3-5d]` — defaults for runtime kind (litellm|native_openai|native_anthropic|lyzr), per-agent overrides allowed; provider routing rules (cost/latency/region) merged here from §7.
- [ ] **`eval.yaml` — global eval defaults** `[HIGH] [v0.6] [2d]` — gate, gate_mode, regression_tolerance, runs_per_case, default judge model. Per-agent `evals:` block in agent.yaml overrides.
- [ ] **`knowledge.yaml` — KB sources + ingestion config** `[HIGH] [v0.6] [3d]` — vector DB pointer, KG endpoint, chunking strategy, embedding model. Per-agent `knowledge:` refs point at indices/namespaces declared here.
- [ ] **Layered context resolution: Global → Agent → Session** `[HIGH] [v0.6] [3d]` — formalize merge precedence; expose via `mdk config show` showing the resolved config for a given agent.

### Tier 2 — Agent definition expansion (v0.7, ~2 weeks)

- [ ] **Input/output examples in agent.yaml** `[HIGH] [v0.7] [2d]` — `examples: [{input: ..., output: ...}]`; `mdk validate` smoke-tests against these; powers downstream synthetic data generation.
- [ ] **Goals + objectives + KB refs as first-class agent.yaml fields** `[HIGH] [v0.7] [3d]` — structured `goals: [...]`, `objectives: [{id, threshold, judge}]`, `knowledge: [{kind, path|index|namespace}]`. Becomes the contract for test generation.
- [ ] **Modularize agent objectives for evaluation** `[HIGH] [v0.7] [3d]` — each objective is independently scored + reported (per-objective subscore in eval table). Eval gate can target individual objectives.

### Tier 3 — Multi-source onboarding + dynamic test gen (v0.7, ~3-4 weeks)

- [ ] **Onboard from Agent JSON** `[MED] [v0.7] [1d]` — `mdk onboard --from-json agent.json` builds equivalent of agent.yaml on disk.
- [ ] **Onboard from trace logs** `[MED] [v0.7] [1w]` — given recorded RunRecords, synthesize a starter agent.yaml (provider, schemas inferred from data shape).
- [ ] **Onboard from chat UI export** `[MED] [v0.7] [1w]` — common chat export formats (OpenAI, Anthropic, Claude.ai exports) → agent.yaml seed.
- [ ] **Black-box application testing (`mdk eval <http-url>`)** `[HIGH] [v0.7] [1w]` — hit an HTTP endpoint instead of a local agent; same eval framework, different runner.
- [ ] **Generate scenario test cases from agent goals** `[HIGH] [v0.7] [1w]` — LLM-generates cases across categories: positive / negative / edge / red-team. Output is a *suggested* dataset for human review — never auto-merged.
- [ ] **Synthetic test data generation** `[MED] [v0.7] [3-5d]` — opt-in via `mdk eval generate --synthetic`. Strong warning about over-reliance on synthetic data.
- [ ] **Expected-results generation (optional)** `[LOW] [v0.7] [3d]` — LLM-generates expected output for cases that lack one. Marked `auto_generated: true` so human reviewer can flip.
- [ ] **Test category tagging in dataset** `[MED] [v0.7] [1d]` — `tags: [positive | negative | edge | red_team | adversarial]`; eval table reports per-category pass rate.
- [ ] **Multi-run consistency analysis** `[MED] [v0.7] [2d]` — partially shipped (`--runs N`); add stddev + p10/p90 + confidence-interval bands to the eval report.

### Tier 4 — Knowledge architecture (v0.8 + v0.9, ~10-12 weeks total)

- [ ] **Evaluate open-source vector DB candidates** `[HIGH] [v0.8] [3d]` — `pgvector` (recommended; already have Postgres; PostgreSQL License — permissive), `Qdrant` (Apache 2.0), `Chroma` (Apache 2.0), `Weaviate` (BSD-3), `LanceDB` (Apache 2.0). All license-clean for customer resale. Write a 1-page decision doc.
- [ ] **Integrate pgvector (or chosen VDB)** `[HIGH] [v0.8] [1-2w]` — `MemoryProvider.vector_search()` + ingestion pipeline. `knowledge.yaml: vector_db:` config.
- [ ] **Evaluate open-source knowledge graph candidates** `[HIGH] [v0.9] [3d]` — `Apache AGE` (**recommended**; Postgres extension; Apache 2.0). **EXCLUDE**: Neo4j Community (GPLv3 — copyleft); Memgraph (BSL — service-competition restrictions). Acceptable alternatives: Kuzu (MIT), TerminusDB (Apache 2.0). Decision doc must explicitly state license + resale safety.
- [ ] **Integrate KG (Apache AGE)** `[HIGH] [v0.9] [1w]` — entity + relationship extraction at ingest; keep schema simple — this is a demo/visualization layer, not a deep-query system.
- [ ] **Canonical KB ingestion pipeline** `[HIGH] [v0.9] [2w]` — `mdk knowledge ingest <path>` runs: chunking → metadata → embedding → entity extraction → graph relationships. Configurable via `knowledge.yaml`.
- [ ] **Hybrid retrieval (vector + KG)** `[HIGH] [v0.9] [1w]` — query fans out to both; merge by score with configurable weights.
- [ ] **`mdk knowledge graph --serve` (d3.js visualization)** `[HIGH] [v0.9] [1-2w]` — HTTP endpoint returns `{nodes, edges}`; static HTML page renders a force-directed d3.js graph. Click a node → metadata + related KB chunks. **This is the demo wow-moment per Deva.** Showcases what the agent "knows" visually.
- [ ] **KG subgraph search + highlight** `[HIGH] [v0.9] [1w]` — search box on the d3.js page: "show me everything related to eval gates" → highlights the relevant subgraph + shows top-k KB chunks. Powered by hybrid retrieval (vector for KB chunks; graph traversal for entity neighborhoods).
- [ ] **Avoid cloud-native proprietary services** `[MED] [v0.9] [—]` — already done at model layer (LiteLLM). Infra layer (Azure) — scope a separate Terraform/Helm pass for GCP/AWS if/when business case is clear.

### Tier 5 — Memory architecture (v0.8, ~4 weeks parallel with VDB work)

- [ ] **Memory provider Protocol** `[HIGH] [v0.8] [3d]` — `MemoryProvider.read(layer, key)` + `write(...)` + `search(...)`; per-layer backends.
- [ ] **Session memory (per-run scratchpad, in-process)** `[HIGH] [v0.8] [2d]` — short-lived dict; cleared on run end.
- [ ] **Working memory (conversation context)** `[HIGH] [v0.8] [3d]` — Redis or Postgres with TTL; keyed by `conversation_id`.
- [ ] **Episodic memory (recent runs by tenant)** `[MED] [v0.8] [3d]` — already partially shipped (RunRecord); add retrieval helpers.
- [ ] **Semantic memory (KB-grounded long-term)** `[HIGH] [v0.8] [—]` — backed by vector DB (above).
- [ ] **Organizational memory (cross-tenant shared)** `[MED] [v0.9] [1w]` — separate VDB namespace + KG subgraph; opt-in per tenant.

### Tier 6 — Adapter architecture (v1.0, ~4 weeks)

- [ ] **Generic HTTP agent adapter** `[HIGH] [v1.0] [1w]` — POST to URL, expect JSON; black-box testing for any HTTP-served agent.
- [ ] **Lyzr adapter (read-only, migration bridge)** `[HIGH] [v0.7] [3-5d]` — invoke a Lyzr-hosted agent from MDK for eval/bench. License-check first (confirm Lyzr SDK is Apache 2.0 / MIT). Time-boxed: this is a bridge to migrate customer agents OFF Lyzr, not stay integrated.
- [ ] **`mdk import lyzr <agent-id>`** `[HIGH] [v0.8] [1w]` — pulls a Lyzr agent definition and synthesizes the equivalent MDK `agent.yaml`. Powers the migration story.
- [ ] **`docs/migrate-from-lyzr.md`** `[HIGH] [v0.8] [2d]` — walks customers through importing existing Lyzr agents into MDK-native. Critical because most current customer agents live on Lyzr.
- [ ] **Browser/Playwright adapter** `[LOW] [v1.0+] [2w]` — evaluate Web UIs. Gated on customer use case.
- [ ] **Trace replay adapter** `[—] [done]` — already shipped as `mdk run --replay` and `mdk trace replay`.

### Tier 7 — Comprehensive reporting + reconciliation (v1.0, ~3 weeks)

- [x] **Four-dimension eval reporting** `[HIGH] [v0.6] [done 2026-05-13]` — `accuracy` / `faithfulness` / `coverage` / `latency`, each scored per case, rolled up via `DimensionalMeans` on every `EvalSummary`. Dataset rows opt-in via `grounding`, `expected_coverage`, `latency_budget_ms` fields; legacy datasets see the v0.5 view byte-for-byte. CLI Rich table + `--output json` + markdown reporter all surface the rollup. Critical back-compat: gate stays on accuracy alone (`--gate 0.7` still means "70% accuracy"). 34 new tests; full suite 1166 → 1200. PR [#59](https://github.com/jeremyyuAWS/movate-cli/pull/59).
- [ ] **Per-objective scoring breakdown** `[HIGH] [v1.0] [3d]` — eval table shows pass/fail per `objective.id` defined on agent.yaml.
- [ ] **Eval reconciliation across deterministic + LLM + HITL** `[HIGH] [v1.0] [1w]` — configurable precedence; "if all three disagree, fall back to HITL" pattern.
- [ ] **HITL workflow nodes (formalize)** `[HIGH] [v1.0] [1w]` — already in v1.1 plan; specify the request/resolve API.
- [ ] **Configurable fallback strategy chain (LLM → HITL → deterministic)** `[MED] [v1.0] [3d]` — per-eval-config; defaults to LLM-only.
- [ ] **Persist evaluation results to datastore** `[—] [done]` — already shipped (EvalRecord); just add per-dimension subscores when v1.0 lands.

### Tier 8 — Doctor + observability polish + license hygiene (v0.6+, ~1-2 weeks)

- [ ] **`mdk doctor --explain`** `[HIGH] [v0.6] [3d]` — for each check, render: what it does, why it matters, what failure means, the copy-pasteable fix.
- [ ] **Doctor output explainability doc** `[MED] [v0.6] [2d]` — `docs/doctor-explained.md` documents every line — what it tests, what's safe to ignore.
- [ ] **License column in `mdk doctor`** `[HIGH] [v0.6] [2d]` — every dep line shows SPDX license alongside install status. Operators see resale-safety at a glance. Pre-empts the "is this stack OK to embed in a customer solution?" question.
- [ ] **`mdk doctor --licenses` deep report** `[MED] [v0.6] [2d]` — full SPDX breakdown, per-dep license file links, separates required vs optional, flags any non-permissive entries in red.
- [ ] **`docs/license-posture.md`** `[HIGH] [v0.6] [2d]` — explains Movate's stance: every dep in MDK is permissively licensed (MIT / Apache 2.0 / BSD) so customer engagements can embed + resell MDK-based solutions without copyleft contamination. Documents the explicit avoid-list (GPL / AGPL / SSPL / BSL with restrictive clauses). Linkable from doctor and from customer-facing solution docs.
- [ ] **CI license check** `[HIGH] [v0.6] [3d]` — `pip-licenses` or equivalent runs on every PR; fails merge if a new dep introduces a copyleft / SSPL / BSL license. Allowlist of approved SPDX IDs in repo.
- [ ] **License-clean gate on new deps** `[HIGH] [ongoing]` — every new optional extra (vector DB, KG, adapter) goes through license review before merge. Mark approved deps in `pyproject.toml` comments with their SPDX ID.

### Tier 9 — Enterprise readiness (v1.0+, ongoing)

#### Teams integration — v0.7 milestone (CEO ask: sales-led demos)

The full design is in [ADR 003](docs/adr/003-teams-integration.md). Issues
[#65–#69 + #70 + #72](https://github.com/jeremyyuAWS/movate-cli/issues?q=is%3Aopen+label%3Ateams-integration)
track each slice; status here mirrors the v0.7 milestone.

- [x] **ADR 003 — Teams as a self-serve front door (design)** `[HIGH] [v0.7] [done 2026-05-13]` — design doc with three vertical slices (3.1 run-existing → 3.2 eval-with-upload → 3.3 saved-configs). Status: Proposed (open questions on multi-tenant prospects, Langfuse trace linking across tenants, mobile UX, streaming). PR [#60](https://github.com/jeremyyuAWS/movate-cli/pull/60).
- [x] **Teams 3.1.a — Bot Framework webhook skeleton** `[HIGH] [v0.7] [done 2026-05-13]` — hand-rolled Activity protocol + `mdk teams-bot serve` Typer command + 28 tests. Hand-rolled instead of `botbuilder-core` to avoid ~30MB of transitive deps; SDK can land in the hardening PR. PR [#63](https://github.com/jeremyyuAWS/movate-cli/pull/63).
- [x] **Teams 3.1.b — Adaptive Cards + MovateClient integration** `[HIGH] [v0.7] [done 2026-05-13]` — `@movate run faq-agent {...}` actually executes and renders a card with response/cost/latency/optional trace link. Five outcome variants → four card templates. PR [#82](https://github.com/jeremyyuAWS/movate-cli/pull/82). Closes [#66](https://github.com/jeremyyuAWS/movate-cli/issues/66).
- [x] **Teams 3.1.c — per-user identity binding** `[HIGH] [v0.7] [done 2026-05-13]` — `/movate connect`, `whoami`, `disconnect` DM-only; Fernet-encrypted `teams_users` sqlite table; LRU-cached per-user MovateClients; strict mode for multi-tenant. PR [#83](https://github.com/jeremyyuAWS/movate-cli/pull/83). Closes [#67](https://github.com/jeremyyuAWS/movate-cli/issues/67).
- [ ] **Teams 3.1.d — file attachment handling** `[HIGH] [v0.7] [1w]` — drag `agent.zip` + `dataset.jsonl` into Teams; validate + ingest before run. Issue [#68](https://github.com/jeremyyuAWS/movate-cli/issues/68).
- [ ] **Teams 3.1.e — manifest + Azure Bot Service** `[HIGH] [v0.7] [blocked:#65]` — Teams app manifest, appPackage zipper, Bot Service Bicep resource. **Blocked on Azure tenant migration ([#65](https://github.com/jeremyyuAWS/movate-cli/issues/65)).**
- [ ] **Teams hardening — JWT validation** `[MED] [v1.0] [1w]` — production auth before any public Teams exposure. Issue [#70](https://github.com/jeremyyuAWS/movate-cli/issues/70).
- [ ] **Teams 3.2 — eval with bring-your-own data** `[HIGH] [v0.8] [1w]` — drag dataset + agent, scorecard updates per case via Bot Framework `UpdateActivity`. Surfaces the 4-dim rollup that shipped on 2026-05-13. Issue [#72](https://github.com/jeremyyuAWS/movate-cli/issues/72).

#### Original Tier 9 line items

- [ ] **Reusable enterprise policy enforcement** `[HIGH] [v1.0] [1w]` — `mdk policy export/import` so teams can share `policy.yaml` snippets. Marketplace later.
- [ ] **Multi-tenant deployments (formalize)** `[HIGH] [v1.0] [—]` — mostly shipped; add tenant-creation API, per-tenant config overrides.
- [ ] **Helm chart for self-hosted K8s deployment** `[HIGH] [v1.0] [1w]` — alternative to Bicep; gives customers a "self-host on your cluster" path. Works on EKS / GKE / AKS / on-prem K8s.
- [ ] **SaaS tenant onboarding flow** `[HIGH] [v1.0] [1w]` — `mdk tenants create` API + provisioning runbook for hosted-by-Movate customers.
- [ ] **`docs/adr/001-cloud-portability.md`** `[HIGH] [v0.6] [≤2h]` — captures the principle: stay Azure-first today, but every new infrastructure decision must be cloud-portable in principle. Lists what we won't add (Cosmos DB, Azure-only auth, etc). Cheap, prevents future lock-in regret.
- [ ] **GCP / AWS deployment runbooks** `[MED] [v1.1+] [1w each]` — Terraform modules for AWS (ECS + RDS) and GCP (Cloud Run + CloudSQL). Deferred until a real customer asks. Architecture's already portable; just need the IaC.
- [ ] **Enterprise governance compliance (SOC2 audit trail)** `[LOW] [v1.1+] [—]` — most building blocks shipped (audit log via failures table, RunRecord, OTel). Formal SOC2 scoping when first enterprise customer asks.

---

## How to use this file

1. Pick the highest item from §0 ("Top 10") that isn't blocked.
2. Move it to `[ip]` while you work.
3. On merge, flip to `[x]` with the actual completion date in a commit message — the file itself stays clean.
4. Re-rank the Top 10 every two weeks. Leverage shifts as context changes.

## How to keep this file current

> The pattern: **each PR-shipping session ends with two doc updates** —
> a daily progress log (`docs/progress/<YYYY-MM-DD>.md`) AND the
> top-of-list paragraph here. The daily log is the linear narrative;
> this file is the persistent ranked backlog.

When a PR merges (or a coherent session of merges lands), at end of session:

1. **Drop a top-of-list paragraph** in `🎯 Top 10 highest-leverage shortlist` →
   `### Session <YYYY-MM-DD>` — most-recent first. Each paragraph follows
   the existing pattern: `**<Feature> shipped this session.**` + numeric test
   delta + 2-3 sentences of what / why / where + PR link + linked issue.
   Keep the bold opener + the closing **bold conclusion** sentence — readers
   skim those.
2. **Flip checkboxes** down in §1–§9 for any pre-existing line items the
   session completed. Replace `[ ]` with `[x]`, change `[next]` /
   `[ip]` to `[done <YYYY-MM-DD>]`, and append a one-line outcome summary
   with a PR link. Don't delete the original description — operators
   read this for context on WHY something was on the list.
3. **Add new line items** for follow-up work the session uncovered. New
   items default to `[ ]` and get linked to their GitHub issue.
4. **Re-rank the Top 10** if today's work shifted what's most leverage-
   per-effort. Items move between groups (A / B / C / D) freely.

Mechanically: this file changes on every PR-shipping day. The
[daily progress log](docs/progress/) is the source of truth for
"what was shipped"; this file is the source of truth for "what's next
+ relative priority." Don't let them drift.
