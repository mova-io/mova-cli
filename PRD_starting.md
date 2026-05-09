# MDK — Declarative Agent Platform PRD

## Vision

MDK (Movate Development Kit) is a declarative, Git-native enterprise platform for defining, orchestrating, evaluating, governing, and deploying AI agents and multi-agent workflows.

MDK standardizes AI systems development through:

* Canonical YAML-based contracts
* LangGraph runtime orchestration
* GitHub-native SDLC
* Built-in evals and observability
* Enterprise governance controls
* Portable multi-model execution via LiteLLM
* Azure-native deployment architecture

MDK is not “a wrapper around LangGraph.”

MDK is:

> An enterprise operating system for AI agent development and execution.

---

# 1. Goals

## Primary Goals

### 1. Declarative AI System Definitions

Enable developers to define:

* agents
* workflows
* policies
* evals
* environments

using canonical YAML contracts.

---

### 2. Runtime Standardization

Compile declarative definitions into:

* LangGraph workflows
* typed state engines
* middleware-instrumented runtimes
* observable execution pipelines

---

### 3. Enterprise Governance

Provide:

* versioning
* auditability
* eval gating
* model policies
* RBAC-ready tenancy
* deployment controls

---

### 4. Git-Native Workflow

All AI systems are:

* diffable
* reviewable
* version-controlled
* CI/CD deployable

through GitHub workflows.

---

### 5. Multi-Provider Model Portability

Support:

* Azure OpenAI
* OpenAI
* Anthropic
* Gemini
* Ollama

through a provider abstraction layer backed by LiteLLM.

---

### 6. Production-Grade Observability

Provide:

* tracing
* workflow replay
* cost tracking
* latency analysis
* drift detection
* eval scoring

through Langfuse and OpenTelemetry.

---

# 2. Non-Goals (Phase 1)

MDK will NOT initially include:

* visual drag/drop workflow builders
* Kubernetes operator
* marketplace/registry UI
* autonomous self-modifying agents
* distributed graph execution
* billion-scale vector retrieval
* fine-tuning platform
* full multi-region failover

---

# 3. Core Architectural Principles

## 3.1 Declarative First

All topology/configuration must live in YAML contracts.

Python should only implement:

* tools
* integrations
* custom business logic

NOT orchestration structure.

---

## 3.2 Runtime Abstraction

MDK compiles into LangGraph but does not expose LangGraph internals directly.

LangGraph is replaceable implementation detail.

---

## 3.3 Git as Source of Truth

GitHub repositories are canonical storage for:

* prompts
* workflows
* policies
* schemas
* evals

---

## 3.4 Strong Typing

All runtime state must use:

* Pydantic v2
* TypedDict
* JSONSchema

No untyped workflow state.

---

## 3.5 Evals Are Mandatory

No production deployment without:

* regression tests
* grounding checks
* adversarial evals
* latency thresholds
* cost thresholds

---

# 4. Technology Stack

## CLI

* Typer
* Rich
* asyncio

---

## Runtime

* LangGraph
* LangChain Core only
* FastAPI

---

## Model Layer

* LiteLLM
* Provider abstraction layer

---

## Observability

* Langfuse
* OpenTelemetry

---

## Validation

* Pydantic v2
* JSONSchema

---

## Database

* Azure PostgreSQL Flexible Server
* pgvector extension

---

## Cache / Coordination

* Azure Redis Cache

---

## Object Storage

* Azure Blob Storage

---

## Deployment

* Azure Container Apps

---

## CI/CD

* GitHub Actions

---

## Frontend (future)

* Bolt.new
* React
* Altair

---

# 5. Repository Structure

```text
mdk/
├── agents/
│   ├── faq-agent/
│   │   ├── agent.yaml
│   │   ├── prompt.md
│   │   ├── rules.md
│   │   ├── schemas/
│   │   ├── tools/
│   │   ├── evals/
│   │   └── tests/
│
├── workflows/
│   ├── customer-support/
│   │   ├── workflow.yaml
│   │   ├── state_schema.json
│   │   └── routing.yaml
│
├── policies/
│   ├── pii.yaml
│   ├── grounding.yaml
│   └── safety.yaml
│
├── models/
│   ├── providers.yaml
│   └── routing.yaml
│
├── environments/
│   ├── dev.yaml
│   ├── staging.yaml
│   └── prod.yaml
│
├── evals/
│   ├── templates/
│   ├── datasets/
│   └── judges/
│
├── infra/
├── scripts/
├── tests/
├── src/mdk/
└── pyproject.toml
```

---

# 6. Canonical Agent Definition

## Example

```yaml
api_version: mdk/v1

kind: Agent

metadata:
  name: faq-agent
  version: 1.0.0
  owner: jeremy@movate.com
  description: FAQ support agent

spec:

  role: Customer Support Specialist

  objective: |
    Answer customer questions accurately using approved
    knowledge sources only.

  model:
    provider: azure
    model: gpt-4.1
    temperature: 0.2

  prompts:
    system: ./prompt.md
    rules: ./rules.md

  input_schema: ./schemas/input.json
  output_schema: ./schemas/output.json

  tools:
    - kb_search
    - escalation_lookup

  skills:
    - grounding
    - citation_enforcement

  guardrails:
    pii_redaction: true
    hallucination_detection: true

  observability:
    tracing: true
    token_tracking: true

  evals:
    required: true
    suite:
      - regression
      - adversarial
      - grounding
```

---

# 7. Workflow Definitions

## Example

```yaml
api_version: mdk/v1

kind: Workflow

metadata:
  name: returns-processing

spec:

  state_schema: ./schemas/state.json

  entrypoint: order_lookup

  nodes:

    - id: order_lookup
      type: agent
      ref: agents/order-agent

    - id: ocr
      type: agent
      ref: agents/ocr-agent

    - id: validator
      type: agent
      ref: agents/validator-agent

  edges:

    - from: order_lookup
      to: ocr

    - from: ocr
      to: validator
```

---

# 8. Runtime Compilation Pipeline

```text
YAML Definitions
    ↓
Pydantic Validation
    ↓
MDK Internal Graph Model
    ↓
LangGraph Compilation
    ↓
Middleware Injection
    ↓
Observability Wiring
    ↓
Deployment Packaging
```

---

# 9. Middleware Pipeline

Every workflow node execution must pass through middleware.

## Execution Flow

```text
INPUT
 ↓
Schema Validation
 ↓
Policy Enforcement
 ↓
Cost Tracking
 ↓
Trace Creation
 ↓
Model Execution
 ↓
Output Validation
 ↓
Eval Hooks
 ↓
Trace Persistence
 ↓
OUTPUT
```

---

# 10. CLI Requirements

## Frameworks

* Typer
* Rich

---

# 11. Required CLI Commands

## `mdk init`

Scaffold new agent/workflow.

```bash
mdk init faq-agent
```

---

## `mdk validate`

Validate:

* schemas
* references
* policies
* workflow topology
* eval coverage

---

## `mdk compile`

Compile declarative workflows into runtime graph.

---

## `mdk run`

Execute workflows locally.

Supports:

* SQLite local state
* local traces
* mock providers

---

## `mdk graph`

Generate:

* Mermaid
* PNG
* ASCII
* interactive graph

---

## `mdk trace`

Replay workflow traces.

Show:

* node execution
* latency
* token usage
* costs
* retries
* tool calls

---

## `mdk eval`

Run eval suites.

---

## `mdk deploy`

Deploy to Azure Container Apps.

---

# 12. CLI UX Requirements

Use Rich for:

* progress bars
* spinners
* tree views
* live workflow panels
* colored trace output
* syntax highlighting

---

## Example UX

```text
Compiling workflow...

━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%

✓ Parsed workflow definition
✓ Generated typed state graph
✓ Injected middleware
✓ Bound observability hooks
✓ Generated LangGraph runtime

Build complete in 2.1s
```

---

# 13. Provider Abstraction Layer

MDK must own the provider abstraction.

Developers MUST NOT import LiteLLM directly.

---

## Interface

```python
class BaseLLMProvider:

    async def complete(self, request):
        pass

    async def stream(self, request):
        pass

    async def embed(self, request):
        pass
```

---

## Default Adapter

```text
MDK Runtime
   ↓
Provider Interface
   ↓
LiteLLM Adapter
   ↓
LLM Providers
```

---

# 14. Model Policies

Support model governance.

## Example

```yaml
model_policy:

  allowed_providers:
    - azure
    - anthropic

  deny_models:
    - gpt-4o-realtime

  max_cost_per_run_usd: 0.50

  fallback_chain:
    - azure/gpt-4.1
    - anthropic/claude-sonnet-4
```

---

# 15. Environment Profiles

Support runtime environment overrides.

## Example

```yaml
profiles:

  dev:
    model: gpt-4o-mini

  staging:
    model: gpt-4.1

  production:
    routing:
      primary: gpt-4.1
      fallback: claude-sonnet-4
```

---

# 16. Observability Requirements

## Langfuse Integration

Track:

* prompts
* responses
* traces
* tool calls
* workflow transitions
* latency
* token usage
* costs

---

## OpenTelemetry

Emit:

* metrics
* spans
* runtime events

---

## Trace Replay

Support:

* workflow replay
* node replay
* state inspection
* drift detection

---

# 17. Database Requirements

## PostgreSQL

Use for:

* workflow metadata
* tenancy
* policies
* eval metadata
* workflow state
* JSONB runtime artifacts

---

## pgvector

Use for:

* embeddings
* retrieval
* memory
* grounding search

---

# 18. Redis Requirements

Use Redis for:

* workflow coordination
* streaming state
* session caching
* rate limiting
* event bus/pub-sub

---

# 19. Blob Storage Requirements

Use Azure Blob Storage for:

* uploaded files
* datasets
* eval artifacts
* screenshots
* reports
* trace exports

---

# 20. GitHub Workflow Requirements

## CI/CD Pipeline

### validate.yml

* schema validation
* topology validation

### eval.yml

* regression evals
* adversarial evals
* grounding checks

### deploy.yml

* ACA deployment
* container promotion

### security.yml

* dependency scanning
* secret scanning

---

# 21. Evaluation Framework

## Required Frameworks

* DeepEval
* Ragas
* TruLens

---

## Required Categories

* correctness
* grounding
* task completion
* latency
* tool usage
* workflow adherence
* hallucination risk
* consistency
* safety
* UX/tone

---

# 22. Event Bus Architecture

Internal runtime events:

```python
AgentStarted
AgentCompleted
ToolCalled
RetryTriggered
EvalCompleted
WorkflowFailed
```

Consumers:

* CLI
* Langfuse
* metrics
* dashboards

---

# 23. Security Requirements

## Phase 1

* JWT auth
* tenant isolation
* API key management
* audit logging

---

## Future

* RBAC
* SSO
* Azure AD integration
* policy enforcement engine

---

# 24. Deployment Architecture

## Runtime Topology

```text
GitHub Actions
     ↓
Azure Container Registry
     ↓
Azure Container Apps
     ↓
FastAPI Runtime
     ↓
LangGraph Execution
```

---

# 25. Future Roadmap

## Phase 2

* agent registry
* visual workflow editor
* workflow debugger UI
* deployment promotions
* policy engine UI
* hosted MDK cloud

---

## Phase 3

* distributed execution
* multi-region failover
* dynamic routing optimization
* automated eval tuning
* autonomous workflow healing

---

# 26. Success Criteria

MDK succeeds when:

* workflows are fully declarative
* runtime execution is observable
* evals gate production deployments
* workflows are reproducible
* developers can onboard rapidly
* AI systems become portable and governable
* enterprise customers trust deployment safety

---

# 27. Final Architectural Principle

MDK is NOT:

* a Python framework
* a LangGraph helper
* a YAML wrapper

MDK IS:

> A declarative enterprise operating system for AI agents and workflows.
