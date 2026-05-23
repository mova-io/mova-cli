# ADR 013 — End-to-end identity: `mdk login` (SSO), scopes, and an optional edge gateway

**Status:** Proposed
**Date:** 2026-05-23
**Deciders:** Engineering (auth/security — Deva sign-off required for the IdP + gateway choices, per ADR 001)
**Context window:** v1.0 Azure operability — "make accessing live endpoints easy + auth scalable/reliable/secure"
**Supersedes:** N/A
**Builds on:** ADR 012 (run-side auth resilience — the runtime *accepts* OIDC JWTs; this ADR adds the *human login* + *authorization* + *front door* around it)
**Related / constrained by:** ADR 001 (cloud-portability),
`src/movate/runtime/middleware.py` (`make_auth_dependency`, `AuthContext.scope`),
`src/movate/core/auth.py` (`mint/parse/check`, `ApiKeyRecord`),
`src/movate/runtime/oidc.py` + `src/movate/core/oidc_provider.py` (ADR 012c),
`src/movate/core/rate_limit.py` (`RateLimiter` Protocol),
`src/movate/core/user_config.py` (`TargetConfig.auth`), `infra/azure/`

---

## Decision

Add three **additive** identity layers on top of ADR 012, so that **humans use
short-lived federated identity, machines keep opaque keys, every call is checked
against a least-privilege scope, and an optional portable gateway provides a
stable front door** — while the runtime's own portable auth stays the source of
truth:

1. **(L1) `mdk login` — interactive SSO via OIDC.** A human runs
   `mdk auth login --target prod` and authenticates against the configured IdP
   (Entra / Okta / Google / Keycloak — generic) via the **device-code** flow
   (browser auth-code+PKCE as an alternative). The resulting **short-lived**
   token is cached (OS keychain, ADR 012b) and auto-refreshed; the runtime
   already validates it (ADR 012c). Opaque `mvt_*` keys remain — now scoped to
   **machine / CI** use.

2. **(L2) A first-class scope/authorization model.** Replace today's ad-hoc
   `scope: str` (only `"fleet-admin"` is meaningful) with a defined,
   **least-privilege** scope set carried on *both* opaque keys (key record) and
   OIDC tokens (mapped from a configured claim), enforced uniformly by a
   `require_scope(...)` dependency on each endpoint group.

3. **(L3) An optional edge gateway, as a portable adapter — not the source of
   truth.** A gateway tier (Azure API Management on Azure; Envoy/Traefik for
   other clouds / self-host) provides a stable custom domain, a **self-serve
   developer portal** (publishes the OpenAPI + onboarding), **edge JWT
   pre-validation**, **shared throttling**, and a **WAF**. It *forwards* the
   client's bearer to the runtime, which re-validates authoritatively
   (defense-in-depth). It introduces **no new credential type**.

In one sentence: **"humans log in with SSO (short-lived tokens), machines keep
scoped opaque keys, every request is checked against a least-privilege scope,
and an optional portable gateway gives a stable front door + dev portal — all
additive, with the runtime's portable auth still authoritative."**

---

## Context

After ADR 012, the runtime has two portable auth paths: opaque `mvt_*` keys
(salted-hash row in `api_keys`, looked up per request) and — opt-in — OIDC JWT
acceptance (stateless, JWKS-validated). What's still missing for *operating*
this at scale:

* **Human onboarding is manual + insecure.** An operator mints a 90-day key
  (`az containerapp exec`) and hands it to the user (Slack/email); the user
  manages an env var. The runtime can *accept* an OIDC token, but **nothing
  obtains one for a user** — the login half of SSO doesn't exist yet.
* **Authorization is coarse.** `AuthContext.scope` is effectively a
  `fleet-admin` boolean; the Angular BFF's "fleet key" is all-tenant
  admin-elevated — a fat target with no least-privilege story.
* **No stable front door / discovery.** Consumers hit the Container App ingress
  hostname directly; there's no custom domain, no published dev portal, no
  edge throttling/WAF.
* **Long-lived secrets.** 90-day human keys (no MFA, no IdP revocation), a
  shared fleet key, and a static bootstrap key in Key Vault.

This ADR is the *direction* for closing those — humans → short-lived SSO,
least-privilege everywhere, a portable front door — **without** regressing the
opaque-key default or the portability/security properties ADR 001/012 set.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Cloud portability (ADR 001)** — IdP-agnostic OIDC; gateway + IdP are optional, adapter-isolated; no Azure-AD-*only* auth | HIGH |
| **Security posture** — short-lived federated tokens > long-lived keys; least privilege; remove static service secrets | HIGH |
| **Backward compatibility** — existing `mvt_*` keys, the `api_keys` schema, and the `/api/v1` contract must keep working untouched | HIGH |
| **Onboarding / DX** — a new consumer should self-serve, not wait on an operator to hand over a key | HIGH |
| **Operability at scale** — edge throttling + WAF; offload token validation; no per-pod-limit surprises for external traffic | MED |
| **Minimal dependencies** — device-code is plain OIDC HTTP; avoid mandatory cloud SDKs (`msal`/`azure-identity` stay optional) | MED |

---

## Architecture

```
  ┌─────────────────────────── client ────────────────────────────┐
  │ human:   mdk auth login --target prod                          │
  │            └─ OIDC device-code → IdP (Entra/Okta/…) → token     │
  │               cached in OS keychain (ADR 012b), auto-refreshed  │
  │ machine: mvt_<env>_<tenant>_<keyid>_<secret>  (scoped, rotatable)│
  └───────────────────────────────┬────────────────────────────────┘
                                   │  Authorization: Bearer <token>
            ┌──────────────────────▼───────────────────────┐
            │ (L3) OPTIONAL gateway (APIM | Envoy/Traefik)   │  ◀── adapter, not
            │  custom domain · dev portal · edge JWT check   │      source of truth
            │  shared throttle · WAF  → forwards the bearer  │
            └──────────────────────┬───────────────────────┘
                                   │  (same bearer, unchanged)
            ┌──────────────────────▼───────────────────────┐
            │ runtime  make_auth_dependency (ADR 012 branch):│
            │   mvt_… → key path (get_api_key/check_record)  │
            │   eyJ…  → OIDC validate (JWKS, aud/iss/exp)     │
            │        ▼                                        │
            │   (L2) require_scope(<needed>) on the route     │  ◀── least privilege
            │        ▼  AuthContext(tenant, scopes, …)        │
            └────────────────────────────────────────────────┘
```

The runtime auth (the ADR 012 token-shape branch) stays authoritative even
behind the gateway — the gateway is defense-in-depth + DX. The seams already
exist: `TargetConfig.auth`, the token-shape branch, `AuthContext.scope`.

---

## Decisions

### Decision 1 (D1): `mdk login` uses generic OIDC device-code (auth-code+PKCE optional)

`mdk auth login --target <t>` runs the **OIDC device-authorization** flow
(RFC 8628): request a device + user code, print the verification URL, poll the
token endpoint until the user approves in a browser. Device-code is the right
default — it works over SSH / headless / CLI with no local redirect server. A
browser **auth-code + PKCE** flow is offered for desktop convenience. The flow
is **plain OIDC HTTP** (httpx + the issuer's discovery doc) — **no mandatory
cloud SDK**; `msal`/`azure-identity` remain optional adapters (ADR 012 D4). The
target opts in via `TargetConfig.auth: oidc`; the default stays `key`.

Rejected: embedding `msal` as a required dep (Azure-specific, violates ADR 001);
ROPC/password grant (no MFA, deprecated).

### Decision 2 (D2): Short-lived tokens, cached + refreshed; humans never hold long-lived keys

The access token is short-lived (IdP-controlled, typically 1h); cache it +
the refresh token per-target in the **OS keychain** (ADR 012b), refresh silently
on expiry, re-prompt `mdk auth login` only when the refresh token is gone.
`mdk auth logout` clears it; `mdk auth whoami` shows the resolved identity +
scopes. This is the security win: revocation/MFA/expiry are the IdP's job, and a
leaked laptop token dies in an hour instead of 90 days.

### Decision 3 (D3): A defined least-privilege scope set, on keys *and* tokens

Define a small, flat scope set: **`read`, `run`, `eval`, `kb:write`, `admin`,
`fleet-admin`**. Carried on:
- **opaque keys** — a `scopes` field on `ApiKeyRecord` (additive column;
  `mdk auth create-key --scope run,read`);
- **OIDC tokens** — mapped from a configured claim (`MOVATE_OIDC_SCOPE_CLAIM`,
  e.g. Entra app roles / a `scp` claim).

Enforced by a `require_scope(*needed)` FastAPI dependency layered on the auth
dependency, per endpoint group (e.g. `POST /agents` needs `admin`; `…/runs`
needs `run`; `GET …` needs `read`; `mdk fleet`-style cross-tenant needs
`fleet-admin`). **Backward compatibility is mandatory:** existing keys with no
`scopes` are treated as a **legacy default grant** (`read` + `run` + `eval`, but
**not** `admin`/`fleet-admin`) via a migration that stamps the default — no
existing integration loses access, but no legacy key silently gains admin.

Rejected: hierarchical scopes (over-engineered for the set size); per-endpoint
ACL tables (operational drift).

### Decision 4 (D4): Explicit machine-vs-human split

Document + tool the split: **SSO tokens = humans** (short-lived, MFA, scoped by
IdP role); **`mvt_*` keys = machines / CI** (long-lived but **scoped** per D3,
rotatable per D5). `mdk auth create-key` gains `--scope`; the docs steer humans
to `mdk login`. The Angular BFF moves from one all-powerful fleet key to a
**`fleet-admin`-scoped** service identity (ideally workload identity, D6).

### Decision 5 (D5): Zero-downtime key rotation + lifecycle UX

`mdk auth rotate-key` mints a new key, keeps the old valid for a grace window
(both accepted), then revokes the old — no downtime. Add pre-expiry warnings
(`mdk auth status` flags keys within N days of `expires_at`) and
`mdk auth revoke --all-for <tenant>` for compromise response. (TTL stays 90d for
machine keys; humans no longer have long-lived keys at all, per D2.)

### Decision 6 (D6): Service-to-service uses workload identity, not shared secrets

Worker→API and BFF→runtime authenticate with **workload identity** (Azure
Managed Identity + OIDC federation; portable = any OIDC workload-identity
provider) rather than a shared fleet key or the static **Key Vault bootstrap
key**. This removes two long-lived static secrets. Can land independently of
L1–L3; recorded here as part of the end-to-end identity picture. (Touches infra
+ the bootstrap path — flagged for the CODEOWNER.)

### Decision 7 (D7): The gateway is an optional adapter; the runtime stays authoritative

A gateway tier is **optional** and **not** the identity source of truth:
- **Azure:** an APIM module in `infra/azure/` (behind an `enableGateway` flag,
  mirroring `enableTeamsBot`) — custom domain, developer portal (publishes
  `/openapi.json` + self-serve onboarding), edge JWT pre-validation, **shared
  throttling** (which also blunts the per-pod in-process-limit gap for external
  traffic), WAF. It **forwards the client's bearer** (OIDC or `mvt_`) unchanged;
  the runtime re-validates (defense-in-depth).
- **Non-Azure / self-host:** the same role via Envoy/Traefik + ACME TLS —
  portable, per ADR 001 (APIM-specific config is Azure IaC, like Bicep).

**No new credential type:** we deliberately do *not* adopt APIM subscription keys
as identity (that would be a third, parallel credential to manage). The bearer
(SSO token or scoped key) is the single identity; APIM subscription keys, if
used at all, are only a coarse "known consumer" gate, never the authorization.

Rejected: making APIM mandatory / the auth source of truth (Azure lock-in,
violates ADR 001; breaks local/dev + non-Azure).

### Decision 8 (D8): No change to the opaque-key format, `api_keys` core schema, or the default contract

`mint/parse/check` for `mvt_*` keys, the wire `/api/v1` contract, and the
`MDK_<T>_KEY` env-var resolution are **untouched**. The only schema change is the
**additive** `scopes` column (D3) with a back-compat default migration. With SSO
unconfigured and no scopes set, behavior is unchanged.

---

## Consequences

**Positive**
- Humans self-serve via SSO with MFA + short-lived tokens; no more pasted 90-day
  keys or keys-over-Slack. Onboarding drops from "ask an operator" to `mdk login`.
- Least privilege everywhere: the BFF/fleet key stops being all-powerful;
  per-tenant + per-machine keys carry only the scopes they need.
- A stable front door (custom domain + dev portal + OpenAPI) and edge
  throttling/WAF for externally-exposed traffic.
- Two long-lived static secrets (shared fleet key, KV bootstrap key) removed via
  workload identity.

**Negative / costs**
- More moving parts: IdP app-registration + claim mapping, an optional gateway
  tier (APIM cost/ops), and careful **scope migration** so no existing key is
  locked out or silently elevated.
- Device-code UX has a manual browser step (acceptable for a login).
- Two credential lifetimes to reason about (short SSO vs. long machine keys).

**Neutral**
- New env/config (`MOVATE_OIDC_SCOPE_CLAIM`, gateway `enableGateway` flag,
  scope grants on keys) — all additive, default-off. (Flagged per CLAUDE.md
  rule 5: new `MOVATE_*`/schema/CLI surfaces, backward-compatible.)

---

## Implementation plan (separate PRs, after this ADR is accepted)

1. **(L2) Scopes first — it's the foundation.** Define the scope set; add the
   `scopes` field to `ApiKeyRecord` + a back-compat default migration; map the
   OIDC scope claim; add `require_scope(...)` and apply it per endpoint group;
   `mdk auth create-key --scope`. Server-side, no new deps. Tests: legacy key →
   default grant; insufficient scope → 403; OIDC claim → scopes.
2. **(L1/D1–D2) `mdk login`.** OIDC device-code client (+ optional PKCE), token
   cache in keychain, silent refresh; `mdk auth login/logout/whoami`;
   `TargetConfig.auth: oidc` end-to-end with the runtime acceptance from ADR
   012. No mandatory cloud SDK.
3. **(D5) Rotation/lifecycle UX** — `mdk auth rotate-key` (grace overlap),
   expiry warnings, bulk revoke.
4. **(D7/L3) Optional gateway** — APIM Bicep module behind `enableGateway`
   (custom domain, dev portal, edge JWT + throttle + WAF) + portable
   Envoy/Traefik docs. **Deva sign-off** (ADR 001). Largest; lands last.
5. **(D6) Workload identity for service-to-service** — replace the shared fleet
   key + static KV bootstrap secret with MI/OIDC federation. Can interleave.

(Orthogonal but related: the **shared-state rate limiter** — a Redis/Postgres
backend behind the existing `RateLimiter` Protocol — fixes the per-pod limit for
*internal/direct* traffic and complements L3's edge throttling. It needs no ADR;
tracked separately.)
