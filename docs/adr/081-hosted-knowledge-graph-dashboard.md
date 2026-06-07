# ADR 081 — Hosted knowledge-graph dashboard on Azure Container Apps

**Status:** Proposed
**Date:** 2026-06-06
**Deciders:** Engineering (Movate)
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 046 (knowledge-graph surface — the graph **query API** + the sigma.js
dashboard this ADR hosts; nothing in the API or the viewer assets changes),
ADR 053 (hosted playground — the **Easy-Auth-gated external Container App**
pattern this ADR reuses almost verbatim: UAI identity, KV secret-refs, Entra
app-registration runbook, `RedirectToLoginPage`),
ADR 010 / ADR 075 (GraphRAG extraction + the `StorageProvider`/Neo4j graph
backends whose data the dashboard reads — untouched),
ADR 018 (per-tenant BYOK / scoped keys — the viewer carries a least-privilege
**read-scoped** runtime bearer, not a fleet key),
ADR 078 (Temporal-on-ACA — the sibling "turn a built capability into a hosted
surface" module; same external-app shape).

---

## Context

The GraphRAG stack is **already shipped**: extraction (ADR 010), persistence
behind the `StorageProvider` Protocol on SQLite/Postgres + a Neo4j adapter
(ADR 075), a read-only **graph query API** in the runtime (`GET
/api/v1/projects/{id}/graph`, `/graph/nodes/*`, analytics, and an SSE growth
stream — ADR 046), and a vendored **sigma.js + graphology dashboard** with
entity search, a centrality/shortest-path/community analytics sidebar, a growth
timeline, a project switcher, and KB provenance drill-down.

The one gap: that dashboard only runs as **`mdk graph dashboard` on a laptop** —
a local proxy that reads the runtime URL + bearer from `~/.movate/config.yaml`
and keeps the bearer server-side. There is **no hosted viewer**. A customer or
internal stakeholder who wants to *browse* the knowledge graph must install the
CLI, configure a target, and run a local server. That's friction for a surface
that is otherwise demo- and stakeholder-ready.

The graph *API* is already hosted (it lives in the `api` Container App). So the
hosted-viewer problem reduces to: **run the existing dashboard headless in a
container, pointed at the in-env api app, behind SSO** — exactly the shape ADR
053 already solved for the playground.

## Decision

Ship a hosted knowledge-graph dashboard as an **optional, default-off Azure
Container App** that runs the *existing* `mdk graph dashboard` viewer headless,
proxying the in-env runtime's graph API, gated by Entra Easy Auth.

### D1 — Reuse the dashboard; add a headless run mode to the CLI
The container runs `mdk graph dashboard --host 0.0.0.0 --no-open`. The blocker:
`graph serve`/`dashboard` resolved the runtime URL + bearer **only** from
`~/.movate/config.yaml`, which does not exist in a container. So we add a
**headless mode** mirroring `mdk playground serve`'s `--runtime-url`:

- New options `--runtime-url` (`MDK_GRAPH_RUNTIME_URL`) and `--api-key`
  (`MDK_GRAPH_API_KEY`); `--project` also reads `MDK_GRAPH_PROJECT_ID`.
- When **both** URL + key are supplied, the viewer uses them directly and never
  reads the local config. Otherwise it falls back to the existing `--target`
  pipeline **byte-for-byte** (the laptop path is unchanged).
- The bearer still stays **server-side** (the load-bearing security property of
  the proxy is preserved — the browser never sees it).

This is the only code change; the viewer assets, proxy, and graph API are
untouched.

### D2 — One new Bicep module, mirroring the playground
`infra/azure/modules/containerapp-graph.bicep`: same Docker image as the runtime
(only the command differs), UserAssigned identity (pre-created so AcrPull +
KV-Secrets-User land on pass 1), KV secret-refs, external ingress. It drops the
playground's Postgres data layer — the viewer only proxies the API, it has no DB
of its own. Wired into `main.bicep` behind `enableGraphApp`, gated on
`enableGraphApp && enableApiWorker` (no runtime → nothing to show), with a
`graphUrl` output. Default-off + additive: with the flag false, **zero**
resources are emitted and the template is byte-for-byte unchanged.

### D3 — Entra Easy Auth, external ingress (not a wide-open URL)
The graph exposes entities/relations extracted from customer KBs — potentially
sensitive. So, exactly as ADR 053: ingress is **external** (shareable FQDN) but
access is gated by ACA Easy Auth fronted by **Entra ID**
(`unauthenticatedClientAction: RedirectToLoginPage`). The Entra app registration
is **operator-pre-created** (a runbook step, not silent automation); its client
id is a param and its secret is a KV-backed app secret. Public **ingress** ≠
public **access**.

### D4 — Least-privilege, read-scoped runtime bearer
The viewer carries a **read-scoped** runtime key (the graph API only needs
`read`), minted by the operator and stored in KV as `graph-runtime-key` — never
a fleet-admin/bootstrap key. This bounds the blast radius if the app is
compromised: it can read graph data for its tenant and nothing else.

## Consequences

**Positive**
- Turns an already-built feature into a customer/stakeholder-facing surface for
  near-zero marginal code — one CLI mode + one Bicep module, both copied from
  reviewed patterns.
- The headless CLI mode is independently useful (CI, SSH tunnels, any container).
- Zero blast radius when off; the runtime, graph API, and native paths are
  untouched.

**Negative / trade-offs**
- Two-pass deploy + an operator runbook (Entra app registration + two KV
  secrets), same ceremony as the playground. Documented in
  `docs/graph-app-deploy.md`.
- The viewer is a thin proxy, so it's only as available as the api app it
  targets (acceptable — it's a read-only browse surface).
- Easy Auth gates the *human* at the door; the viewer then uses ONE shared
  read-scoped key for all authenticated users (no per-user graph scoping beyond
  the key's tenant). Per-user attribution is a future follow-on, same as the
  playground's Phase-2 note.

## Alternatives considered
- **Extract the graph routes into a standalone FastAPI service.** More moving
  parts, a second deployable, and it duplicates auth/tenant logic the runtime
  already owns. Rejected — the API is already hosted; we only lacked a viewer.
- **Internal-only ingress + port-forward (like the Temporal Web UI default).**
  Fine for an ops tool, wrong for a stakeholder-facing graph browser. Easy Auth
  gives a shareable URL without exposing data. (Operators who want ops-only can
  still leave `enableGraphApp=false` and run `mdk graph dashboard` locally.)
- **Bake a key into the image / pass the bearer to the browser.** Breaks the
  server-side-bearer property. Rejected.

## Compatibility (CLAUDE.md rule 5)
Fully additive. New opt-in flag `enableGraphApp` (default false); new env vars
`MDK_GRAPH_RUNTIME_URL` / `MDK_GRAPH_API_KEY` / `MDK_GRAPH_PROJECT_ID`; new CLI
options `--runtime-url` / `--api-key` on `mdk graph serve`/`dashboard` (the
existing `--target` path is unchanged). No change to `agent.yaml`/`project.yaml`,
the `/api/v1` shapes, storage schema, or existing deploy behavior.
