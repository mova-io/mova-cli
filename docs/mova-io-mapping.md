# Mova iO Platform → MDK Mapping

> _Reference scorecard. Tracks every building-block in the Mova iO
> platform diagram against its MDK implementation status. Updated
> as features ship. See [BACKLOG.md](../BACKLOG.md) Groups J-L
> for the underlying roadmap._

**Last updated:** 2026-05-14 (Phase J-5 closes)

## Status legend

| Marker | Meaning |
|---|---|
| ✅ | Shipped on main (or open PR ready to auto-merge) |
| 🟡 | Partial — some predecessor exists, doesn't fully match the box |
| 📋 | On roadmap (BACKLOG Group J / K / L) |
| 🚧 | Blocked on engine / architecture work |
| ❌ | Out of MDK's scope (mova-io platform owns it) |

---

## AI Consumption layer

> _Mova iO owns these UX surfaces; MDK exposes the API they sit on._

| Mova iO box | MDK status | Where |
|---|---|---|
| Experience Platform | 🟡 | `mdk serve` (FastAPI runtime exposes `/api/v1/*`) |
| Collaboration Channels | 🟡 | `mdk teams-bot` (Slack adapter is symmetric follow-up) |
| IDE | ✅ | The MDK CLI itself + Mova iO wizard endpoints |
| Conversational AI | ✅ | `mdk chat` REPL, runtime `/runs` endpoint |

## Agent Marketplace

| Mova iO box | MDK status | Where |
|---|---|---|
| Agent Catalog | ✅ | `ROLE_TEMPLATES` registry + `mdk add --list-roles --json` (PR #5) |
| Agent Profiles | ✅ | `AgentSpec.persona / role / capabilities / tags` |
| Agent Usage & Reviews | ❌ | Mova iO platform owns this |
| Agent Search | ✅ | `--list-roles --json` powers wizard facets |

## Agent Creation & Orchestration Layer

| Mova iO box | MDK status | Where |
|---|---|---|
| Intent Recognition | 🟡 | `text-classifier` role works as intent router |
| Planning | ✅ | `mdk plan --from "<desc>"` (PR #17 / Phase J-3) |
| Memory Management | 🟡 → 🚧 | Chat history in `mdk chat`; persistent memory engine is Sprint T |
| MCP Integrators | ✅ | Skills system supports MCP backends |
| Agent Profiles / Job Duties | ✅ | `persona` + `role` fields on AgentSpec |
| Reasoning | ✅ | `Executor` (retries, fallback, prompt render) |
| RAG / GRAG / Hybrid | 🟡 | `mdk knowledge {add, list, query}` (PR #18 / Phase J-4) — surface only; vector engine waits for v0.8 |
| Agent–Agent Integrators | 🟡 | Sequential workflows shipped (v0.3); conditional/parallel = Phase 7 |
| Agent Workflow Orchestration | ✅ | `core/workflow/{spec,ir,compiler,runner}` |
| Reflection | ✅ | `ReflectionConfig` + Executor judge loop (PR #12 / Phase J-1) |
| Prompt Library | 🟡 | Per-role `prompt.md` + `contexts/<name>.md` shared fragments |
| Agent Ops — usage, rate limit | ✅ | `rate_limit`, `tenant_budget`, `mdk jobs` |
| Tool Library | ✅ | `mdk scaffold tool`, `mdk skills`, MCP |
| Explainability | ✅ | `mdk explain <run-id>` (PR #16 / Phase J-2) |
| Model Library | ✅ | LiteLLM + `mdk pricing` + drift detection |
| Agent Observability (log + trace) | ✅ | Langfuse, OTel, `mdk logs`, `mdk trace replay`, baseline diff |

## Safe AI Layer

| Mova iO box | MDK status | Where |
|---|---|---|
| Security Guardrails | 🟡 | Bearer auth, tenant isolation, `ModelPolicy`, `RuntimePolicy`, `SkillPolicy` (infra security) |
| Ethical & Responsible AI | ✅ | Safe AI MVP — PII / topic / content guardrails (PR #8 / Phase J-0) + CLI wrapper (PR #13) |
| Secure Prompt Engineering | 🟡 | `prompt_linter` (existing); injection-pattern detection is a v0.8 extension |

**Coverage moved from 40% to ~80%** with PR #8 + PR #13.

## Model Layer

| Mova iO box | MDK status | Where |
|---|---|---|
| Managed LLMs | ✅ | LiteLLM → OpenAI / Anthropic / Azure / Bedrock |
| Open LLMs | ✅ | LiteLLM → Ollama / vLLM / HuggingFace |
| Fine-tuned LLMs | 🟡 | Can *call* fine-tuned endpoints; tuning happens outside MDK |
| SLMs | ✅ | Same path as open LLMs (Phi, Llama-3.1-8B, etc.) |
| Guardrails | ✅ | Same as Safe AI / Ethical & Responsible (J-0 engine) |
| Evaluation Metrics | ✅ | `mdk eval` — exact-match, LLM-judge, coverage, per-objective, `--gate`, drift baseline |
| FinOps | ✅ | Pricing table, per-run cost, per-tenant budget, drift alarm |
| Chunking | 🟡 | Paragraph split in `chunk_document` (PR #18); semantic chunking is v0.8 |

## Data & Knowledge Layer

| Mova iO box | MDK status | Where |
|---|---|---|
| Structured data | ✅ | JSON-Schema-typed input/output on every agent |
| Unstructured data | 🟡 | Markdown / text ingestion in `mdk knowledge` (PR #18); PDF / Word / HTML = v0.8 |
| Knowledge Asset Catalog | ✅ | `knowledge.yaml` + `mdk knowledge list` (PR #18) |
| Ontology | ❌ | New initiative; not on Movate roadmap |
| Vector Store | 📋 | v0.8 swap-in behind existing `KnowledgeStore` Protocol (PR #18) |
| Data Quality | ❌ | Could surface via eval dimension; not in scope |
| Graph Store | 📋 | Apache AGE / Neo4j; future; not on near roadmap |
| Correlation & Traceability | ✅ | `run_id`, `workflow_run_id`, OTel span hierarchy, Langfuse trace IDs |

## AI Infrastructure

| Component | Status | Where |
|---|---|---|
| Container hosting | ✅ | Azure Container Apps (movate-dev environment) |
| Container registry | ✅ | Azure Container Registry |
| Database | ✅ | Postgres Flex |
| Secrets | ✅ → 📋 | Key Vault + UAI today; `mdk secrets` (item 138) formalises the management layer |
| Logs / observability sink | ✅ | Log Analytics + Langfuse + OTel exporter |
| CI/CD | ✅ | GitHub Actions on `mova-io/mova-cli` |

---

## Per-layer scorecard

| Layer | Before Phase J | After Phase J | Delta |
|---|---|---|---|
| AI Consumption | 85% | 85% | — |
| Agent Marketplace | 50% | 75% | +25% (`mdk add` + role catalog) |
| Agent Creation & Orchestration | 80% | **95%** | +15% (planning + reflection + explain + knowledge) |
| Safe AI | **40%** | **80%** | **+40%** (J-0 engine + J-13 CLI) |
| Model Layer | 70% | 80% | +10% (chunking surface) |
| Data & Knowledge | **30%** | **65%** | **+35%** (knowledge.yaml + retriever) |
| AI Infrastructure | 100% | 100% | — |

**Aggregate coverage:** ~65% → **~85%**. The 15% remaining is concentrated in:

* Vector store engine (Data & Knowledge — Sprint T)
* Memory engine (Agent Creation — Sprint T)
* Compose / multi-agent (Agent Creation — Sprint U)
* `mdk secrets` formalisation (Infrastructure — Sprint O)

---

## Demo arc — the full happy path

```bash
# 1. Bootstrap a project from natural language
mdk plan "Triage support tickets and draft replies" --apply --target ./demo

# 2. Inspect what got scaffolded
cd demo
mdk validate --project

# 3. Add knowledge base for the agents to ground against
mdk knowledge add ./docs/runbook.md --id runbook
mdk knowledge query "what's the SLA for P1 tickets?"

# 4. Enable Safe AI guardrails
mdk guardrails enable input.pii
mdk guardrails test "leak: jane@acme.com"   # see the redact preview

# 5. Run an agent
mdk run triage '{"ticket": "..."}'

# 6. Discover + inspect runs
mdk list
mdk explain cccccccc       # full panel: prompt, response, cost, guardrails, reflection

# 7. Evaluate the team
mdk eval --project --gate 0.7

# 8. Export schemas for downstream type-gen
mdk export json-schema triage --direction input | quicktype -s schema -l ts

# 9. Deploy to Azure
mdk deploy
```

Every command above either ships on `main` or has an open PR ready to merge as of Phase J-5.

---

## Phase J — net new this push

| Phase | What | PR |
|---|---|---|
| J-pre-0 | `mdk add` + 5 role templates | [#5](https://github.com/mova-io/mova-cli/pull/5) |
| J-pre-1 | project-mode `validate` / `eval` | [#6](https://github.com/mova-io/mova-cli/pull/6) |
| docs | BACKLOG Group J | [#7](https://github.com/mova-io/mova-cli/pull/7) |
| J-0 | Safe AI MVP (engine) | [#8](https://github.com/mova-io/mova-cli/pull/8) |
| interrupt | `mdk list` | [#9](https://github.com/mova-io/mova-cli/pull/9) |
| docs | BACKLOG Group K | [#10](https://github.com/mova-io/mova-cli/pull/10) / [#11](https://github.com/mova-io/mova-cli/pull/11) |
| J-1 | Reflection pattern | [#12](https://github.com/mova-io/mova-cli/pull/12) |
| polish | `mdk guardrails` CLI wrapper | [#13](https://github.com/mova-io/mova-cli/pull/13) |
| polish | `mdk export json-schema` | [#14](https://github.com/mova-io/mova-cli/pull/14) |
| docs | BACKLOG Group L (sprints) | [#15](https://github.com/mova-io/mova-cli/pull/15) |
| J-2 | `mdk explain <run-id>` | [#16](https://github.com/mova-io/mova-cli/pull/16) |
| J-3 | `mdk plan --from "<desc>"` | [#17](https://github.com/mova-io/mova-cli/pull/17) |
| J-4 | RAG surface | [#18](https://github.com/mova-io/mova-cli/pull/18) |
| **J-5** | **This doc + PPT slide** | **THIS PR** |

**14 PRs, ~9000+ lines added, 1500+ tests passing.**

---

## Update procedure

When a roadmap item ships:

1. Update the corresponding row's status (📋 → ✅ or 🟡)
2. Add the PR link in the "Where" column
3. Recompute the per-layer percentage (rough numerator/denominator)
4. Update the aggregate coverage delta
5. Add the demo arc step if it's a new operator-facing command

This doc is a living artifact — keep it current as part of every roadmap-merge PR.
