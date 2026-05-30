"""Movate-themed demo *scenario* — sample agents, a workflow, and a knowledge graph.

This module is the second half of the demo "wow pack": where
:mod:`movate.core.demo.seeder` generates synthetic *telemetry* (runs / evals /
failures / voice turns) so the dashboards light up, this module generates the
*content* a live walkthrough needs:

* **Sample agent bundles** — 2-3 ready-to-list agents, including one with a
  per-agent ``voice:`` block (ADR 048 D5) and one paired with a **workflow**
  bundle (ADR 037), so ``mdk demo doctor`` and the playground show a real
  fleet, not an empty registry.
* **A knowledge graph** — a small, coherent Movate-support-themed graph of
  :class:`~movate.core.models.Entity` nodes + :class:`~movate.core.models.Relation`
  edges (ADR 010 entities/relations) so the graph viewer / node drill-down
  renders a non-trivial network instead of "no graph — build one with mdk kb
  ingest".

**Design constraints (CLAUDE.md — same as the seeder):**

* **Pure + deterministic.** Normal synchronous Python over stdlib ``hashlib``
  + ``math``; no storage, no async, no I/O, no LLM. The CLI layer
  (:mod:`movate.cli.demo_cmd`) takes the generated records and writes them
  through the :class:`~movate.storage.base.StorageProvider` Protocol. Embeddings
  are produced by a tiny deterministic hash-embedder (see
  :func:`_demo_embedding`) so the bundle is byte-for-byte reproducible and the
  seed stays offline / free — no provider key, no network.
* **Tagged + purgeable.** Every record carries a ``tenant_id`` with the
  :data:`~movate.core.demo.seeder.DEMO_TENANT_PREFIX` (``demo-``) prefix, so
  ``mdk demo clear`` purges the agents, workflow, and graph alongside the
  telemetry via the same ``tenant_id LIKE 'demo-%'`` predicate.
* **Stdlib only.** No new deps.

The embeddings are **illustrative** — a deterministic hash projection, not a
real semantic embedding. They make ``search_entities`` return *something*
stable for a demo; they are not comparable to a real embedding model's space
(the ``embedding_model`` id is stamped ``demo-hash-v1`` so nothing downstream
mistakes them for production vectors).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from movate.core.demo.seeder import DEMO_MARKER_KEY, DEMO_TENANT_PREFIX
from movate.core.models import (
    AgentBundleRecord,
    Entity,
    Relation,
    WorkflowBundleRecord,
)

# ---------------------------------------------------------------------------
# Demo scope constants — the (tenant, agent) the scenario is seeded under.
#
# The graph + sample agents are seeded under ONE canonical demo tenant +
# agent so the graph viewer / playground have a single, predictable target
# to point at. The tenant carries the demo prefix so `mdk demo clear` purges
# everything; the agent name doubles as the graph viewer's "project" path
# segment (GET /api/v1/projects/{agent}/graph).
# ---------------------------------------------------------------------------

DEMO_TENANT_ID = f"{DEMO_TENANT_PREFIX}acme"
"""The canonical demo tenant the scenario (agents + graph) is seeded under.
Matches the first tenant slug the telemetry seeder uses (``demo-acme``) so the
content and the telemetry share a tenant — a coherent single-tenant demo."""

DEMO_GRAPH_AGENT = "support-triage"
"""The agent whose knowledge graph is seeded. Also the first telemetry agent,
so the graph and that agent's runs/evals tell one story. Used as the
``/api/v1/projects/{agent}/graph`` path segment in the viewer."""

DEMO_PROJECT_ID = "default"
"""Project scope (ADR 040) the graph nodes/edges are tagged with — matches the
runtime/CLI default project so the project-scoped viewer query finds them."""

_EMBEDDING_MODEL = "demo-hash-v1"
"""Stamp on every demo entity's ``embedding_model``. Deliberately NOT a real
model id so nothing downstream compares these hash-projection vectors against a
production embedding space."""

_EMBEDDING_DIM = 32
"""Small fixed dimensionality for the deterministic demo embeddings. Big enough
that ``search_entities`` cosine ranking returns a stable, non-degenerate order;
small enough to keep the seed cheap."""


# ---------------------------------------------------------------------------
# Deterministic, offline embedding
# ---------------------------------------------------------------------------


def _demo_embedding(text: str) -> list[float]:
    """A deterministic unit-norm pseudo-embedding for ``text``.

    Projects a SHA-256 digest of ``text`` into :data:`_EMBEDDING_DIM` floats in
    ``[-1, 1]`` and L2-normalizes, so:

    * the same text always yields the same vector (reproducible demo), and
    * ``search_entities``' cosine ranking is well-defined (non-zero norm).

    This is NOT a semantic embedding — it carries no meaning beyond "stable per
    string". Good enough to make the graph's vector-seed step return a
    consistent order for a demo; never used for real retrieval (the
    ``embedding_model`` stamp makes that explicit).
    """
    # Draw enough bytes by hashing (text || counter) until we have DIM floats.
    raw = bytearray()
    counter = 0
    while len(raw) < _EMBEDDING_DIM * 2:
        raw.extend(hashlib.sha256(f"{text}\x00{counter}".encode()).digest())
        counter += 1
    vec = [
        ((raw[i] << 8 | raw[i + 1]) / 65535.0) * 2.0 - 1.0 for i in range(0, _EMBEDDING_DIM * 2, 2)
    ]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _content_hash(*parts: str) -> str:
    """SHA-256 hex of the normalized parts — the entity/relation dedup key."""
    return hashlib.sha256("\x00".join(p.strip().lower() for p in parts).encode()).hexdigest()


def _bundle_hash(files: dict[str, str]) -> str:
    """Content-addressed hash over a bundle's files.

    Mirrors :func:`movate.runtime.agent_resolver.content_hash` (sha256 over the
    JSON of ``files`` with sorted keys) so a re-seed of identical bytes yields a
    stable hash. Re-implemented here (not imported) to keep the cli/core demo
    path free of a ``runtime`` import (CLAUDE.md rule 6 — cli ⊥ runtime)."""
    import json  # noqa: PLC0415 - local to keep module import-light

    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Knowledge-graph content — a Movate customer-support themed network.
#
# Nodes are the kinds of things a support-triage agent's KB graph holds:
# products, plan tiers, policies, integrations, SOPs, and common ticket
# topics. Edges connect them the way a GraphRAG answer would traverse
# ("Pro tier REQUIRES SAML SSO", "Refund SOP APPLIES_TO Billing", ...).
# The shape is hand-authored (not random) so the drill-down tells a story.
# ---------------------------------------------------------------------------

# (name, type, description)
_GRAPH_NODES: tuple[tuple[str, str, str], ...] = (
    ("Movate Assist", "Product", "The flagship AI customer-support agent platform."),
    ("Pro Tier", "Tier", "Mid-tier plan: SAML SSO, priority routing, 10 seats."),
    ("Enterprise Tier", "Tier", "Top plan: SSO, audit log, dedicated VPC, SLA."),
    ("Free Tier", "Tier", "Entry plan: 1 seat, community support, no SSO."),
    ("SAML SSO", "Feature", "SAML 2.0 single sign-on for tenant identity."),
    ("Audit Log", "Feature", "Immutable per-tenant audit trail of admin actions."),
    ("Priority Routing", "Feature", "Routes high-value tickets to senior agents first."),
    ("Refund Policy", "Policy", "14-day pro-rated refund window for paid plans."),
    ("Data Retention Policy", "Policy", "Telemetry retained 90 days, then purged."),
    ("Billing", "Topic", "Invoices, charges, refunds, and plan changes."),
    ("Onboarding", "Topic", "First-touch setup: workspace, SSO, first agent."),
    ("Password Reset SOP", "SOP", "Standard procedure for resetting a locked account."),
    ("Refund SOP", "SOP", "How an agent processes a refund request end-to-end."),
    ("Escalation SOP", "SOP", "When and how to escalate a ticket to engineering."),
    ("Salesforce Integration", "Integration", "Two-way sync of cases with Salesforce."),
    ("Slack Integration", "Integration", "Posts ticket alerts into a Slack channel."),
    ("Zendesk Import", "Integration", "One-time import of historical Zendesk tickets."),
    ("Latency Regression", "Incident", "p95 latency doubled after a rerank-stage deploy."),
    ("Cost Spike", "Incident", "Model-swap drove ~4x spend on one agent for a day."),
    ("Quality Drift", "Incident", "Eval pass-rate fell below the gate over 3 runs."),
)

# (src_name, relation_type, dst_name, description)
_GRAPH_EDGES: tuple[tuple[str, str, str, str], ...] = (
    ("Movate Assist", "HAS_TIER", "Free Tier", "Movate Assist offers a Free tier."),
    ("Movate Assist", "HAS_TIER", "Pro Tier", "Movate Assist offers a Pro tier."),
    ("Movate Assist", "HAS_TIER", "Enterprise Tier", "Movate Assist offers an Enterprise tier."),
    ("Pro Tier", "REQUIRES", "SAML SSO", "The Pro tier unlocks SAML SSO."),
    ("Enterprise Tier", "REQUIRES", "SAML SSO", "Enterprise includes SAML SSO."),
    ("Enterprise Tier", "REQUIRES", "Audit Log", "Enterprise includes the audit log."),
    ("Enterprise Tier", "REQUIRES", "Priority Routing", "Enterprise includes priority routing."),
    ("Pro Tier", "REQUIRES", "Priority Routing", "Pro includes priority routing."),
    ("Refund Policy", "APPLIES_TO", "Billing", "The refund policy governs billing disputes."),
    ("Refund SOP", "IMPLEMENTS", "Refund Policy", "The refund SOP enacts the refund policy."),
    ("Refund SOP", "APPLIES_TO", "Billing", "Refund SOP is used on billing tickets."),
    ("Password Reset SOP", "APPLIES_TO", "Onboarding", "Password reset is part of onboarding."),
    ("Escalation SOP", "ESCALATES_TO", "Latency Regression", "Latency incidents escalate to eng."),
    ("Data Retention Policy", "GOVERNS", "Audit Log", "Retention policy bounds audit-log storage."),
    ("Salesforce Integration", "SYNCS_WITH", "Billing", "Salesforce syncs billing cases."),
    ("Slack Integration", "NOTIFIES", "Escalation SOP", "Slack alerts fire on escalation."),
    ("Zendesk Import", "FEEDS", "Onboarding", "Zendesk import seeds onboarding history."),
    ("Cost Spike", "AFFECTS", "Billing", "The cost spike showed up on the spend dashboard."),
    ("Latency Regression", "AFFECTS", "Priority Routing", "Latency hurt priority-routed tickets."),
    ("Quality Drift", "AFFECTS", "Refund SOP", "Drift degraded refund-ticket answers."),
    ("Onboarding", "INVOLVES", "SAML SSO", "Onboarding configures SAML SSO."),
    ("Billing", "INVOLVES", "Refund Policy", "Billing questions invoke the refund policy."),
)


@dataclass
class ScenarioBundle:
    """Everything :func:`generate_scenario` produces, ready for batch insert.

    The CLI persists ``agents`` + ``workflows`` (registry rows) and
    ``entities`` + ``relations`` (graph) through the storage Protocol. ``stats``
    is a small summary for the seed's success panel + the doctor's checks.
    """

    agents: list[AgentBundleRecord]
    workflows: list[WorkflowBundleRecord]
    entities: list[Entity]
    relations: list[Relation]
    stats: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sample agent + workflow bundle authoring
# ---------------------------------------------------------------------------


def _agent_yaml(name: str, *, description: str, voice: bool, workflow: bool) -> str:
    """Render a minimal-but-valid ``agent.yaml`` for a demo agent.

    Parses cleanly as :class:`~movate.core.models.AgentSpec`. The ``voice:``
    block is added only when ``voice`` is set (ADR 048 D5 — additive, opt-in).
    """
    lines = [
        "api_version: movate/v1",
        "kind: Agent",
        f"name: {name}",
        "version: 1.0.0",
        f"description: {description}",
        "owner: movate-demo",
        "role: support-triage",
        "model:",
        "  provider: openai/gpt-4o-mini-2024-07-18",
        "  params:",
        "    temperature: 0.0",
        "    max_tokens: 512",
        "prompt: prompt.md",
        "schema:",
        "  input: schema/input.json",
        "  output: schema/output.json",
    ]
    if voice:
        lines += [
            "voice:",
            "  enabled: true",
            "  mode: pipeline",
            "  stt: deepgram",
            "  tts: cartesia",
            '  voice_id: "movate-demo"',
            "  language: en-US",
        ]
    if workflow:
        lines.append("# paired with the demo-triage-flow workflow (mdk workflow)")
    return "\n".join(lines) + "\n"


_INPUT_SCHEMA = (
    '{"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}\n'
)
_OUTPUT_SCHEMA = (
    '{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}\n'
)


def _agent_bundle(
    name: str, *, description: str, prompt: str, voice: bool = False, workflow: bool = False
) -> AgentBundleRecord:
    """Build one demo :class:`AgentBundleRecord` under the demo tenant."""
    files = {
        "agent.yaml": _agent_yaml(name, description=description, voice=voice, workflow=workflow),
        "prompt.md": prompt,
        "schema/input.json": _INPUT_SCHEMA,
        "schema/output.json": _OUTPUT_SCHEMA,
        # A tiny eval dataset so `mdk eval <agent> --mock` has cases to run.
        "evals/dataset.jsonl": (
            '{"input": {"question": "How do I reset my password?"}, '
            '"expected": {"answer": "Use the reset link on the login page."}}\n'
            '{"input": {"question": "Can I get a refund?"}, '
            '"expected": {"answer": "Paid plans have a 14-day pro-rated refund window."}}\n'
        ),
    }
    return AgentBundleRecord(
        name=name,
        tenant_id=DEMO_TENANT_ID,
        version="1.0.0",
        created_by=None,  # system/seed import
        content_hash=_bundle_hash(files),
        files=files,
    )


_WORKFLOW_YAML = """\
api_version: movate/v1
kind: Workflow
name: demo-triage-flow
version: 1.0.0
description: A two-step triage workflow — classify the ticket, then draft a reply.
schema:
  state: schema/state.json
nodes:
  - id: classify
    agent: ticket-summarizer
    next: draft
  - id: draft
    agent: support-triage
entry: classify
"""

_WORKFLOW_STATE_SCHEMA = (
    '{"type": "object", "properties": {"question": {"type": "string"}, '
    '"category": {"type": "string"}, "answer": {"type": "string"}}}\n'
)


def _workflow_bundle() -> WorkflowBundleRecord:
    """Build the demo workflow bundle (paired with the support-triage agent)."""
    files = {
        "workflow.yaml": _WORKFLOW_YAML,
        "schema/state.json": _WORKFLOW_STATE_SCHEMA,
    }
    return WorkflowBundleRecord(
        name="demo-triage-flow",
        tenant_id=DEMO_TENANT_ID,
        version="1.0.0",
        created_by=None,
        content_hash=_bundle_hash(files),
        files=files,
        published=True,
    )


def _sample_agents() -> list[AgentBundleRecord]:
    """The 3 demo agents: a plain one, a voice-capable one, a workflow one."""
    return [
        _agent_bundle(
            "support-triage",
            description="Triages inbound support tickets and drafts first replies.",
            prompt=(
                "You are Movate Assist's support-triage agent. Classify the "
                "ticket, then draft a concise, friendly reply grounded in the "
                "knowledge base.\n"
            ),
            workflow=True,
        ),
        _agent_bundle(
            "voice-concierge",
            description="Voice-capable concierge — answers spoken account questions.",
            prompt=(
                "You are Movate's voice concierge. Answer the caller's spoken "
                "question in one or two short sentences suitable for "
                "text-to-speech.\n"
            ),
            voice=True,
        ),
        _agent_bundle(
            "billing-assistant",
            description="Answers billing, invoice, and refund questions.",
            prompt=(
                "You are Movate's billing assistant. Answer billing questions "
                "using the refund policy and current plan tiers.\n"
            ),
        ),
    ]


def _build_graph() -> tuple[list[Entity], list[Relation]]:
    """Materialize the hand-authored node/edge tables into Entity/Relation rows.

    Entities are created first (so the name→id map exists), then relations
    reference their endpoints by id. Every row is demo-tagged via the tenant
    prefix and carries a deterministic ``content_hash`` so a re-seed upserts in
    place rather than duplicating.
    """
    by_name: dict[str, Entity] = {}
    entities: list[Entity] = []
    # A synthetic source-chunk id per node so the provenance panel has
    # *something* to show (the demo doesn't ingest real documents).
    for name, etype, description in _GRAPH_NODES:
        chash = _content_hash(name, etype)
        entity = Entity(
            entity_id=chash[:32],  # stable id derived from the dedup hash
            tenant_id=DEMO_TENANT_ID,
            agent=DEMO_GRAPH_AGENT,
            project_id=DEMO_PROJECT_ID,
            name=name,
            type=etype,
            description=description,
            embedding=_demo_embedding(f"{name} {description}"),
            embedding_model=_EMBEDDING_MODEL,
            content_hash=chash,
            source_chunk_ids=[f"demo-chunk-{chash[:12]}"],
            metadata={DEMO_MARKER_KEY: True, "demo_source": "movate-support-kb"},
        )
        by_name[name] = entity
        entities.append(entity)

    relations: list[Relation] = []
    for src_name, rtype, dst_name, description in _GRAPH_EDGES:
        src = by_name[src_name]
        dst = by_name[dst_name]
        rhash = _content_hash(src.entity_id, dst.entity_id, rtype)
        relations.append(
            Relation(
                relation_id=rhash[:32],
                tenant_id=DEMO_TENANT_ID,
                agent=DEMO_GRAPH_AGENT,
                project_id=DEMO_PROJECT_ID,
                src_entity_id=src.entity_id,
                dst_entity_id=dst.entity_id,
                type=rtype,
                description=description,
                weight=0.9,
                content_hash=rhash,
                source_chunk_ids=[f"demo-chunk-{rhash[:12]}"],
                metadata={DEMO_MARKER_KEY: True},
            )
        )
    return entities, relations


def generate_scenario() -> ScenarioBundle:
    """Generate the full demo scenario (sample agents + workflow + graph).

    Deterministic — no RNG, no clock dependence — so a re-seed is byte-for-byte
    reproducible and idempotent (upserts in place). All records are demo-tagged
    (``demo-`` tenant prefix) and ready to batch-insert through the storage
    Protocol. See :class:`ScenarioBundle`.
    """
    agents = _sample_agents()
    workflows = [_workflow_bundle()]
    entities, relations = _build_graph()
    stats = {
        "agents": len(agents),
        "workflows": len(workflows),
        "graph_nodes": len(entities),
        "graph_edges": len(relations),
        "voice_agents": sum(1 for a in agents if "voice:" in a.files["agent.yaml"]),
    }
    return ScenarioBundle(
        agents=agents,
        workflows=workflows,
        entities=entities,
        relations=relations,
        stats=stats,
    )
