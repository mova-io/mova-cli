# Self-hosted Temporal — VM + docker-compose (ADR 078 follow-on)

The ACA-hosted Temporal frontend (`containerapp-temporal.bicep`) proved
chronically unreachable: ACA's **raw-TCP internal ingress** stopped routing to a
healthy frontend after a redeploy, and a restart / re-roll / ingress
disable-enable / readiness probe all failed to repopulate the envoy upstream
(incident 2026-06-08 — clients got `dial tcp <ingress-ip>:7233: i/o timeout`
while the container was provably healthy and bound on `:7233`).

The Temporal stack itself is fine — proven by running the **identical** mdk worker
against a local server: it connected and registered instantly. The problem is
purely ACA's TCP-ingress layer.

This directory hosts Temporal **self-hosted** (sovereignty requirement — no
Temporal Cloud) on a single Azure VM with **direct ports**, removing ACA ingress
from the path entirely. It **reuses the existing shared Azure Postgres**
(`temporal` + `temporal_visibility` DBs, already schema'd) — no data migration.

## Files
- `docker-compose.yml` — `temporalio/auto-setup` (`:7233`) + `temporalio/ui`
  (`:8080`), env mirrored verbatim from the working ACA module.
- `deploy-temporal-vm.sh` — `az vm create` (Ubuntu + cloud-init installs Docker,
  writes the compose + `.env`, `docker compose up -d`), opens NSG ports
  22/7233/8080, and adds the VM IP to the Postgres firewall.
- `.env.example` — the three values the compose needs (password pulled from Key
  Vault by the deploy script; never committed).

## Deploy
```bash
cd infra/azure/temporal-vm
./deploy-temporal-vm.sh         # pulls the PG password from Key Vault automatically
```
Then point the mdk worker + the Temporal-UI landing tile at
`TEMPORAL_HOST=<vm-fqdn>:7233` / UI `http://<vm-fqdn>:8080`.

## Trade-offs (per the chosen option)
- **Single-node** — no HA. Durable state is safe in Postgres across a VM reboot;
  the frontend is briefly unavailable while the container recycles.
- The VM is a **pet** (OS patching). Fine for the `movate-dev` / demo env; for a
  production, customer-facing Temporal use the AKS + Helm path (ADR 078 Phase 3).
- The UI is **open** (demo posture). Restrict the `:8080` NSG rule or front it
  with an auth proxy for anything beyond dev.

## Why not ACA / Cloud
- **ACA**: raw-TCP internal ingress is the failing layer (above). Switching to
  ACA HTTP/2 ingress is possible but keeps us on the platform that already broke.
- **Temporal Cloud**: ruled out — Temporal must be self-hosted (sovereignty).
