# ADR 043 — Self-improving agent loop: closed-loop diagnose → patch → canary → promote/revert

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Builds on / depends on:**
ADR 014 (durable agent registry — patches create new agent versions),
ADR 016 D1/D2/D3/D5 (harvest + continuous eval + canary + safety-first
defaults — the existing legs of the loop this ADR closes),
ADR 017 D5 (durable + HITL — the human-review gate transport),
ADR 035 (outbound events / webhooks — patch lifecycle is just more typed
events on the existing outbox + SSE),
ADR 040 (projects as cloud entity — patches are project-scoped),
**Failure Pattern Diagnoser** (landing as a separate PR tonight — exposes
`POST /api/v1/diagnose/cluster_and_propose` over failed-case clusters; this
ADR's D3 pipeline calls it).
**Flagship:** #2 of MDK's Claude-orchestrated platform story (Flagship #1
= the Failure Pattern Diagnoser; the **Bundle Composer (ADR 042)** is a
separate, parallel Claude-orchestrated capability — explicitly out of scope
here).

---

## Context

ADR 016 already shipped four of the five legs of a self-healing platform:
- **D1 — Harvest** prod runs into proposed eval cases.
- **D2 — Continuous eval + drift alerting** against the durable baseline.
- **D3 — Canary** / champion–challenger rollout with version-tagged runs.
- **D5 — Safety-first defaults** (canary, kill-switch, opt-in auto-promote,
  opt-in auto-rollback — the *defaults* + *guardrails*, not an automated
  action loop).

The **fifth leg is missing**: when D2 fires a drift alert, or when D1 surfaces
a cluster of harvest misses, or when an eval-failure cluster forms, **a human
still has to diagnose the failures and write the fix.** Eval data sits on the
table while the agent silently degrades; the on-call engineer reads
twenty failed cases, infers the common failure mode, edits the prompt or
ingests a missing KB doc, opens a PR, waits for review, deploys, and only
then does the existing D3 canary + D5 revert machinery come back online.

The bottleneck is **diagnose + propose**, not detect, measure, route, or
revert. Drift detection has existed in the industry for years. The *new* bit
is: **Claude reads the failed cases, proposes the specific typed fix,
canaries it on the existing rails, and auto-promotes if the eval improves —
or auto-reverts if it doesn't.** The human reviews PR-sized proposals on
their own schedule, not 3 a.m. runtime fires.

This ADR is the **connective tissue** that closes ADR 016's loop. It adds:
- A typed `proposed_patches` row + lifecycle (D1).
- A declarative per-agent `improvement_policy` (D2).
- An event-driven pipeline that walks `diagnose → propose → (review) →
  canary → eval → promote-or-revert` over existing infra (D3).
- A taxonomy of **typed patch kinds** the runtime can apply deterministically
  (D4) — no free-form diffs.
- A **HITL gate as the default** (D5) — auto-canary is explicit opt-in,
  per-agent, per patch-kind.
- Strict, observable **auto-revert criteria** (D6) layered on ADR 016 D5.
- An **audit event** for every action (D7) via the existing R5 telemetry.
- **Auto-expiration** of unreviewed drafts (D8) so the queue stays bounded.

In one sentence: *"The diagnose+propose step that lives in a human's head
today becomes a typed, evaluable, reversible, audited artifact — and the
existing ADR 016 canary/eval/revert rails carry it home."*

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Close the loop ADR 016 already designed** — diagnose+patch is the only manual leg | HIGH |
| **Safety / reversibility** — every auto-action must be canaried + evaluable + revertable + audited | HIGH |
| **HITL by default** — v1 must not auto-promote without a human gate; the trust model is earned, not asserted | HIGH |
| **Reuse existing infra** — canary (016 D3), eval (016 D2), registry (014), events (035), audit (R5) — net-new is just the patch table + the pipeline | HIGH |
| **Per-agent control** — agents differ in risk tolerance; policy lives on `agent.yaml`, not on the tenant | MED |
| **Typed, deterministic patches** — runtime applies kinds it understands; no free-form diff smuggling | MED |
| **Bounded queue** — drafts auto-expire; the proposed-patches list is not a graveyard | LOW |

---

## Architecture

```
   ADR 016 D2 (drift)         ADR 016 D1 (harvest miss)         eval-failure cluster
          \                          |                                  /
           \________________  _______|________________________________/
                            \ |
                             ▼
            (Failure Pattern Diagnoser — separate PR, tonight)
            POST /api/v1/diagnose/cluster_and_propose
                             |
                             ▼
            proposed_patches.insert (status=draft)         ← D1 (new table)
                             |
                             ▼
            improvement_policy.evaluate                    ← D2 (new agent.yaml block)
              ├─ require_human_review? ──► notify via ADR 035 outbox
              │                              └─► await POST .../approve  (HITL — D5)
              └─ auto_canary? ───────────► POST .../canary
                             |
                             ▼
            canary_traffic (ADR 016 D3 — UNCHANGED)
                             |
                             ▼
            eval.measure on the canary slice (ADR 016 D2 — UNCHANGED)
                             |
                             ▼
            auto_promote   OR   auto_revert  per policy    ← D6 (typed criteria)
                             |
                             ▼
            audit_event via R5 telemetry                   ← D7 (existing infra)
                             |
                             ▼
            ADR 016 D3 promotion store / ADR 014 registry version
```

Everything in CAPS-UNCHANGED is reused as-is. The net-new code is the
`proposed_patches` row, the `improvement_policy` block, the policy
evaluator, the typed patch-kind dispatcher, and the lifecycle event wiring.

---

## Decisions

### D1 — `proposed_patches` is the central typed artifact

A new persisted row, behind `StorageProvider` (per ADR 014's discipline — no
new infra):

```
proposed_patches(
  id                          uuid pk,
  tenant_id                   uuid,
  project_id                  uuid,     -- ADR 040
  agent_name                  text,
  kind                        text,     -- enum, D4
  diff                        jsonb,    -- typed shape per kind (D4)
  source_cluster_id           uuid,     -- ties back to the Diagnoser cluster
  expected_improvement_json   jsonb,    -- {metric, baseline, projected, n}
  confidence                  numeric,  -- Diagnoser's self-reported confidence
  status                      text,     -- draft|canary|promoted|reverted|rejected|expired
  created_at                  timestamptz,
  decided_at                  timestamptz null,
  decided_by                  text null
)
```

Status transitions are strictly: `draft → canary → promoted` OR
`draft → canary → reverted` OR `draft → rejected` OR `draft → expired`. Any
illegal transition is rejected at the storage seam.

### D2 — `improvement_policy` on `agent.yaml` (declarative, opt-in)

Per-agent, **off by default**:

```yaml
improvement:
  enabled: true
  triggers: [drift_alert, eval_failure_cluster, harvest_miss]
  policy:
    auto_canary: true
    canary_traffic_pct: 5
    auto_promote_threshold: 0.02      # eval-pass-rate delta
    auto_revert_on_failure: true
    require_human_review: true        # HITL gate — DEFAULT TRUE for v1
    patch_kinds_allowed: [prompt_edit, kb_ingest, context_add]
    patch_kinds_excluded: [model_swap]  # too risky for auto
    draft_ttl_days: 30
    auto_revert_threshold: 0.02       # eval-pass-rate drop
    cost_per_run_max_increase_pct: 25 # cost guardrail (D6)
```

Per-agent (not per-tenant) because agents differ in risk tolerance — a
customer-facing FAQ bot and an internal triage bot don't share a regret
budget. Backward-compatible: the whole block is optional; absence ≡
`enabled: false`.

### D3 — Pipeline (event-driven, on existing rails)

```
trigger          ▶  on bus
─────────────────────────────────────────────
drift_alert      ▶  ADR 016 D2 NotificationDispatcher / outbox
harvest_miss     ▶  ADR 016 D1 emits when N unmatched cases cluster
eval_failure     ▶  eval-as-job emits when failure cluster forms
                 │
                 ▼
Diagnoser.cluster_and_propose            (separate PR — landing tonight)
                 │
                 ▼
proposed_patches.insert(status=draft)    (D1)
                 │
                 ▼
improvement_policy.evaluate              (D2 — pure function)
       ├─ require_human_review=true ─▶ emit patch.proposed event (ADR 035)
       │                               and STOP (await /approve)
       └─ require_human_review=false + auto_canary=true ─▶ /canary
                 │
                 ▼
ADR 016 D3 canary_traffic                (UNCHANGED)
                 │
                 ▼
ADR 016 D2 eval-as-job on canary slice   (UNCHANGED)
                 │
                 ▼
policy.decide(eval_result)               (D6 — typed criteria)
       ├─ improved beyond auto_promote_threshold ─▶ /promote
       └─ degraded beyond auto_revert_threshold  ─▶ /revert
                 │
                 ▼
audit_event via R5 telemetry             (D7 — UNCHANGED)
```

The pipeline runs on the **existing scheduler + outbox** (ADR 035) — no new
queue, no new worker. Each step is idempotent on `proposed_patch_id`.

### D4 — Typed patch kinds (no free-form diffs)

The runtime applies a closed enum of kinds, each with a typed `diff` schema:

| Kind                | `diff` shape (jsonb)                                    | Runtime application |
|---------------------|---------------------------------------------------------|---------------------|
| `prompt_edit`       | `{section, before, after, rationale}`                   | New agent version (ADR 014) with the prompt patched |
| `kb_ingest`         | `{source_url_or_blob, expected_chunks, doc_id}`         | Ingest into the project's KB (ADR 040) |
| `context_add`       | `{key, value_template, scope}`                          | New agent version with appended context |
| `context_remove`    | `{key, reason}`                                         | New agent version with the key removed |
| `model_swap`        | `{from_model, to_model, fallback}`                      | New agent version pointing at the new provider |
| `temperature_change`| `{from, to}`                                            | New agent version with the new value |
| `retrieval_k_change`| `{from, to}`                                            | New agent version with the new retrieval-k |

Each kind has a deterministic *applier* in the runtime — no free-form text
patching, no shell-out to git apply, no LLM-at-apply-time. The Diagnoser
*proposes* a typed kind+diff; the runtime *applies* it deterministically.
Anything outside this enum is a Diagnoser bug → rejected at the storage seam.

### D5 — HITL gate is the v1 default

`require_human_review: true` is the v1 default. A patch in `draft` status
that needs review:

1. Emits a `patch.proposed` event via ADR 035's outbox.
2. Lands in `GET /api/v1/agents/{n}/proposed-patches` for an admin to review.
3. **Blocks until an explicit `POST .../patches/{id}/approve` from an admin**
   (per ADR 013 scopes — `admin`).
4. Only then transitions to `canary`.

Auto-canary requires explicit per-agent opt-in (`require_human_review: false`
+ `auto_canary: true`). This is asymmetric on purpose — the trust budget is
the operator's, not ours, and v1 ships with the gate up. ADR 016 D5
established the policy ("auto-promote is opt-in"); this ADR mechanizes it
into a per-patch checkpoint.

### D6 — Auto-revert criteria (typed, observable, configurable)

Any one of the following fires `/revert`:

- **Eval-pass-rate drops** by more than `auto_revert_threshold` (default
  0.02) vs. the pre-canary baseline on the same eval set.
- **Error rate up** by more than **2σ** over the trailing baseline window.
- **Cost-per-run up** by more than `cost_per_run_max_increase_pct` (default
  25 %).

Every criterion is a typed comparator over an existing measured signal
(eval / error-rate / cost — all already in ADR 015 / ADR 016). No new
telemetry. **Relationship to ADR 016 D5:** ADR 016 D5 says auto-rollback is
*opt-in, not default*. This ADR honors that — `auto_revert_on_failure` is a
policy field, defaulted `true` only **when the operator has already opted
into the improvement loop**. Operators who enable the loop but want to
revert by hand can set `auto_revert_on_failure: false`; the patch then
stays at canary and emits a `patch.canary_degraded` event for human action.
This composes with — does not replace — ADR 016 D5's posture.

### D7 — Audit event per action (reuse R5 telemetry)

Every transition emits an audit event via the existing R5 / ADR 035 outbox:

`patch.proposed` · `patch.approved` · `patch.rejected` · `patch.canary_started`
· `patch.eval_measured` · `patch.promoted` · `patch.reverted` · `patch.expired`

Each event carries the `proposed_patch_id`, the `agent_name`,
`project_id`, the actor (`Diagnoser` / human user / `policy.auto`), the
typed `kind`, the `diff` hash, and the relevant measured signal. Auditors
replay any decision by querying the event log — no new audit surface.

### D8 — Auto-expiration of unreviewed drafts

A `draft` patch older than `draft_ttl_days` (default 30) is transitioned to
`expired` on the existing scheduler tick. Emits `patch.expired`. Prevents
the queue from becoming an unread inbox.

---

## API surface (additive — `/api/v1`)

| Method | Path | Scope | Purpose |
|---|---|---|---|
| GET    | `/api/v1/agents/{n}/proposed-patches`               | `read`  | List patches (filter by status) |
| GET    | `/api/v1/agents/{n}/proposed-patches/{id}`          | `read`  | Detail (kind, diff, expected improvement, audit trail) |
| POST   | `/api/v1/agents/{n}/proposed-patches/{id}/approve`  | `admin` | HITL gate — `draft → ready-for-canary` |
| POST   | `/api/v1/agents/{n}/proposed-patches/{id}/canary`   | `admin` | Start canary (also called by the auto-canary path) |
| POST   | `/api/v1/agents/{n}/proposed-patches/{id}/promote`  | `admin` | Promote (also called by the auto-promote path) |
| POST   | `/api/v1/agents/{n}/proposed-patches/{id}/reject`   | `admin` | Explicit reject (`draft → rejected`) |
| GET    | `/api/v1/agents/{n}/improvement-policy`             | `read`  | Read effective policy |
| PUT    | `/api/v1/agents/{n}/improvement-policy`             | `admin` | Update policy |
| GET    | `/api/v1/agents/{n}/improvement-history`            | `read`  | Audit-grade event stream for this agent's patch history |

All endpoints contract-tested per ADR 033 (API hardening). Errors and
JSON shapes follow the existing `/api/v1` conventions.

### CLI parity

```
mdk patches list <agent> [--status draft|canary|promoted|reverted|rejected|expired]
mdk patches show <agent> <patch-id>
mdk patches approve <agent> <patch-id>
mdk patches canary  <agent> <patch-id> [--traffic-pct N]
mdk patches promote <agent> <patch-id>
mdk patches reject  <agent> <patch-id> [--reason ...]

mdk improvement show <agent>
mdk improvement set  <agent> --enabled --require-human-review ...
```

Per CLAUDE.md compat rule: new commands only; no existing CLI shape changes.

---

## Failure modes

- **Canary fails before auto-revert criteria fire.** The existing ADR 016 D3
  canary infrastructure carries the kill-switch (route 0 % instantly); the
  patch transitions to `reverted` with `actor=policy.auto.kill_switch` and
  emits the audit event.
- **Diagnoser proposes a bad patch.** Canary catches it: the eval on the
  canary slice shows no improvement (or degradation), auto-revert fires,
  patch goes to `reverted`. Worst case is a small slice of traffic ran
  against a bad version for one canary window — the existing ADR 016 D3
  safety budget, unchanged.
- **In-flight runs during a revert.** Drained gracefully via existing canary
  infrastructure (sticky-by-session or weighted, per ADR 016 D3) — runs
  already on the challenger complete; new traffic routes to the champion.
- **Diagnoser proposes a kind outside `patch_kinds_allowed`.** Rejected at
  the storage seam before the row lands; emits a `patch.rejected` audit
  event with `actor=policy.kind_excluded`.
- **Diagnoser endpoint unavailable.** The pipeline is event-driven and
  retried via the outbox; persistent failure is alerted via the existing
  drift-alert channel. No silent loss.
- **HITL gate stalls** (no admin reviews). D8 auto-expires the draft after
  `draft_ttl_days`; the operator gets a `patch.expired` event and can lower
  the gate or extend the TTL.
- **Auto-revert thresholds tuned too tight.** The patch flaps
  `canary ↔ reverted` — the audit log makes the pattern visible; operators
  loosen the threshold. Worth a runbook note.

---

## Consequences

**Positive**
- The loop ADR 016 designed actually closes — agents self-heal between human
  attention.
- The human reviews PR-sized typed artifacts on their own schedule, not 3 a.m.
  runtime fires. The on-call burden drops sharply.
- Every action is reversible (canary → revert) and auditable (R5 events) — the
  enterprise trust story.
- Almost entirely **wiring + a typed table + a policy block** on top of ADR
  016 D2/D3, ADR 035, ADR 014. Net-new infra: zero.

**Negative / costs**
- Over-eager auto-promotion if thresholds are too loose — mitigated by D5
  (HITL default) and D6 (multi-criterion revert).
- More LLM spend on the Diagnoser at trigger time — bounded by the same
  budgeting story as ADR 016 D2's continuous eval (per-tenant budget +
  sampling).
- Operational discipline: tuning per-agent policy is a new knob set; needs a
  runbook.

**Neutral**
- One new table (`proposed_patches`), one new agent.yaml block
  (`improvement`), one new event family (`patch.*`), one new API/CLI surface.
  All additive, all opt-in.

---

## Resolved decisions (locked in upfront)

- **R1.** HITL gate is **ON by default** (`require_human_review: true`).
  Auto-promote requires explicit per-agent opt-in. The v1 trust posture is
  "show the human the typed diff; let them say yes".
- **R2.** Per-agent improvement policy (not per-tenant). Agents differ in
  risk tolerance; a single global toggle is the wrong granularity.
- **R3.** Typed patch kinds (D4 enum) — no free-form diffs. The runtime
  applies kinds it understands, deterministically.
- **R4.** Audit every action via the existing R5 / ADR 035 telemetry. No new
  audit surface; auditors already know how to query the event log.

---

## Alternatives considered

- **"Just open a GitHub PR for the patch and wait for human review."**
  Rejected — too slow. We have eval data **right now** that can validate
  the patch on a canary slice in minutes. A GitHub PR loses the live
  signal, defers a fix that the canary could ship within an eval window,
  and re-introduces the bottleneck this ADR exists to remove. (Operators
  who *want* PR-based review can set `require_human_review: true` + plug
  the `patch.proposed` webhook into their PR-bot — that's the integration,
  not the platform default.)
- **"Auto-promote without HITL by default."** Rejected — too risky for v1.
  The trust budget belongs to the operator. HITL is the default; auto-promote
  is explicit per-agent opt-in. (R1.)
- **"Single global improvement policy per tenant."** Rejected — agents
  differ in risk tolerance (customer-facing vs. internal vs. experimental).
  Per-agent policy is the right granularity. (R2.)
- **"Free-form text diffs from Claude."** Rejected — non-deterministic to
  apply, hard to audit, hard to test. Typed kinds (D4) are reviewable,
  auditable, and applied deterministically by the runtime. (R3.)
- **"New audit surface for patches."** Rejected — duplicates the R5 /
  ADR 035 outbox. Patch events are just more typed events on the existing
  rails. (R4.)

---

## Boundaries (explicitly NOT in scope)

- **Bundle Composer (ADR 042)** — a parallel Claude-orchestrated capability,
  separate ADR.
- **Changes to existing canary, eval, or revert behavior** (ADR 016
  D2/D3/D5) — this ADR uses them as-is. Any change to those primitives
  belongs in ADR 016 (or a successor), not here.
- **Cross-agent learning** — "if agent X failed this way, propose the same
  fix to agent Y" is a future ADR. v1 is per-agent.
- **Free-form patches outside the typed-kinds set (D4)** — a future ADR may
  widen the enum after the typed v1 earns trust.
- **Net-new audit infrastructure** — reuse R5 / ADR 035 (R4).
- **The Failure Pattern Diagnoser itself** — a separate PR (landing
  tonight). This ADR consumes its `POST /api/v1/diagnose/cluster_and_propose`
  endpoint; it does not redesign or extend it.
- **CI changes** — `cli ⊥ runtime` is unchanged; the pipeline runs on the
  existing worker + outbox.

---

## Cross-references / overlap with ADR 016

This ADR is **connective tissue**, not a replacement:

- ADR 016 **D1 (harvest)** — this ADR's `harvest_miss` trigger consumes the
  *unmatched cluster* signal ADR 016 D1 already produces. No change to D1.
- ADR 016 **D2 (continuous eval + drift)** — this ADR's `drift_alert`
  trigger is the same alert ADR 016 D2 fires; this ADR adds a second
  consumer (the Diagnoser) alongside the existing human notification.
- ADR 016 **D3 (canary)** — this ADR's canary step *is* ADR 016 D3,
  unchanged. The patch row carries the version pointer; ADR 016 D3 carries
  the traffic.
- ADR 016 **D5 (safety-first defaults)** — ADR 016 D5 says auto-rollback is
  *opt-in, not default*. This ADR honors that: `auto_revert_on_failure` is a
  per-agent policy field, and it only takes effect once the operator has
  opted into the improvement loop on that agent. The new auto-revert
  criteria (D6 here) **compose with, do not replace,** ADR 016 D5's posture
  — D6 specifies *the typed criteria when revert fires*; D5 specifies *that
  revert is opt-in*. Both are true.

The one place to watch for divergence: if a future ADR ever flips ADR 016
D5's default (auto-rollback becomes on-by-default platform-wide), this
ADR's `auto_revert_on_failure` field becomes redundant and should be
deprecated in favor of the platform default. Flagged as a known
forward-compat watch-item.
