# ADR 001 — Cloud-portability as a design principle

**Status:** Accepted
**Date:** 2026-05-12
**Deciders:** Engineering + Deva (Movate)
**Context window:** v0.5 → v1.0 design horizon

---

## Decision

Every new infrastructure choice in movate-cli **must be portable across
Azure / AWS / GCP / on-prem** as a design property — even though we
deploy on Azure today and have no immediate plan to add a second cloud.

When a feature requires picking between a cloud-portable building block
and a proprietary cloud-native service, **we pick the portable one** —
unless the cost difference is dramatic and explicitly justified in a
follow-up ADR.

## Context

Deva (May 12, 2026): "Multi-cloud is eventual." Movate's customer
engagements will, over time, span customers running on AWS and GCP as
well as Azure. We don't have to ship multi-cloud today. We do have to
avoid quiet accumulation of Azure-only assumptions that would force a
painful rewrite later.

This ADR exists so future-us doesn't quietly pick Azure Cosmos DB,
Azure AD-only auth, Azure Functions, or any other "you can't easily
move this off Azure" service without a deliberate conversation.

## What this rules in

These are the building blocks we already use and they're all portable:

| Building block | Why it's portable | Available on |
|---|---|---|
| **Postgres** | Standard SQL; mature client drivers everywhere | RDS, CloudSQL, Azure Flex, on-prem |
| **Container images** (Docker) | Just OCI; runs on any OCI-compatible runtime | ECS, GKE, ACA, K8s, on-prem |
| **OIDC federation** for auth | RFC standard | GitHub Actions, GitLab, any OIDC IdP |
| **LiteLLM** for model routing | HTTP-only; speaks every major provider | Anywhere with internet |
| **Object storage** (S3 API) | de facto standard | S3, GCS S3-compat, Azure Blob via gateway, MinIO, R2 |
| **Vector DB candidates** | All open-source, run anywhere | pgvector (already on Postgres), Qdrant, Chroma, Weaviate |
| **Knowledge graph** (Apache AGE) | Postgres extension | Anywhere Postgres runs |
| **OTel** (tracing) | Vendor-neutral protocol | Any OTLP-compatible backend |

## What this rules out

Without a separate ADR justifying the choice, we **do not** add:

| Service | Why excluded |
|---|---|
| **Cosmos DB** | Azure-only SQL flavor; no portable equivalent on AWS/GCP |
| **Azure Functions / Lambda** | Each cloud has incompatible function packaging; portable equivalent is "container running a small HTTP handler" |
| **Azure AD-only auth** (no OIDC) | Forces customers onto Azure to authenticate; we use OIDC federation instead |
| **Service Bus / SQS / Pub/Sub** specific clients | Portable equivalent: Postgres queue (what we use today via KEDA) or a thin adapter layer |
| **Azure Search** / Kendra / Vertex Search | Open-source vector + graph DBs are portable |
| **Azure Blob-specific APIs** (vs. S3-compatible HTTP) | S3 API is the de facto cross-cloud standard |
| **Azure DNS / managed certs that don't speak ACME** | Let's Encrypt + cert-manager runs anywhere |
| **Bicep** for non-Azure infrastructure | Bicep is fine for our Azure-specific IaC; Terraform / Helm covers everything else |

We **do** keep Bicep for our actual Azure deployments — that's the
cloud-specific IaC for one specific cloud. When AWS or GCP support is
added, those get parallel Terraform modules. We don't try to unify
the IaC; each cloud has its own native language and we accept the
duplication.

## Concrete implications

### For new code
- Storage backends use SQL or container-volume primitives. No Cosmos.
- New queues use Postgres tables (already in use) or — if scale demands
  — an OSS broker (NATS, Kafka). Not cloud-specific managed queues.
- Authentication accepts any OIDC provider. Movate's deployments use
  GitHub Actions OIDC federation today; customers can plug their own
  IdP later (Azure AD, Okta, Google Workspace, custom) without
  changing the code.
- File storage uses an S3-compatible HTTP API behind an adapter; the
  default implementation can be MinIO for local dev, S3 / GCS / Azure
  Blob's S3 compatibility layer for prod.

### For dependencies
- Cross-cloud-incompatible Python SDKs (e.g. `azure-cosmos`,
  `google-cloud-functions`) require explicit ADR + Deva sign-off
  before adding.
- Multi-cloud SDKs (`boto3`, `azure-storage-blob`) are fine — they're
  isolated behind our adapter layer and only loaded when that adapter
  is in use.

### For deployment artifacts
- `infra/azure/main.bicep` — Azure-specific IaC. Stays as-is.
- `infra/aws/` (future) — Terraform modules for ECS + RDS + ECR + KMS,
  with parallel module structure to Bicep so operators can pattern-match.
- `infra/gcp/` (future) — Terraform modules for Cloud Run + CloudSQL +
  Artifact Registry + Secret Manager.
- `infra/helm/` (future) — Helm chart for self-hosted K8s deployments
  (works on EKS, GKE, AKS, on-prem). This is the **portable**
  deployment path; cloud-specific IaC is for customers who want the
  managed-services experience.

## When to revisit

This ADR holds until at least one of:

1. A customer engagement requires AWS or GCP deployment — at which
   point we ship the Terraform modules referenced above, validating
   that the portability principle actually held.
2. A specific cloud-native service offers a 10x cost or capability
   advantage that we can't replicate with portable primitives. At
   that point we write a follow-up ADR explicitly accepting the
   lock-in and documenting the workaround for non-blessed clouds.
3. Movate's engagement model shifts to "Azure-only by strategy." In
   which case we'd retire this ADR and update internal docs to make
   the new posture explicit.

## Related

- [`docs/license-posture.md`](../license-posture.md) — the other "say
  no early so we don't regret later" doc, covering OSI license hygiene.
- [`BACKLOG.md`](../../BACKLOG.md) §9 Tier 9 — enterprise readiness
  items that pick up the Helm chart + multi-cloud Terraform work.
- [`infra/azure/`](../../infra/azure/) — the current Azure-specific
  IaC; the reference shape for what future Terraform modules emulate.
