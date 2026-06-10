# MDK Certification Suite

A validation suite that runs **real** MDK workflows to prove the platform is
production-ready — governance, Temporal durability, HITL, tracing, retries,
policy enforcement, cost tracking, audit — not just "it can run a chatbot".

## The testing contract (the real/simulated boundary)

- **Real:** the agents, the workflow, Temporal durable execution, the governance
  shadow + policy enforcement, guardrails, tracing, HITL pause/resume.
- **Simulated:** only the *external SaaS side-effects* (email, ServiceNow, SAP,
  ERP, Slack, identity provisioning). Each call records an auditable row to a
  SQLite ledger (`harness/sim_systems.py`) instead of hitting a live system — so
  scenarios are repeatable, have no external creds, and can assert *what the
  workflow did to the world*. The ledger IS an audit trail.
- **Deterministic:** agent outputs are seeded so platform assertions are stable
  (we assert "the >$5000 branch triggered Director approval", not LLM prose).
  A `--live` mode runs the same bundle against a real LLM for demos.

## Layout

```
harness/
  sim_systems.py   # the simulated-systems ledger + per-system skill entrypoints
  asserts.py       # platform-capability assertions (governance/HITL/cost/side-effects)
scenarios/
  <name>/          # a real MDK project bundle + an acceptance test
```

## Capability coverage (the certification matrix)

Each scenario maps to capabilities it proves. The suite is "certified" when the
matrix (governance · temporal · HITL · tracing · retries · parallelism ·
approvals · policy · KB · evals · cost · audit) is fully green across scenarios.
