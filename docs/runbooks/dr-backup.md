# Disaster recovery: backup & restore

How to **back up and restore** a deployed movate (`mdk`). Two layers, in order
of preference:

1. **Primary DR — Azure Postgres point-in-time-restore (PITR).** Automated,
   transactionally consistent, covers **every** table (runs, jobs, evals, KB,
   threads, *and* the control-plane state below). This is what you reach for in
   an actual disaster. 🔒 The restore procedure runs against a **deployed**
   Azure Database for PostgreSQL Flexible Server.
2. **Escape hatch — `mdk export` / `mdk import`.** A portable *logical* backup
   of only the **operator-critical control-plane state** an operator can't
   easily recreate. Use it for portability (move state to a fresh
   sqlite/postgres of any version, on any cloud), seeding a new deployment, or a
   belt-and-suspenders off-Azure copy. It is **not** a substitute for PITR.

> **🔒 Production-readiness gate.** The PITR procedure below must be **drilled
> against a real deployed server** before you sign off on production readiness.
> A restore you've never run is a restore you don't have.

---

## Layer 1 — Azure Postgres PITR (the primary DR)

movate's durable state lives in **Azure Database for PostgreSQL Flexible
Server** (`infra/azure/modules/postgres.bicep`). Flexible Server takes
**automated backups continuously** and supports **point-in-time restore** to any
instant inside the retention window — no cron, no `pg_dump` to babysit.

### What's configured

| Setting | Default (bicep) | Notes |
|---|---|---|
| `backupRetentionDays` | **7** | Tunable 7–35. The PITR window. Raise it for prod (`main.bicepparam`). |
| `geoRedundantBackup` | **Disabled** | v1.0 is single-region; backups live in the server's region. Revisit at v1.1 for cross-region DR. |
| `storage.autoGrow` | **Enabled** | The disk grows before it fills — backups never fail for lack of space. |
| `highAvailability` | **Disabled** | Burstable SKU has no HA. On GeneralPurpose, flip it on for a hot standby (complements, doesn't replace, PITR). |

Retention is the single most important DR knob: **PITR can only restore as far
back as `backupRetentionDays`.** If your incident-detection-to-restore time can
exceed the window, raise it before you need it.

### Restore procedure (PITR)

A PITR creates a **brand-new server** restored to a chosen timestamp; it never
mutates the source. Plan to repoint the app at the new server afterward.

```bash
# 0. Identify the source server + a target timestamp (UTC, inside the window).
RG=movate-prod-rg
SRC=movate-prod-pg            # the live server name
NEW=movate-prod-pg-restore    # the restored server (new name)
WHEN="2026-05-25T09:15:00Z"   # the instant to restore to (just before the incident)

# 1. Restore to a new server at that instant.
az postgres flexible-server restore \
  --resource-group "$RG" \
  --name "$NEW" \
  --source-server "$SRC" \
  --restore-time "$WHEN"

# 2. Re-apply the extension allow-list the runtime needs (pgvector, ADR 009).
#    A restored server resets server parameters to defaults.
az postgres flexible-server parameter set \
  --resource-group "$RG" --server-name "$NEW" \
  --name azure.extensions --value VECTOR

# 3. Re-create the firewall rule that lets Azure-internal (ACA) traffic in.
az postgres flexible-server firewall-rule create \
  --resource-group "$RG" --name "$NEW" \
  --rule-name AllowAllAzureServices \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0
```

Then **repoint the runtime** at the restored server by updating `MOVATE_DB_URL`
(the FQDN changes with the new server name) on the API + worker + scheduler
Container Apps and restarting them:

```bash
NEW_FQDN=$(az postgres flexible-server show -g "$RG" -n "$NEW" \
  --query fullyQualifiedDomainName -o tsv)
# Update the secret/env that feeds MOVATE_DB_URL, then bounce the apps.
az containerapp revision restart -g "$RG" -n movate-prod-api  ...
```

Validate before cutting traffic over:

```bash
# A read against the restored DB through the new runtime revision.
mdk doctor target --target prod          # storage durable + reachable
mdk jobs list --target prod              # data is there
mdk auth list --target prod              # keys survived
```

Once validated and traffic is on the new server, decommission the old one (keep
it a few days as a fallback).

### When PITR is the right tool

- Accidental bulk delete / bad migration / data corruption — restore to *just
  before* it.
- Total server loss — restore from the automated backups (in-region; for
  cross-region you need geo-redundant backups, a v1.1 item).
- You need **all** the data back, transactionally consistent — not just the
  control plane.

---

## Layer 2 — `mdk export` / `mdk import` (the escape hatch)

A portable **logical** backup of the small set of **operator-critical,
non-reconstructible control-plane rows**. Where PITR restores everything into
the same Azure server family, a JSON snapshot is portable to a *fresh*
sqlite/postgres of any version, on any cloud — the ADR-001 portability contract.

### In scope (exported by default)

| Entity | Why it's critical |
|---|---|
| **Agent registry** (all bundle versions) | The published agents themselves — and the version history rollback depends on. |
| **API keys** (hash + salt) | The credentials your callers authenticate with. Restored keys keep working (see secrets note). |
| **Canary configs** | Champion/challenger rollout state. |
| **Eval + job schedules** | Cron cadences that drive continuous eval / scheduled runs. |
| **Per-tenant provider keys** (BYOK ciphertext) | Each tenant's own OpenAI/Anthropic key (ADR 018). |

### Out of scope (deliberately **not** exported)

High-volume, reconstructible, or operationally-ephemeral history: **runs, jobs,
eval/bench records, KB chunks, knowledge-graph entities/relations, conversation
threads, agent memory, feedback, trigger-delivery / run-submission dedup
ledgers, and tenant budgets.** Including them would balloon the snapshot and
duplicate what PITR already protects. **There is no `--include-history` flag** —
a half-restored history (runs referencing jobs referencing keys) is worse than
none. **For history, use PITR.**

### Storage target

`mdk export` / `mdk import` are **local / DB-direct operator commands** — they
talk to the backend selected by environment, exactly like `mdk worker`:

- `MOVATE_DB_URL=postgresql://...` → that Postgres server.
- otherwise → the local SQLite default (`~/.movate/local.db`).

To back up a **remote production** database, run the command **where
`MOVATE_DB_URL` points at it** (a bastion host, an ACA Job, or your laptop with
the DSN exported). There is no remote HTTP export endpoint, so there is no
`--target` flag.

### Usage

```bash
# Export to a default-named file in the cwd (./movate-backup-<ts>.json).
MOVATE_DB_URL=postgresql://user:pw@host/movate mdk export

# Named + gzipped.
MOVATE_DB_URL=postgresql://... mdk export prod-cp-backup.json.gz

# Stream JSON to stdout (pipe into a vault / object store).
MOVATE_DB_URL=postgresql://... mdk export - | gzip > backup.json.gz

# Restore into a FRESH deployment (safe default: skip-existing, idempotent).
MOVATE_DB_URL=postgresql://...new... mdk import prod-cp-backup.json.gz

# Force-refresh every row from the backup.
mdk import backup.json.gz --mode overwrite
```

`import` reports per-entity **imported / skipped** counts. `skip-existing` (the
default) never clobbers a row that already exists, so re-running an import is
**idempotent** (imports 0 new rows the second time). `overwrite` re-saves every
row (upsert-keyed configs/schedules/keys last-write-win; immutable agent-bundle
versions are replaced in place).

### Secrets — read this before restoring

- **API keys** persist only `secret_hash` + `salt`, never the plaintext key. The
  export carries those hashes, so a restored key row keeps the **same** hash —
  **every API key your callers already hold keeps authenticating after a
  restore.** Nothing to re-issue.
- **Per-tenant provider keys** persist a **Fernet ciphertext** + a masked
  fingerprint (ADR 018), decryptable **only** with `MOVATE_PROVIDER_KEY_SECRET`.
  That secret is **NOT** in the export. The restore environment **must** set the
  **same** `MOVATE_PROVIDER_KEY_SECRET` used at export time, or the restored
  provider keys won't decrypt at run time (the rows restore fine; the resolver
  just can't read them). If the secret differs, re-set the keys with
  `mdk keys set <provider>` after the restore.

---

## Full restore drill

Run this end-to-end periodically (and once before production sign-off) so the
procedure is muscle memory, not a hope.

1. **Pick a target instant.** Choose a timestamp inside the PITR window
   (`backupRetentionDays`).
2. **PITR to a new server** (Layer 1 procedure). Note the new FQDN.
3. **Re-apply server config:** `azure.extensions = VECTOR`, the
   `AllowAllAzureServices` firewall rule.
4. **Repoint + restart** the API / worker / scheduler Container Apps at the new
   `MOVATE_DB_URL`.
5. **Validate:**
   - `mdk doctor target --target prod` — storage durable + reachable.
   - `mdk jobs list` / `mdk auth list` / `mdk agent list` — data is present.
   - Submit a smoke run and confirm it completes.
6. **Belt-and-suspenders cross-check (escape hatch):** before the disaster,
   keep a recent `mdk export` of the control plane off-Azure. After a restore,
   diff it against the live control plane:
   ```bash
   # Export the just-restored control plane and compare entity counts to your
   # last known-good off-Azure backup.
   MOVATE_DB_URL=postgresql://...new... mdk export restored-cp.json
   diff <(jq -S '.entities | map_values(length)' last-good-cp.json) \
        <(jq -S '.entities | map_values(length)' restored-cp.json)
   ```
   If a tenant's provider keys came back but won't decrypt, confirm
   `MOVATE_PROVIDER_KEY_SECRET` matches; re-set with `mdk keys set` if not.
7. **Cut over traffic.** Keep the old server a few days as a fallback, then
   decommission.

---

## ADRs / sources behind this runbook

- `infra/azure/modules/postgres.bicep` — the Flexible Server backup config
  (retention, geo-redundancy, auto-grow).
- [`../adr/009-pgvector-kb-storage.md`](../adr/009-pgvector-kb-storage.md) — why
  a restored server needs the `azure.extensions = VECTOR` allow-list re-applied.
- [`../adr/014-durable-agent-registry.md`](../adr/014-durable-agent-registry.md)
  — the agent registry the escape hatch exports.
- [`../adr/018-tenant-provider-keys.md`](../adr/018-tenant-provider-keys.md) —
  the Fernet at-rest posture for per-tenant provider keys.
- `src/movate/core/dr_backup.py` — the backend-agnostic export/import logic.
