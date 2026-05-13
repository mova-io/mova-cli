# ADR 002 — Skills and shared contexts as first-class agent.yaml objects

**Status:** Proposed
**Date:** 2026-05-12
**Deciders:** Engineering + Deva (Movate)
**Context window:** v0.6 → v0.8 design horizon
**Supersedes:** N/A
**Related:** [ADR 001 — Cloud-portability](001-cloud-portability.md); PRD Phase 7

---

## Decision

We introduce **two new top-level agent-adjacent kinds**:

1. **`kind: Skill`** — a reusable callable an agent can invoke during a
   turn, declared in `skills/<name>/skill.yaml`. One contract surface,
   pluggable implementation backends (`python`, `http`, `mcp`).
2. **A `contexts/` folder** — shared prompt fragments referenced by
   name from `agent.yaml: contexts:`, prepended to the agent's prompt
   at render time.

The two are deliberately separate concerns — skills are *behavioral*
(the agent does something), contexts are *documentary* (the agent
knows something). Conflating them at this layer would make either
half harder to evolve. Knowledge bases / RAG are a third concern
(`knowledge/` folder, Tier 3) and stay out of this ADR.

Operators see a unified project layout:

```
my-project/
├── policy.yaml
├── agents/<name>/agent.yaml            # references skills + contexts by name
├── skills/<name>/skill.yaml            # NEW
├── contexts/<name>.md                  # NEW
└── knowledge/                          # FUTURE — Tier 3
```

This ADR exists because skill support is the **single biggest demo-visibility lever** left in the v1.x roadmap (the only Phase 7 piece customers immediately notice in a demo) and because doing it wrong locks us into either an over-coupled tool registry or a fragmented per-runtime tool API.

## Context

PRD Phase 7 reserves `skills`, `tools`, and tool registries as a unit.
Today (v0.6) agents are single-turn: input → prompt → model → output.
Many real customer demos need:

* **Tool-use loops** (web search, calculator, internal HTTP API
  lookup). Currently impossible without each customer hand-rolling
  it in their agent's runtime.
* **Shared style / glossary / terminology** across a fleet of agents
  serving the same brand. Today this is copy-pasted into every
  `prompt.md`. Deva flagged this as a maintenance smell on May 12.
* **External tool servers** (MCP). A Movate-internal MCP server
  exposing CRM or warranty data should plug in without bespoke
  per-agent code.

Two adjacent risks if we don't decide deliberately:

1. **Per-runtime tool fragmentation.** LiteLLM, native Anthropic, and
   native OpenAI all support tool-use, but via slightly different
   wire shapes. If `kind: Skill` ends up coupled to one runtime,
   every other runtime grows its own parallel implementation. The
   `BaseLLMProvider` Protocol gets a `complete_with_tools` parameter
   that diverges in subtle ways.
2. **Skill ≠ Agent ≠ Knowledge-base ≠ Context** — collapsing any two
   leaves us with a leaky abstraction that's worse for both. We've
   seen this in MDK (where "skills" tried to also be RAG).

## Decision detail

### 1. `kind: Skill` — one contract, pluggable backend

Every skill is a folder under `skills/<slug>/` with:

```
skills/web-search/
├── skill.yaml            # contract (analogous to agent.yaml)
├── impl.py               # Python entry — when implementation.kind = python
└── README.md             # operator docs (optional)
```

`skill.yaml` shape:

```yaml
api_version: movate/v1
kind: Skill
name: web-search
version: 0.1.0
description: Search the web via DuckDuckGo

# Input/output use the same shorthand syntax as agent.yaml (PR #47).
# A path-string still works for complex contracts.
input:
  query: string
  max_results: integer?
output:
  results:
    - title: string
      url: string
      snippet: string

implementation:
  kind: python                 # | http | mcp
  entry: skills.web_search.impl:search
  # Backend-specific fields go here. For `kind: http`:
  #   url: https://internal-api.example.com/search
  #   auth: bearer-from-env:MY_TOKEN
  # For `kind: mcp`:
  #   server: stdio:./mcp-servers/crm

cost:
  per_call_usd: 0.001          # participates in agent budget accounting

# Optional safety hints, surfaced in `mdk show <skill>` and
# enforceable via project policy in a follow-up.
side_effects: read-only        # | mutates-state | network | filesystem
```

The compiled `SkillSpec` is processed analogously to `AgentSpec`:
schemas validated, implementation backend resolved at runtime, cost
recorded against the calling agent's budget. **No new YAML dialect** —
this is the same loader + validator stack with one more `kind`.

### 2. `implementation.kind` — three backends, one Protocol

```python
class SkillBackend(Protocol):
    name: str  # python | http | mcp

    async def execute(
        self,
        skill: SkillSpec,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]: ...
```

* **`python`**: `entry` is an `importlib`-style `pkg.mod:func` pointer.
  Resolved at skill registration; the function is called with
  `input` and the `ctx` (which exposes `trace_id`, `tenant_id`,
  budget hooks).
* **`http`**: POST `input` as JSON to `url`, parse JSON response, validate
  against `output` schema. Auth via env var.
* **`mcp`**: spawn or connect to the MCP server; route the call via the
  MCP `tools/call` method.

All three backends produce a `dict[str, Any]` that the executor
schema-validates before feeding it back into the LLM as a
`tool_result`. The backend's failure mode (exception, non-2xx HTTP,
MCP error) maps onto a uniform `SkillError` so the LLM sees a
consistent shape regardless of backend.

**Day-1 scope: `python` only.** HTTP + MCP land in follow-up PRs once
the Python path is proven. This keeps the first PR's surface
manageable.

### 3. Agent references skills by name

```yaml
# agents/faq-agent/agent.yaml
skills:
  - web-search
  - calculator
```

At load time:

1. The agent loader resolves each name against the project's `skills/`
   registry.
2. For each resolved `SkillSpec`, the loader compiles its input
   schema into the model's native tool-spec format. For LiteLLM this
   is OpenAI-style function-call JSON Schema; native Anthropic gets
   the Anthropic-tool-use shape. The `BaseLLMProvider` Protocol
   grows a `to_tool_spec(skill) -> dict` method so the conversion
   stays runtime-local.
3. The executor's `execute()` enters a **tool-use loop** when the
   agent has any skills:

```
loop:
  response = await provider.complete(request, tools=tool_specs)
  if response.kind == "final":
    break
  if response.kind == "tool_use":
    skill = registry.resolve(response.tool_name)
    result = await backend.execute(skill, response.tool_input, ctx)
    request = request.with_tool_result(response.tool_id, result)
    continue
```

* **Max-turns guard**: `agent.yaml: limits.max_tool_turns` (default
  10). Hard cap to defeat infinite tool-use loops a model can get
  stuck in.
* **Cost tracking**: each skill call's `cost.per_call_usd` is added
  to the run's `metrics.cost_usd` so budget enforcement composes
  naturally.
* **Fallback chain interaction**: if a `complete()` call inside the
  loop exhausts retries, we fall through to the next provider in
  `model.fallback`, but **only at turn boundaries** — mid-loop
  provider swaps complicate state recovery. The fallback provider
  starts the loop fresh from turn 0 of the request. Documented as a
  known trade-off.

### 4. Contexts — shared prompt fragments

A `contexts/` folder at the project root. Each entry is a markdown
file:

```
contexts/
├── company-style-guide.md
├── terminology.md
└── safety-disclaimer.md
```

Agents reference them by base name:

```yaml
# agent.yaml
contexts:
  - company-style-guide
  - terminology
```

At render time, the loader prepends each context's body to the
prompt template (with a `\n\n---\n\n` separator), in the listed
order. Contexts are pure data; no Python, no Jinja side effects.

**Why prepend not interpolate?** v1 keeps the surface trivial — no
template syntax to learn, no per-agent setup. A Jinja-namespace
form (`{{ context.style }}`) becomes a v2 escape hatch if anyone
needs interpolation.

### 5. `mdk show` and `mdk validate` extend naturally

* `mdk show <skill>` mirrors `mdk show <agent>`.
* `mdk show <agent>` adds rows for `skills: [...]` and
  `contexts: [...]`.
* `mdk validate` is rerun on every `skill.yaml` in the project as
  well as every `agent.yaml`. The cross-link check (agent references
  a skill that exists) lands here.

### 6. Operator commands

`mdk skills <subcommand>`:

* `list` — every registered skill in the current project.
* `run <name> '<json input>'` — invoke a skill directly (without a
  surrounding agent) for debugging.
* `scaffold <name>` — create `skills/<name>/skill.yaml` +
  `impl.py` + `README.md` from a template.

Ship `list` and `scaffold` in v1; `run` is nice-to-have for v1.1.

## What this rules in

These are explicit consequences of the decision:

* **Skills get their own `kind:` + folder**, sibling to agents and
  (future) workflows. They aren't a sub-block of `agent.yaml`.
* **`BaseLLMProvider` grows a tool-use surface**: `complete()` gains
  a `tools` parameter and the response carries a `kind` discriminator
  (`final` | `tool_use`). Each provider implements `to_tool_spec()`
  to convert a `SkillSpec` into its native format.
* **Cost participates in budget**: skill `per_call_usd` is added to
  `RunRecord.metrics.cost_usd` so existing tenant budget enforcement
  and `policy.max_cost_per_run_usd` both work without changes.
* **Tool-use loop is bounded**: hard `max_tool_turns` cap, default 10,
  per-agent override.
* **Contexts are markdown only** in v1. No templating, no scripting.
* **Knowledge / RAG stays separate** (`knowledge/`, Tier 3). This
  ADR does not commit any RAG choices.

## What this rules out

* **Skills-as-Python-only.** The Protocol is multi-backend from day
  1, even if only `python` ships in the first PR. Locking to one
  backend would force a rewrite when MCP support lands.
* **Per-agent inline tool definitions.** No `tools: [...]` block in
  `agent.yaml` with the JSON Schema spelled out — that's the path
  to per-agent duplication. Tools are skills; skills are referenced
  by name.
* **A separate global "tool registry" config.** The filesystem IS
  the registry. `skills/` discovery happens at agent-load time.
* **Mid-loop provider fallback.** Stays at turn boundaries. Mid-loop
  fallback requires re-running tool calls or carrying provider-specific
  tool-state across the swap; too complex for v1.
* **Streaming + tool-use in the same call.** Streaming is paused
  during the tool-use loop. Final-turn streaming still works.
* **Contexts as executable code.** No Python in `contexts/`. If you
  need dynamic context, that's a skill (a `python` skill that
  generates the context on-the-fly).

## Concrete implications

| Area | Change |
|---|---|
| `core/models.py` | New `SkillSpec` Pydantic model; new `kind: Skill` discriminator; `AgentSpec` adds `skills: list[str]` and `contexts: list[str]` fields. |
| `core/loader.py` | New `load_skill(path) -> SkillBundle`; project-level `load_skill_registry()` scans `skills/*/skill.yaml`; agent loader resolves names against the registry. |
| `core/skill_backend/{python,http,mcp}.py` | One module per backend implementing `SkillBackend`. v1 ships `python` only. |
| `providers/base.py` | `BaseLLMProvider.complete()` gains `tools` param + `kind: "tool_use" \| "final"` in `CompletionResponse`; new `to_tool_spec(skill)` method. |
| `providers/litellm.py` | OpenAI-style tool conversion. |
| `providers/{anthropic,openai_native}.py` | Native tool-use wiring per SDK. |
| `core/executor.py` | Tool-use loop wrapping the existing single-shot path. Max-turns guard. |
| `cli/skills_cmd.py` | New `mdk skills list | scaffold` (and later `run`). |
| `cli/validate.py` | Reruns validation on every `skill.yaml`; cross-link check (agent → skill) fails loudly on missing references. |
| `cli/show.py` | Renders skills + contexts on `mdk show <agent>`; new branch for `mdk show <skill>`. |
| `templates/skill_init/` | Scaffold templates for `mdk skills scaffold`. |
| `contexts/` loader | Trivial — concatenate markdown files at prompt render time. |

## MVP scope (PR 1 of N)

**Ship in one PR:**

1. `SkillSpec` + `kind: Skill` loader.
2. `python` backend only.
3. `AgentSpec.skills` field + registry resolution at agent load.
4. Tool-use loop in `Executor.execute()` with max-turns guard.
5. LiteLLM tool-spec conversion (one runtime).
6. `mdk validate` extended; `mdk show <skill>`.
7. Tests: skill loader, registry, dispatch (mocked Python call), full
   tool-use loop against `MockProvider` with a scripted tool result.

**Defer to follow-ups (sequenced):**

* PR 2: `http` backend.
* PR 3: `mcp` backend.
* PR 4: Contexts (`contexts/` folder + agent reference).
* PR 5: `mdk skills scaffold` template + `mdk skills run` debug command.
* PR 6: Native-Anthropic / native-OpenAI tool-spec conversion.
* PR 7: Project policy gates on skill `side_effects` annotations.

## Open questions (resolve before PR 1)

1. **Tool-use loop ownership.** Does the loop live in `Executor` (today's
   pattern — provider-agnostic) or inside `BaseLLMProvider.complete()`
   (each provider owns its own loop)? Trade-off: the former keeps
   provider implementations thin but requires translating tool-result
   shapes; the latter risks divergence.
   *Tentative answer: in `Executor`.* Each provider exposes
   `to_tool_spec()` + parses tool-use responses into a normalized
   shape; the loop lives one layer up.

2. **`SkillError` taxonomy.** What types do we surface to the model in
   a `tool_result` content block when a skill fails? At minimum:
   `not_found`, `validation_failed`, `backend_error`, `timeout`,
   `budget_exceeded`. Mirrors `ErrorInfo` on RunResponse.

3. **Per-skill timeouts.** Inherit from agent `timeouts.call_ms`, or
   declare on the skill (`skill.yaml: timeouts:`)? Skills like
   `web-search` may need longer; calculator wants instant.
   *Tentative: per-skill override, inherits from agent if absent.*

4. **`tool_choice` parameter.** Some providers let you force a
   specific tool (`tool_choice: { name: "web-search" }`) for the next
   call. Do we surface this in our API? Probably yes, but defer to
   PR 6 with native-SDK support.

5. **`mdk init` template behavior.** Should the default scaffold
   create a `skills/` folder + a sample skill? Probably yes —
   discoverability matters. Probably no — most simple agents don't
   need skills. *Tentative: no skills by default, `mdk init -t with-skills`
   gets one.*

## When to revisit

Revisit this ADR when one of:

* **More than 3 customer engagements** need a non-Python backend
  (HTTP / MCP) at the same time — that's the signal to prioritize
  PR 2/3 over other Tier 2 work.
* **Provider tool-use shapes diverge meaningfully**. If two
  providers' tool-use semantics can't be normalized through
  `to_tool_spec()`/parse — e.g., one streams tool-call deltas and
  the other emits them whole — we need to revisit whether the loop
  belongs in `Executor` or per-provider.
* **Skills + workflows interact.** If workflows want to invoke skills
  directly (`kind: Workflow` node calls a `kind: Skill` without an
  agent in between), we need a separate node type and a new
  contract — not a hack on `kind: Agent`.
* **RAG / knowledge integration** wants to surface as a skill. The
  cleanest path is probably: knowledge bases are their own kind
  (`kind: KnowledgeBase`), and a built-in `retrieval` skill knows
  how to query them. That decision lives in a separate ADR.

## Related

* PRD Phase 7 — original mention of "skills, tools, multi-provider
  routing, RBAC."
* PR #47 — schema shorthand. `skill.yaml: input:` reuses the same
  syntax.
* PR #48 — layered defaults. Skills' `timeouts:` and `cost:` blocks
  may eventually become layerable too (skill-level defaults +
  agent-level overrides).
* MDK's `skills/` directory — pre-movate-cli prototype. We're
  intentionally NOT porting that code; this ADR designs the surface
  fresh. Lessons learned: don't conflate skill-with-RAG, keep the
  contract surface small, treat skills as siblings to agents rather
  than children.
