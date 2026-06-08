# Self-hosted Langfuse v3 — VM + docker-compose (ADR 015/039 follow-on)

Replaces the ACA `langfuse/langfuse:2` app, which **silently dropped traces**:
the mdk `LangfuseTracer` is **v3-native** (`client.start_span` /
`start_observation`), incompatible with a v2 server (incident 2026-06-08 — UI,
login, keys, and a manual v2-format ingestion all worked, but app-emitted v3-SDK
traces never landed). Issue #756.

Langfuse v3 requires an async-ingestion stack (the part v2 lacked):

| service | role |
| --- | --- |
| `langfuse-web` | UI + API + ingestion endpoint (`:3000`) |
| `langfuse-worker` | drains the queue → ClickHouse |
| `clickhouse` | analytics / trace store |
| `redis` | ingestion queue + cache |
| `minio` | S3-compatible blob store (raw event payloads) |

The **transactional DB reuses the existing shared Azure Postgres** (`langfuse`
database). ClickHouse/Redis/MinIO are compose-local with VM-persistent volumes —
far simpler than running stateful ClickHouse across ACA container apps.

## Deploy
```bash
cd infra/azure/langfuse-vm
./deploy-langfuse-vm.sh        # pulls secrets from Key Vault; resets the langfuse
                              # schema for a clean v3 migration (demo data throwaway)
```
Then point the apps at it: `LANGFUSE_HOST=http://<vm-ip>:3000` on
api / worker / temporal-worker, and update the landing-page tile.

- **Shared login:** `demo@movate.dev` / `MovateDemo2026!` (Langfuse has no true
  anonymous mode; this is the frictionless equivalent).
- **Project `MDK`** is initialized with the **same public/secret keys the apps
  already send** (from Key Vault), so traces flow with no app key change.

## Trade-offs / follow-ups
- **Single-node, no HA**; the VM is a pet (OS patching). Fine for `movate-dev`.
- **Security (demo posture):** `:3000` open + shared cred + plaintext HTTP.
  Add TLS / restrict the NSG / rotate before non-demo use (#767).
- **Codify in IaC** (#762): this is a shell deploy today; promote to bicep.
- ClickHouse/Redis/MinIO creds are generated per-deploy and live only in the
  VM's `/opt/langfuse/.env` (chmod 600), never committed.
