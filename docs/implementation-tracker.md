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

> ✅ **Next-10 arc complete** (`2026.5.23.13` → `.27`). Two loops closed since:
> **ADR 017 D2 orchestration substrate** — scheduler (#382, `.29`) + event/webhook
> triggers (#384, `.31`); and the **ADR 016 improvement loop** — harvest (#5) →
> continuous-eval/drift (#10) → **canary / champion–challenger (#386, `.33`)**.
> **Item 14 (durable + HITL — finish the `HUMAN` node) is next** — the remaining
> orchestration capstone (depends on 11+13, both merged); the heaviest item (a
> durable pause/persist/resume-on-signal state machine, ~2 PRs).

## Items 11+ (sequenced; orchestration ADR 017 woven in by leverage/dependency)

| # | Item | ADR / ref | LOE (Claude) | Depends on | Status | PR | Merged |
|---|------|-----------|--------------|------------|--------|----|--------|
| 11 | **Generalize the scheduler** — cron→enqueue arbitrary agent/workflow jobs (ACA Jobs substrate); extends item 10's eval scheduler | 017.D2 / 016 | L (~1h) 🔒 | #10 | ✅ | #382 | `2026.5.23.29` |
| 12 | Canary / champion–challenger (version-tagged runs, weighted routing, assisted-promote) — completes the improvement loop | 016.D3 | L (~1–1.5h) | registry, obs, scopes | ✅ | #386 | `2026.5.23.33` |
| 13 | Event / webhook triggers — run an agent/workflow on an inbound event | 017.D2 | M (~35m) | #11 | ✅ | #384 | `2026.5.23.31` |
| 14 | Durable + HITL — `HUMAN` node pause/persist/resume-on-signal (long, human-gated pipelines) | 017.D5 / #28 | L (~1.5–2h, ~2 PRs) | #11, #13 | ⬜ | | `____` |
| 15 | External-orchestrator adapter pack — `mdk[prefect]` task + `mdk[airflow]` `MovateAgentOperator` + webhook contract (movate as a *callable*, no core dep) | 017.D3 | M–L (~45m) 🔒 | — | ⬜ | | `____` |
| 16 | Key rotation UX (`mdk auth rotate-key` grace overlap, expiry warnings, bulk revoke) | 013.D5 | M (~25m) | scopes | ⬜ | | `____` |
| 17 | Batch inference API (bulk async run over a dataset) | feature | M (~25m) | — | ⬜ | | `____` |
| 18 | Streaming responses (SSE) | #75 | S (~20m) | — | ⬜ | | `____` |
| 19 | Workload identity for service-to-service (removes shared fleet key + KV bootstrap secret) | 013.D6 | M (~35m) 🔒 | — | ⬜ | | `____` |
| 20 | Langfuse self-host Bicep module (`enableLangfuse`: ClickHouse/Redis/Blob, KV, private ingress) | 015.3 | L (~45m) 🔒 | langfuse-v3 | ⬜ | | `____` |
| 21 | Edge gateway (APIM / Envoy — custom domain, dev portal, edge throttle/WAF) | 013.L3 | L (~1h) 🔒 | scopes | ⬜ | | `____` |
| — | _(Temporal/Prefect durable backend — only if a single external engine is required; Deva sign-off, ADR 001)_ | 017.D4 | — | — | — | | |

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
