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
| 8 | Langfuse v3 SDK bump — `tracing/langfuse.py` v2→v3; `langfuse>=3` extra | 015.2 | S–M (~25m) | — | ⬜ | | `____` |
| 9 | Response cache — `CacheProvider` adapter (exact + optional semantic), Redis/Postgres-backed | feature | M (~35m) | — | ⬜ | | `____` |
| 10 | Continuous eval + drift alerting — scheduler enqueues eval-job on cadence/publish; baseline-diff; alerts | 016.D2 | L (~1–1.5h, ~2 PRs) | #2, scheduler | ⬜ | | `____` |

## After the next 10 (rough order)

| Item | ADR / ref | LOE (Claude) |
|------|-----------|--------------|
| Key rotation UX (`mdk auth rotate-key` grace overlap, expiry warnings, bulk revoke) | 013.D5 | M (~25m) |
| Langfuse self-host Bicep module (`enableLangfuse`: ClickHouse/Redis/Blob, KV, private ingress) | 015.3 | L (~45m) 🔒 |
| Canary / champion–challenger (version-tagged runs, weighted routing, assisted-promote) | 016.D3 | L (~1–1.5h) |
| Workload identity for service-to-service (removes shared fleet key + KV bootstrap secret) | 013.D6 | M (~35m) 🔒 |
| Edge gateway (APIM / Envoy — custom domain, dev portal, edge throttle/WAF) | 013.L3 | L (~1h) 🔒 |
| Batch inference API (bulk async run over a dataset) | feature | M (~25m) |
| Streaming responses (SSE) | #75 | S (~20m) |

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
