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

6. [ ] **KEDA Postgres scaler for worker autoscaling** `[HIGH] [post-v1.0] [≤1d]` — replace the current CPU-utilization scale rule in `containerapp-worker.bicep` with a KEDA scaler keyed on `SELECT COUNT(*) FROM jobs WHERE status='queued' AND (tenant_id = '<scope>' OR tenant_id IS NULL)`. CPU is a lagging indicator (queue is already deep by the time CPU rises); queue depth is the leading one. ACA supports KEDA scalers natively. Code-side: add the scaler spec to the worker Bicep + bake the connection-string env var into the worker container's KEDA trigger metadata.
7. [ ] **Job retry policy with exponential backoff + dead-letter** `[HIGH] [post-v1.0] [1-2d]` — currently every ERROR is terminal. Production needs: transient errors (network blip, provider 5xx) re-queue with exponential backoff + jitter; max retries (configurable per agent, default 3); persistent-failures land in a `DEAD_LETTER` status so operators can triage without losing the input. Schema: `attempt_count` + `next_retry_at` columns on `jobs`. Worker: `claim_next_job` picks rows where `next_retry_at IS NULL OR next_retry_at < NOW()`. Reuses `core/retry.py` `RetryableError` concept that's already in the codebase for in-process retries.
8. [ ] **Rate limiting per API key** `[MED] [post-v1.0] [1-2d]` — currently a misbehaving client can flood `POST /run`. Add a leaky-bucket counter keyed on `api_key_id` (in-process for v1.x, Redis for v1.1 once multi-replica API needs shared state). Default: 60 req/min per key. 429 response with `Retry-After` header on overflow.
9. [ ] **`/ready` endpoint with deep checks** `[MED] [post-v1.0] [≤1d]` — `/healthz` returns 200 unconditionally (lightweight liveness; ACA's livenessProbe uses it). `/ready` checks DB connectivity + agent registry non-empty + tracer reachable; ACA's readinessProbe gates traffic on it. Avoids serving 500s during a Postgres restart window.

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
- [ ] **Per-tenant cost ceiling enforcement** `[HIGH] [v1.0] [2-3d]` — Postgres-backed monthly budget; auto-pause on breach.
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
- [ ] **`movate watch <agent>` (hot-reload on YAML change)** `[MED] [v0.2] [≤1d]` — TDD-style dev loop.
- [x] **Templates beyond `agent_init` — `faq`, `summarizer`, `classifier`** `[HIGH] [v0.2] [done]` — registry at [src/movate/templates/__init__.py](src/movate/templates/__init__.py); `movate init -t faq` (and `summarizer`, `classifier`). FAQ + summarizer ship with a `judge.yaml.example`; classifier uses exact-match. 21 tests in [tests/test_templates.py](tests/test_templates.py).
- [x] **Live-API smoke tests (env-gated)** `[HIGH] [v0.2] [done]` — [tests/test_smoke_litellm.py](tests/test_smoke_litellm.py) + [scripts/smoke.sh](scripts/smoke.sh). 3 tests covering OpenAI direct, Anthropic direct, and full executor against real OpenAI. Module-level `pytestmark = pytest.mark.smoke`; CI filters with `-m "not smoke"`. Each test independently gated on the relevant API key.
- [ ] **Workflow templates — `returns-processing`, `triage-then-respond`** `[MED] [v0.3] [≤1d]`.
- [ ] **VS Code launch configs (debug a single agent run)** `[MED] [v0.2] [≤2h]` — port from MDK if useful.
- [x] **`movate run --replay <run-id>`** `[HIGH] [v0.4] [done]` — `core/run_replay.py` + `cli/run.py` flag. Re-executes a recorded `RunRecord` against the current agent bundle (prompt/model/schemas reload from disk). Surfaces `output_changed`, `status_changed`, `changed_keys`, cost + latency deltas. Output changes are not failures (debug tool); only a current-run error trips exit 1. Mutually exclusive with positional INPUT. Workflow replay deferred. 14 tests in [tests/test_run_replay.py](tests/test_run_replay.py).
- [ ] **`movate diff <agent-a> <agent-b>`** `[MED] [v0.2] [≤1d]` — show prompt-hash, model, schema deltas; great for PR review.
- [ ] **Prompt linter** `[MED] [v0.2] [≤1d]` — flag missing JSON-only instruction, undeclared `{{ input.* }}` refs, no output schema example.
- [ ] **Cost forecast on `validate`** `[MED] [v0.2] [≤1d]` — print expected cost based on dataset + average tokens.
- [ ] **`--dry-run` on `run`** `[MED] [v0.2] [≤2h]` — render prompt, show what *would* be sent, exit 0.
- [ ] **Structured logging (structlog) everywhere** `[MED] [v0.4] [≤1d]` — already a dep; standardize on it.
- [ ] **Docs site (mkdocs) — internal** `[LOW] [v0.6] [1w]` — defer; per-user decision is internal-only, README + `--help` is enough through v0.5.

---

## How to use this file

1. Pick the highest item from §0 ("Top 10") that isn't blocked.
2. Move it to `[ip]` while you work.
3. On merge, flip to `[x]` with the actual completion date in a commit message — the file itself stays clean.
4. Re-rank the Top 10 every two weeks. Leverage shifts as context changes.
