# Implementation tracker — ADR 012–016 build-out

Living tracker for implementing the accepted/proposed architecture ADRs
(012–016) plus a few high-leverage standalone features. Ordered by build
sequence (dependencies + leverage). **Versioning is CalVer `YYYY.M.D.N`,
auto-bumped per merged PR** — the "Merged" column is `mdk --version` at merge.

**LOE is Claude wall-clock** (background build-agent + verify/bump/PR + CI/merge),
**not human dev-days** — grounded in this session's observed ~9–16 min agent
builds. Buckets: **S ≈ ≤25 min · M ≈ 25–45 min · L ≈ 1–2 hr (often 2 PRs).**
🔒 = code lands fast in Claude time, but *final validation* is gated on a live
**Azure subscription / IdP** (external — not Claude time).

**Status key:** ⬜ queued · 🔄 in flight · ✅ merged

## Next 10

| # | Item | ADR / ref | LOE (Claude) | Depends on | Status | PR | Merged |
|---|------|-----------|--------------|------------|--------|----|--------|
| 1 | Registry storage layer (`AgentBundleRecord` + storage methods + `agent_bundles` table) | 014.1 | M (~25m) | — | ✅ | #366 | `2026.5.23.13` |
| 2 | Resolve-from-registry — runtime loads agents from storage; version-keyed cache; FS→registry import (**closes #109 async gap**) | 014.2 | M (~35m) 🔒 | #1 | ✅ | #368 | `2026.5.23.15` |
| 3 | Scopes / least-privilege — scope set + `require_scope` + `ApiKeyRecord.scopes` (+ back-compat migration) + OIDC claim map + `create-key --scope` | 013.L2 | M (~35m) | — | ✅ | #369 | `2026.5.23.16` |
| 4 | OTLP → Azure Monitor — wire `OtelTracer` to App Insights; `MOVATE_TRACE_SINK=otlp` | 015.1 | S (~20m) 🔒 | — | ✅ | #370 | `2026.5.23.17` |
| 5 | Harvest — prod runs + feedback → *proposed* eval cases (`mdk eval harvest` + API) | 016.D1 | M (~30m) | (#2 helps) | ✅ | #372 | `2026.5.23.19` |
| 6 | Versioning UX — optimistic concurrency (If-Match/409), `GET …/versions`, `revert` (#80), `mdk agent history\|revert` | 014.3 | M (~30m) | #1, #2 | ✅ | #373 | `2026.5.23.20` |
| 7 | `mdk login` — OIDC device-code SSO + token cache/refresh + `TargetConfig auth:oidc` + login/logout/whoami | 013.L1 | L (~50m) 🔒 | — | ✅ | #374 | `2026.5.23.21` |
| 8 | Langfuse v3 SDK bump — `tracing/langfuse.py` v2→v3; `langfuse>=3` extra | 015.2 | S–M (~25m) | — | ✅ | #376 | `2026.5.23.23` |
| 9 | Response cache — `CacheProvider` adapter (exact + optional semantic), Redis/Postgres-backed | feature | M (~35m) | — | ✅ | #378 | `2026.5.23.25` |
| 10 | Continuous eval + drift alerting — scheduler enqueues eval-job on cadence/publish; baseline-diff; alerts | 016.D2 | L (~1–1.5h, 1 PR) | #2, scheduler | ✅ | #380 | `2026.5.23.27` |

> ✅ **Next-10 arc + both major ADR arcs complete** (`2026.5.23.13` → `.36`):
> - **ADR 017 orchestration** — D2 scheduler (#382) + event/webhook triggers (#384);
>   D5 durable + HITL `HUMAN` node, pause/persist (#388) + resume-on-signal (#389).
> - **ADR 016 improvement loop** — harvest (#5) → continuous-eval/drift (#10) →
>   canary / champion–challenger (#386).
>
> **Standalone quick wins done** (`.1`→`.4`): SSE streaming (#391), batch inference
> (#393), key rotation (#394) — all fully validatable in Claude time.
>
> **Remaining feature items 15, 19–21 are all 🔒** (code lands in Claude time;
> final validation needs a live Azure subscription / IdP). The **robustness &
> hardening backlog (items 22–29)** is below. Pick by priority — these are not
> dependency-ordered, and the 🔒 ones are gated on a staging deploy + smoke pass.

## Items 11+ (sequenced; orchestration ADR 017 woven in by leverage/dependency)

| # | Item | ADR / ref | LOE (Claude) | Depends on | Status | PR | Merged |
|---|------|-----------|--------------|------------|--------|----|--------|
| 11 | **Generalize the scheduler** — cron→enqueue arbitrary agent/workflow jobs (ACA Jobs substrate); extends item 10's eval scheduler | 017.D2 / 016 | L (~1h) 🔒 | #10 | ✅ | #382 | `2026.5.23.29` |
| 12 | Canary / champion–challenger (version-tagged runs, weighted routing, assisted-promote) — completes the improvement loop | 016.D3 | L (~1–1.5h) | registry, obs, scopes | ✅ | #386 | `2026.5.23.33` |
| 13 | Event / webhook triggers — run an agent/workflow on an inbound event | 017.D2 | M (~35m) | #11 | ✅ | #384 | `2026.5.23.31` |
| 14 | Durable + HITL — `HUMAN` node pause/persist/resume-on-signal (long, human-gated pipelines) | 017.D5 / #28 | L (~1.5–2h, 2 PRs) | #11, #13 | ✅ | #388, #389 | `2026.5.23.36` |
| 15 | External-orchestrator adapter pack — `mdk[prefect]` task + `mdk[airflow]` `MovateAgentOperator` + webhook contract (movate as a *callable*, no core dep) | 017.D3 | M–L (~45m) 🔒 | — | ⬜ | | `____` |
| 16 | Key rotation UX (`mdk auth rotate-key` grace overlap, expiry warnings, bulk revoke) | 013.D5 | M (~25m) | scopes | ✅ | #394 | `2026.5.24.4` |
| 17 | Batch inference API (bulk async run over a dataset) | feature | M (~25m) | — | ✅ | #393 | `2026.5.24.3` |
| 18 | Streaming responses (SSE) — `POST …/runs/stream` + `mdk run --target --stream` | #75 | S (~20m) | — | ✅ | #391 | `2026.5.24.1` |
| 19 | Workload identity for service-to-service (removes shared fleet key + KV bootstrap secret) | 013.D6 | M (~35m) 🔒 | — | ⬜ | | `____` |
| 20 | Langfuse self-host Bicep module (`enableLangfuse`: ClickHouse/Redis/Blob, KV, private ingress) | 015.3 | L (~45m) 🔒 | langfuse-v3 | ⬜ | | `____` |
| 21 | Edge gateway (APIM / Envoy — custom domain, dev portal, edge throttle/WAF) | 013.L3 | L (~1h) 🔒 | scopes | ⬜ | | `____` |
| — | _(Temporal/Prefect durable backend — only if a single external engine is required; Deva sign-off, ADR 001)_ | 017.D4 | — | — | — | | |

## Robustness & hardening (items 22+) — production-readiness gaps

From the platform-robustness review: the *functional* surface is feature-complete,
but these close the gap from "feature-complete" to "production-robust." Three
classes — **(A) deferred safety behaviors** explicitly punted during the feature
build, **(B) operational hardening**, **(C) operator docs**. Several are 🔒 (code
lands in Claude time; final validation needs a live Azure subscription). The
single biggest caveat is not a row below: **none of the 🔒 infra code (items 11,
15, 19–21) has been exercised on a real Azure subscription — a staging deploy +
smoke pass is the gate to calling the platform production-ready.**

| # | Item | Class | ADR / ref | LOE (Claude) | Depends on | Status | PR | Merged |
|---|------|-------|-----------|--------------|------------|--------|----|--------|
| 22 | **Auto-rollback on drift** — when a scheduled eval flags a challenger regression, opt-in auto-revert to champion (weight→0 + restore champion); informs by default, auto only when enabled | A | 016.D5 | M (~30m) | #10, #12 | ✅ | #396 | `2026.5.24.6` |
| 23 | **Trigger replay / idempotency** — delivery-id / nonce store so a duplicate inbound event doesn't double-enqueue a run | A | 017.D2 | M (~30m) | #13 | ✅ | #398 | `2026.5.24.8` |
| 24 | **Per-dimension drift** — persist per-dimension eval means on `EvalRecord`; extend `drift.py` to compare per-dimension, not just aggregate mean/pass-rate | A | 016.D2 | M (~30m) | #10 | ✅ | #397 | `2026.5.24.7` |
| 25 | **Per-tenant rate-limiting / quota** at the runtime edge — token-bucket / sliding-window per (tenant, scope), 429 on exceed; portable app-level (always-on, complements #21) | B | 013 / feature | M (~35m) | scopes | ✅ | #400 | `2026.5.24.10` |
| 26 | **DR / backup runbook + tooling** for Postgres storage — documented backup/restore + point-in-time, plus a `mdk` export/import escape hatch | B | feature | M (~35m) 🔒 | — | ⬜ | | `____` |
| 27 | **Golden-signal SLOs + alerting** — readiness/liveness probes + latency / error-rate / queue-depth metrics → Azure Monitor alert rules (beyond drift) | B | 015 | M–L (~45m) 🔒 | #4 | ⬜ | | `____` |
| 28 | **Load / soak test harness** — drive the job-queue + KEDA autoscale path under load; capture baseline throughput/latency; documented results | B | feature | M (~35m) 🔒 | #11 | ⬜ | | `____` |
| 29 | **Operator runbooks** — configure/operate/troubleshoot the new surfaces (scheduler, triggers, durable/HITL, canary, harvest, continuous-eval, batch, SSE) | C | docs | M (~30m) | (features land) | ⬜ | | `____` |
| 30 | **Per-tenant BYOK provider keys** — each tenant stores its own OpenAI/Anthropic key (Fernet-encrypted at rest); `ProviderKeyResolver` (tenant→shared fallback); `mdk keys set\|list\|delete\|test` + `/api/v1/provider-keys`; admin-gated, value never returned | D | **018** | M–L (~45m) | scopes | ⬜ | | `____` |

## How this is maintained
As each PR merges, its row flips to ✅ and the **Merged** column is stamped with
the CalVer version at merge. New items append to the relevant section. The
authoritative feature backlog stays in [`BACKLOG.md`](BACKLOG.md); this file is
the focused build queue for the 012–016 arc.

> **A note on "Claude time":** the dominant wall-clock cost per item is the
> background build-agent run (~9–16 min observed) + a CI cycle (~5–12 min) + my
> verify/bump/PR, plus the occasional rebase when two PRs share the version
> line. The estimates assume that pipeline. They are **not** an estimate of
> human engineering effort, which for these items would be the days-scale
> figures in `BACKLOG.md`.
