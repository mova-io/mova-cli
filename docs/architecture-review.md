# MDK architecture review

**Status:** Review packet (synthesis — not a new decision)
**Audience:** Engineering + Deva, architecture/roadmap review
**Date:** 2026-05-27
**Reads with:** [`CLAUDE.md`](../CLAUDE.md), [`docs/architecture-principles.md`](architecture-principles.md),
the ADRs under [`docs/adr/`](adr/), the front-end audit
[`docs/front-end-api.md`](front-end-api.md).

> This packet **synthesizes** the existing canonical docs + the recent ADR arc
> (023→038) for a single review pass. It invents nothing: every claim traces to
> an ADR or the code layout, and it is deliberately honest about *status* —
> "Accepted (ADR)" is a decision, not always shipped code. A short
> reconciliation list of ADR-status drift is in §9.

---

## 1. Positioning

MDK (`movate-cli` / `mdk`) is an enterprise-grade **orchestration + governance +
evaluation + deployment** plane for production AI workflows — **not** an
"autonomous agents" framework, and explicitly not a competitor in the
autonomous-planning / swarm race (ADR 038 makes this the stated north-star).
The guiding principle is **adapt, don't adopt** (ADR 017): MDK extends the
native engine it already has and treats external frameworks (LangChain,
LangGraph, Airflow, Prefect, Temporal) as *swappable adapters behind a seam*,
never as the core dependency. This follows from CLAUDE.md rule 8 — favour
composable Python over framework sprawl; a new framework needs a *proven*
scaling need plus Deva sign-off (ADR 001). The product wedge is that **every
pattern MDK supports is observable, evaluable, bounded, and deployable** (the
"governable filter", ADR 038 D5) — governance is the moat, not pattern count.

---

## 2. The two planes + the seams

```
        CONTROL PLANE                         EXECUTION PLANE
  ┌───────────────────────┐            ┌────────────────────────┐
  │  movate.cli            │   ⊥ never  │  movate.runtime        │
  │  author / deploy /     │  imports   │  FastAPI app serving    │
  │  eval / ops (~81 files)│ ◀────────▶ │  deployed agents/jobs   │
  └───────────┬───────────┘            └───────────┬────────────┘
              │     both depend on core + adapters  │
              ▼                                      ▼
        ┌──────────────────────  movate.core  ──────────────────────┐
        │  contracts + orchestration: models, loader, executor,      │
        │  eval, workflow/, config, auth, scheduler, canary, drift   │
        └───────────────┬───────────────────────────────────────────┘
                        │  depends only on adapter *Protocols*
        ┌───────────────┴───────────────────────────────────────────┐
        │  providers/  BaseLLMProvider   storage/  StorageProvider    │
        │  tracing/    Tracer            core/skill_backend/ SkillBackend │
        └─────────────────────────────────────────────────────────────┘
   movate.kb (ingest + retrieval) sits on top of the StorageProvider Protocol.
```

The hard boundary: **control plane (`cli`) ⊥ execution plane (`runtime`)** —
the runtime never imports `cli`, which is exactly why server-side LLM authoring
needs a refactor (the planner lives behind `cli` today; see §6). `core` depends
on **adapter Protocols**, never a concrete backend — the four seams are
`BaseLLMProvider` (`providers/base.py`), `StorageProvider` (`storage/base.py`),
`Tracer` (`tracing/base.py`), and `SkillBackend` (`core/skill_backend/base.py`).
Backends are selected at the edge (the provider registry, `storage.build_storage()`,
the composite tracer); tracing is wired at the edges and never imported into
execution logic (`null.py`/`composite.py` make "no tracer configured" a no-op).

**Engine note (important framing):** the runtime runs MDK's **own execution
engine** — `core/executor.py`, a tool-use loop with a hard turn cap and the
ADR 023 pre-retrieval phase — over **LiteLLM** as the default multi-provider
adapter (`providers/registry.py` constructs `ProviderRegistry(default_litellm=…)`).
**LangChain is NOT the engine** — `providers/langchain_native.py` is just one
optional provider adapter alongside `anthropic` / `openai_native` / `litellm` /
`lyzr` / `mock`. LangGraph (ADR 030) is likewise a *proposed optional execution
backend behind the workflow runner seam*, not a core dependency.

---

## 3. The pillars

**Authoring.** `mdk init` is the front door and always leaves a *runnable*
project (ADR 026): `project.yaml` + `.env.example` + `AGENTS.md` + the agent
under `agents/<name>/` + an initial snapshot; `--bare` keeps a first-class
standalone agent. On top sits the **authoring copilot** (ADR 025): a typed
**Action Catalog** (`authoring/catalog.py` + `authoring/actions/*`:
contexts/instructions/kb/skills/model/evals/retrieval/metadata) driven by a
uniform **plan → preview → apply → verify → undo** spine, exposed three ways —
conversational `mdk dev`, an `AGENTS.md` that teaches external coding agents,
and an MCP server (`authoring/mcp_server.py`). The LLM is a swappable
`BaseLLMProvider`; the copilot never touches the filesystem directly — only
typed, validated, reversible catalog actions. A `~16-shape` template gallery
(`templates/`) + an interactive picker + a workflow starter back it (ADR 028).

**Execution.** `core/executor.py` is the single shared engine both planes call
(so `mdk run` local, runtime-inline, and the worker behave identically). It runs
a **tool-use loop** (LLM turn → skill/tool calls → repeat, with a hard cap),
optional ADR-023 pre-retrieval, the provider seam with fallback, and per-step
cost/latency/token accounting (ADR 024). Provider selection + fallback live in
`providers/registry.py` over `BaseLLMProvider`.

**Knowledge (RAG).** `kb/` is a full retrieval stack over the `StorageProvider`
Protocol — `chunk`/`embed`/`ingest`, `lexical` + vector + **hybrid** `search`,
`rerank` (cross-encoder, optional extra), `multi_hop`, query `rewrite`,
`grounding_gap`, and **GraphRAG** (`graph_extract` + `graph_retrieval`, ADR 010;
*partially wired* — entity/relation layer in `storage/base.py`). pgvector / HNSW
is the Postgres vector path (ADR 009). Grounding-by-default is the opt-in
**auto-RAG** pre-retrieval phase in the shared executor (ADR 023): add a
`retrieval:` block to `agent.yaml` and the executor retrieves-then-renders
identically across all three planes; absent the block, execution is byte-for-byte
unchanged.

**Skills.** `core/skill_backend/` implements the `SkillBackend` Protocol with
three backends — **python / http / mcp** (+ an agent-as-tool backend). Skills are
LLM-invoked tools dispatched inside the executor loop, gated by **`SkillPolicy`**
(`core/config.py`) which carves which side-effect categories an agent may use.

**Governance.** The strongest pillar. **Scopes** — a least-privilege flat scope
model (`read`/`run`/`eval`/`kb:write`/`admin`/`fleet-admin`) on every endpoint
(ADR 013, `core/auth.py`). **HITL + orchestration** — durable execution + a real
`HUMAN` pause/resume node on the native runner (ADR 017, D5 shipped). **Eval
gates + canary/drift** — the continuous-improvement loop (ADR 016): harvest prod
runs into eval cases, scheduled continuous eval with drift alerting, and
champion-vs-challenger **canary** with assisted, scope-gated promotion
(`core/canary.py`, `core/drift.py`, `core/harvest.py`). **Audit** —
`tracing/audit.py` + an authoring audit/replay log (`authoring/audit.py`).
**Budgets + quotas** — per-run cost/token budgets (executor + `authoring/budget.py`)
and per-tenant usage metering + quotas (ADR 036, ADR-stage).

**Observability.** Per-step execution model — nested spans, retained per-step
cost/latency, and a CLI tree via `mdk explain` (ADR 024) — readable offline from
the persisted `RunRecord` without a backend. Sinks compose behind the `Tracer`
seam: **Langfuse** (v3), **OTel** → collector → **Azure Monitor** (ADR 015/019/020),
metrics, audit, log-correlation. Reporting surfaces it without rebuilding it
(ADR 031): deepen Langfuse (eval scores/datasets/trace links), **dashboards-as-code**
(`dashboards/`: grafana/prometheus/azure), and an offline `mdk report` rollup.
DB-pool saturation metrics + a `doctor` capacity check are the ADR-034 scale lens.

**Orchestration.** `core/workflow/` is a first-class declarative engine —
`WorkflowSpec` (`spec.py`: nodes agent/intent-router/human, edges, `state_schema`,
`entrypoint`, workflow evals), `WorkflowRunner` (`runner.py`), an IR (`ir.py`) +
compiler (`compiler.py` + `compilers/`), durable + HITL execution (ADR 017),
workflow authoring through the same copilot spine (ADR 029), and **LangGraph as a
proposed optional compile/execution target** (ADR 030). The whole thing builds
toward the governable agent-pattern north-star (ADR 038).

**Deployment.** A **durable, shared agent registry** behind `StorageProvider`
(ADR 014): publishing an agent is an instant versioned DB write every pod sees,
decoupled from the rare runtime image rebuild — with history, rollback, and
optimistic concurrency. Runs on Azure Container Apps + a Postgres-backed job
queue with **KEDA** queue-depth autoscaling and a worker (ADR 017). The
**`/api/v1`** runtime surface (75 versioned routes; §6) is the front-end contract;
API hardening (ADR 033) makes it production-grade for a browser front end.

---

## 4. Decision log — ADR 023 → 038

| # | Title | Status | One-line decision |
|---|---|---|---|
| 023 | Auto-retrieval (pre-retrieval) | Accepted | Opt-in declarative `retrieval:` block adds a pre-retrieval phase to the **shared executor**, so grounding behaves identically across `run`/runtime/worker; off unless `auto_into` set. |
| 024 | Per-step execution observability | Accepted | Per-step model (turns → skill/retrieval children): nested spans (no Tracer change), retained per-step cost/latency in `RunRecord`, and a `mdk explain` Rich tree, offline-first. |
| 025 | Authoring copilot | Accepted | A typed **Action Catalog** + a `plan→preview→apply→verify` spine, driven by 3 surfaces (`mdk dev`, `AGENTS.md`, MCP server); LLM-agnostic, validated, reversible — control-plane only. |
| 026 | `mdk init` front-door UX | Accepted | `init` always yields a *runnable* project (or extends one); name resolution for `run`/`validate`/`dev`; standalone `--bare` agents first-class; editor launch; configurable `--llm` LLM. |
| 027 | `mdk dev` live-reload loop | Accepted | Re-run the agent on save (extend `watch.py`, also `mdk watch --run`) and show new output + diff; single foreground loop (no background thread). |
| 028 | Template discoverability + workflow starter | Accepted | Interactive `init` picker + non-interactive `mdk templates`; a runnable 2-node `workflow_init` starter; shared use-case metadata feeding both the picker and the `--llm` matcher. |
| 029 | Workflow authoring | Accepted | Extend the ADR-025 spine to a `workflow` entity — new catalog actions over `workflow.yaml`, `mdk dev <workflow>` visualises/runs/traces, planner authors in NL, `verify` = validate + `--mock` run + eval gate. |
| 030 | LangGraph optional execution backend | **Proposed** | Promote LangGraph from codegen-export to a first-class **opt-in** backend behind the runner seam (native runner stays default); grow compiler (conditional/parallel/cycles + typed state) + `StorageProvider` checkpointer. **Dep adoption gated on Deva sign-off.** |
| 031 | Reporting & dashboards | Accepted | Surface telemetry via 3 surfaces, no new core dep: deepen Langfuse (D1), Grafana/Prometheus/Azure **dashboards-as-code** (D2), offline `mdk report` rollup (D3). |
| 032 | Front-end API completion | Accepted (not on `main`) | Three additive `/api/v1` capabilities: draft/preview + server-side LLM authoring (D1), aggregate monitor endpoints over a factored `core/reporting` (D2), async KB ingest via `JobKind.INGEST` (D3). |
| 033 | API hardening | Accepted (not on `main`) | Uniform cross-cutting hardening on `/api/v1`: cursor pagination, request-id correlation, rate-limit headers, idempotency everywhere, `ETag`/`If-Match` (soft→enforced), payload limits, OpenAPI completeness, deprecation policy. |
| 034 | Data-plane scalability | Accepted | PgBouncer/server-side pooling under autoscale with a `pods × pool_max ≤ max_connections` ceiling (D1, infra → Deva sign-off), read replicas behind `StorageProvider` (D2), pool observability + `doctor` check (D3). |
| 035 | Outbound events + webhooks | Accepted | Typed tenant-scoped lifecycle events emitted at the edges (D1); HMAC-signed at-least-once webhook subscriptions over the worker queue (D2); front-end realtime SSE stream (D3). |
| 036 | Usage metering + quotas | Accepted | Per-tenant usage rollup + `GET /usage` (D1); admission-time quotas with soft 80% / hard 100% (D2, **policy → Deva sign-off**); billing export (D3). Reuses per-run cost records. |
| 037 | Workflow API parity | Accepted | Workflow CRUD/validate/version/publish over `/api/v1` (D1), run-management parity incl. HITL signal + per-node trace (D2), authoring-over-API (D3) — agent-parity for workflows. |
| 038 | Governable agent-pattern library | **Proposed (north-star, not scheduled)** | Patterns = compositions of **governed primitives** over `WorkflowSpec`/`WorkflowRunner` (not bespoke engines): canonical node taxonomy, a cross-cutting governance contract, a template library, inline JUDGE/GATE + bounded SUPERVISOR; the "governable filter" declines swarms/debate/recursive-spawn. |

---

## 5. Scale posture (ADR 034)

The data-plane scale analysis is concrete. Per-pod `asyncpg` pools already exist
and bundle-serving is version-keyed cached per pod (ADR 021) — both good. Two
risks remain: under KEDA autoscale, **`N_pods × pool_max` can exceed Postgres
`max_connections`** → silent connection exhaustion; and all reads hit the single
primary.

The decisions: **D1** front Postgres with **PgBouncer** (or Azure Flexible
Server built-in pooling) in transaction mode, size per-pod pools against KEDA
max-replicas via the documented ceiling **`pods × pool_max ≤ max_connections −
headroom`**, set `statement_cache_size=0` for asyncpg-behind-pgbouncer, and add a
`mdk doctor` capacity check. **D2** route lag-tolerant reads (lists, dashboards,
history) to an optional **read replica** behind `StorageProvider`, writes always
to primary, **falls back to primary when no replica configured** (portable,
opt-in). **D3** emit pool in-use/idle/wait metrics into the ADR-031 dashboards.

This pairs with ADR 032 D3 (**async KB ingest** via `JobKind.INGEST` on the
KEDA-autoscaled worker — moving bulk crawl/embed off the request path).

**Shipped vs gated:** D3 pool metrics + the D1 doctor check + pool-sizing are
buildable with no infra dependency and ship first; **D1 PgBouncer provisioning
and D2 read replicas are infra-shaped (bicep/env) and gated on Deva sign-off**
(see §9). NB: on `origin/main` the pool-metrics code and `doctor` check are not
yet present (the ADR-034 implementation commit is not merged); on the review
branch they are an in-flight item.

---

## 6. Front-end API readiness

The `/api/v1` surface is the documented compat contract the Mova iO Angular
front end drives (CLAUDE.md rule 5). The audit (`docs/front-end-api.md`)
inventories **75 versioned routes + 9 unversioned**, each with a required scope,
and a hermetic contract test (`tests/test_front_end_api_contract.py`) pins the
key paths/methods/scopes so a rename/removal fails CI (it is a *floor*, not a
full snapshot — additive growth is fine).

The five conceptual front-end operations map cleanly: **add** (catalog
CRUD), **validate** (linter + cost forecast), **deploy** (publish + canary
promote/rollback/revert), and **monitor** (the async run → poll job → fetch run →
trace/explain → eval scorecard loop) are all **ready**. Auth is bearer-key +
least-privilege scopes (ADR 013), with an OIDC/SSO path when `MOVATE_OIDC_ISSUER`
is set, and CORS pinned per environment.

The **gaps**, all tracked by ADR 032 (Accepted as decisions; **not yet on
`main`** — `core/reporting.py` and the preview/usage/ingest endpoints are not
present):

- **No server-side LLM authoring + no draft/preview.** Every create endpoint is
  structured-fields / pre-built-bundle only (the wizard writes `agent_prompt`
  verbatim — no model call); the `mdk init --llm` planner lives behind `cli` and
  the runtime never imports `cli`. ADR 032 D1 closes this with
  `POST /agents/preview`, which requires factoring the generator
  (`movate.scaffold.generate_agent_from_description`, a non-`cli` module) to be
  runtime-importable.
- **No aggregate monitor data API.** A dashboard must page raw `/runs`; the
  `mdk report` rollup is CLI-only. ADR 032 D2 factors the aggregation into a
  backend-agnostic `core/reporting` consumed by both `mdk report` and a new
  `GET /report` + `GET /agents/{name}/metrics`.
- **Synchronous KB ingest.** ADR 032 D3 adds async ingest (above).

ADR 033 (API hardening) and ADR 037 (workflow API parity) layer on top — also
Accepted-as-decisions, ADR-stage on `main`.

---

## 7. Agent-pattern north-star (ADR 038) + Agentic-Mesh mapping

ADR 038 records the *direction* the template/authoring/compiler ADRs (028/029/030)
build toward — explicitly **backlog, not a scheduled milestone**. Patterns are
**compositions of governed primitives** over the existing `WorkflowSpec` /
`WorkflowRunner`, never new engines: a canonical node taxonomy
(INPUT·RETRIEVE·AGENT·TOOL·VALIDATE·JUDGE·GATE·HUMAN·OUTPUT·SUPERVISOR), a
cross-cutting governance contract on every node/edge (typed state, retry,
budget caps, max-depth/max-iteration guards, durable checkpoint, replay,
policy/scope, trace span), a shipped template library, and two flagship
differentiators: **inline JUDGE/GATE nodes** (governance enforced at *runtime*,
not just CI eval) and **bounded SUPERVISOR delegation** (allowlist + max-depth +
budget — the bounds are the point). The **governable filter** (D5) is the gate:
observable + evaluable + bounded + deployable, or it's declined.

**Agentic-Mesh building-block scorecard:**

| Block | MDK posture |
|---|---|
| Agent / Registry / Lifecycle | **Strong** — `AgentBundle` contract; durable versioned registry (ADR 014); publish/canary/promote/rollback/revert lifecycle. |
| Observability | **Strong** — per-step spans + offline tree (ADR 024); Langfuse/OTel/Azure + dashboards-as-code (ADR 031). |
| Governance | **Strong** — scopes (ADR 013), HITL (ADR 017), eval gates + canary/drift (ADR 016), budgets/quotas (ADR 036), audit. |
| Orchestration / Runtime | **Strong** — `WorkflowSpec`/runner, durable + HITL, KEDA worker, native engine over LiteLLM. |
| Trust | **Partial** — auth/scopes + HMAC triggers + (proposed) signed webhooks/SSRF guard (ADR 035); no formal trust-fabric. |
| Connectivity | **Partial** — skills (python/http/mcp), MCP authoring server, inbound triggers; no cross-agent message bus beyond the queue. |
| Roles | **Partial** — adopted as *naming only* (Planner/Orchestrator/Executor/Observer/Judge/Enforcer) mapping onto the node taxonomy + judge/policy layer. |
| Marketplace / cross-org Federation / autonomous planning | **Declined by design** — the "Organizational" quadrant (ecosystem/federation/legal-entity/supply-chain) and uncontrolled autonomy don't fit a single-enterprise, governable framework (ADR 038 scope-out). |

---

## 8. PENDING DECISIONS (for Deva)

Three sign-offs are the gating items; each is framed *decision · trade-off ·
recommendation* to resolve in the room.

**(a) ADR 030 — adopt the `langgraph` runtime dependency (opt-in extra).**
- *Decision:* ship `LangGraphBackend` behind the runner seam as the opt-in extra
  `mdk[langgraph]` (native runner stays default + portable floor), unlocking
  cyclic/ReAct, parallel fan-out/fan-in, and supervisor graphs.
- *Trade-off:* a new (optional) dependency = version churn + a second backend to
  keep behaviorally consistent + checkpointer correctness; mitigated by a thin
  backend, a shared conformance suite, and a `StorageProvider`-backed checkpointer
  (no LangGraph persistence lock-in). Per ADR 001 / ADR 017 D4 a dependency needs
  Deva sign-off.
- *Recommendation:* approve the **no-dep** compiler-growth + export work now (PR1
  ships value via `mdk export` with zero dependency); gate the **runtime
  dependency** (the `mdk[langgraph]` extra) on this sign-off. Low risk because
  it's isolated behind the seam and opt-in.

**(b) ADR 034 — D1 PgBouncer + D2 read replicas (infra/bicep).**
- *Decision:* provision server-side pooling (PgBouncer or Azure built-in,
  transaction mode) and an optional read replica, both shaped in bicep/env.
- *Trade-off:* removes the autoscale connection-exhaustion cliff and gives read
  fan-out, but adds infra to provision/operate; PgBouncer transaction-mode quirks
  (prepared statements / session state) and replica lag (stale reads → route only
  lag-tolerant queries) are documented risks.
- *Recommendation:* approve — the non-infra D3 pool metrics + D1 doctor check
  ship first as the early-warning system; bring PgBouncer + replica in as the
  bicep/env change once a capacity target is set. Portable (PgBouncer is generic;
  Azure built-in pooling is the Azure option).

**(c) ADR 036 — D2 quota policy (commercial).**
- *Decision:* enforce per-tenant ceilings (monthly cost/token/run) at admission —
  soft warn at 80%, hard block at 100% with `402`/`429`.
- *Trade-off:* required to commercialize a multi-tenant platform (hard abuse
  ceilings beyond burst rate-limiting), but cost is **estimated** from
  `pricing.yaml` (not the provider's actual bill — document the gap), and
  near-ceiling concurrency needs atomic counters or an accepted slight overage.
  The *thresholds* are a commercial/pricing decision, not an engineering one.
- *Recommendation:* approve the engineering (D1 metering + `GET /usage` + D3
  export) which is decision-free and depends only on ADR 032 D2 landing; **the
  quota *policy/thresholds* (D2) need a Deva commercial decision** before
  enforcement ships.

---

## 9. ADR status reconciliation (noted for the meeting, not edited)

A few ADRs carry a status that lags reality — worth reconciling so the decision
log reads true at the review (this packet did **not** edit any ADR):

- **ADR 013 (scopes)** and **ADR 016 (continuous-improvement loop)** are marked
  **Proposed**, but their substance is shipped and depended on as such: the scope
  vocabulary + `require_scope` is live and audited in `docs/front-end-api.md`, and
  canary/harvest/drift code (`core/canary.py`, `core/harvest.py`, `core/drift.py`)
  + the `/api/v1` canary/harvest/eval-schedule endpoints exist. Candidates to flip
  to **Accepted (partially shipped)**, the way ADR 017 was reconciled.
- **ADR 014 (durable registry)** is marked **Proposed**, but the registry
  endpoints (versions/publish/revert, `If-Match`) are in the live `/api/v1`
  inventory — likely Accepted in practice.
- **ADRs 032 / 033 / 038 are not present on `origin/main`** as files (they live on
  unmerged branches); 032/033 read "Accepted" and 038 "Proposed". Their *code* is
  largely ADR-stage on `main` (`core/reporting.py`, the preview/usage/ingest
  endpoints, the ADR-034 pool metrics, and the API-hardening middleware are not on
  `main` yet). Treat §4's "Accepted (not on main)" rows accordingly.
- General pattern: several 023→031 ADRs are dated "proposed and approved the same
  day" — fine, but it means "Accepted" denotes *decision agreed*, not *shipped*.
  §3/§6 distinguish shipped vs ADR-stage explicitly.

---

## 10. Appendix — links

- Layer map / seams / compat contracts — [`docs/architecture-principles.md`](architecture-principles.md)
- Operating rules — [`CLAUDE.md`](../CLAUDE.md)
- Front-end `/api/v1` audit + route inventory — [`docs/front-end-api.md`](front-end-api.md)
- Front-end TS client + contract narrative — [`docs/angular-client.md`](angular-client.md);
  platform-box scorecard — [`docs/mova-io-mapping.md`](mova-io-mapping.md)
- Contract test — `tests/test_front_end_api_contract.py`
- Dashboards-as-code (ADR 031 D2) — [`dashboards/`](../dashboards/) (grafana / prometheus / azure)
- ADRs — [`docs/adr/`](adr/)
- Dependency-license policy — [`docs/license-posture.md`](license-posture.md)
- Changelog — [`CHANGELOG.md`](../CHANGELOG.md)
- Platform design (human-owned) — `docs/v1.0-azure-design.md`,
  `docs/azure-movate-architecture.md`, `docs/v1.0-overview.md`
