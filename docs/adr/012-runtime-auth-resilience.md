# ADR 012 — Run-side authentication resilience (401 recovery, durable key storage, optional OIDC)

**Status:** Proposed
**Date:** 2026-05-22
**Deciders:** Engineering (auth/security change — Deva sign-off required for the optional OIDC client dependency, per ADR 001)
**Context window:** v1.0 Azure operability — "make auth to deployed agents better"
**Supersedes:** N/A
**Related:** ADR 001 (cloud-portability — *constrains* this ADR),
`src/movate/cli/kb_cmd.py` (`_resolve_target_bearer`, `_remote_request`),
`src/movate/cli/auth.py` (`refresh_runtime_key_inline`, `save-/refresh-runtime-key`),
`src/movate/runtime/middleware.py` (`make_auth_dependency`),
`src/movate/core/auth.py` (`mint_/parse_api_key`, `check_record`),
`src/movate/credentials/store.py` (`CredentialsStore`),
`src/movate/core/user_config.py` (`TargetConfig`), `infra/azure/modules/containerapp-api.bicep`

---

## Decision

Make authenticating the CLI against a deployed runtime **resilient and less
brittle**, in three additive pillars — **none of which changes the default
opaque-key auth path or breaks existing credentials/keys**:

1. **(a) Automatic 401 recovery.** When a `--target` call returns 401, the CLI
   transparently attempts **one** programmatic key refresh (reusing the existing
   `refresh_runtime_key_inline`) and retries the request once — *only* when the
   target is refresh-capable and its backend is durable; otherwise it falls back
   to today's manual hint. Never loops, never refreshes blindly.
2. **(b) Durable / secure key storage.** Add an **optional OS-keychain backend**
   behind the existing `CredentialsStore` seam (macOS Keychain / Windows
   Credential Manager / Linux Secret Service via `keyring`). The plaintext
   `~/.movate/credentials` (0600) **stays the default and the fallback** — the
   keychain is opt-in, additive, and the file format is unchanged.
3. **(c) Optional OIDC auth (generic — Azure AD is one issuer, not the design).**
   The runtime learns to accept an **OIDC JWT bearer in addition to** opaque
   `mvt_*` keys: detect token shape, validate a JWT against a configured issuer's
   JWKS, map claims → tenant. A target may opt into `auth: oidc`; the CLI obtains
   the token via a pluggable token provider (Azure `DefaultAzureCredential` being
   one **optional, adapter-isolated, only-loaded-when-used** implementation).
   **Opaque keys remain the portable default.**

In one sentence: **"401s self-heal once, keys can live in the OS keychain, and
the runtime can additionally trust any OIDC issuer — all opt-in, with the
portable opaque-key path unchanged and still the default."**

---

## Context

Today the CLI authenticates to a deployed runtime with an **opaque bearer key**
(`mvt_<env>_<tenant8>_<keyid12>_<secret>`), minted by `mdk auth create-key`,
stored in `~/.movate/credentials` (`.env`-format, mode 0600), autoloaded into
`MDK_<TARGET>_KEY` env vars at startup, and looked up server-side against a
salted-hash record in the `api_keys` storage table (`middleware.py` →
`core/auth.check_record`). This design is deliberate and good: per-key salt+hash
(no shared signing secret), backend-portable, no JWT rotation burden.

Three rough edges hurt day-to-day Azure operations:

* **401s dead-end.** `_remote_request` (kb_cmd.py) and the auth.py/`run` HTTP
  call sites treat 401 as fatal: print "run `mdk auth refresh-runtime-key`",
  `raise typer.Exit(2)`. A key expiring mid-session (TTL defaults to 90 days) or
  a Container App pod recycle that dropped an ephemeral-SQLite key forces a
  manual context-switch. The refresh primitive **already exists**
  (`refresh_runtime_key_inline`, used by `mdk deploy`) — it's just not wired into
  the 401 path.
* **Keys sit in plaintext.** `~/.movate/credentials` is a 0600 `.env` file.
  Adequate for a laptop, but enterprise operators increasingly expect secrets in
  the OS keychain (and some endpoint-DLP policies flag plaintext key files).
* **Only per-tenant opaque keys exist.** Customers running their own IdP (Entra,
  Okta, Google Workspace) can't currently present a federated token to the
  runtime; they must hand-mint and distribute `mvt_*` keys. ADR 001 already
  anticipates this: *"Authentication accepts any OIDC provider… customers can
  plug their own IdP later… without changing the code."* That promise isn't
  built yet.

This ADR is the *direction* for closing those three gaps **without** regressing
the portable default or the existing security properties.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Backward compatibility** — existing `mvt_*` keys, credential files, and the `/api/v1` auth contract must keep working untouched | HIGH |
| **Cloud portability (ADR 001)** — no Azure-AD-*only* auth; OIDC must be generic; cloud SDKs stay optional + adapter-isolated | HIGH |
| **Security posture** — never weaken the salted-hash key model; auto-recovery must not mint/leak keys carelessly; keychain must be genuine hardening | HIGH |
| **Operational friction** — a 401 mid-session shouldn't require a manual command + re-run | MED |
| **Minimal dependencies** — new deps (`keyring`, `PyJWT`, `azure-identity`) must be optional extras, license-clean, and justified | MED |

---

## Architecture

```
            ┌──────────────────────────── CLI (control plane) ───────────────────────────┐
            │                                                                             │
  target ──▶│ _resolve_target_bearer(target)                                              │
            │      ├─ auth: key  (default) → MDK_<T>_KEY  ◀── CredentialsStore             │
            │      │                                          ├─ file backend (default)    │
            │      │                                          └─ keychain backend (opt-in) │  ◀── (b)
            │      └─ auth: oidc (opt-in)  → OidcTokenProvider.get_token()                 │  ◀── (c) client
            │                                   └─ AzureCliCredential / DefaultAzure… (adapter, optional dep)
            │                                                                             │
            │ _remote_request(...)  ── 401 ──▶ recover_once(target):                       │  ◀── (a)
            │        │                          refresh-capable + durable backend?         │
            │        │                            yes → refresh_runtime_key_inline; retry 1│
            │        ▼                            no  → today's manual hint, Exit(2)        │
            └────────┼────────────────────────────────────────────────────────────────────┘
                     ▼  Authorization: Bearer <token>
            ┌──────────────────────────── Runtime (execution plane) ─────────────────────┐
            │ make_auth_dependency:  token shape?                                          │
            │     ├─ "mvt_…"  → parse_api_key → storage.get_api_key → check_record  (today)│
            │     └─ "eyJ…"   → OIDC validate (JWKS@MOVATE_OIDC_ISSUER, aud, exp)   ◀── (c) │
            │                     → AuthContext(tenant from claims)                         │
            └─────────────────────────────────────────────────────────────────────────────┘
```

The seams already exist: `CredentialsStore` (storage backend swap), token-shape
branch in `make_auth_dependency`, and `refresh_runtime_key_inline` (recovery).
The new code is small and lands behind these seams.

---

## Decisions

### Decision 1 (D1): 401 → one guarded auto-refresh + retry, in a shared helper

Factor the duplicated `httpx` call/`raise typer.Exit(2)` blocks (kb_cmd.py
`_remote_request`, auth.py `_list_keys_remote`/`whoami`, the `run --target`
path) into **one** client helper. On 401 it attempts recovery **exactly once**:

- **Guard 1 — refresh-capable target.** The target must resolve to an Azure
  Container App we can `az containerapp exec` against (the same precondition
  `refresh_runtime_key_inline` already enforces). Non-Azure / unknown targets
  skip straight to the manual hint.
- **Guard 2 — durable backend.** Auto-refresh is pointless against an ephemeral
  SQLite runtime (a freshly minted key dies on the next pod recycle). Recovery
  only engages when the target is known to use durable Postgres; otherwise the
  manual hint (today's behavior) is the honest answer.
- **One shot only.** A second 401 after a refresh is fatal (`Exit(2)`) — no
  loops, no thundering-herd of `az exec` calls.

Rejected: blind refresh-on-every-401 (masks real misconfig, hammers `az exec`);
client-side token caching with proactive expiry (we don't hold key TTL locally,
and the server is the source of truth).

### Decision 2 (D2): Keychain is an **optional backend behind `CredentialsStore`**, file stays default

Introduce a `CredentialBackend` seam with two impls: the current **file backend
(default)** and a **keychain backend** (`keyring`, opt-in via
`mdk auth use-keychain` / `MOVATE_CRED_BACKEND=keychain`). `read/get/set/delete`
keep their signatures; `loader.autoload_credentials` is unchanged (it still just
fills env vars). Migration is opt-in and reversible; we never silently move a
user's keys out of their file.

- `keyring` is a **new shipped dep → opt-in `pyproject.toml` extra**
  (`mdk[keychain]`), license-checked (`scripts/check_licenses.py`), absent by
  default so the core install stays lean and portable (headless CI/containers
  have no keychain).
- Portability (ADR 001): the keychain backend is an isolated adapter, only
  loaded when selected — exactly the boto3/azure-storage-blob carve-out ADR 001
  already allows.

Rejected: encrypt-the-file-with-a-derived-key (re-implements a keychain badly,
still needs a master secret somewhere); mandatory keychain (breaks headless).

### Decision 3 (D3): Runtime accepts OIDC JWT **in addition to** opaque keys — generic, issuer-configured

In `make_auth_dependency`, branch on token shape: `mvt_…` → today's path
unchanged; JWT (`eyJ…`) → validate signature against the JWKS of a configured
issuer (`MOVATE_OIDC_ISSUER`), check `aud` (`MOVATE_OIDC_AUDIENCE`) and `exp`,
and derive `tenant_id` from a configured claim. OIDC is **off unless
`MOVATE_OIDC_ISSUER` is set**, so existing deployments are byte-for-byte
unaffected.

- **Generic, not Azure-bound.** Azure AD / Entra is *one* value of
  `MOVATE_OIDC_ISSUER`; Okta / Google / Keycloak work identically. This is the
  literal ADR 001 promise ("accepts any OIDC provider").
- **Server dep:** `PyJWT[crypto]` (+ JWKS fetch/cache) — permissive license,
  runtime extra. Validation is standard RFC 7519 / OIDC discovery.
- **Tenant mapping** is explicit config (`MOVATE_OIDC_TENANT_CLAIM`), not
  hardcoded, so it fits any IdP's claim scheme.

Rejected: replacing opaque keys with OIDC (kills the portable, no-IdP-needed
default and the offline/dev story); accepting unsigned/`alg:none` or
issuer-wildcard tokens (security hole).

### Decision 4 (D4): Client OIDC token acquisition is a **pluggable provider; Azure SDK is an optional adapter**

A target's `TargetConfig` may set `auth: oidc`. `_resolve_target_bearer` then
calls an `OidcTokenProvider` instead of reading `MDK_<T>_KEY`. The default
provider shells out to **already-present tooling** (`az account get-access-token`
via the Azure CLI the operator already uses for `refresh_runtime_key_inline`) so
**no new dependency is required** for the common Azure case. A richer
`DefaultAzureCredential` provider (managed identity, env creds, etc.) is offered
behind an **optional `mdk[azure-identity]` extra**.

- **`azure-identity` requires Deva sign-off** (ADR 001 dependency rule): it's a
  cloud-specific SDK. It is acceptable *because* it's isolated behind the
  `OidcTokenProvider` adapter and only imported when a target opts into the
  Azure provider — the same standard ADR 001 applies to `boto3`. This ADR
  records the request; the dep doesn't land until signed off.

### Decision 5 (D5): No change to key format, the `api_keys` schema, or the default contract

`mint/parse/check` for `mvt_*` keys, the `api_keys` table, the credential file
format, and the existing `MDK_<T>_KEY` env-var contract are **untouched**. All
three pillars are strictly additive and default-off. This keeps the blast radius
small and the upgrade a no-op for anyone who doesn't opt in.

---

## Consequences

**Positive**
- A mid-session 401 self-heals once instead of dead-ending — the common
  "expired key / pod recycle" friction disappears for durable Azure targets.
- Operators who want it can keep runtime keys in the OS keychain; plaintext-file
  DLP findings go away without forcing the change on everyone.
- The ADR-001 OIDC promise becomes real: customers federate their own IdP
  (Entra/Okta/Google) without us minting/distributing per-tenant keys — and it's
  generic, so we're not Azure-locked.

**Negative / costs**
- Three new optional deps to vet/license-gate (`keyring`, `PyJWT[crypto]`,
  `azure-identity`), each in an opt-in extra. More extras = more install-matrix
  surface to test.
- Auto-recovery adds a "is this target durable + refresh-capable?" probe and a
  retry path — more client states to reason about and test (especially the
  "refresh succeeded but still 401" case).
- OIDC validation introduces JWKS fetch/cache + clock-skew handling in the
  runtime hot path; misconfigured issuer/audience is a new class of 401.

**Neutral**
- New env vars (`MOVATE_OIDC_ISSUER/AUDIENCE/TENANT_CLAIM`, `MOVATE_CRED_BACKEND`)
  and `TargetConfig.auth` — all additive, documented, default-off. (Flagged per
  CLAUDE.md rule 5: these are new `MOVATE_*`/config surfaces, backward-compatible.)

---

## Implementation plan (separate PRs, after this ADR is accepted)

1. **(a) 401 auto-recovery.** Extract the shared client helper; wire one guarded
   refresh+retry (D1). Pure client-side; no new deps. Tests: 401→refresh→200,
   401→refresh→401 fatal, non-durable/non-Azure target skips to manual hint.
2. **(b) Keychain backend.** `CredentialBackend` seam + `keyring` adapter behind
   `mdk[keychain]`; `mdk auth use-keychain` migration (opt-in, reversible). File
   stays default. Tests: backend round-trip, file fallback when keychain absent,
   loader unaffected.
3. **(c-server) OIDC acceptance.** Token-shape branch + JWKS validation in
   `make_auth_dependency` behind `MOVATE_OIDC_ISSUER`; `PyJWT[crypto]` runtime
   extra. Tests: valid JWT→AuthContext, bad aud/iss/exp/alg→401, opaque path
   unchanged when OIDC unset.
4. **(c-client) OIDC token provider.** `TargetConfig.auth: oidc` +
   `OidcTokenProvider` (default = `az account get-access-token`, no new dep).
   The `azure-identity` provider lands **only after Deva sign-off** (D4).
5. **Docs:** auth model page (key vs. OIDC), keychain how-to, the recovery
   semantics; `infra/azure` notes for setting the OIDC issuer on the Container App.
