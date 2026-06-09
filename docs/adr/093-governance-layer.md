# ADR 093 — A unified governance layer: one policy model, one enforcement seam, one audit spine

Status: Proposed
Date: 2026-06-08
Deciders: Engineering + Deva (Movate) — **governance is a product-direction +
compliance surface; the unified model + the "enforce" rollout posture require
Deva sign-off** (per CLAUDE.md §1 + the ADR 018 / ADR 036 precedent).
Consolidates (changes none of their wire contracts on day one):
ADR 018 (per-tenant BYOK), ADR 036 (usage metering + quotas), ADR 038
(governable pattern library), ADR 056 (judge/eval gate), ADR 062/083 (HITL +
notifier), ADR 087 (operational metrics), ADR 090 (agent lifecycle + control
plane), ADR 092 (parallel/supervisor governance contract).

## Context

mdk is embedded in customer deliverables, so the dominant question enterprises
ask is not "can it run an agent" but **"can we prove it runs agents within the
bounds we set."** mdk already enforces a surprising amount of this — but the
machinery is **fragmented**, which means it can neither be reasoned about as a
whole nor shipped as a sellable capability:

| Concern | Where it lives today |
|---|---|
| Model / provider / skill / runtime policy | `policy.yaml` → `ModelPolicy` / `RuntimePolicy` / `SkillPolicy` (`core/config.py`) |
| Per-tenant quotas (warn/deny, 429 at the edge) | `quotas.yaml` → `core/quotas.py` + `runtime/middleware.py` (ADR 036) |
| Cost / budget caps | `Budget.max_cost_usd_per_run`, tenant budget, `core/executor.py` |
| Input guardrails (prompt injection) | `ModelPolicy.input_guardrails` → `GuardrailViolationError` |
| Pattern / workflow bounds | fan-out cap, supervisor allowlist + delegation cap + aggregate budget, per-node activity policy (ADR 092) |
| HITL approval gates | HUMAN nodes + `approvers` (ADR 062 / 083) |
| Quality gates | `mdk eval --gate`, baselines (ADR 056) |
| Agent lifecycle (active/deprecated/disabled) | `AgentStatus` + `AgentRuntimeState` (ADR 090) |
| Audit trail ("who did what") | `tracing/audit.py` (`record_audit_event`) |
| Credential isolation | per-tenant BYOK (ADR 018) |

The cost of fragmentation:

- **4+ config formats** (`policy.yaml`, `quotas.yaml`, `agent.yaml` budget,
  `workflow.yaml` governance) — no single declared contract.
- **~6 enforcement sites** with **different decision shapes** (an exception
  here, a 429 there, a silent cap elsewhere, a compile error in the validator)
  — no consistent semantics, no consistent rollout.
- **No unified audit view** — a denial in the quota middleware and a budget cap
  in the executor land in different places, so "show me every policy decision
  for tenant X last week" is not answerable.
- **No observe-then-enforce posture** outside quotas — so adding any new control
  risks breaking a customer, which means controls don't get added.
- **No single place to SEE the effective policy** for an agent / project /
  tenant — reviewers and auditors can't.

The gap is **not** missing controls. It is the absence of a **layer** — a
uniform model that makes Declare → Resolve → Enforce → Audit → Report
consistent across every control, so governance can be reasoned about, rolled
out safely, audited, and sold.

This ADR does **not** add new enforcement on day one. It defines the seam and
adapts the existing checks behind it; new gates and the flip to `enforce` are
later, opt-in phases.

## Decision

Introduce a **governance layer** as a cross-cutting service behind Protocol
seams — the same adapter philosophy mdk uses for storage, providers, and
tracing (CLAUDE.md §6–7). Eight decisions.

### D1 — One `GovernancePolicy` model + a layered resolver

Consolidate the scattered policy blocks (and the dimensions that are currently
implicit) into a single `GovernancePolicy` with typed sub-policies:
`model`, `runtime`, `skill`, `data` (KB/context/tool read-scope), `cost`
(budget caps), `pattern` (fan-out/supervisor bounds), `quality` (eval gate),
`approval` (HITL triggers), `quota`. The existing `ModelPolicy` /
`RuntimePolicy` / `SkillPolicy` become sub-policies — **no field renames**; their
current `policy.yaml` location keeps working.

A `PolicyResolver` computes the **effective** policy for a
`(org, project, tenant, agent|workflow)` context by layering the levels with
**most-restrictive-wins**: a `deny` at any level beats an `allow` below it, and a
tenant can **never loosen** an org cap. (This monotonicity is the property that
makes the layer *governance* rather than mere configuration.) Reuses the
inheritance mechanics already in `core/layered_defaults.py`.

### D2 — A `Gate` Protocol + uniform `Decision` — the enforcement seam

One decision shape for every control:

```python
class Effect(StrEnum):       # ALLOW | WARN | DENY
class Decision:              # effect, reason, obligations, policy_id
class Gate(Protocol):
    kind: GateKind
    def evaluate(self, ctx: GovernanceContext) -> Decision: ...
```

`obligations` carry *conditional* requirements a gate can attach to an `ALLOW`
(e.g. "require HITL approval", "redact PII from output", "tag run as
high-risk"). Each existing check — model allowlist, budget, quota, skill
side-effect class, input guardrail, pattern caps, HITL trigger, eval gate —
becomes a `Gate` implementation. New governance is a **new `Gate` behind the
Protocol** (CLAUDE.md §7), never a new bespoke branch in execution logic.

### D3 — A `GovernanceEngine` called at the existing edges (no new edges)

The executor, the workflow runner, and the runtime middleware call
`GovernanceEngine.check(kind, ctx) -> Decision` at the points they already
enforce today — admission, pre-call, execution, post-call — instead of
hardcoding each check. The engine resolves the effective policy (D1), runs the
relevant gates (D2), combines their decisions (deny-wins), emits audit (D5), and
returns the obligations for the caller to honor. **The engine is wired at the
edges; it is never imported deep into core execution logic** (CLAUDE.md §6 —
tracing/governance are edge concerns).

### D4 — Universal `warn → enforce` rollout posture

`core/quotas.py` already ships the correct posture (`warn` = log + header +
allow; `deny` = block). **Generalize it to every gate.** Each policy declares a
mode per gate; the engine honors it. This is the mechanism that lets governance
ship without breaking a single customer: every control ships in `warn`, the
audit log (D5) sizes the blast radius, then individual gates flip to `enforce`
per tenant when the data says it is safe. An empty/absent policy ⇒ zero gates ⇒
byte-for-byte current behavior (the rule-5 contract).

### D5 — Audit as the spine

Every gate decision — **including `ALLOW`** — emits a `record_audit_event`
(`tracing/audit.py`) carrying `policy_id`, `gate_kind`, `effect`, `reason`, and
`obligations`. That yields the immutable **"who did what, under which policy,
and what did the system decide"** trail — the actual compliance artifact, and
the measurement that drives a warn-mode rollout. Audit is dual-routed: the
existing `movate.audit` logger **and** a queryable store behind the
`StorageProvider` Protocol, so `mdk governance audit` and a control-plane pane
can read it. Audit logging **never raises** (it must not break a request).

### D6 — The AI-specific gate taxonomy (what to govern, where it fires)

Generic IAM is insufficient — agents carry AI-specific runaway risks. The gate
set, mapped to the edge it fires at:

- **Admission** (API edge): tenant quota, rate, concurrency. *(exists — ADR 036)*
- **Pre-call**: model/provider allowlist (data-residency + cost), skill
  side-effect class, **data-access scope** (which KB / contexts / tools an agent
  may read — currently implicit), input guardrails (injection). *(partly exists)*
- **Execution**: budget/cost caps (runaway-spend risk), pattern bounds
  (swarm-entropy risk — ADR 038/092), loop/depth caps. *(exists — ADR 092)*
- **Post-call**: output guardrails (PII / safety / leakage), schema conformance.
  *(new)*
- **Decision gates**: HITL approval for high-stakes actions (ADR 062/083);
  eval-gate for quality regression (ADR 056). *(exists — to be expressed as gates)*
- **Lifecycle**: agent status (disabled/deprecated), provenance. *(exists — ADR 090)*

### D7 — Visibility: static (`mdk validate` / CI) + runtime (control plane)

- **Static.** `mdk validate` already surfaces *workflow* governance (ADR 092
  Phase 4). Extend to `mdk governance show <agent|project|tenant>` rendering the
  **effective resolved policy**, and `mdk governance lint --strict` as a CI gate
  (policy-as-code: a PR that violates the org policy fails the build).
- **Runtime.** A governance pane on the control plane (the ADR 090 surface
  already exists) showing gate decisions, denials, budget burn, and the HITL
  queue — the operator's live view of the contract.

### D8 — Boundaries (so it is a layer, not a ninth silo)

- Gates, the policy store, and the audit sink are **Protocols** (like
  `StorageProvider` / `Tracer` / `Notifier`) — new controls and new backends
  slot in behind them.
- The `GovernanceEngine` is a **cross-cutting service wired at the edges**, not
  threaded through core execution logic. `core` depends on the `Gate` Protocol,
  never a concrete control backend.
- Control plane (`cli`: `mdk governance …`) ⊥ execution plane (`runtime`:
  enforcement) — authoring/visibility and enforcement stay separate.
- This ADR **consolidates** the listed ADRs under one model; it does not
  supersede their decisions. Each existing control keeps its behavior until it
  is adapted behind the seam (D2) and, separately, flipped to `enforce` (D4).

### D9 — Governance runs on four planes, not one (the scope)

D1–D8 describe a **synchronous, per-request** decision: a `Gate` evaluating one
`GovernanceContext` at a point in time. That is necessary but it is only *one
plane* of governance — a single request context structurally cannot see
accumulated facts, out-of-band events, or the artifacts that ran before any
request. A complete governance layer invokes the **same `Decision` / audit
primitive (D2/D5) from four trigger planes**:

1. **Synchronous / per-request** *(D1–D8 — the seam)*. Pure function of one
   request: model/skill allowlist, input guardrails, per-call cost cap.
2. **Stateful / aggregate.** Decisions that require *memory across requests* — a
   per-request context cannot hold them: cumulative tenant spend
   (`TenantBudgetExceeded`), rate/concurrency over a window (`quotas.py`),
   session budgets, rolling quality, cost-drift anomaly (already detected in the
   executor). These are **stateful gates** — still `Gate`s, still `Decision`s,
   but they read/update a governance store (D10).
3. **Asynchronous / continuous.** Not request-triggered at all: a scheduled
   **reconciler** (the ADR 090 control-plane pattern) that scans every agent's
   *effective* policy against the org baseline and flags drift; eval-regression
   monitors (ADR 056); the **warn-mode rollup** that tells you a gate is safe to
   flip `warn → enforce`; attestation + compliance-report generation. The output
   is a **posture**, not a per-call verdict.
4. **Artifact / lifecycle / supply-chain.** Govern the *things* before they run a
   request — triggered by `publish` / `deploy` / `ingest` / a CI PR, not an
   inbound call: bundle provenance + signing, prompt/model version pinning +
   approval-to-deploy, deprecation (ADR 090 `AgentStatus`), skill/**context**
   registration policy, dependency-license gate (`check_licenses`), data
   residency at ingestion. This is where `mdk governance lint --strict`
   (policy-as-code in CI) and a deploy-time gate live.

Plus **Plane 0 — meta-governance**: the layer must govern *itself* — policy as
versioned, **signed** code; separation of duties (the author of an agent ≠ the
approver of an org cap); and an **audit of policy changes**. Without it,
governance is theater (anyone who can edit the policy can erase the controls).

**Data governance** is the cross-cut that proves the model: lineage, retention,
residency, PII, KB-ingestion policy flow across requests and the whole pipeline,
so they surface on *every* plane at once — a per-call `redact_pii` obligation
(1), a retention window (2), a lineage attestation (3), residency-at-ingest (4).

The primitive does not change across planes. What changes is the **trigger** and
the **consumer**: a request returns a `Decision`; a schedule produces a posture;
a lifecycle event gates an artifact — all landing in **one audit spine**.

### D10 — A `GovernanceState` seam for stateful gates (unlocks Plane 2)

Add a `GovernanceState` Protocol behind the `StorageProvider` Protocol so a gate
can read accumulated facts (spend-this-window, request-count, last-eval-score)
and the engine can record post-decision counters — without coupling `core` to a
concrete store (CLAUDE.md §6). This is the single architectural addition that
turns the existing per-tenant budget/quota machinery into **stateful gates**
behind the one engine, rather than a parallel enforcement path. Pure gates
(Plane 1) ignore it; stateful gates (Plane 2) depend on the Protocol, never a
backend.

## Phasing (each independently shippable; warn-first)

The sync seam (Phases 1–5 below) ships first; the further planes (D9) are
**additive follow-ons that reuse the same `Decision`/audit primitive** and are
sequenced after the existing checks are consolidated behind it:

1. **Seam, no behavior change.** `GovernancePolicy` + `PolicyResolver` (D1),
   `Gate`/`Decision`/`GovernanceEngine` (D2/D3), `warn`/`enforce` modes (D4).
   Everything defaults empty/`warn` ⇒ no-op. Ship the model + the unified audit
   shape (D5) with zero enforcement change.
2. **Adapt existing checks to the Protocol.** Model/skill/runtime policy, budget,
   quota, pattern caps refactor *in place* as `Gate`s behind the engine — pure
   consolidation + unified audit; no new enforcement.
3. **Visibility first.** `mdk governance show` / `audit` (D7) + the control-plane
   pane. Operators can *see* the effective policy and every decision while
   everything is still in `warn`.
4. **New gates.** Data-access scope, output guardrails/PII, eval-gate-as-policy
   (D6) — shipped in `warn`.
5. **Flip to enforce.** Per-gate, per-tenant, once the warn-mode audit says the
   blast radius is acceptable. This is a config + Deva decision, not a code
   change.

Then the further planes (D9), in leverage order — each additive, each `warn`-first:

6. **Plane 2 — stateful gates.** The `GovernanceState` seam (D10) + re-express
   tenant budget / quota as stateful gates behind the one engine. Highest
   leverage; reuses storage; consolidates real fragmentation.
7. **Plane 4 — lifecycle gates.** Deploy/publish/ingest gates + `mdk governance
   lint` in CI. Reuses the control plane (ADR 090) + `check_licenses`; most
   *enterprise* value (approval-to-deploy, provenance, residency-at-ingest).
8. **Plane 3 — reconciler + posture.** The scheduled policy/drift scan +
   compliance reporting, once warn-mode audit data is rich enough to drive it.
9. **Plane 0 — meta-governance.** Signed policy + change audit + separation of
   duties — added when `enforce` goes live, because that is when tampering matters.

## Consequences

**Compat / blast radius (rule 5).** Phase 1–2 are additive + default-preserving:
an empty/absent governance policy resolves to zero gates, so every existing
agent/workflow/tenant is byte-for-byte unchanged. The existing `policy.yaml` /
`quotas.yaml` / `agent.yaml` budget / `workflow.yaml` governance keep their
schemas (they become *sub-policies* of the unified model, not replacements). No
`/api/v1`, CLI-flag, env, or storage **schema** change in Phase 1 (the audit
store + `mdk governance` surface are additive). The only behavior-changing step
is D4's per-gate flip to `enforce`, which is an explicit, audited, per-tenant
operator decision — never a default.

**Why a layer, not a console.** The differentiator is **governance-as-code,
enforced uniformly at one seam, with observe-then-enforce rollout, fully
audited.** The bounds live in the same versioned files as the agents and
workflows, validate in CI, and bite at runtime under one decision model — the
thing a bolt-on policy console cannot provide and a natural extension of what
mdk already is. The risk this ADR retires is *architectural entropy*: without
the seam, every new control is another bespoke enforcement site; with it, every
new control is one `Gate`.

**What this is NOT.** Not a new policy DSL (the sub-policies stay typed config),
not a replacement for the existing controls (it consolidates them), and not a
day-one enforcement change (warn-first, default-empty).

## Verification (per phase)

```
ruff check src tests && ruff format --check src tests && mypy src
pytest -m "not smoke"                         # default-empty policy ⇒ no-op; existing suites unchanged
python scripts/check_licenses.py --strict     # no new shipped dep expected
```

- Phase 1: a default-empty `GovernancePolicy` resolves to zero gates and every
  workflow/agent/tenant path is byte-for-byte unchanged; the resolver's
  most-restrictive-wins monotonicity is property-tested (a tenant override can
  never loosen an org cap).
- Phase 2: each adapted gate produces the SAME effect it did before
  (an executor budget cap, a quota 429, a model-deny) — now via a `Decision` +
  an audit record; a conformance test pins the before/after equivalence.
- Phase 3: `mdk governance show` renders the resolved effective policy; every
  gate decision (incl. `allow`) appears in `mdk governance audit`.
- Phase 5: flipping a single gate `warn → enforce` for one tenant blocks exactly
  that gate for exactly that tenant, with an audit record, and nothing else.
