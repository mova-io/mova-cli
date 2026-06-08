# ADR 090 — Agent lifecycle state + control-plane seam

Status: Accepted
Date: 2026-06-08

## Context

All agents are co-tenant in **one runtime image**: `scan_agents()` loads every
`agent.yaml` into `app.state.agents` at startup, and `GET /api/v1/agents` returns
the lot ([app.py] `v1_list_agents`, [registry.py] `scan_agents`). The Chainlit
playground builds its picker from that endpoint with **zero filtering**
([playground/app.py] `on_chat_start` → `client.list_agents()`), so **every** agent
shows in the dropdown — including stale/deprecated ones that no longer respond
(broken `agent.yaml`, a removed skill ref, a retired model). Observed on the live
demo: ~12 agents in the picker, several of which error or hang.

The root cause is a **missing lifecycle concept**. The agent model has no
`status`/`deprecated`/`disabled`/`health` field anywhere
([models.py] `AgentSpec`, `AgentMetadata`, `AgentBundleRecord`). An agent is
either *in the registry* or *soft-deleted* — there is no "off", no "deprecated",
and no health signal. Operators cannot hide or disable a bad agent without
deleting it.

Scaling today is **per-service, not per-agent**: the API scales on HTTP
concurrency; the worker already scales on a **KEDA Postgres queue-depth scaler**
([containerapp-worker.bicep]). KEDA is in the stack — just not at agent
granularity. A future direction (Tier 2, **out of scope here**) makes each agent
its own KEDA-scaled, scale-to-zero ACA app. We want the lifecycle model we add
now to be the **same contract** a future ACA reconciler would drive, so the
control-plane UI does not change when the backend graduates from an in-memory
flag to a real scaled container.

## Decision

Introduce **agent operational state** as a first-class, *mutable* concept kept
**separate from the immutable bundle** (ADR 014: bundles are immutable; lifecycle
is operational, not authored). Build a thin **control-plane** API + UI over it.

### D1 — `AgentRuntimeState`: mutable operational state, keyed by (tenant_id, name)

A new lightweight record — **not** a field on `agent.yaml` or `AgentBundleRecord`:

```python
class AgentStatus(str, Enum):
    ACTIVE = "active"        # served + listed + shown in pickers (default)
    DEPRECATED = "deprecated"# served (existing callers keep working) but hidden from pickers
    DISABLED = "disabled"    # not served — runs 409; hidden from pickers

class AgentRuntimeState(BaseModel):
    tenant_id: str
    name: str
    status: AgentStatus = AgentStatus.ACTIVE
    updated_at: datetime
    updated_by: str
    note: str | None = None   # optional operator reason ("retired 2026-06, use faq-v2")
```

**Absent row ⇒ `ACTIVE`.** Every existing agent (filesystem or registry) reads as
active with no migration — back-compat by construction. The state is operator-set
at runtime, never declared in the bundle.

*Why a separate row, not a field on `agent.yaml`:* lifecycle is an operational
decision (an operator retires an agent), not an authoring one; baking it into the
immutable bundle would force a re-publish to disable. Keeping it separate is also
the Tier-2 seam — a future ACA reconciler reads/writes this exact row, mapping
`active→running`, `disabled→scaled-to-zero`, `deprecated→running-but-unlisted`.

### D2 — `StorageProvider` gains agent-state methods (additive Protocol extension)

```python
async def get_agent_state(self, name, *, tenant_id) -> AgentRuntimeState | None
async def set_agent_state(self, state: AgentRuntimeState) -> None
async def list_agent_states(self, *, tenant_id) -> list[AgentRuntimeState]
```

Implemented in `sqlite` + `postgres` (new `agent_runtime_state` table, additive
migration: `(tenant_id, name)` PK, defaulted). The Protocol methods carry default
implementations that **degrade to "all active"** (`get→None`, `list→[]`, `set→`
no-op + warn) so a backend without the table — or the in-memory test double until
updated — keeps working. Matches the kb/StorageProvider seam rule (CLAUDE.md
rule 6): `core` depends on the Protocol, never a concrete backend.

### D3 — Health is computed, not stored

`GET /api/v1/agents/{name}/health` resolves the bundle and **loads** it
(`resolve_agent_bundle` → `load_agent`); a clean load = `healthy`, a load/resolve
failure = `unhealthy` with the diagnostic (this catches exactly the "deprecated
agents that don't work": missing skill, bad model ref, malformed yaml). An
optional `?probe=run` does one `MockProvider` dry-run turn for a deeper signal.
Health is **derived state** — cached briefly in `app.state`, never persisted, so
it can never go stale in storage.

### D4 — Control-plane API (additive `/api/v1`, no breaking changes)

- `GET /api/v1/agents` — **unchanged shape**, but each `AgentCatalogItemView`
  gains additive `status: str = "active"` + `health: str = "unknown"` fields, and
  a new optional `?status=active|deprecated|disabled` filter. Default (no filter)
  still returns everything (back-compat for existing callers).
- `PATCH /api/v1/agents/{name}/status` — body `{status, note?}`, scope `admin`,
  writes `AgentRuntimeState`. Returns the new state.
- `GET /api/v1/agents/{name}/health` — scope `read`, derived health (D3).

`POST /agents/{name}/run` and the streaming routes reject a `DISABLED` agent with
**409** (`agent_disabled`); `ACTIVE`/`DEPRECATED` run normally.

### D5 — Picker + control-plane UI

- **Chainlit picker** filters to `status == ACTIVE` **and** not-known-unhealthy
  (one extra filter in `on_chat_start`; the immediate fix for the reported pain).
- **Landing page** gains a 7th tile **"🎛️ Agent Control Plane"** → a lightweight
  static `agents.html` (same style as the existing tiles) that calls the runtime
  API: a table of name · version · status · health · last-run, with
  enable/disable/deprecate toggles (`PATCH …/status`) and a "test" button
  (`…/health?probe=run`). No new server — static page + bearer token, identical
  pattern to the existing landing tiles.

## Consequences

**Compat / blast radius (rule 5):** one additive storage table
(`agent_runtime_state`, defaulted, absent ⇒ active), three additive Protocol
methods (defaulted to degrade gracefully), two additive fields on
`AgentCatalogItemView` (defaulted), two new `/api/v1` routes, one new optional
query param. **No** change to `agent.yaml` schema, `AgentBundleRecord`, existing
route shapes, CLI flags, or env vars. Existing agents read as `active`; the native
path is unchanged. CalVer bump per the git hook.

**Forward-compat to Tier 2 (out of scope, future ADR):** `AgentRuntimeState` is
the seam. When agents become per-agent ACA apps, a reconciler consumes the same
rows (`active→min≥1`, `disabled→scale-to-zero`, `deprecated→running+unlisted`) and
the control-plane UI + API are unchanged — only the backend that honors the status
changes. This ADR deliberately does **not** build the reconciler, per-agent Bicep,
or scale-to-zero; it builds the lifecycle contract they will use.

**Why not put status in `agent.yaml`:** disabling would require a re-publish of an
immutable bundle; lifecycle is operational, not authored (see D1).

**Why not hard-delete bad agents:** delete is destructive + loses history;
`deprecated`/`disabled` are reversible operator states and preserve the registry.

## Verification

```
ruff check src tests && ruff format --check src tests
mypy src
pytest -m "not smoke" tests/test_agent_runtime_state*.py \
       tests/test_runtime_agents_v1.py tests/test_playground_app*.py
pytest -m "not smoke"            # full suite — default-active keeps everything green
python scripts/check_licenses.py --strict
```

- New tests: state storage round-trip (sqlite + in-memory double); the three
  endpoints (status patch, health, filtered list); `DISABLED` agent → 409 on run;
  picker filters disabled/deprecated/unhealthy.
- Parity: default (no `?status`) list is byte-compatible with today's response
  plus the two defaulted fields.
