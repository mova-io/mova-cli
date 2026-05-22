# Architecture principles

**Status:** Canonical
**Audience:** Movate engineers + anyone (human or AI) changing this codebase
**Pairs with:** [`CLAUDE.md`](../CLAUDE.md) (the short, auto-loaded rules),
[`docs/license-posture.md`](license-posture.md), the ADRs under
[`docs/adr/`](adr/), and the design docs (`v1.0-azure-design.md`,
`azure-movate-architecture.md`, `v1.0-overview.md`).

This document is the **stable contract** for how `movate-cli` (`mdk`) is
layered. It describes the architecture *as it actually is today* (not an
aspiration), so changes can be checked against it. When the architecture must
change, update this doc **in the same PR** and, for anything structural, write
an ADR first.

---

## Why this exists

`mdk` is not a normal app — it is a reusable **framework + runtime + CLI +
deployment tooling**. At that scale the dominant risk is not bugs; it is
**architectural entropy**: hidden coupling, layers bleeding into each other,
abstractions multiplying, dependencies creeping in. These principles are the
guardrails that keep the layers honest as the codebase (and AI-assisted change
velocity) grows.

---

## The layers

```
        CONTROL PLANE                         EXECUTION PLANE
  ┌───────────────────────┐            ┌────────────────────────┐
  │  movate.cli            │            │  movate.runtime        │
  │  authoring / deploy /  │            │  FastAPI app that      │
  │  eval / ops commands   │            │  serves deployed agents│
  └───────────┬───────────┘            └───────────┬────────────┘
              │  both depend on core + adapters     │
              ▼                                      ▼
        ┌─────────────────────── movate.core ───────────────────────┐
        │  contracts + orchestration: models, loader, executor,      │
        │  eval, workflow, config, auth, schema. The agent contract  │
        │  (agent.yaml → AgentBundle) lives here.                    │
        └───────────────┬───────────────────────────────────────────┘
                        │  depends only on adapter *Protocols*
        ┌───────────────┴───────────────────────────────────────────┐
        │  ADAPTER SEAMS (swap an implementation without touching     │
        │  callers):                                                  │
        │   • providers/  BaseLLMProvider   (LLM/model adapters)      │
        │   • storage/    StorageProvider   (persistence + retrieval) │
        │   • tracing/    Tracer            (observability)           │
        └─────────────────────────────────────────────────────────────┘
   movate.kb (ingest + retrieval) sits on top of the storage Protocol.
```

### Plane / package responsibilities

| Package | Plane | Responsibility | Must NOT |
|---|---|---|---|
| `movate.cli` | Control | Author, validate, run, eval, deploy, manage. ~81 files. | Be imported by `runtime` or `core`. |
| `movate.runtime` | Execution | FastAPI service that runs deployed agents/jobs. | Import `cli`. Contain authoring/deploy logic. |
| `movate.core` | Contracts + orchestration | `models.py` (the data contracts), `loader.py` (agent.yaml→bundle), `executor.py`, `eval.py`, `workflow/`, `config.py`. | Import `cli` (see "known debt"). Import a *concrete* storage/provider/tracer. |
| `movate.providers` | Adapter | LLM/model adapters behind `BaseLLMProvider` (`base.py`) + `registry.py`. anthropic / openai_native / litellm / lyzr / langchain_native / mock. | Import `storage`, `runtime`, or `cli`. |
| `movate.storage` | Adapter | `StorageProvider` Protocol (`base.py`) + `postgres.py` / `sqlite.py`; `InMemoryStorage` lives in `testing/`. | Leak a backend type across the Protocol. |
| `movate.tracing` | Adapter | `Tracer` Protocol (`base.py`) + langfuse / otel / stdout / null / composite. | Be imported by execution *logic* (only wired at the edges). |
| `movate.kb` | Domain | Ingest (chunk→embed→store) + retrieval (vector/lexical/hybrid). | Import a concrete storage backend — use the Protocol via `build_storage()`. |

Supporting packages (`credentials`, `notify`, `memory`, `snapshot`,
`templates`, `scaffold`, `guardrails`, `menu`, `playground`, `teams_bot`, …)
are leaf utilities or integrations; they depend inward on `core`/adapters, never
the reverse.

---

## Boundary rules (the ⊥ list)

These are verified against the code today (`grep` for the import; empty = clean):

- **Control plane ⊥ execution plane** — `runtime` does not import `cli`. ✓
- **`core` ⊥ a concrete adapter** — `core` depends on the *Protocols*
  (`StorageProvider`, `BaseLLMProvider`, `Tracer`), never `storage.postgres`,
  `providers.openai_native`, etc. The concrete backend is selected at the edge
  (`storage.build_storage()`, the provider registry).
- **`kb` ⊥ a concrete storage backend** — KB retrieval talks to the
  `StorageProvider` Protocol, not `postgres`/`sqlite` directly. ✓
- **`providers` ⊥ `storage`** — model adapters don't touch persistence. ✓
- **Observability ⊥ execution logic** — tracing is wired at the edges; a
  `Tracer` is injected, never imported into the hot path. (`null.py` /
  `composite.py` make "no tracer configured" a no-op, not a branch.)
- **Agent contract ⊥ runtime** — `agent.yaml` describes *what* an agent is
  (`core.loader`/`core.models`), not *where* it runs. Keep it
  runtime-agnostic.

**Known tracked debt (do NOT silently fix; fix in a dedicated PR + ADR if
structural):**

- `core/config.py:710` does a function-local
  `from movate.cli.eval_scorecard_cmd import ALL_CATEGORIES` — a layering
  inversion (core → cli). It is lazy (no import-time cycle) but violates the
  rule above. Tracked; the fix is to move `ALL_CATEGORIES` into `core`.

When an `import-linter` contract gate is added (planned), these rules become
machine-enforced; the one known leak above is the initial allowed exception.

---

## Adapter seams — extend here, don't hardcode

Adding an integration means **adding an implementation behind an existing
Protocol**, not wiring the integration into a caller:

- **New model/provider** → implement `BaseLLMProvider` (`providers/base.py`),
  register in `providers/registry.py`. Don't special-case it in `executor`.
- **New persistence/vector backend** → implement `StorageProvider`
  (`storage/base.py`). Callers (`kb`, `runtime`, `cli`) stay unchanged. (This
  is exactly how pgvector lands — see ADR 009.)
- **New observability sink** → implement `Tracer` (`tracing/base.py`); compose
  via `composite.py`.

If a change *can't* be expressed as a new adapter behind an existing Protocol,
that's a signal it needs an ADR before code.

---

## Backward-compatibility contracts

These surfaces are consumed by users / customer deliverables. Changing them is
a **breaking change** — call it out explicitly in the PR and prefer an additive,
versioned path:

- **`agent.yaml` / `project.yaml` schema** (`core.models`, `core.canonical_schema`)
- **Public CLI surface** — command names, flags, `--json` output shapes
- **Runtime HTTP API** — `/api/v1/...` request/response shapes
- **Storage schema** — table columns / migrations (`storage.postgres`,
  `storage.sqlite`); migrations must be additive + idempotent
- **Environment variables** — `MOVATE_*`, `MDK_*`, provider keys
- **Deployment behavior** — bicep params, image-tag scheme, deploy modes

Default stance: **preserve compatibility unless explicitly instructed
otherwise.** Deprecate (with a logged warning, asserted via `caplog`) before
removing.

---

## Dependency policy

- **Minimal dependencies. Favor composable Python over framework-heavy
  designs.** No new framework unless it solves a *proven* scaling problem the
  existing stack can't, and the benefit exceeds maintenance cost.
- A new runtime/shipped dependency must (a) be permissively licensed — it is
  policed by `scripts/check_licenses.py` (see `docs/license-posture.md`), and
  (b) carry a one-line justification in the PR. A genuinely non-allowlist
  license needs an ADR + sign-off.
- Heavy / optional integrations go in an **opt-in extra** in `pyproject.toml`
  (e.g. `easyocr`, `cross-encoder`), not the core dependency set.

---

## Change control — when to slow down

**Write an ADR first** (under `docs/adr/`, match the latest ADR's structure)
for: storage/schema changes, a new adapter Protocol or seam, runtime API
changes, the deployment lifecycle, security/auth, or anything that touches a
backward-compat contract above.

**Plan before code** (state root cause → approach → impacted modules →
alternatives → blast radius, and get agreement) before editing anything in
`storage`, `runtime`, `core`, `providers`, `credentials`, or `infra`.

**One PR = one responsibility.** If you spot adjacent debt while working,
*document it* (a tracked task / a note here), don't fold an opportunistic
refactor into an unrelated change.
