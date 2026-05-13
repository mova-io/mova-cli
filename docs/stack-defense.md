# Stack defense — open-source components in customer VPCs

**Status:** Canonical
**Last reviewed:** 2026-05-13
**Audience:** Movate technical leads (Deva +), sales engineers, customer-engagement architects
**Pairs with:** [`docs/license-posture.md`](license-posture.md), [`docs/adr/001-cloud-portability.md`](adr/001-cloud-portability.md)

---

## TL;DR

When Movate delivers an MDK-built agent system into a **customer VPC**
(Azure tenant, AWS account, GCP project, on-prem k8s), what actually
lands is **not MDK itself** — MDK is Movate's proprietary framework
kept in our repo. What lands is:

1. **The compiled Movate runtime image** (FastAPI app + worker, built from `src/movate/runtime/` + `src/movate/core/`)
2. **The open-source dependencies that runtime needs to function at runtime** (this doc)
3. **The infrastructure resources the runtime needs** (Postgres, optionally pgvector, optionally KV/secrets manager — covered in §3 below)

This document gives Movate technical staff the **defensible answers**
to customer security/architecture review questions about each of the
open-source components in (2) and (3). Every entry covers:

- **What it is + version pin** — what the customer's reviewer will Google
- **Why we chose it** — alternatives we evaluated + why this one wins
- **License + resale safety** — SPDX id, whether customer can embed + commercialize
- **Customer-VPC implications** — what runs where, what surface area it adds, what egress it opens
- **Maturity signals** — release cadence, GitHub stars, who else uses it in production
- **Removal cost if a customer requires it** — can we swap it cleanly, or is it load-bearing?

If a customer reviewer asks "why is X in your stack?", the answer
should come from this doc verbatim.

---

## Quick reference matrix

| Component | License | Customer-VPC role | Removal cost |
|---|---|---|---|
| **Python 3.11+** | PSF | Runtime interpreter | Hard — entire codebase |
| **Pydantic 2.x** | MIT | Schema validation, config models | Hard — pervasive |
| **FastAPI** | MIT | HTTP runtime (`/run`, `/eval`, `/jobs/{id}`) | Medium — adapter swap |
| **uvicorn** | BSD-3 | ASGI server for FastAPI | Trivial — drop-in replacement |
| **LiteLLM** | MIT | Multi-provider model client | Medium — already abstracted behind `BaseLLMProvider` |
| **httpx** | BSD-3 | HTTP client (LiteLLM, MovateClient, Langfuse) | Trivial |
| **asyncpg / aiosqlite** | Apache 2.0 / MIT | Storage backends | Medium — Protocol-driven |
| **cryptography** | Apache 2.0 + BSD | Fernet for at-rest secrets, JWT signing | Easy — could swap to KMS |
| **bcrypt** | Apache 2.0 | API key password hashing | Trivial — could swap to argon2 |
| **OpenTelemetry SDK** | Apache 2.0 | Distributed tracing | Easy — env-gated |
| **Langfuse SDK** | MIT | LLM observability | Easy — env-gated |
| **Pydantic models** | MIT | (same as above; just adding the table for clarity) | — |
| **Jinja2** | BSD-3 | Prompt template rendering | Hard — pervasive |
| **JSON Schema (jsonschema)** | MIT | Agent I/O schema validation | Hard — agent contract |
| **PyYAML** | MIT | agent.yaml / workflow.yaml / policy.yaml parsing | Hard — config layer |
| **structlog** | Apache 2.0 / MIT | Structured logs | Trivial |

Optional (only if the customer's agent declares them):
| Component | License | Role | Triggered by |
|---|---|---|---|
| **Anthropic Python SDK** | MIT | Native Anthropic adapter | `runtime: native_anthropic` in agent.yaml |
| **OpenAI Python SDK** | Apache 2.0 | Native OpenAI adapter | `runtime: native_openai` |
| **LangChain core** | MIT | LangChain runnable wrapper | `runtime: langchain` |
| **pgvector** | PostgreSQL License | Vector retrieval | `knowledge.yaml: vector_db: pgvector` (v0.8+) |
| **Apache AGE** | Apache 2.0 | Property-graph storage | `knowledge.yaml: knowledge_graph: age` (v0.9+) |

**Every line above is on a permissive license — no GPL, no AGPL, no BSL, no SSPL.** Resale-clean by default.

---

## §1 — Core runtime dependencies

### Python 3.11+

**License:** PSF (Python Software Foundation License) — compatible with commercial redistribution.

**Why this version floor:** 3.11 ships `tomllib` in stdlib (no third-party TOML dep needed for pyproject parsing), substantially faster startup than 3.10, and PEP 654 exception groups (used by our retry+fallback layer to surface multi-error contexts). 3.12+ keeps working; we test against the lowest supported version in CI.

**Customer-VPC implications:** Python is already in every modern Linux distro's base packages. ACA / EKS / GKE base images ship 3.11+ out of the box. The customer's compliance team has seen Python in 100% of audits.

**Defense talking points:**
- Used by Instagram, Spotify, Netflix, NASA — no novelty risk
- 4-week security patch cycle from python.org
- CVE history is short and well-publicized
- Movate pins to `>=3.11` (not exact) so security patches flow without code changes

---

### Pydantic 2.x (`pydantic>=2.6,<3`)

**License:** MIT.

**What it does:** Defines every typed object in MDK — `AgentSpec`, `RunRequest`, `RunResponse`, `Metrics`, `JobRecord`, `ApiKeyRecord`, `PolicyConfig`, every wire schema. Validates input at the boundary; rejects malformed data before it can corrupt downstream state.

**Why we chose it (vs. alternatives):**

| Alternative | Why we rejected |
|---|---|
| dataclasses + manual validation | No declarative validation; every field check is hand-rolled boilerplate that drifts |
| marshmallow | Slower; declarative but pre-2-x design (load/dump split is dated) |
| attrs + cattrs | Two libraries instead of one; cattrs serialization is unstable |
| protobuf | Wire format; doesn't address Python-runtime validation |

**Why Pydantic 2:** Rust-backed validation (10-50× faster than v1). Standard Python type hints as the schema source — no DSL to learn. Pydantic v2 is the de-facto type system for FastAPI + most modern Python web frameworks.

**Customer-VPC implications:** Pure Python + Rust core (statically compiled, no runtime native-build needed). Single PyPI install. Zero network/file-system surface area beyond what the agent itself does.

**Maturity:** 8K+ commits, 18K+ GitHub stars, used by AWS Lambda Powertools, FastAPI, Anthropic SDK, OpenAI SDK. Pydantic Inc. is venture-backed and the maintainer team is full-time.

**Removal cost:** Hard. Pydantic is the type system for ~60+ models across `src/movate/core/`. A swap would be a multi-month rewrite. The risk is acceptable because the license is permissive and the maintainer is durable.

---

### FastAPI (`fastapi>=0.110`)

**License:** MIT.

**What it does:** The HTTP runtime exposing `POST /run`, `GET /jobs/{id}`, `GET /agents`, etc. Routes Bot Framework webhooks for the Teams integration. Auto-generates OpenAPI docs at `/docs`.

**Why we chose it (vs. alternatives):**

| Alternative | Why we rejected |
|---|---|
| Flask | Sync-first; we need async for concurrent LLM calls |
| Django REST | Heavyweight; brings ORM + admin we don't need |
| Starlette directly | FastAPI IS Starlette + Pydantic integration we'd hand-roll anyway |
| aiohttp | Lower-level; we'd build the routing/validation FastAPI gives free |
| Sanic | Smaller community; weaker FastAPI-vs-everything-else maturity gap |

**Customer-VPC implications:** Single Python process per replica; horizontal scale via ACA / k8s replicas. No background scheduler (workers are separate processes). Listening ports configurable; ingress controlled by the orchestrator.

**Maturity:** 78K+ GitHub stars; production at Uber, Netflix, Microsoft, Cloudflare. Sebastian Ramirez maintains it full-time backed by sponsorship.

**Removal cost:** Medium. The HTTP routes are thin (~30 endpoints total). Swapping to Starlette directly or another framework is mechanical — ~2 weeks. We don't see a forcing function.

---

### uvicorn (`uvicorn[standard]>=0.29`)

**License:** BSD-3-Clause.

**What it does:** ASGI server that runs FastAPI. Handles HTTP/1.1 + HTTP/2 + WebSockets, process model, graceful shutdown.

**Why this one:** It IS the canonical ASGI server for FastAPI; alternative gunicorn-via-asgi adapters are slower and have rougher shutdown semantics. The `[standard]` extra pulls in `httptools` + `uvloop` for the performance path.

**Customer-VPC implications:** Pure Python + small Rust/C extensions for HTTP parsing. Process-per-container; let the orchestrator manage replicas.

**Removal cost:** Trivial. hypercorn or Daphne are drop-in replacements. We have no reason to switch.

---

### LiteLLM (`litellm>=1.50,<2`)

**License:** MIT.

**What it does:** Single client that abstracts ~40 LLM providers (OpenAI, Anthropic, Azure OpenAI, Vertex AI, Bedrock, etc.) behind a uniform interface. Movate's `LiteLLMProvider` is one of three runtime adapters (alongside `AnthropicProvider` and `OpenAIProvider` for native SDK access).

**Why we chose it (vs. alternatives):**

| Alternative | Why we rejected |
|---|---|
| Direct SDKs only (openai, anthropic, etc.) | Forces per-provider conditional code in every agent; no fallback chain |
| LangChain LLM wrappers | Heavyweight; brings the whole LangChain framework |
| Custom per-provider adapters | We'd be rewriting LiteLLM's 40-provider catalog ourselves |
| Vellum / Helicone | Vendor lock-in to a hosted service |

**Why LiteLLM:** Native-SDK feature parity for the top 10 providers; community-maintained for the long tail. Our `BaseLLMProvider` Protocol means LiteLLM is one impl, not the only path — if a provider becomes critical and LiteLLM's adapter lags, we ship a native one (we did this for Anthropic + OpenAI in v0.6).

**Customer-VPC implications:** Pure Python; pulls each provider SDK as needed. **Outbound calls only** — no inbound network surface. Customer's egress firewall must allow whichever model provider's API endpoint the agent's `model.provider` field declares.

**Defense talking point — version pin:** `>=1.50,<2`. We pin tight because LiteLLM has had breaking minor releases. Upgrade is a quarterly cadence with regression tests in our CI; customers won't see a surprise behavior change.

**Maturity:** 17K+ GitHub stars; BerriAI Inc. (venture-backed). Used by Lightning AI, Replicate, Aporia.

**Removal cost:** Medium. Our `BaseLLMProvider` Protocol is the seam; today three impls behind it. Removing LiteLLM means agents declaring `runtime: litellm` need to switch to `native_anthropic` / `native_openai` / etc. Doable in a sprint.

---

### httpx (`httpx>=0.27`)

**License:** BSD-3-Clause.

**What it does:** Async HTTP client used by LiteLLM, MovateClient (for `mdk submit`), Langfuse SDK, and the Teams bot's attachment downloader.

**Why this one:** Drop-in `requests`-style API + native async + HTTP/2 + WebSockets. The de-facto async client for Python.

**Customer-VPC implications:** Pure Python with optional `h2` for HTTP/2. Connection pooling: we hold one client per long-lived process to amortize TLS handshakes. No persistent disk state.

**Removal cost:** Trivial. `aiohttp` is the obvious alternative; mechanical migration.

---

## §2 — Storage layer

### Postgres (customer-provided)

**License:** PostgreSQL License (permissive, similar to BSD).

**What it does:** Persistent storage for runs, jobs, evals, API keys, tenant budgets, workflow state, teams_users bindings (when Teams identity binding is enabled).

**Why Postgres specifically (vs. MongoDB, DynamoDB, MySQL):**

| Alternative | Why we rejected |
|---|---|
| MongoDB | License (SSPL) is non-permissive; resale-blocking |
| DynamoDB | Vendor lock-in to AWS; no analog in Azure/GCP |
| MySQL | License is GPL-licensed core + commercial-only Enterprise; opaque |
| CockroachDB | BSL license; commercial-competition restriction |
| SQLite | Already our fallback for single-instance dev; doesn't scale to multi-replica |

**Why Postgres:** Genuinely permissive (PostgreSQL License); battle-tested at scale; first-class support on every cloud (Azure Flexible Server, RDS, Cloud SQL); rich ecosystem (pgvector for v0.8 RAG, Apache AGE for v0.9 KG — both ride on the same Postgres install).

**Customer-VPC implications:** Customer either uses managed (Azure Flexible Server, AWS RDS, GCP Cloud SQL) or self-hosted. MDK doesn't bundle Postgres — the runtime image just opens a connection to wherever the customer provisioned it. No special network surface added by MDK.

**Defense talking point — single source of truth:** The same Postgres instance carries every piece of MDK state (runs / jobs / evals / API keys / KG when v0.9 lands / vector index when v0.8 lands). Customers don't need to provision multiple data stores.

---

### asyncpg (`asyncpg>=0.29`)

**License:** Apache 2.0.

**What it does:** Async Postgres driver used by `src/movate/storage/postgres.py`.

**Why this one (vs. alternatives):**

| Alternative | Why we rejected |
|---|---|
| psycopg2 | Sync only; defeats async runtime |
| psycopg3 | Newer; less mature async support |
| SQLAlchemy + asyncpg | Heavier (full ORM); we use raw SQL for control + perf |

**Why asyncpg:** Fastest async Postgres driver by ~3-5× over alternatives in benchmarks. Maintained by MagicStack (the founder of edgedb).

**Customer-VPC implications:** Single client library in the runtime image; opens connections to the customer's Postgres FQDN over TLS. No bundled native binaries beyond what `pip install` produces.

---

### aiosqlite (`aiosqlite>=0.20`)

**License:** MIT.

**What it does:** Async wrapper around stdlib `sqlite3` for the development backend (single-instance dev, tests, Teams bot's user-binding db).

**Why this one:** The canonical aio wrapper around `sqlite3`. SQLite itself ships with Python; aiosqlite just adds the async API.

**Customer-VPC implications:** SQLite is single-file storage. Customers using SQLite get zero external dependencies. Customers using Postgres see this dep installed but unused — couple-MB cost, no runtime impact.

---

## §3 — Auth + crypto

### cryptography (`cryptography>=42`)

**License:** Apache 2.0 + BSD-3 (dual-licensed).

**What it does:** Fernet symmetric encryption for at-rest secrets (Teams identity binding's per-user API keys). JWT validation when the Teams bot hardening PR lands. Maintained by the Python Cryptographic Authority (the project that also maintains `pyca/pyopenssl`).

**Why this one:** The standard-bearer for Python cryptography. CVE-tracked, FIPS-relevant where the customer cares. Backed by Rust's `cryptography` crate under the hood for the heavy operations.

**Customer-VPC implications:** Pulls Rust-compiled wheels from PyPI. No network surface. Operations happen entirely in-process; the key material is whatever the customer's KMS / KV provides via env.

**Defense talking point — KMS integration:** Today the bot reads its Fernet key from env (sourced from KV / Secrets Manager). The interface is KMS-shaped — when a customer prefers their HSM-backed KMS (Azure Key Vault HSM, AWS KMS, GCP Cloud KMS), the swap is a single function override.

---

### bcrypt (`bcrypt>=4.0`)

**License:** Apache 2.0.

**What it does:** Hashes MDK API key secrets before storage. Standard cost factor (12 rounds).

**Why this one:** Industry-standard password hashing; the original from Niels Provos. The Python binding is maintained by PyCA.

**Customer-VPC implications:** Single binary wheel. No network.

**Defense talking point — could-we-swap-to-argon2:** Yes. argon2-cffi is also Apache 2.0. We picked bcrypt because it's older + more familiar to security reviewers. argon2 is technically stronger; we can swap if a customer specifically requires it (no architectural lock-in).

---

## §4 — Observability

### OpenTelemetry SDK (optional `[otel]` extra)

**License:** Apache 2.0 (entire OTel ecosystem).

**What it does:** Distributed tracing. Every run produces a span tree (workflow → node → provider call → token usage) that ships to whatever OTLP-compatible backend the customer points us at (Jaeger, Tempo, Honeycomb, Datadog, Azure Monitor, etc.).

**Why this one:** Vendor-neutral; CNCF-graduated; the de-facto standard for distributed tracing. Customer can plug it into whatever observability stack they already have.

**Customer-VPC implications:** Egress to the customer's chosen OTLP collector. We don't ship a collector — we emit spans to wherever `OTEL_EXPORTER_OTLP_ENDPOINT` points.

**Defense talking point — opt-in:** OpenTelemetry is an OPTIONAL extra (`pip install movate-cli[otel]`). Customers who don't want it never install it.

---

### Langfuse SDK (optional `[langfuse]` extra)

**License:** MIT (SDK is MIT; Langfuse server itself is MIT too, AGPL was changed in v3.0).

**What it does:** LLM-specific observability — prompt / response / cost / latency per run, with a hosted or self-hosted dashboard.

**Why we chose it (vs. alternatives):**

| Alternative | Why we rejected |
|---|---|
| LangSmith | Vendor lock-in; LangChain-only; not OSS |
| Helicone | Hosted-only; we wanted OSS option |
| Arize Phoenix | Different focus (eval-heavy); we have our own eval |
| Custom OTel spans | Lose LLM-specific UI (token counts, prompt diffs, etc.) |

**Why Langfuse:** Self-hostable, MIT-licensed SDK + MIT-licensed server (since v3.0). Customer can deploy the Langfuse server in their VPC and never let prompt data leave.

**Customer-VPC implications:** SDK in our runtime; egress to the customer's Langfuse instance (self-hosted or langfuse.com). Opt-in via env var.

**Defense talking point — self-host option:** If the customer's data-residency rules forbid sending prompts to a third party, the Langfuse server is one `docker compose up` in their VPC. We don't bind to langfuse.com.

---

## §5 — Templating + validation

### Jinja2 (`jinja2>=3.1`)

**License:** BSD-3-Clause.

**What it does:** Renders prompt templates. Every agent's `prompt.md` is a Jinja2 template with `{{ input.field }}` substitution.

**Why this one:** Standard Python templating. The same library Django + Flask + Ansible use.

**Customer-VPC implications:** Pure Python. **AUTOESCAPE IS OFF** for prompt rendering (we render plaintext, not HTML); MDK's prompt linter catches `{{ input.X }}` refs not declared in the input schema before runtime.

**Defense talking point — prompt-injection:** Customer data flowing through `{{ input.X }}` is NOT auto-escaped (we render text, not HTML). The system prompt + agent.yaml are operator-controlled. Prompt-injection defenses live at the agent design level (instruction-following hardening, output validation against output schema, content-filter checks). The Jinja2 dep itself doesn't add injection risk beyond what plain string formatting would.

---

### jsonschema (`jsonschema>=4.21`)

**License:** MIT.

**What it does:** JSON Schema Draft 2020-12 validator for agent input/output schemas. Every agent declares strict schemas; every run validates both sides before recording.

**Why this one:** Canonical Python JSON Schema implementation. The 2020-12 draft is the modern stable.

**Customer-VPC implications:** Pure Python. No surface area.

---

### PyYAML (`pyyaml>=6.0`)

**License:** MIT.

**What it does:** Parses agent.yaml, workflow.yaml, policy.yaml, runtime.yaml, eval.yaml, knowledge.yaml, skill.yaml.

**Why this one:** The default YAML library for Python. Battle-tested.

**Customer-VPC implications:** Pure Python. **We use `yaml.safe_load` everywhere** — no `yaml.load` that could execute arbitrary code from a malicious YAML file. Audited.

---

## §6 — Optional (per-customer-feature) deps

### Anthropic SDK (`anthropic>=0.40` — `[anthropic]` extra)

**License:** MIT.

**Triggered by:** `runtime: native_anthropic` in agent.yaml. Optional otherwise; LiteLLM provides Anthropic access too.

**Defense talking point:** We ship native_anthropic so customers wanting Anthropic's full feature surface (prompt caching, vision, native MCP tool-use, structured outputs) don't lose anything by using MDK. Without the extra installed, agents declaring `runtime: native_anthropic` fail at `mdk validate` with a clean install hint.

---

### OpenAI SDK (`openai>=1.40` — `[openai]` extra)

**License:** Apache 2.0.

**Triggered by:** `runtime: native_openai`. Same story as Anthropic.

---

### LangChain core (`langchain-core>=0.3` — `[langchain]` extra)

**License:** MIT.

**Triggered by:** `runtime: langchain`. Wraps an existing LangChain Runnable as an MDK agent — bridge for customers with existing LangChain pipelines.

**Defense talking point — why not the full langchain package:** We pull only `langchain-core` (the abstract Runnable interface), not the heavyweight `langchain` package with hundreds of integrations. Customer-VPC footprint stays small.

---

### pgvector (v0.8+)

**License:** PostgreSQL License.

**Triggered by:** `knowledge.yaml: vector_db: pgvector`. ADR 004 captures the design.

**Why pgvector (vs. Qdrant, Chroma, Weaviate, LanceDB):**

| Alternative | License | Why rejected for first-line |
|---|---|---|
| Qdrant | Apache 2.0 | Separate service to deploy + monitor |
| Chroma | Apache 2.0 | Less mature; smaller community |
| Weaviate | BSD-3 | Separate service; richer features we don't need |
| LanceDB | Apache 2.0 | File-based; doesn't co-locate with our Postgres-first state |

**Why pgvector:** Lives inside the same Postgres customers already have. Zero new infrastructure to provision. Operational simplicity wins over feature breadth at our scale.

**Customer-VPC implications:** Postgres extension; enabled with `CREATE EXTENSION vector;`. Customer's DBA needs to allow extension installation (managed Postgres services all support pgvector now).

---

### Apache AGE (v0.9+)

**License:** Apache 2.0.

**Triggered by:** `knowledge.yaml: knowledge_graph: age`. ADR 005 captures the design.

**Why Apache AGE (vs. Neo4j, Memgraph, Kuzu, TerminusDB):**

| Alternative | License | Why rejected |
|---|---|---|
| Neo4j Community | GPLv3 | **Copyleft — resale-blocking** |
| Memgraph | BSL | **Service-competition restriction — resale-blocking** |
| Kuzu | MIT | Acceptable fallback; less mature |
| TerminusDB | Apache 2.0 | Acceptable fallback; less mature |

**Why AGE:** Apache 2.0 (resale-clean); Postgres extension (same operational story as pgvector); openCypher query language (Neo4j's de-facto standard) without Neo4j's license.

---

## §7 — Build / dev-only (NOT deployed to customer VPC)

These appear in our repo but aren't in the production wheel:

| Tool | License | Role |
|---|---|---|
| `pytest` + plugins | MIT | Tests (CI only) |
| `ruff` | MIT | Linter (dev) |
| `mypy` | MIT | Type checker (dev) |
| `hatchling` | MIT | Build backend |
| `uv` | Apache 2.0 / MIT | Package manager (dev) |

None of these reach a customer's VPC. Listed here so reviewers asking "what's in your `pyproject.toml`?" have the full picture.

---

## §8 — Hard "won't add" list

These licenses are blocked at the policy layer (enforced via `mdk doctor --licenses` + future CI check):

| License | Why blocked |
|---|---|
| **GPLv2 / GPLv3** | Copyleft; would force customer code to also open-source |
| **AGPL** | Network-copyleft; would force customer SaaS to open-source |
| **SSPL** (MongoDB-style) | Forces operators of "competing services" to open-source the entire stack |
| **BSL** (Business Source) | Generally restricts commercial-competition use; case-by-case but default-blocked |
| **Custom commercial / EULA** | Unauditable; requires legal sign-off per engagement |

If a new dependency proposal comes in with one of the above, the PR is rejected at the license-gate check before review.

---

## §9 — Process: how a new dep gets added

```
Proposal (PR)
  ↓
License check (CI: `pip-licenses` against allow-list)
  ↓ pass
Engineering review (does it pull in transitives we don't want?)
  ↓ pass
Customer-VPC review (is this NEEDED in the runtime image, or dev-only?)
  ↓ pass
Documentation (add an entry to THIS doc + license-posture.md)
  ↓ pass
Merge
```

The license check is automated (`tier 8` item in BACKLOG.md). The other reviews are human; this doc is the artifact that captures the "why" once they pass.

---

## §10 — FAQ for customer review sessions

> **Q: What if our security team flags `<dep>` and we can't ship it?**
>
> A: Every entry above has a "Removal cost" line. Trivial / Easy items are <1-day swaps; Medium are 1-2 week refactors; Hard items would force us to recommend a different architecture. If a customer truly can't have one of the Hard items (Pydantic, Jinja2, jsonschema), the engagement isn't a fit for MDK — surface this early.

> **Q: Do you have an SBOM (software bill of materials)?**
>
> A: Generated from `pyproject.toml` via `pip-licenses --format=json` or CycloneDX. Available on request per engagement; expect to ship with the v1.0 release artifact.

> **Q: How often are deps updated?**
>
> A: Security-only floating; tight upper bounds for major versions. We run Dependabot + manual quarterly upgrade windows. Customer engagements pin to a specific MDK release tag — no surprise upgrades.

> **Q: What about supply-chain attacks?**
>
> A: We pin every dep with a lower bound + upper bound; we don't use `latest`. We're tracking sigstore / pypi-attestations adoption; will adopt when the standards stabilize. Customer can audit our `uv.lock` / `requirements.txt` per release.

> **Q: Is there a single point of dependency failure?**
>
> A: Pydantic is the closest to one (it's pervasive). Mitigation: Pydantic Inc. is venture-backed with a paid team; the license is permissive so a community fork is always possible. We're not exposed to a single maintainer disappearing.

> **Q: Do you embed any commercial / paid-tier deps?**
>
> A: No. Every entry in this doc is permissively licensed and free for commercial use. Customer pays for MDK (Movate's IP) + their model provider bills + their cloud bill. Zero per-seat / per-call OSS fees flow through MDK.
