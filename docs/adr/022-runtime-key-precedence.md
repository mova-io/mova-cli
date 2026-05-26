# ADR 022 — Runtime-bearer keys: `~/.movate/credentials` is authoritative, not the shell

**Status:** Proposed
**Date:** 2026-05-25
**Deciders:** Engineering (auth / credentials resolution)
**Context window:** v1.0 inner loop — kill the single most common operator auth
failure ("I saved/rotated a runtime key but every call still 401s"), without
breaking the universal env-overrides-config convention for user-owned provider
secrets.
**Related / constrained by:** ADR 013 (auth resilience, scopes, `mdk login`),
ADR 012 (auth resilience baseline), ADR 018 (per-tenant provider keys),
`movate.credentials.loader.autoload_credentials` / `key_source`, the
`CredentialsStore`, and the shipped shell-shadow *warnings* (#85) and the
`mdk fix unshadow-runtime-keys` *remediation* (#96).

## Decision

**Split credential-resolution precedence by credential class.** For **runtime
bearer keys** — the `MDK_<TARGET>_KEY` tokens that `mdk` itself mints, saves, and
rotates into `~/.movate/credentials` — the **saved file (or keychain) value is
authoritative** and wins over a plain shell-exported `MDK_<TARGET>_KEY`. For
**provider keys** (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) — secrets the *user*
owns and routinely exports — the **existing convention is unchanged**: shell env >
project `.env` > credentials file.

Concretely, runtime-bearer resolution becomes:

1. **Explicit CLI input** (`--key` / `--key-stdin`) — always wins (unchanged).
2. **Saved value** in `~/.movate/credentials` / keychain — **authoritative**
   (NEW: beats a plain shell `MDK_<TARGET>_KEY`).
3. **Shell `MDK_<TARGET>_KEY`** — used **only when there is no saved value** for
   that target (preserves pure-shell / CI workflows that never ran
   `mdk auth save-runtime-key`).

When a saved value AND a *differing* shell value both exist, `mdk` uses the saved
value and emits **one loud line** naming the shadow and how to reconcile it —
never a silent 401. This inverts today's "shell always wins" rule **only** for the
`MDK_<TARGET>_KEY` class, and **only** when a saved value actually exists.

## Context

The recurring, reproduced failure: an operator runs `mdk auth save-runtime-key
dev -` (or `mdk deploy` auto-mints/rotates a key into the file), but a **stale**
`export MDK_DEV_KEY=…` left in their shell profile (`~/.zshrc`) shadows it. Every
subsequent `mdk run --target dev` / `mdk deploy` sends the stale bearer and 401s.
The saved key — the one `mdk` itself just wrote and considers current — never takes
effect. The operator has no obvious signal that a shell var is the culprit; the
fix is the non-obvious `unset MDK_DEV_KEY` + edit `~/.zshrc`.

Today's precedence is uniform: `autoload_credentials()`
(`movate/credentials/loader.py`) **never clobbers** an already-set env var, so
shell > `.env` > file for *every* credential, including `MDK_*_KEY`. That rule is
correct for **provider keys** — they are user-owned, commonly exported, and the
whole world expects `OPENAI_API_KEY=… cmd` to override a config file. But it is
**wrong for runtime bearers**, because:

- The credentials file is the store `mdk` *writes to* — `save-runtime-key`,
  `deploy` auto-recovery, `pull-runtime-key`, and key rotation all persist there.
  If the thing `mdk` writes can be silently overridden by a value `mdk` can't see
  the provenance of, the file is not trustworthy as a source of truth.
- A shell-exported `MDK_<TARGET>_KEY` is almost never *intentional* day-to-day —
  it is leftover from a one-off debugging session or an old profile line. The one
  legitimate pure-shell case (CI, ephemeral containers) has **no credentials
  file**, so it lands on rule 3 and is unaffected.

We already shipped the *passive* half of the cure — shell-shadow **warnings** at
`auth status` / `login` / `deploy` (#85) and the `mdk fix unshadow-runtime-keys`
profile-line remediation (#96). Those help, but they are advisory: the operator
still hits the wall first, and `mdk fix` cannot `unset` a var in the parent shell.
This ADR makes the **resolution itself** do the right thing.

## Decisions in detail

### D1 — Class-split precedence (the core call)
Resolution precedence is no longer uniform. Runtime bearers (`MDK_<TARGET>_KEY`,
matched by the existing `_looks_like_runtime_key_env` shape detector) are
**file-authoritative** per the 3-rule order above. Provider keys, notification
secrets, and observability keys keep **shell > `.env` > file**. The split is the
decision: the two classes have different owners (mdk vs. the user) and therefore
different "who is the source of truth" answers.

### D2 — Saved-wins is conditional, never silent
File-authority for runtime bearers applies **only when a saved value exists**. No
saved value → the shell `MDK_<TARGET>_KEY` is used exactly as today (CI / pure-shell
unbroken). When both exist and differ, `mdk` uses the saved value **and prints one
actionable line** (stderr), e.g.:

```
⚠ ignoring stale $MDK_DEV_KEY in your shell — using the key saved in
  ~/.movate/credentials. To make the shell value win, persist it:
  `mdk auth save-runtime-key dev -`, or clear the saved key.
```

This is the anti-footgun: the override is observable, reversible, and self-
explaining. (A matching value is a silent no-op — nothing to warn about.)

### D3 — Escape hatches stay first-class
Intentional override paths remain, in priority order: (1) `--key` / `--key-stdin`
on the command always wins; (2) to make a shell value the durable truth, persist it
with `save-runtime-key` (then it *is* the saved value); (3) to fall back to the
shell, clear the saved key (`mdk auth forget-runtime-key <target>` / edit the file).
We deliberately do **not** add a new override env var (e.g.
`MDK_<TARGET>_KEY_OVERRIDE`) — the "file exists → file wins, else shell" rule
already yields a clean, low-surface escape hatch.

### D4 — `auth status` attribution + compat surfacing
`key_source()` (loader.py) gains a state for "shell value present but **shadowed**;
saved key won" so `mdk auth status` renders the truth, not a misleading "shell"
attribution. This is a **behavior change to credential resolution** (CLAUDE.md
compat rule 5, `MDK_*` env-var semantics) — it ships with a CHANGELOG entry, is
called out in `mdk auth status`, and is gated to the `MDK_<TARGET>_KEY` class so no
provider-key or third-party-tool behavior changes.

### D5 — Scope: this ADR decides, a follow-up task implements
This ADR is the *decision*; implementation is a separate task (not tonight). The
implementation touches `autoload_credentials` (clobber-with-notice for runtime-key
vars when a differing saved value exists), `key_source` (the shadowed-but-overridden
state), and the runtime-bearer read at the `run --target` / `deploy` call sites. It
must land with tests for the five cases in the matrix below.

## Consequences

**Positive**
- Kills the #1 recurring auth failure: a saved/rotated runtime key takes effect
  immediately, regardless of a stale shell export.
- The credentials file `mdk` writes to becomes a **trustworthy** source of truth for
  the credentials `mdk` manages — rotation and `deploy` auto-recovery "just work."
- Backward-compatible for the dominant pure-shell/CI path: **no file → shell still
  wins**, unchanged. The inversion bites only the exact broken scenario.
- Complements, rather than duplicates, #85 (warnings) and #96 (`mdk fix`): those
  remain useful for cleaning up the *persistent* shell-profile source; this fixes the
  *live* resolution.

**Negative / risks**
- A genuine behavior change to credential resolution (compat rule 5), scoped to
  `MDK_<TARGET>_KEY`. Operators who *deliberately* exported a per-shell runtime key
  while *also* having a saved one will now get the saved one — surfaced loudly (D2)
  with a one-line reconcile, but still a change.
- Asymmetry to explain: provider keys follow env-wins, runtime bearers follow
  file-wins. Mitigated by docs + `auth status` attribution; justified by the
  different ownership of the two classes.
- Resolution logic gets a branch (class-dependent precedence) — covered by the
  D5 test matrix; the `_looks_like_runtime_key_env` detector already exists, so the
  classification seam is in place.

**Test matrix (must all be covered by the impl task):**
file-only → file; shell-only (no file) → shell (unchanged); both-equal → silent;
both-differ → file value + one notice; provider key with both set → shell
(unchanged, proves the class split).

## Alternatives considered

- **(a) Keep env-wins; rely on warnings + `mdk fix` only.** The shipped #85/#96
  path. *Rejected as the end-state* (kept as the complementary cleanup for the
  persistent profile source): warnings are passive — the operator still 401s first,
  and `mdk fix` cannot `unset` the live shell var, so the live session stays broken
  until the operator acts manually.
- **(b) Invert precedence for ALL credentials (provider keys included).** Uniform
  "file wins." *Rejected:* violates the universal env-overrides-config convention for
  user-owned provider secrets; breaks `OPENAI_API_KEY=… mdk run` one-offs and CI
  that exports provider keys; large blast radius for no benefit (provider-key
  shadowing is not the reported pain).
- **(c) Freshness / timestamp-based "newest key wins."** *Rejected:* a shell env var
  carries no timestamp; "which is newer" is unknowable, and any heuristic would be
  surprising and non-deterministic.
- **(d) Dedicated override env var (`MDK_<TARGET>_KEY_OVERRIDE`).** A second var that
  intentionally wins while plain `MDK_<TARGET>_KEY` defers. *Rejected:* extra env
  surface and docs burden; the D3 "file exists → file wins, else shell" rule already
  provides a clean escape hatch without a new variable.
