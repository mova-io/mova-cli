# ADR 003 — Microsoft Teams as a self-serve front door for non-technical users

**Status:** Proposed
**Date:** 2026-05-13
**Deciders:** Engineering + Movate CEO + Sales leadership
**Context window:** v0.7 → v0.8 design horizon
**Supersedes:** N/A
**Related:** [ADR 002 — Skills and shared contexts](002-skills-and-contexts.md); v0.5 HTTP runtime (`mdk serve`); PRD Phase 6 (Azure deploy)

---

## Decision

We will ship a **Microsoft Teams app** that lets non-technical users
(starting with the sales team) submit one-shot agent runs and small
eval jobs from inside Teams. The app is a thin client over the
existing v0.5 HTTP runtime — **no new execution surface, no new
storage layer**. Teams becomes a second client of the same `/run`
and `/eval` endpoints the CLI already uses, just rendered through
Adaptive Cards instead of Rich tables.

The integration ships in three vertical slices, each independently
demoable:

| Slice | What sales can do | Backend dependency |
|---|---|---|
| **3.1 — Run an existing agent** | `@movate run faq-agent {"q": "..."}` → result rendered as a card | Deployed runtime + `mdk auth` keys |
| **3.2 — Eval a bundled agent.yaml** | Drag `agent.zip` into a channel + supply `dataset.jsonl` → scorecard reply | Slice 3.1 + ephemeral skill upload |
| **3.3 — Saved configs + scheduled runs** | "Run my warranty-classifier every Monday at 9am" persistent card | Storage + scheduler (already partially in place via `mdk jobs`) |

Slice 3.1 is the demo unlock the CEO asked for; 3.2 and 3.3 layer on
top without re-architecting.

This ADR exists because Teams is the **single biggest reach lever**
left for the v1.x audience inside Movate. Every sales engineer
already lives in Teams. Asking them to install a CLI, get an API
key from an admin, and write JSON requests is the precise friction
that's keeping the first sales-led demo from happening.

## Context

Today (post-v0.5) movate has two client surfaces:

* **CLI (`mdk`)** — for engineers. Requires Python, an API key in
  an env var, and a terminal.
* **HTTP runtime (`mdk serve`)** — for programmatic clients. Requires
  the caller to construct JSON, hold a bearer token, and poll for
  job completion.

Both surfaces assume technical fluency. The first sales demo planned
for Q3 2026 requires a Movate AE to:

1. Walk a prospect through a use case in a sales call.
2. Show "a Movate agent answering your question right now".
3. Optionally show "and here's how we'd evaluate it on your data".

Neither (1) nor (2) are doable in Teams today; the AE has to switch
to a separate terminal and the prospect loses the thread.

Two adjacent forces shape the response:

1. **The CEO ask is concrete.** The brief is *"sales does the first
   demo themselves"*. That rules out an internal-tools approach
   that just hides the CLI behind a web form — sales already has
   Teams open, and a separate web app is more friction, not less.
2. **The Azure deploy is in flight.** The runtime needs a public
   HTTPS endpoint for the Teams Bot Service to call. Phase 6 is the
   pathway for that. Without a deployed runtime, the Teams app has
   nothing to call.

We considered three alternatives before landing on Teams:

* **Slack app.** Lower friction to build (open API), but Movate is
  Teams-first internally and sales prospects are predominantly
  Teams shops. Rejected on audience.
* **Web SPA (movate.movate.com).** Full control, but yet another
  surface area to deploy, secure, and onboard. Rejected: every
  prospect's first reaction would be "can you show me in Teams?".
* **Outlook add-in.** Discoverable, but the interaction model
  (compose-and-send) is wrong for an iterative tool-use loop.
  Rejected on UX fit.

Teams wins on three vectors: zero install for sales, native to
Movate's daily workflow, and Adaptive Cards give us a rendering
surface that's competitive with the CLI's Rich output.

## Decision drivers

* **Sales-led first demo by Q3.** The whole point is removing
  Engineering as a bottleneck.
* **Don't fork the runtime.** Teams must be a *client* of the same
  `/run` and `/eval` endpoints the CLI uses, not a parallel path.
  This is the cardinal architectural rule.
* **Bring-your-own-data must work.** A prospect dragging in their
  own `dataset.jsonl` is the killer demo moment. The Teams app
  must accept file uploads and route them through to `/eval`.
* **No prospect-side install.** Movate-internal users see the
  Teams app from the org's app catalog. Prospects see it shared
  ad-hoc into a channel they're invited to.
* **Audit + cost containment.** Every Teams-originated run must
  carry a tenant + user attribution into `RunRecord` so finance
  can see "$X of model spend originated from sales demos this
  month".
* **Reversible.** If Teams adoption stalls (it won't, but) we can
  yank the manifest and keep all the underlying runtime work — the
  HTTP API gains nothing Teams-specific.

## Architecture

```
   Sales user / prospect
            │
            ▼
   ┌─────────────────────┐
   │  Teams desktop /    │     Adaptive Cards in / out;
   │  Teams mobile       │     file attachments via
   └────────┬────────────┘     Microsoft Graph drive
            │ (Bot Framework Activity)
            ▼
   ┌─────────────────────┐
   │  Azure Bot Service  │     Routes Teams activities to
   │  (channel: msteams) │     our webhook; manages
   └────────┬────────────┘     signing, retries, replies
            │ HTTPS (BotFramework JWT)
            ▼
   ┌─────────────────────────────────────────┐
   │  mdk teams-bot  ── new container        │
   │  ─────────────────                      │
   │  • FastAPI app, /api/messages endpoint  │
   │  • Adaptive Card builders for           │
   │      run-result, eval-scorecard,        │
   │      error, confirmation                │
   │  • File attachment handler              │
   │      (downloads from MS Graph,          │
   │       passes to MovateClient.submit)    │
   │  • Per-user state in Bot Framework      │
   │      ConversationState (which agent,    │
   │      which API key is bound)            │
   └────────┬────────────────────────────────┘
            │ HTTPS + bearer token
            ▼
   ┌─────────────────────┐
   │  mdk serve  (ACA)   │     UNCHANGED — same /run, /eval,
   │  + worker pool      │     /jobs/{id} the CLI uses.
   │  + Postgres         │     Every Teams request carries
   └─────────────────────┘     a mvt_<env>_<tenant>_<keyid>_<secret>
                               key issued to the Teams app on
                               behalf of the user.
```

The new code lives entirely in **`src/movate/teams_bot/`** plus the
**`manifest/`** + **`appPackage/`** directory that gets uploaded to
Teams Admin Center. Nothing in `src/movate/core/` or
`src/movate/cli/` changes.

### Why Bot Framework, not a Power Platform connector

Power Automate connectors would be the lowest-code path but they:

* Can't do streaming or interactive card updates (the eval scorecard
  needs to update as cases complete).
* Have hard limits on attachment size (4 MB) below what a typical
  `dataset.jsonl` can hit.
* Don't carry a stable user identity into our system — making the
  audit + budget story messy.

Bot Framework is the official Microsoft path for "real Teams apps".
The investment (Azure Bot Service registration, manifest, hosting)
is the table stakes we'd hit either way once the demos get serious.

## What goes where

| Concern | Location | Notes |
|---|---|---|
| Teams bot HTTP server | `src/movate/teams_bot/app.py` | FastAPI; `/api/messages` endpoint; uses `botbuilder-core` SDK |
| Adaptive Card builders | `src/movate/teams_bot/cards/` | One module per card type; pure functions from `RunResponse`/`EvalSummary` → card dict |
| File attachment handler | `src/movate/teams_bot/attachments.py` | Downloads via `msgraph-core`, validates via `load_agent` / `load_dataset` |
| User → API key mapping | `src/movate/teams_bot/identity.py` + Postgres table `teams_users` | One Movate API key per Teams AAD object id; admin onboarding cmd `/movate connect` |
| Bot manifest + icons | `appPackage/manifest.json`, `appPackage/icons/` | Uploaded to Teams Admin Center; references the ACA webhook URL |
| Container image | `Dockerfile.teams-bot` | New image; shares the base layer with the serve image |
| ACA deployment | `infra/azure/containerapp-teams-bot.bicep` | Sibling to `containerapp-worker.bicep`; needs ingress |
| Bot Service resource | `infra/azure/main.bicep` | New `Microsoft.BotService/botServices` resource + Teams channel |

The teams_bot package is **importable but optional** — it lives behind
an extra (`movate-cli[teams]`) so dev installs without
`botbuilder-core` keep working.

## How a one-shot run actually flows

1. Sales user types `@movate run faq-agent {"question": "what's the warranty?"}` in a channel.
2. Teams sends a Bot Framework `Activity` to `/api/messages`.
3. `app.py` parses the mention, extracts the user's AAD object id,
   looks up their Movate API key in `teams_users`.
4. Without a bound key → reply with an Adaptive Card telling them
   to DM `/movate connect` to bind one. (One-time setup.)
5. With a bound key, call `MovateClient.submit_and_wait(agent="faq-agent", input={...})`.
6. On success, render a `run_result` card with: response JSON in a
   fenced block, cost, latency, trace link to Langfuse. Reply to
   the original message.
7. On failure, render an `error` card with the failure category +
   a one-line hint. (No stack traces in Teams — those go to
   Langfuse.)

The eval flow (slice 3.2) is the same up to step 5, then submits
each dataset case as a separate job and renders an updating
scorecard via Bot Framework's "update activity" capability.

## Auth model

Two distinct concerns:

**Bot ↔ Movate runtime.** The bot holds a **bot-fleet API key** —
one Movate key for the bot container, with broad permissions
across every tenant the bot can act on behalf of. This key sits in
Key Vault, mounted into the ACA container.

**Teams user ↔ Movate tenant.** Each Teams user is mapped to a
Movate **tenant + per-user API key** via `/movate connect` in a DM:

```
User in DM: /movate connect
Bot:        Reply with your Movate API key (from `mdk auth create-key`).
User:       mvt_dev_acme_4f8a_...
Bot:        ✓ bound to tenant `acme`. You can now @movate the bot in
            channels where this app is installed.
```

The bot calls the runtime with the **user's** key, not the bot's
fleet key. The fleet key is only used for admin operations (listing
available agents, healthchecks). This keeps the audit trail intact:
every `RunRecord` records the originating user via the user's API
key's `created_by`.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Sales submits a $50 demo run by accident | Enforce per-user daily budget in Movate policy (already supported). Bot card includes a cost preview before confirming an eval over >5 cases. |
| Prospect uploads an `agent.yaml` that references unsafe skills | Bot validates via `mdk validate` (same code path) before submission; rejects on policy violation with a readable card. Skill `side_effects` policy gate from PR #58 carries through. |
| File upload > 4MB exceeds Teams attachment limit | Bot prompts user to provide a URL (e.g. SharePoint link to a larger dataset). Fallback only — most demo datasets are <100KB. |
| Teams app deprecated / Microsoft pricing change | Bot manifest is decoupled from runtime. Yanking the Teams channel doesn't affect CLI users. |
| Onboarding friction: DM-to-bind-key is unusual | Provide a Movate admin command (`mdk teams create-bind-token <user-email>`) that pre-binds a key and DMs the user a one-click "I'm bound" button. |
| Bot impersonation if API key leaks in Teams DM | Keys never echoed back in any card. Bot stores key encrypted at rest (KMS). Rotation flow: `/movate rotate-key` in DM. |

## Phasing

**Phase 6.5 / v0.7 — Slice 3.1 (Run, no upload).**

* New package skeleton + Bot Framework wiring.
* Adaptive Cards for `run_result`, `error`, `confirmation`.
* `/movate connect` + `teams_users` table.
* ACA deployment of the bot container + Bot Service registration.
* Documented onboarding for one sales engineer (alpha tester).
* Exit: an internal user can `@movate run faq-agent {"q":"..."}` in
  a Teams channel and see a card reply within 5 seconds.

**Phase 6.6 / v0.8 — Slice 3.2 (Eval with bring-your-own data).**

* File attachment handler + temp directory lifecycle.
* `eval_scorecard` card with per-case updates.
* Surface `dimensional_means` from PR #59 — Teams gets the 4-dim
  view "for free" because it's already in the JSON response.
* Exit: a prospect drags `agent.zip` + `dataset.jsonl` into a Teams
  channel and watches the scorecard update case-by-case.

**Phase 6.7 / v0.9 — Slice 3.3 (Saved configs + scheduled).**

* Postgres table for saved configs + their bindings.
* Bot card for "save this run as a recurring", backed by the
  `mdk jobs` scheduling primitives.
* Exit: a saved demo card auto-runs daily and posts the scorecard
  to a designated channel.

Each slice is one PR cluster (~4–6 PRs each). No slice depends on
unmerged work from the next — we can stop at the end of any slice
if priorities shift.

## Open questions

* **Multi-tenant prospects.** When a prospect joins a channel, do
  they get a temp Movate tenant for the demo, or do they ride
  Movate's own tenant? Today: ride Movate's. Future: per-prospect
  trial tenants spun up by Slice 3.3.
* **Langfuse linking.** The trace replay link in the result card —
  Langfuse trace IDs are tenant-scoped. Initial cut: the link is
  shown only to Movate-internal users (not prospects), gated on
  the user's tenant being `movate-internal`.
* **Streaming responses.** Bot Framework supports streaming in
  newer SDKs but the Adaptive Card model is fundamentally
  request/response. v0.7 ships card-update-after-completion;
  streaming is a v1.x consideration.
* **Mobile UX.** Adaptive Cards render on Teams mobile but the
  experience needs explicit verification — particularly file
  upload and the eval scorecard. First sprint test target.

## Why we're confident this is the right shape

Three falsification tests we'd run within a week of starting:

1. **Build a single Adaptive Card** that renders a fixed `RunResponse`
   payload. Ship it as a standalone preview in the Teams Developer
   Portal. If the card can't show what `mdk run --output json`
   shows, the whole shape is wrong.
2. **Wire one bot endpoint** that forwards a fixed input to
   `mdk serve` and replies with the response. End-to-end in a
   sandbox tenant. If the latency from "user types" to "card replies"
   exceeds 6 seconds for a typical agent, we need to revisit
   (probably switch to webhook + delayed reply pattern).
3. **One sales engineer pilots Slice 3.1** for a week. If they
   reach for the CLI inside that week, we missed the UX target.
   Iterate before broadening rollout.

If any of those three tests fail, we re-open this ADR. None of them
are gated on the full Phase 6 Azure migration — we can run them on
the in-flight personal-subscription deployment while the migration
proceeds in parallel.

---

## Appendix A — Teams app manifest sketch

```json
{
  "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
  "manifestVersion": "1.16",
  "version": "0.7.0",
  "id": "<bot-aad-app-id>",
  "name": { "short": "Movate", "full": "Movate Agent Runner" },
  "description": {
    "short": "Run and evaluate Movate agents from Teams.",
    "full": "Submit Movate agent runs and small eval jobs from inside Teams. Backed by the same runtime the mdk CLI uses."
  },
  "developer": {
    "name": "Movate",
    "websiteUrl": "https://movate.com",
    "privacyUrl": "https://movate.com/privacy",
    "termsOfUseUrl": "https://movate.com/terms"
  },
  "bots": [{
    "botId": "<bot-aad-app-id>",
    "scopes": ["personal", "team", "groupchat"],
    "supportsFiles": true,
    "isNotificationOnly": false
  }],
  "permissions": ["identity", "messageTeamMembers"],
  "validDomains": ["movate.com"]
}
```

## Appendix B — Identifiers + env vars

| Name | Purpose |
|---|---|
| `MOVATE_TEAMS_BOT_APP_ID` | Bot's AAD app id (also `botId` in manifest) |
| `MOVATE_TEAMS_BOT_APP_PASSWORD` | Bot's AAD app secret (Key Vault) |
| `MOVATE_TEAMS_FLEET_API_KEY` | Bot's own Movate API key for admin ops (Key Vault) |
| `MOVATE_TEAMS_RUNTIME_BASE_URL` | Where the bot calls — e.g. `https://movate-runtime.acme.io` |
| `MOVATE_TEAMS_LANGFUSE_PUBLIC_HOST` | For trace replay links surfaced in cards (optional) |
