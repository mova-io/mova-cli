# ADR 053 — Hosted playground: an Azure-hosted, Entra-gated shared testing portal

**Status:** Proposed
**Date:** 2026-05-29
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in any of them):**
the **Azure deploy design** (`docs/v1.0-azure-design.md`,
`docs/azure-movate-architecture.md`) — the runtime already deploys to **Azure
Container Apps** via Bicep (`infra/azure/modules/containerapp-{api,worker,scheduler,teams-bot,otel-collector}.bicep`,
`containerapp-env.bicep`, `keyvault.bicep`, `postgres.bicep`); this ADR adds
**one** new app into that same env;
ADR 013 (end-to-end identity — `mdk login` SSO, OIDC/Entra, flat least-privilege
scopes; `src/movate/runtime/oidc.py`); the hosted portal's auth sits at the
**platform** layer (Easy Auth) and the runtime key it carries is a scoped key
under that scope model;
ADR 045 (API ergonomics — **D10 stateful sessions** the playground multi-turn
history rides on, and **D14 feedback** `POST /api/v1/runs/{id}/feedback`, which
the playground already calls — see *Defining architectural fact* below);
ADR 016 (continuous-improvement loop: harvest → eval → canary — Phase 2 wires
👎 into this);
ADR 018 (per-tenant BYOK / Key Vault — the runtime bearer the portal carries is
a **scoped** Key Vault secret-ref, never a fleet-admin key);
ADR 036 (per-tenant usage metering + quotas — the abuse/cost guardrails for a
shareable URL read off this seam).

**Defining architectural fact.** The Chainlit playground **already exists** and
**already does the hard parts**. It lives in `src/movate/playground/` (app.py,
client.py, targets.py, capabilities.py, conversation.py, sse.py, state.py,
uploads.py) + `src/movate/cli/playground.py`, and today runs **locally**:
`mdk playground serve` launches Chainlit on **:8765** and talks to a deployed
runtime over HTTP. It has: a **multi-runtime target switcher** (one UI → any
configured runtime/agent, `targets.py`), **multi-turn history + uploads**,
**voice-mode** (in flight), and **👍/👎 feedback** — `_feedback_actions()` +
`@cl.action_callback("feedback")` in `app.py`, which POSTs via
`client.post_feedback()` to the runtime's **`POST /api/v1/runs/{run_id}/feedback`**
(with `GET /api/v1/runs/{run_id}/feedback` to re-open prior ratings;
`runtime/app.py`, tenant-scoped, persisted via `StorageProvider.save_feedback`).
**Upvote/downvote already works.** This ADR is about **hosting** that tool as a
shared, governed URL — **not building feedback, not building the playground.**

---

## Context

`mdk playground serve` is a **local developer tool**: an engineer runs it on
their laptop, points it at a deployed runtime, and exercises agents
interactively. That is the right shape for one developer. It does **not** answer
the recurring product need:

> *"Send me a link and I'll try your agents."*

— from the Movate team (PMs, SAs, leadership wanting to dogfood) and from
**invited external testers** (a customer's stakeholders evaluating a delivery).
Today the only ways to satisfy that are (a) everyone installs the CLI + the
`[playground]` extra and runs it locally against a shared runtime, or (b) one
person screen-shares. Both are friction; neither produces **attributable,
persistent feedback** from the people whose opinion matters.

The gap is narrow and well-defined. We do **not** need to build a portal — we
need to **host the one we already have**, behind real auth, with a persistent
data layer, deployed the same way the runtime is. Three forces shape the
decisions:

1. **It must be shareable but never wide-open.** A public URL that runs agents
   spends **real LLM tokens** (BYOK — the customer's provider keys, ADR 018) on
   every message. An unauthenticated shareable URL is a **cost-and-abuse
   liability**, not a feature. "Shareable" must mean "shareable *to identified
   people*," which is an **auth** problem, and one Azure already solves at the
   platform layer.

2. **It must reuse the runtime's deploy path, not invent a new one.** The
   runtime is already an ACA app in a known env with Key Vault, Postgres, and a
   container registry. A hosted playground that lives **anywhere else** doubles
   the operational surface. It belongs **in the same env**, deployed by the
   same `mdk deploy` machinery, reading the same Key Vault.

3. **Feedback only matters if it persists and attributes.** Local
   `mdk playground serve` uses an ephemeral data layer — close the tab, lose the
   thread. A *shared* instance needs **one durable data layer** so every
   tester's history and every 👍/👎 survives, and so feedback rows can carry
   **who** gave them. The runtime's deployed **Postgres** is already there.

This ADR is **additive**. CLAUDE.md rule 5 surfaces are touched in exactly one
place — a **new** `--mode playground` value on `mdk deploy` (flagged in D5) — and
the **local `mdk playground serve` dev mode is unchanged** (R3).

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Shareable but governed** — one URL anyone *invited* can use; never a wide-open public endpoint that burns BYOK tokens (D4, D7) | HIGH |
| **Reuse the runtime's deploy substrate** — same ACA env, Key Vault, Postgres, registry; one new app, not a parallel stack (D1, D3, D5) | HIGH |
| **Zero app-auth code** — identity solved at the platform layer (Easy Auth + Entra), so no auth surface to write, review, or own in-app (D4) | HIGH |
| **Persistent, attributable feedback** — one durable data layer; history + 👍/👎 survive; rows can carry the tester's identity (D3, D6) | HIGH |
| **Reuse what already works** — the playground (switcher, history, uploads, voice, feedback) ships as-is; we host it, we don't rebuild it | HIGH |
| **Boundary discipline** — `cli` (control plane) ⊥ `runtime` (execution plane); the portal is its **own** app, not embedded in the runtime (Alternatives) | HIGH |
| **Cost/abuse guardrails on a shareable surface** — scale-to-zero, a scoped key, the runtime's per-key rate limiting, optional per-tenant quotas (D7) | MED |
| **Operator-supervised cloud/IAM steps** — the `az`/Entra B2B/Easy-Auth wiring is operator-run, not silent automation (Boundaries) | MED |

---

## Architecture

```
                 invited human (Movate team / Entra B2B guest tester)
                              │  https://<playground>.<region>.azurecontainerapps.io
                              ▼
                  ┌────────────────────────────────────────┐
                  │  ACA built-in auth (Easy Auth) + Entra  │  ◄── D4: org SSO + B2B guests,
                  │  (platform layer, zero app code)        │       zero app-auth code
                  └────────────────────────────────────────┘
                              │  (authenticated; X-MS-CLIENT-PRINCIPAL headers)
                              ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  containerapp-playground.bicep   (NEW — public ingress)           │  D1
   │  image: package + [playground] extra (chainlit)                   │  D2
   │  cmd: mdk playground serve --host 0.0.0.0 --port <ingress>        │
   │  env: MDK_PLAYGROUND_RUNTIME_URL → runtime internal FQDN          │  D3
   │       runtime bearer ← Key Vault secret-ref (SCOPED key)          │  D3 / ADR 018
   │       Chainlit data layer → deployed Postgres (shared instance)   │  D3
   └──────────────────────────────────────────────────────────────────┘
        │  multi-runtime switcher (D8): one URL → any deployed runtime/agent
        ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  containerapp-api.bicep  (EXISTING runtime — unchanged)           │
   │  POST /api/v1/runs/{run_id}/feedback   ◄── 👍/👎 ALREADY wired    │  D6
   │  GET  /api/v1/runs/{run_id}/feedback   (re-open prior ratings)    │
   │  ... shares the SAME env, Key Vault, Postgres                     │
   └──────────────────────────────────────────────────────────────────┘

   Deploy:  mdk deploy --mode playground   (NEW value, additive to auto|runtime|agents)   D5
   Phase 2: 👎 → eval-harvest (ADR 016) · aggregate-feedback dashboard · per-tester identity on rows   D6
```

---

## Decisions

### D1 — The playground is a NEW Container App in the runtime's env, with public ingress

Add `infra/azure/modules/containerapp-playground.bicep` — a new ACA app in the
**same Container App environment** as the runtime (the env from
`containerapp-env.bicep`). It models on the **existing**
`containerapp-api.bicep` (which already runs `movate serve` behind
`ingress: { external: true, targetPort: 8000 }` and emits its FQDN as a Bicep
output) but:

- runs **`mdk playground serve --host 0.0.0.0 --port <ingress targetPort>`** as
  its container command (Chainlit, not the FastAPI runtime),
- has **public/external ingress** so the FQDN is genuinely shareable — `external:
  true`, the **shareable URL** the ADR is named for. (Public **ingress** ≠ public
  **access**: access is gated by Easy Auth in front of it, D4.)

There is **no playground ACA module today** — this is the one net-new piece of
infra. It is one Bicep file following the established module pattern (params for
`keyVaultUri`, `minReplicas`, registry/image, env wiring), and it sits beside
the runtime, not in place of it.

### D2 — Image: reuse the package image with the `[playground]` extra

The portal runs the **same package** as the runtime, with the **`[playground]`
extra** installed (which pulls `chainlit>=1.3` and the async-ORM deps — see
`pyproject.toml`). Two viable shapes; the ADR commits to the *contract* (one
codebase, one CLI, the playground entrypoint) and leaves the image *mechanism* a
small implementation choice:

- a **playground build target** in the existing single `Dockerfile`
  (`pip install .[playground]`), **or**
- a **thin second image** layered on the runtime image that adds the
  `[playground]` extra.

Either way the app command is `mdk playground serve …` pointed at the runtime —
not a new program. **Constraint surfaced (verified in `pyproject.toml`):** the
`[playground]` and `[airflow]` extras are declared **conflicting** (Chainlit's
data layer needs `sqlalchemy>=2.0`; Airflow pins 1.4), so the playground image
**must not** also carry the `[airflow]` extra. This is naturally satisfied by a
playground-only image and is noted so the Dockerfile target doesn't co-install
them.

### D3 — Config: runtime URL via env, bearer via Key Vault secret-ref, Postgres as the shared data layer

Three configuration facts make the hosted instance work as a shared portal:

- **Runtime URL** — `MDK_PLAYGROUND_RUNTIME_URL` points at the runtime's
  **internal** ACA ingress FQDN (the apps share an env, so the playground reaches
  the runtime on the env-internal network; cf. how `containerapp-api.bicep`
  already passes the otel-collector its env-internal FQDN). The playground's
  existing `--runtime-url` / `targets.py` resolution consumes it.
- **Runtime bearer** — the portal authenticates to the runtime with a bearer
  token sourced as a **Key Vault secret-ref** (the same `keyVaultUrl →
  secretRef` pattern `containerapp-api.bicep` already uses for
  `bootstrap-api-key`, provider keys, etc.). This key is **SCOPED** — a
  least-privilege runtime key per ADR 013's flat scope model (run agents + write
  feedback), **never a fleet-admin or bootstrap key**, and per ADR 018 it lives
  in Key Vault, never in the image or an env literal.
- **Data layer** — point the **Chainlit data layer** at the deployed **Postgres**
  (`postgres.bicep`, already in the env) so **history + feedback persist across
  the shared instance** and across replicas/restarts. Local
  `mdk playground serve` keeps its ephemeral/dev data layer (R3); only the hosted
  instance binds Postgres.

### D4 — Auth (key decision): ACA built-in auth (Easy Auth) + Entra ID — zero app code

The shareable URL is gated by **Azure Container Apps built-in authentication
("Easy Auth") fronted by Entra ID**, configured on the playground app's ingress.
This is the **load-bearing decision** of the ADR:

- **Org SSO at the platform layer, zero app code.** Easy Auth terminates the
  auth handshake *in front of* the container — the Chainlit app writes **no auth
  code**, has **no login UI to build or review**, and there is **no in-app auth
  surface to own**. Authenticated identity arrives as platform-injected
  principal headers the app can read for D6.
- **External testers via Entra B2B guests.** Invited non-Movate testers are
  added as **Entra ID B2B guests** in the tenant; they authenticate with their
  own identity. No bespoke account system.
- **Per-tester identity flows onto feedback rows.** Because every request is
  authenticated, the tester's identity is available to attribute on feedback
  (the runtime's feedback row already requires a `user_id`; see `runtime/app.py`
  — *"feedback requires a user_id — either authenticate or pass …"*). Full
  per-tester attribution on rows is **Phase 2** (D6), but Easy Auth is what makes
  it possible at all.
- **NEVER a wide-open public URL.** Restated as a hard rule because the failure
  mode is expensive: an unauthenticated portal spends BYOK LLM tokens on every
  anonymous message (driver 1). Public **ingress** with Easy Auth in front is the
  design; public **access** is explicitly rejected (Alternatives).

**Alternative considered (and kept as a fallback):** Chainlit's app-level auth
(`@cl.password_auth_callback` or its OAuth callback). This **works** and is the
right answer in an environment without Easy Auth (e.g. a non-Azure host). It is
**rejected as the default** because it puts an auth surface *into the app* —
code we'd write, review, and maintain — when the platform gives it to us for
free with stronger org SSO + B2B semantics. Documented as the fallback so a
non-ACA deployment isn't blocked.

### D5 — Deploy path: add `mdk deploy --mode playground` (additive)

Extend `mdk deploy` with a **new** `--mode playground` value that builds the
playground image (D2) and rolls the playground ACA app (D1) — symmetric with how
runtime-mode builds + rolls `containerapp-api`. Alternatively/additionally it
chains from `mdk infra apply` (which provisions the Bicep). Today
`src/movate/cli/deploy.py` validates `mode in ("auto", "runtime", "agents")` and
errors otherwise; this ADR **adds `playground` to that set** — additive, no
existing mode changes behavior.

> **CLAUDE.md rule 5 — flagged.** `--mode` is a **public CLI flag**; adding the
> `playground` value is a change to that surface. It is **purely additive** (a
> new accepted value; `auto`/`runtime`/`agents` are untouched, including
> `auto`'s detection logic), so no existing invocation changes meaning. Recorded
> here as the one compat-surface touch in this ADR.

### D6 — Feedback: already built; Phase 2 closes the quality loop

**Already shipped (verified):** the Chainlit 👍/👎 actions
(`_feedback_actions()` + `@cl.action_callback("feedback")` in
`playground/app.py`) POST via `client.post_feedback()` to the runtime's
**`POST /api/v1/runs/{run_id}/feedback`**, persisted tenant-scoped. The hosted
instance **inherits this unchanged** — feedback works on the shared URL on day
one of Phase 1. Nothing to build here.

**Phase 2 additions** (each its own PR, none required for the shareable-URL
milestone):

- **Aggregate-feedback dashboard panel** — a read view (read-scope per ADR 045
  D14) summarizing 👍/👎 across the shared instance, so "what are testers saying"
  is answerable at a glance.
- **👎 → eval-harvest (ADR 016).** Wire downvotes into the harvest → eval flow:
  a 👎 becomes a candidate **eval case** / **drift signal**, so tester
  dissatisfaction feeds the continuous-improvement loop instead of dying in a
  table. This is the high-value edge — the shared portal becomes a **quality
  signal generator**, not just a demo surface.
- **Per-tester identity on feedback rows** — carry the Easy-Auth principal (D4)
  onto the `user_id`/attribution on each feedback row, so "who said this" is
  answerable. The runtime row already requires a `user_id`; this populates it
  from the authenticated principal rather than a service identity.

### D7 — Cost / abuse guardrails on a shareable surface

A shareable URL that spends BYOK tokens needs defense in depth, all from
**existing** seams:

- **Scale-to-zero or low min-replica.** The Bicep app (D1) configures
  `minReplicas: 0` (or a low floor) so an idle portal costs nothing — it spins up
  on the first authenticated request. (`containerapp-api.bicep` already
  parameterizes `minReplicas`.)
- **Scoped runtime key (D3 / ADR 018).** The bearer the portal carries is
  least-privilege; a leaked portal key cannot administer the fleet.
- **The runtime's existing per-key rate limiting.** Requests from the portal hit
  the runtime's existing `X-RateLimit-*` per-key limiter (ADR 045 / the auth
  dependency), capping spend velocity automatically.
- **Optional per-tenant quotas (ADR 036).** Where a hard spend ceiling is wanted,
  the runtime's per-tenant quota substrate caps total portal spend over a window.
- **Auth itself (D4)** is the first guardrail — only invited identities reach the
  app at all.

### D8 — Reuse: one portal, all surfaces

The hosted instance is the **same playground binary**, so it inherits, for free:

- the **multi-runtime target switcher** (`targets.py`) — **one URL → any deployed
  runtime/agent**, so a single portal covers every delivery instead of one URL
  per runtime,
- **multi-turn history + uploads** (now persistent via D3's Postgres),
- **voice-mode** (in flight) when present in the build.

There is **one portal** and it exposes **all** the playground's surfaces; we add
hosting + auth + persistence around it, nothing inside it.

---

## Resolved decisions (locked in upfront)

- **R1 — This ADR ships no new playground *feature*.** Feedback, the switcher,
  history, uploads, voice are **existing**. The deliverable is hosting,
  auth, persistence, and a deploy mode around the existing app.
- **R2 — Public ingress, never public access.** The portal's ingress is external
  (shareable FQDN) but **always** behind Easy Auth (D4). An unauthenticated
  reachable portal is out of the question (driver 1).
- **R3 — Local `mdk playground serve` is unchanged.** The dev workflow (laptop →
  :8765 → runtime, ephemeral data layer) is untouched. The hosted instance is an
  *additional* deployment shape, opt-in via `--mode playground`.
- **R4 — The runtime is untouched.** The runtime app, its feedback endpoints, its
  Bicep, and its scopes are **not modified** by Phase 1. The portal is a new
  consumer of the existing runtime API.
- **R5 — The scoped runtime key is never fleet-admin.** The portal's bearer is a
  least-privilege key (ADR 013 scopes) in Key Vault (ADR 018). This is structural,
  not advisory.

---

## Phased plan

**Phase 1 — the SSO-gated shared URL (the milestone).**
`containerapp-playground.bicep` (D1) + the `[playground]`-extra image (D2) +
`mdk deploy --mode playground` (D5) + **Entra Easy Auth** on the ingress (D4) +
the **Postgres** Chainlit data layer (D3). Outcome: a **shareable, SSO-gated
URL** where invited Movate staff and Entra B2B guests can exercise any deployed
runtime/agent (D8), with **history persisted and 👍/👎 feedback working**
(inherited, D6). This is the whole product promise of the ADR.

**Phase 2 — close the quality loop (D6).** Aggregate-feedback **dashboard panel**
+ **👎 → eval-harvest (ADR 016)** so downvotes become eval cases / drift signals
+ **per-tester identity** on feedback rows from the Easy-Auth principal. Each is
its own PR; none gate Phase 1.

---

## Consequences

**Positive.**
- **"Send me a link and I'll try it" finally has an answer** — a governed,
  shareable URL, not a CLI install or a screen-share.
- **Near-zero net-new surface.** We host an existing tool: one Bicep module, one
  image target, one additive deploy mode, and platform-layer auth. No new auth
  code, no new portal codebase, no new feedback system.
- **Feedback becomes a quality asset.** Persistent (D3) and, in Phase 2,
  attributed (D6) and harvested into evals (ADR 016) — tester opinion feeds the
  improvement loop instead of evaporating.
- **Boundaries stay clean.** The portal is its own app: `cli` ⊥ `runtime` is
  preserved, the two scale and authenticate independently, and the runtime is
  untouched (R4).

**Risks / watch items.**
- **Cost on a shared key (D3/D7).** Many testers share one scoped runtime key, so
  one budget covers all of them. Mitigation: scale-to-zero, the runtime's per-key
  rate limit, and optional ADR-036 quotas — but operators must **set a quota
  before sharing widely**, called out in the runbook.
- **B2B guest sprawl (D4).** Adding external testers as Entra B2B guests is an
  IAM action with a lifecycle; stale guests linger. Mitigation: this is an
  operator-owned IAM step (Boundaries) with a documented offboarding note.
- **Extras conflict (D2).** The playground image must not co-install `[airflow]`
  (declared conflicting in `pyproject.toml`). Mitigation: a playground-only image
  target; noted in D2.
- **Data-layer growth (D3).** A shared, persistent history grows in Postgres.
  Mitigation: it shares the runtime's Postgres lifecycle; a retention policy is a
  Phase-2 operational follow-up, not a Phase-1 blocker.
- **Easy Auth availability (D4).** Easy Auth is Azure-specific. Mitigation: the
  Chainlit app-level auth fallback (D4 alternative) covers non-ACA hosts.

---

## Alternatives considered

- **Azure App Service / Static Web Apps instead of ACA.** *Rejected.* The runtime
  is already ACA, with its env, Key Vault, Postgres, and registry. Hosting the
  portal on a *different* Azure service doubles the operational model and the
  networking story for no benefit; ACA also gives us **Easy Auth** (D4) and
  env-internal networking to the runtime (D3) directly. Matching the runtime's
  substrate is the whole point of D1.
- **A wide-open public URL (no auth).** *Rejected, emphatically.* It spends BYOK
  LLM tokens on every anonymous request (driver 1, R2) and invites abuse. Public
  *ingress* behind Easy Auth is the design; public *access* is never.
- **Embed the playground INTO the runtime app** (serve Chainlit from the runtime
  container). *Rejected.* It collapses `cli`/control-plane ⊥ `runtime`/execution-
  plane (CLAUDE.md rule 6), forces one scaling profile and one auth posture on two
  very different workloads (a chatty UI vs. an agent-execution API), and couples
  their release cadence. A separate app keeps **independent scaling and
  independent auth** — exactly what D1 + D4 want.
- **Chainlit app-level auth as the default** (`@cl.password_auth_callback` /
  OAuth). *Rejected as default, kept as fallback* (D4). Viable, but it puts an
  auth surface in the app when the platform offers stronger SSO + B2B for free.
- **Build a bespoke testing portal.** *Rejected.* The playground already does
  multi-runtime, history, uploads, voice, and feedback. Rebuilding any of it
  would be net-new surface for zero gain (R1).

---

## Boundaries (out of scope / operator-owned)

- **The cloud + IAM wiring is operator-run and supervised**, not silent
  automation: enabling Easy Auth on the app, registering the Entra app
  registration, inviting Entra B2B guests, and granting the Key Vault secret-ref
  access are **`az` / Azure-portal / IAM steps** performed (or explicitly
  approved) by an operator. `mdk deploy --mode playground` provisions and rolls
  the **app**; it does not silently mutate tenant-level identity or access
  policy.
- **Local `mdk playground serve` dev mode is unchanged** (R3) — laptop → :8765 →
  runtime, ephemeral data layer.
- **The runtime, its feedback endpoints, and its Bicep are not modified** (R4).
- **Phase-2 work** (aggregate dashboard, 👎→eval-harvest, per-tester identity) is
  scoped here but **authored in its own PRs** (D6); none gate the Phase-1
  shareable-URL milestone.
- **A non-Azure hosting story** (the Chainlit app-auth fallback path on another
  platform) is acknowledged (D4) but not specified here; it would be its own ADR
  if pursued.
