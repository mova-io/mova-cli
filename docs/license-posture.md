# License posture — resale-clean stack

**Status:** Canonical
**Last reviewed:** 2026-05-12
**Audience:** Movate engineers + customer-engagement leads
**Pairs with:** [`docs/adr/001-cloud-portability.md`](adr/001-cloud-portability.md)

---

## TL;DR

Every dependency in movate-cli today is **permissively licensed** —
MIT, Apache 2.0, BSD, or the PSF/PostgreSQL License. A Movate customer
engagement that embeds movate-cli in a deliverable can be **resold
without copyleft contamination, AGPL service-side obligations, or BSL
competing-services clauses**.

This document is the agreed contract for keeping it that way as we
add features.

## Why this matters

movate-cli is **not** itself a sold product. But Movate customer
engagements *use* movate-cli to build agent systems that the customer
(or Movate, on the customer's behalf) ships in production. Anything in
movate-cli's dependency tree becomes part of those deliverables.

If movate-cli pulled in a **GPL** dep, customer code that embeds
movate-cli inherits copyleft obligations — potentially forcing the
customer to open-source proprietary code, or barring them from selling
the product entirely.

If it pulled in a **Business Source License (BSL)** dep, the customer
might be barred from "providing it as a competing service" — fine for
some engagements, fatal for others.

The simplest defense: **stay permissive everywhere.** This doc records
what we have and what we won't add.

## Current dependency licenses

### Required deps (always installed)

| Package | Version pin | License | SPDX | OK for resale? |
|---|---|---|---|---|
| `pydantic` | `>=2.6,<3` | MIT | `MIT` | ✅ |
| `pyyaml` | `>=6.0` | MIT | `MIT` | ✅ |
| `jinja2` | `>=3.1` | BSD-3-Clause | `BSD-3-Clause` | ✅ |
| `typer` | `>=0.12` | MIT | `MIT` | ✅ |
| `rich` | `>=13.7` | MIT | `MIT` | ✅ |
| `httpx` | `>=0.27` | BSD-3-Clause | `BSD-3-Clause` | ✅ |
| `litellm` | `>=1.50,<2` | MIT | `MIT` | ✅ |
| `python-dotenv` | `>=1.0` | BSD-3-Clause | `BSD-3-Clause` | ✅ |
| `aiosqlite` | `>=0.20` | MIT | `MIT` | ✅ |
| `structlog` | `>=24.1` | MIT or Apache 2.0 | `MIT OR Apache-2.0` | ✅ |
| `jsonschema` | `>=4.21` | MIT | `MIT` | ✅ |

### Optional deps (only when extras are installed)

| Extra | Package | License | SPDX | OK for resale? |
|---|---|---|---|---|
| `[runtime]` | `fastapi` | MIT | `MIT` | ✅ |
| `[runtime]` | `uvicorn[standard]` | BSD-3-Clause | `BSD-3-Clause` | ✅ |
| `[runtime]` | `asyncpg` | Apache 2.0 | `Apache-2.0` | ✅ |
| `[runtime]` | `bcrypt` | Apache 2.0 | `Apache-2.0` | ✅ |
| `[langfuse]` | `langfuse` | MIT | `MIT` | ✅ |
| `[otel]` | `opentelemetry-*` | Apache 2.0 | `Apache-2.0` | ✅ |
| `[anthropic]` | `anthropic` | MIT | `MIT` | ✅ |
| `[openai]` | `openai` | Apache 2.0 | `Apache-2.0` | ✅ |
| `[langchain]` | `langchain-core` | MIT | `MIT` | ✅ |

### Dev-only deps (NOT shipped with the runtime)

`pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`,
`types-*` — all permissively licensed (MIT / Apache 2.0). Dev-only deps
don't ship into customer deliverables, so license posture matters less
here, but we still keep them clean for contributor sanity.

## What we approve to add

When adding a new dependency, **the license must be one of**:

- **MIT** (`MIT`)
- **Apache 2.0** (`Apache-2.0`)
- **BSD** — 2-Clause or 3-Clause (`BSD-2-Clause`, `BSD-3-Clause`)
- **ISC** (`ISC`)
- **PostgreSQL License** (`PostgreSQL`) — for pgvector etc.
- **Python Software Foundation License** (`PSF-2.0`)

Plus a few rarer "permissive enough for commercial embedding" variants —
those need a one-line note in the PR explaining why.

## What we explicitly DO NOT add

Without a separate ADR + Deva sign-off, **we do not introduce**:

| License family | Examples | Why excluded |
|---|---|---|
| **GPL / LGPL** | Neo4j Community Edition, MariaDB, GIMP | Copyleft — propagates to anything that links them; would force customers to open-source their work |
| **AGPL** | MongoDB Community Server, Grafana 9.0+, certain ELK forks | Network-clause copyleft — even hosting them as a service triggers source-disclosure obligations |
| **SSPL** | MongoDB Server 4.4+, Elasticsearch 7.11+, certain Redis | "If you offer the service, you must open-source the entire stack" — kills SaaS resale |
| **BSL** (Business Source License) | Memgraph, MariaDB MaxScale, CockroachDB Enterprise, HashiCorp products post-2023 | Restricts "competing services" for N years before flipping to permissive; case-by-case dangerous |
| **Elastic License 2.0** | Recent Elasticsearch / Kibana | Similar competing-services restriction |
| **"Source-Available" with restrictions** | RedisJSON, Anything with custom "you may not..." clauses | Each is bespoke; usually risky without legal review |

## Specific candidates we've evaluated

### Vector databases (Tier 4 / v0.8)

| Candidate | License | Verdict |
|---|---|---|
| **pgvector** | PostgreSQL License | ✅ **Recommended** — Postgres extension, no new infra |
| Qdrant | Apache 2.0 | ✅ OK |
| Chroma | Apache 2.0 | ✅ OK |
| Weaviate | BSD-3-Clause | ✅ OK |
| LanceDB | Apache 2.0 | ✅ OK |
| Pinecone | (commercial SaaS only) | ❌ Vendor-locked, not OSS |

### Knowledge graphs (Tier 4 / v0.9)

| Candidate | License | Verdict |
|---|---|---|
| **Apache AGE** | Apache 2.0 | ✅ **Recommended** — Postgres extension, no new infra |
| Kuzu | MIT | ✅ OK |
| TerminusDB | Apache 2.0 | ✅ OK |
| **Neo4j Community Edition** | GPLv3 | ❌ **EXCLUDED** — copyleft propagates |
| Neo4j AuraDB (managed) | Commercial SaaS | ⚠️ OK to use as a service, but we don't embed code |
| **Memgraph** | BSL (Business Source License) | ❌ **EXCLUDED** — competing-services clause |
| ArangoDB | Apache 2.0 (Community) | ✅ OK (verify on current release) |

### Search / retrieval

| Candidate | License | Verdict |
|---|---|---|
| **OpenSearch** | Apache 2.0 | ✅ OK (the Elastic fork) |
| Elasticsearch 7.10 and earlier | Apache 2.0 | ✅ OK |
| Elasticsearch 7.11+ | Elastic License 2.0 / SSPL | ❌ **EXCLUDED** |
| MeiliSearch | MIT | ✅ OK |
| Typesense | GPLv3 | ❌ **EXCLUDED** — copyleft |

### Adapters / framework integrations

| Candidate | License | Verdict |
|---|---|---|
| **LangChain core / LangGraph** | MIT | ✅ OK (already in [langchain] extra) |
| **Lyzr SDK** | (we use HTTP API directly, not the SDK) | ✅ Sidestepped — no Lyzr code embedded |
| LlamaIndex | MIT | ✅ OK |
| Haystack | Apache 2.0 | ✅ OK |
| DSPy | MIT | ✅ OK |

## The CI gate

`.github/workflows/ci.yml` runs `scripts/check_licenses.py --strict`, which:

1. Reads each installed dep's SPDX license via `importlib.metadata`.
2. Fails the CI run if any **shipped** dep's license is not in the
   allowlist above.

### Scope: shipped deps only

The gate's mandate is *what reaches a customer deliverable*. It therefore
scopes its scan to the transitive closure of the **shipped requirement
roots** — the core `dependencies` plus the `runtime` and `langfuse`
extras (`SHIPPED_EXTRAS` in the script).

The heavy **opt-in** extras — `easyocr`, `cross-encoder`, `ocr` — are
**out of scope**. They pull in a large ML/GPU stack (`torch`, the NVIDIA
CUDA runtime libs under their proprietary redistributable terms, and
`python-bidi` under LGPL) that an operator installs *deliberately and
separately*; those licenses are the operator's informed choice for that
opt-in path, not part of the default resale-clean deliverable. Bundling
any of them into a default deliverable would still need the ADR + sign-off
process below.

If a dep is reachable from *both* a shipped root and an opt-in extra, it
stays in scope (it ships, so it's policed).

Run `python scripts/check_licenses.py` locally (no `--strict`) for the
full inventory, including the out-of-scope opt-in deps.

## Process for adding a non-allowlist license

If a new dep is genuinely worth a non-allowlist license (very rare),
the process is:

1. Open a follow-up ADR (`docs/adr/00N-license-exception-<dep>.md`)
   that:
   - States the license + why it's needed
   - Documents the specific restriction it imposes on Movate
     deliverables
   - Lists the alternatives considered and why they were inadequate
2. Get explicit Deva sign-off in the ADR commit message.
3. Tag the dep in `pyproject.toml` with an inline `# license: <SPDX>`
   comment so the CI gate's allowlist can be expanded with intent.

## See also

- [`docs/adr/001-cloud-portability.md`](adr/001-cloud-portability.md) —
  the sibling principle: portable + permissive go together.
- [SPDX License List](https://spdx.org/licenses/) — authoritative
  source for license IDs.
- [Choose A License](https://choosealicense.com/) — quick-reference
  for what each license means in practice.
