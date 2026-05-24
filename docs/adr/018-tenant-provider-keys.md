# ADR 018 — Per-tenant provider keys (BYOK): each tenant manages its own OpenAI/Anthropic keys

**Status:** Proposed
**Date:** 2026-05-24
**Deciders:** Engineering (security/runtime — Deva sign-off for any cloud-KMS/Key-Vault dependency, per ADR 001)
**Context window:** v1.0 multi-tenant operability — provider-credential isolation + cost attribution
**Builds on / related:** ADR 001 (cloud-portability + minimal-deps), ADR 012 (runtime auth — credential handling), ADR 013 (end-to-end identity, scopes), ADR 016.D5 (key-lifecycle UX patterns reused for rotation),
`src/movate/providers/*` (each provider already accepts an `api_key=` param, defaulting to the `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env var), the `StorageProvider` Protocol, the Teams bot's Fernet at-rest encryption (`cryptography` + `MOVATE_TEAMS_ENCRYPTION_KEY`)

---

## Decision

Let **each tenant store and manage its own provider API keys** (OpenAI, Anthropic,
…) — "bring your own key" (BYOK) — instead of every tenant sharing one
fleet-level key. Resolution is **tenant-key-first with a shared-key fallback**, so
the change is **additive and back-compatible**.

1. **(D1) Per-tenant key store, encrypted at rest.** A new tenant-scoped store
   keeps each tenant's provider keys **encrypted** (Fernet, reusing the Teams
   bot's pattern; data key from env/KMS). Behind the `StorageProvider` Protocol
   → portable across sqlite/postgres. The plaintext key is **never** persisted,
   **never** returned by any API, and **redacted** in logs/traces.
2. **(D2) A `ProviderKeyResolver` seam in the execution path.** At run time the
   Executor resolves the key for `(tenant_id, provider)`:
   **(a)** the tenant's own key (decrypted) → **(b)** the shared fleet key
   (env/KV) **iff** `MOVATE_ALLOW_SHARED_PROVIDER_KEY` is set (default: **on**,
   for back-compat) → **(c)** a clear `no API key configured for '<provider>'`
   error. The resolved key is passed into the provider's existing `api_key=`
   parameter — no provider surgery. The resolver is an adapter seam: a future
   per-tenant cloud-KMS / Key-Vault backend slots in behind it without touching
   callers.
3. **(D3) Self-service UX.** Operators manage *their own tenant's* keys:
   - CLI: `mdk keys set <provider>` (prompt hidden, never echoed),
     `mdk keys list` (configured providers + a masked fingerprint, never the
     value), `mdk keys delete <provider>`, `mdk keys test <provider>` (optional
     live validation).
   - API: `PUT /api/v1/provider-keys/{provider}` (set; **`admin`** scope; value
     never returned), `GET /api/v1/provider-keys` (list configured + fingerprints),
     `DELETE /api/v1/provider-keys/{provider}`.
   - Playground: a "Your API keys" settings card over the same API.
4. **(D4) Isolation is per-tenant** — movate's existing isolation unit (an API
   key already maps to a tenant; runs/feedback/registry are tenant-scoped). A
   finer per-identity-within-a-tenant scope is explicitly **out of scope** for
   v1 (revisit if a single tenant needs separate per-human billing).

In one sentence: **a tenant's LLM spend, blast radius, and rotation are its own —
resolved tenant-key-first, with the shared fleet key as a back-compat fallback.**

---

## Context

Today provider keys are **process-global**: `providers/openai_native.py` /
`providers/anthropic.py` read `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` from the
environment (or an `api_key=` passed at construction). Every tenant on a runtime
shares one key. Problems for a multi-user platform:

- **No cost attribution** — all tenants bill to one provider account; you can't
  tell who spent what, and one noisy tenant inflates the shared bill.
- **Shared blast radius** — if the one key leaks or is rate-limited/revoked,
  **every** tenant is affected at once.
- **No self-service** — onboarding a tenant means an operator editing fleet
  config / Key Vault; tenants can't bring their own account.

The seam to fix it is already present (providers accept `api_key=`), and the
encryption pattern already exists (Teams Fernet), so this is mostly composition.

| Force | Weight |
|-------|--------|
| **Secret hygiene** — customer keys encrypted at rest, never returned, never logged | HIGH |
| **Tenant isolation (ADR 013)** — a tenant's key is readable/writable only by that tenant's admin | HIGH |
| **Back-compat** — existing single-key deployments must keep working untouched | HIGH |
| **Cloud portability (ADR 001)** — encryption-at-rest must work with no cloud dep; KMS is an optional adapter | MED |
| **Cost attribution** — per-tenant keys → per-tenant provider billing | MED |

---

## Decisions in detail

### D1 — Encrypted per-tenant store
New `tenant_provider_keys` table: `(tenant_id, provider)` primary key, plus
`ciphertext` (Fernet token), `fingerprint` (e.g. last 4 chars of the key for the
masked display), `created_by`, `created_at`, `updated_at`. Encrypt with a
**data-encryption key** the operator supplies via `MOVATE_PROVIDER_KEY_SECRET`
(Fernet key); the same `cryptography` dep the Teams extra already pulls. The
plaintext is encrypted at the edge before `save_*` and decrypted only inside the
resolver — it never lands in a row, an API response, or a log line.

### D2 — `ProviderKeyResolver`
`resolve(tenant_id, provider) -> str | None` with the precedence in the Decision.
Wired where the Executor constructs the provider for a run, passing the resolved
key into the existing `api_key=`. `MOVATE_ALLOW_SHARED_PROVIDER_KEY` defaults
**on** (a tenant with no key transparently uses the fleet key — today's behavior);
set it off to **require** per-tenant keys (strict isolation; a keyless tenant
gets a clean error instead of silently using the shared key).

### D3 — Lifecycle + hardening
- Keys are **set/rotated** in place (a new `set` overwrites + re-fingerprints);
  reuse ADR 016.D5's rotation ergonomics (grace overlap is unnecessary here since
  the provider key is swapped atomically).
- `GET` lists *which* providers a tenant has configured + the masked fingerprint
  — never the secret. `mdk keys test` does a cheap provider call (e.g. a 1-token
  completion / models list) to validate before relying on it.
- Redaction: the key is added to the tracer/loggers' scrub set.

### D4 — Scope
Per-tenant. `PUT`/`DELETE` gate on `admin` (a tenant admin manages that tenant's
keys); `GET` on `read`. All queries tenant-scoped off the `AuthContext` —
cross-tenant access 404s, never leaks existence.

---

## Consequences

**Positive**
- Per-tenant **cost attribution** + **blast-radius isolation**; one tenant's key
  issue never affects another.
- **Self-service onboarding** — a tenant brings its own provider account via
  `mdk keys set` / the API / the playground card.
- **Additive**: shared-key deployments keep working unchanged (fallback on).
- **Portable**: encryption-at-rest needs no cloud dep; a cloud-KMS backend is an
  optional adapter behind the resolver (Deva sign-off, ADR 001).

**Negative / risks**
- A new class of secret at rest → encryption-key management becomes operationally
  important (lose `MOVATE_PROVIDER_KEY_SECRET` → tenants must re-enter keys).
  Mitigation: document key-management; KMS-backed option later.
- Per-tenant keys must be **scrubbed everywhere** (logs, traces, error bodies) —
  a redaction-coverage test is required.
- Slight run-path cost: one resolver lookup per run (cache per-process within a
  request window if needed).

**Net-new:** the `tenant_provider_keys` table + store methods, the
`ProviderKeyResolver`, the `mdk keys` CLI + `/api/v1/provider-keys` endpoints, and
the redaction coverage. **No new dependency** (Fernet via the existing
`cryptography`).

---

## Implementation plan (one focused PR, after this ADR)
1. `tenant_provider_keys` store (encrypt-at-rest) across base/sqlite/postgres +
   InMemory double.
2. `ProviderKeyResolver` + wire it into the Executor's provider construction;
   `MOVATE_ALLOW_SHARED_PROVIDER_KEY` (default on).
3. `mdk keys set|list|delete|test` + `/api/v1/provider-keys` CRUD (admin/read
   gated, value never returned, fingerprints only).
4. Redaction coverage + tests: encrypt/decrypt round-trip; resolver precedence
   (tenant → shared → error); tenant isolation; secret never returned/logged;
   back-compat (no tenant key + fallback on = today's behavior).

## Alternatives considered
- **Per-tenant Key Vault / cloud KMS as the primary store** — rejected as the
  *default* (cloud lock-in, ADR 001); kept as an optional adapter behind D2.
- **Per-identity-within-tenant keys** — deferred (D4): more complex resolution,
  not needed for the multi-tenant unit today.
- **Status quo (shared key only)** — rejected: no isolation, no attribution, no
  self-service — the explicit gap this ADR closes.
