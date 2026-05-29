"""LLM-driven workflow scaffolding (ADR 029).

Sits on top of :mod:`movate.scaffold.llm_scaffold` (single-agent generator)
to produce a **multi-step workflow** scaffold — one ``workflow.yaml`` +
state schema + N constituent agents + a workflow-level eval dataset —
from a single natural-language description like::

    "draft a blog post: research the topic, outline it, write the
    draft, then edit for clarity"

The shape detector classifies the description as ``workflow`` vs
``single-agent`` from explicit step markers ("step 1 / step 2", "→",
"then ... then", "pipeline of", "multi-step"). When the operator opts in
explicitly with ``--shape workflow``, detection is skipped.

The graph planner derives node names + per-node intents from the
description, applying the cap (default <=4, max 6) to keep generation
cheap. Each node is then materialized by calling the *existing*
single-agent generator with a node-specific sub-description — we do
NOT fork or duplicate the agent generator. Compose, don't copy.

Public surface:

* :func:`detect_workflow_shape` — pure-Python classifier (no LLM).
* :func:`plan_workflow_graph` — derive (node_name, node_intent) pairs
  from the description. Pure-Python today; can be swapped for an LLM
  planner later without changing the call sites.
* :func:`GeneratedWorkflow` — the materialized workflow payload (the
  workflow.yaml dict + state_schema + per-node GeneratedAgents + the
  workflow-level eval dataset).
* :func:`generate_workflow_from_description` — orchestrates planning +
  per-node single-agent generation. Returns ``GeneratedWorkflow``.
* :func:`write_workflow_files` — materialize to disk in the canonical
  workflow layout (mirrors ``templates/workflow_starter`` from ADR 028).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import TokenUsage
from movate.providers.base import BaseLLMProvider
from movate.scaffold.llm_scaffold import (
    GeneratedAgent,
    GenerationResult,
    generate_agent_from_description,
)

# ---------------------------------------------------------------------------
# Workflow-shape detection (ADR 029)
# ---------------------------------------------------------------------------
#
# Pure-Python classifier — no LLM. The signals were chosen to be precise
# rather than broad: a generic single-agent description (e.g. "FAQ bot
# for our SaaS pricing") must NOT misclassify as a workflow. The markers
# all encode *explicit* multi-step intent.
#
# Signals (any match → workflow):
#   * step numbering — "step 1", "step 2", "stage 1", "phase 2"
#   * arrow notation — "research → outline → write → edit"
#   * sequential connectives at least twice — "first ... then ... then"
#     ("then" appearing twice catches "X then Y then Z" without
#     triggering on a single sequential phrase like "search then summarize")
#   * compound nouns — "pipeline of", "multi-step", "multistep",
#     "workflow of", "two-step", "three-step", "four-step", "five-step",
#     "six-step", "n-step"
#
# A description with *one* "then" is still ambiguous (could be a single
# agent that does X "then" Y as one task), so we require >=2 "then"s OR
# one of the other compound markers.

_ARROW_RE = re.compile(r"[→]|->")
_STEP_RE = re.compile(
    r"\b(?:step|stage|phase)\s*\d+\b",
    re.IGNORECASE,
)
_THEN_RE = re.compile(r"\bthen\b", re.IGNORECASE)
_COMPOUND_MARKERS = (
    "pipeline of",
    "pipeline that",
    "multi-step",
    "multistep",
    "workflow of",
    "two-step",
    "three-step",
    "four-step",
    "five-step",
    "six-step",
    "n-step",
    "multi step",
)

# Verbs we use BOTH to detect comma-list workflows ("research, outline,
# write, edit") AND to slugify a node phrase into a one-word name. Kept
# at module level so :func:`detect_workflow_shape` and
# :func:`_slugify_node_name` share the same vocabulary.
# Order matters for slugify: longer / more specific verbs win.
_NODE_VERBS = (
    "research",
    "outline",
    "draft",
    "write",
    "edit",
    "review",
    "summarize",
    "summarise",
    "extract",
    "classify",
    "translate",
    "validate",
    "verify",
    "polish",
    "fact-check",
    "factcheck",
    "score",
    "grade",
    "approve",
    "route",
    "triage",
    "analyze",
    "analyse",
    "plan",
    "search",
    "rank",
    "format",
    "tag",
    "label",
    "filter",
    "rewrite",
)


def detect_workflow_shape(description: str) -> bool:
    """True iff ``description`` reads as a multi-step workflow (ADR 029).

    Pure-Python — no LLM, no network. Signals were tuned to be precise
    over broad: a generic single-agent description must not misclassify.
    See module-level comment for the full signal list.
    """
    if not description:
        return False
    text = description.lower()
    # Arrow notation always implies a multi-step graph.
    if _ARROW_RE.search(description):
        return True
    # Explicit step / stage / phase numbering.
    if _STEP_RE.search(description):
        return True
    # Compound nouns: "pipeline of", "multi-step", etc.
    if any(marker in text for marker in _COMPOUND_MARKERS):
        return True
    # Two or more "then"s — "X then Y then Z" pattern.
    if len(_THEN_RE.findall(description)) >= _MIN_THEN_FOR_WORKFLOW:
        return True
    # Comma list where >=3 chunks each lead with one of our verbs:
    # "research, outline, write, edit a blog post" reads as a pipeline.
    # We require >=3 verb-led chunks to avoid catching benign 2-word
    # lists like "summarize, classify the email".
    if description.count(",") >= _MIN_VERB_CHUNKS_FOR_WORKFLOW - 1:
        parts = [p.strip() for p in description.split(",") if p.strip()]
        verb_led = sum(
            1
            for p in parts
            if any(re.search(rf"\b{re.escape(v)}\b", p, re.IGNORECASE) for v in _NODE_VERBS)
        )
        if verb_led >= _MIN_VERB_CHUNKS_FOR_WORKFLOW:
            return True
    return False


# ---------------------------------------------------------------------------
# Workflow graph planner — derive (node_name, node_intent) from description
# ---------------------------------------------------------------------------
#
# Pure-Python today. A real LLM planner could replace this without
# changing call sites — the interface is just (description, max_nodes)
# → list[(name, intent)].
#
# Strategy: split the description on the strongest separator we can
# find, in this priority order:
#   1. Arrow / "→" / "->" (highest precision: arrows are unambiguous)
#   2. Numbered steps ("step 1: ..., step 2: ..., step 3: ...")
#   3. "then ... then ..."
#   4. Comma list with verbs ("research, outline, write, edit")
#
# The cap (default 4, max 6) prevents runaway generation. A description
# parsed to 7 segments is truncated to the cap and the surplus appears
# in the final node's intent so no information is silently dropped.

_DEFAULT_MAX_NODES = 4
_HARD_MAX_NODES = 6
_MIN_NODES = 2

# Minimum number of "then" connectives to interpret a description as a
# sequential pipeline ("X then Y then Z"). One "then" is too ambiguous —
# a single agent description like "summarize then classify" could be one
# task, but two or more "then"s reliably signal a workflow.
_MIN_THEN_FOR_WORKFLOW = 2
# Comma-list workflow detection: minimum number of verb-led chunks
# needed to interpret a comma list as a pipeline ("research, outline,
# write, edit"). Two chunks is too noisy — three is the smallest count
# at which "pipeline" reads more strongly than "list".
_MIN_VERB_CHUNKS_FOR_WORKFLOW = 3
# Workflow-level eval dataset minimum size — we always emit at least
# this many cases (replicated from the first if upstream node datasets
# are too thin) so the smoke pass-rate is statistically meaningful.
_MIN_WORKFLOW_EVAL_CASES = 3
# Workflow-level eval dataset cap — beyond this we drop surplus cases
# so a cheap smoke stays cheap.
_MAX_WORKFLOW_EVAL_CASES = 5


def _slugify_node_name(phrase: str, *, fallback_index: int) -> str:
    """Derive a hyphenated lowercase node id from a phrase.

    Looks for a known verb first ("research the topic" → ``research``);
    falls back to the first 1-2 words ("title generation" → ``title-generation``).
    Always returns a name matching the workflow node id regex
    ``^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$``.
    """
    cleaned = phrase.strip().lower()
    for verb in _NODE_VERBS:
        if re.search(rf"\b{re.escape(verb)}\b", cleaned):
            return verb.replace(" ", "-")
    # Fallback: take the first 1-2 alpha tokens.
    tokens: list[str] = re.findall(r"[a-z0-9]+", cleaned)
    if not tokens:
        return f"step-{fallback_index}"
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0]}-{tokens[1]}"


def _segment(description: str) -> list[str]:
    """Split the description into ordered phrases, one per workflow node.

    Returns the raw phrases without further cleanup; the caller dedupes
    + slugifies + caps. An unsplittable description returns ``[description]``
    (which yields a degenerate 1-node graph the caller rejects).
    """
    # 1. Arrow notation — strongest signal.
    if _ARROW_RE.search(description):
        parts = re.split(r"\s*(?:→|->)\s*", description)
        return [p.strip(" .:;,") for p in parts if p.strip()]
    # 2. Numbered steps. Strip leading "step N:" / "stage N -" labels and split.
    if _STEP_RE.search(description):
        # Split on a step boundary; keep the content between markers.
        # We split on the marker itself so the trailing labels disappear.
        parts = re.split(
            r"\b(?:step|stage|phase)\s*\d+\s*[:\-.,]?\s*",
            description,
            flags=re.IGNORECASE,
        )
        return [p.strip(" .:;,") for p in parts if p.strip()]
    # 3. >=2 "then"s — split on "then" (and on a leading "first").
    if len(_THEN_RE.findall(description)) >= _MIN_THEN_FOR_WORKFLOW:
        # Strip a leading "first," / "first ".
        stripped = re.sub(r"^\s*first[\s,:]+", "", description, flags=re.IGNORECASE)
        parts = re.split(r"\s*,?\s*then\s+", stripped, flags=re.IGNORECASE)
        return [p.strip(" .:;,") for p in parts if p.strip()]
    # 4. Comma list — only if every chunk leads with one of our verbs.
    if "," in description:
        parts = [p.strip(" .:;") for p in description.split(",") if p.strip()]
        if len(parts) >= _MIN_NODES and all(
            any(re.search(rf"\b{re.escape(v)}\b", p, re.IGNORECASE) for v in _NODE_VERBS)
            for p in parts
        ):
            return parts
    # 5. "X and Y" — last-resort split when one of our compound workflow
    # markers fired but no stronger separator is present (e.g.
    # "multi-step pipeline of summarize and tag"). We only split on " and "
    # when both halves contain a known verb, to avoid breaking up benign
    # noun phrases ("emails and contacts").
    if " and " in description.lower():
        parts = [p.strip(" .:;,") for p in re.split(r"\s+and\s+", description) if p.strip()]
        if len(parts) >= _MIN_NODES and all(
            any(re.search(rf"\b{re.escape(v)}\b", p, re.IGNORECASE) for v in _NODE_VERBS)
            for p in parts
        ):
            return parts
    return [description.strip()]


@dataclass(frozen=True)
class PlannedNode:
    """One planned workflow node: a slug + the intent that drives generation."""

    name: str
    intent: str


def plan_workflow_graph(
    description: str,
    *,
    max_nodes: int = _DEFAULT_MAX_NODES,
) -> list[PlannedNode]:
    """Derive an ordered list of planned nodes from a description.

    ``max_nodes`` caps the result (the hard ceiling is ``_HARD_MAX_NODES`` —
    a higher value is silently clamped). When the description parses to
    fewer than two segments, the planner gives up and returns a single-node
    list — the caller is expected to fall back to single-agent scaffolding.

    Truncation behavior: if more segments than ``max_nodes`` are parsed,
    surplus content is appended to the final node's intent so no information
    is silently dropped — the cap controls cost, not coverage.
    """
    max_nodes = min(max(max_nodes, _MIN_NODES), _HARD_MAX_NODES)
    segments = _segment(description)
    if len(segments) < _MIN_NODES:
        return [PlannedNode(name="step-1", intent=description.strip())]

    if len(segments) > max_nodes:
        head = segments[: max_nodes - 1]
        tail = " then ".join(segments[max_nodes - 1 :])
        segments = [*head, tail]

    planned: list[PlannedNode] = []
    used_names: set[str] = set()
    for i, segment in enumerate(segments, start=1):
        base = _slugify_node_name(segment, fallback_index=i)
        # Dedupe by appending a numeric suffix — collisions are rare but
        # would otherwise break the node-id uniqueness check in
        # `compile_workflow`.
        name = base
        suffix = 2
        while name in used_names:
            name = f"{base}-{suffix}"
            suffix += 1
        used_names.add(name)
        planned.append(PlannedNode(name=name, intent=segment))
    return planned


# ---------------------------------------------------------------------------
# Per-node sub-description shaping
# ---------------------------------------------------------------------------
#
# Each node is a fully-canonical agent (agent.yaml + prompt + schemas +
# evals). We synthesize a single-agent description that nudges the
# existing generator toward the right shape for that node — a research
# node wants to *gather* from inputs and produce structured findings; a
# write node wants to produce prose; an edit node wants to *refine*
# prose. Everything else (shape selection, schema generation, prompt
# rendering) is delegated to the existing single-agent generator. This
# is the "compose, don't fork" hard constraint from ADR 029.


def _node_sub_description(
    parent_description: str,
    *,
    node: PlannedNode,
    position: int,
    total_nodes: int,
) -> str:
    """Build a single-agent description for one workflow node.

    The description is fed to the EXISTING single-agent generator. We
    embed the node's position in the pipeline so the generator picks
    up + emits the right input / output shape (a middle node reads
    upstream state and writes to downstream state).
    """
    where = (
        "the entry node"
        if position == 1
        else "the final node"
        if position == total_nodes
        else f"step {position} of {total_nodes}"
    )
    return (
        f"A movate agent that handles {node.intent.strip()!r} as {where} "
        f"in a multi-step workflow. The parent workflow's overall task is: "
        f"{parent_description.strip()!r}. "
        f"Read your inputs from the workflow state and produce a structured "
        f"result the next node can consume. Keep the input + output schemas "
        f"focused on this node's responsibility only."
    )


# ---------------------------------------------------------------------------
# GeneratedWorkflow — the materialized payload write_workflow_files consumes
# ---------------------------------------------------------------------------


class GeneratedWorkflow(BaseModel):
    """Everything :func:`write_workflow_files` needs to write a workflow.

    Mirrors :class:`GeneratedAgent` but at the workflow level. The
    per-node :class:`GeneratedAgent` payloads are kept as-is — the
    writer delegates to :func:`write_agent_files` per node.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    workflow_yaml: dict[str, Any] = Field(
        ...,
        description="The workflow.yaml contents as a dict (api_version, kind, nodes, edges, ...).",
    )
    state_schema: dict[str, Any] = Field(
        ...,
        description="The state.json contents — a JSON Schema 2020-12 object.",
    )
    nodes: list[GeneratedAgent] = Field(
        ...,
        min_length=_MIN_NODES,
        description="The per-node generated agents, in pipeline order.",
    )
    node_names: list[str] = Field(
        ...,
        min_length=_MIN_NODES,
        description="The slugified node IDs, parallel to ``nodes``.",
    )
    workflow_evals: list[dict[str, Any]] = Field(
        default_factory=list,
        description="3-5 workflow-level eval cases: {input, expected} dicts.",
    )


@dataclass(frozen=True)
class WorkflowGenerationResult:
    """A workflow scaffold + the rolled-up token usage to generate it."""

    workflow: GeneratedWorkflow
    tokens: TokenUsage


# ---------------------------------------------------------------------------
# State-schema synthesis
# ---------------------------------------------------------------------------


def _synthesize_state_schema(
    *,
    nodes: list[GeneratedAgent],
    node_names: list[str],
    parent_description: str,
) -> dict[str, Any]:
    """Build the workflow-level state JSON Schema.

    The state carries the union of every node's input + output fields,
    with the entrypoint node's REQUIRED inputs becoming the workflow's
    required state keys. Downstream-node inputs that come from upstream
    outputs are kept optional (they're filled in by the runner as the
    workflow executes). Matches the workflow_starter convention.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    # Pass 1: the entry node's required inputs are the workflow's
    # required state keys.
    entry = nodes[0]
    entry_input = entry.input_schema or {}
    entry_props = entry_input.get("properties", {}) or {}
    entry_required = entry_input.get("required", []) or []
    for key, schema_field in entry_props.items():
        properties[key] = schema_field
        if key in entry_required:
            required.append(key)

    # Pass 2: every node's output fields, flattened into state. We don't
    # mark these required — the runner fills them in as nodes execute.
    for agent in nodes:
        output = agent.output_schema or {}
        for key, schema_field in (output.get("properties", {}) or {}).items():
            properties.setdefault(key, schema_field)

    # Pass 3: middle-node inputs that aren't yet in state get added
    # (optional). Lets a node read upstream output that wasn't already
    # captured by Pass 2.
    for agent in nodes[1:]:
        for key, schema_field in ((agent.input_schema or {}).get("properties", {}) or {}).items():
            properties.setdefault(key, schema_field)

    title_bits = " -> ".join(node_names)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{title_bits} workflow state",
        "description": (
            f"Shared state threaded across the {title_bits} pipeline. "
            f"Generated from: {parent_description.strip()!r}."
        ),
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


# ---------------------------------------------------------------------------
# workflow.yaml synthesis
# ---------------------------------------------------------------------------


def _synthesize_workflow_yaml(
    *,
    name: str,
    description: str,
    node_names: list[str],
) -> dict[str, Any]:
    """Build the workflow.yaml dict from the planned node names.

    Mirrors ``templates/workflow_starter/workflow.yaml`` (ADR 028):
    sequential agent nodes, sequential edges, the workflow-level evals
    block pointing at ``./evals/dataset.jsonl``.
    """
    nodes_block = [
        {"id": node_name, "type": "agent", "ref": f"./agents/{node_name}"}
        for node_name in node_names
    ]
    edges_block = [
        {"from": node_names[i], "to": node_names[i + 1]} for i in range(len(node_names) - 1)
    ]
    return {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": name,
        "version": "0.1.0",
        "description": description.strip(),
        "owner": "",
        "state_schema": "./state.json",
        "entrypoint": node_names[0],
        "evals": {
            "dataset": "./evals/dataset.jsonl",
            "gate": 0.7,
        },
        "nodes": nodes_block,
        "edges": edges_block,
        "tags": ["workflow", "scaffolded"],
    }


# ---------------------------------------------------------------------------
# Workflow eval-dataset synthesis
# ---------------------------------------------------------------------------


def _synthesize_workflow_evals(
    *,
    nodes: list[GeneratedAgent],
    node_names: list[str],
) -> list[dict[str, Any]]:
    """Derive 3-5 workflow-level eval cases from per-node sample evals.

    Each workflow case has ``input`` (the entry node's eval input,
    extended with workflow state defaults) and ``expected`` (the final
    node's eval output, optionally merged with intermediate outputs).
    Capped at 5; we always emit at least 3 by repeating the first
    case if the per-node datasets are skimpier than that.
    """
    if not nodes:
        return []
    entry_cases = nodes[0].sample_evals or []
    final_cases = nodes[-1].sample_evals or []
    pairs: list[dict[str, Any]] = []
    for i, entry_case in enumerate(entry_cases[:_MAX_WORKFLOW_EVAL_CASES]):
        case_input = dict(entry_case.get("input", {}) or {})
        if i < len(final_cases):
            case_expected = dict(final_cases[i].get("expected", {}) or {})
        else:
            case_expected = dict(entry_case.get("expected", {}) or {})
        pairs.append({"input": case_input, "expected": case_expected})

    # Ensure at least 3 cases — replicate the first one if upstream
    # generation was skimpy. A trivially-replicated dataset still proves
    # the workflow executes end-to-end (which is the smoke goal).
    while len(pairs) < _MIN_WORKFLOW_EVAL_CASES and pairs:
        pairs.append(json.loads(json.dumps(pairs[0])))
    return pairs[:_MAX_WORKFLOW_EVAL_CASES]


# ---------------------------------------------------------------------------
# generate_workflow_from_description — the orchestrator
# ---------------------------------------------------------------------------


async def generate_workflow_from_description(
    *,
    description: str,
    name: str,
    model: str,
    provider: BaseLLMProvider,
    target_model: str | None = None,
    max_nodes: int = _DEFAULT_MAX_NODES,
) -> WorkflowGenerationResult:
    """Plan a workflow graph from ``description`` + generate every node.

    This composes :func:`plan_workflow_graph` (no LLM) with the existing
    :func:`generate_agent_from_description` (one LLM call per node). The
    workflow.yaml, state schema, and workflow-level eval dataset are
    synthesized from the union of the per-node payloads.

    ``max_nodes`` controls the cost cap (default 4, hard max 6). A
    description that parses to a single-node graph raises
    :class:`ValueError` — the caller is expected to fall back to
    single-agent scaffolding in that case.
    """
    planned = plan_workflow_graph(description, max_nodes=max_nodes)
    if len(planned) < _MIN_NODES:
        raise ValueError(
            f"workflow planner returned {len(planned)} node(s) from description; "
            f"need at least {_MIN_NODES}. Use --shape single-agent or rephrase."
        )

    total_nodes = len(planned)
    generated_agents: list[GeneratedAgent] = []
    rolled = TokenUsage()
    for position, node in enumerate(planned, start=1):
        sub_desc = _node_sub_description(
            parent_description=description,
            node=node,
            position=position,
            total_nodes=total_nodes,
        )
        result: GenerationResult = await generate_agent_from_description(
            description=sub_desc,
            name=node.name,
            model=model,
            provider=provider,
            target_model=target_model,
        )
        # Force the node-specific name onto the generated agent so the
        # on-disk directory matches the workflow.yaml ref. The
        # single-agent generator already coerces this for the CLI path;
        # we belt-and-brace it here in case a future caller composes
        # this function without that coercion.
        result.agent.agent_yaml["name"] = node.name
        generated_agents.append(result.agent)
        rolled = TokenUsage(
            input=rolled.input + result.tokens.input,
            output=rolled.output + result.tokens.output,
            cached_input=rolled.cached_input + result.tokens.cached_input,
        )

    node_names = [n.name for n in planned]
    workflow_yaml = _synthesize_workflow_yaml(
        name=name,
        description=description,
        node_names=node_names,
    )
    state_schema = _synthesize_state_schema(
        nodes=generated_agents,
        node_names=node_names,
        parent_description=description,
    )
    workflow_evals = _synthesize_workflow_evals(
        nodes=generated_agents,
        node_names=node_names,
    )
    workflow = GeneratedWorkflow(
        workflow_yaml=workflow_yaml,
        state_schema=state_schema,
        nodes=generated_agents,
        node_names=node_names,
        workflow_evals=workflow_evals,
    )
    return WorkflowGenerationResult(workflow=workflow, tokens=rolled)


# ---------------------------------------------------------------------------
# write_workflow_files — materialize a GeneratedWorkflow to disk
# ---------------------------------------------------------------------------


def write_workflow_files(workflow: GeneratedWorkflow, *, target_dir: Path) -> None:
    """Materialize a :class:`GeneratedWorkflow` to disk.

    Writes the canonical workflow layout (mirrors
    ``templates/workflow_starter`` from ADR 028)::

        <target_dir>/
          ├── workflow.yaml
          ├── state.json
          ├── agents/
          │     ├── <node-1>/...    (canonical agent layout per node)
          │     ├── <node-2>/...
          │     └── ...
          └── evals/
                └── dataset.jsonl    (3-5 workflow-level cases)

    Per-node agents are written via :func:`write_agent_files` so the
    on-disk shape exactly matches a hand-init'd agent.
    """
    from movate.scaffold.llm_scaffold import write_agent_files  # noqa: PLC0415

    target_dir.mkdir(parents=True, exist_ok=True)

    # workflow.yaml — block-style for readability.
    (target_dir / "workflow.yaml").write_text(
        yaml.safe_dump(workflow.workflow_yaml, sort_keys=False, default_flow_style=False)
    )

    # state.json — JSON Schema 2020-12 object. We pick `.json` (not
    # `.yaml`) to match `workflow_starter`'s on-disk convention.
    (target_dir / "state.json").write_text(json.dumps(workflow.state_schema, indent=2) + "\n")

    # Per-node agents under agents/<node-name>/.
    agents_root = target_dir / "agents"
    agents_root.mkdir(exist_ok=True)
    for node_name, generated_agent in zip(workflow.node_names, workflow.nodes, strict=True):
        write_agent_files(generated_agent, target_dir=agents_root / node_name)

    # Workflow-level evals/dataset.jsonl.
    if workflow.workflow_evals:
        evals_dir = target_dir / "evals"
        evals_dir.mkdir(exist_ok=True)
        (evals_dir / "dataset.jsonl").write_text(
            "\n".join(json.dumps(e) for e in workflow.workflow_evals) + "\n"
        )
