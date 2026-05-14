"""LLM-bootstrapped project planner (Phase J-3).

Reads a high-level description, calls a planner model with the
available role catalog, and emits a structured plan:

  {
    "project_name": "contract-eval",
    "description": "...",
    "agents": [
      {"name": "contract-parser",
       "template": "document-summarizer",
       "purpose": "extract structure from raw contract text"},
      ...
    ],
    "workflow": ["contract-parser", "checklist-grader", "exec-summary"]
  }

Pure helpers (no I/O beyond the provider call) so the CLI layer can
wire up a real provider OR a MockProvider for tests.

Design tradeoffs (MVP per BACKLOG J-3):
* No domain-specific prompt rewrite per agent — uses role-template
  prompts as-is. Operator customises after scaffold.
* No HITL refinement step — single planner call, take it or leave it.
* No CoT decomposition — one call, one plan.
* Workflow is a simple sequential list (Phase 3 / v0.3 IR).
  Conditional / parallel workflows wait for Phase 7 (LangGraph swap-in).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    Message,
)
from movate.templates import ROLE_TEMPLATES

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedAgent:
    """One agent in a planned project.

    ``name`` is the operator-facing handle (used by ``mdk add <name>``).
    ``template`` MUST be one of :data:`movate.templates.ROLE_TEMPLATES`
    keys; the planner is constrained to that vocabulary.
    """

    name: str
    template: str
    purpose: str


@dataclass(frozen=True)
class ProjectPlan:
    """Structured plan emitted by the planner.

    All fields are validated at parse time — a planner that emits a
    template not in :data:`ROLE_TEMPLATES`, or a workflow that
    references an agent name not in ``agents``, surfaces as a
    :class:`PlanParseError` rather than producing a silently-broken
    plan.
    """

    project_name: str
    description: str
    agents: tuple[PlannedAgent, ...]
    workflow: tuple[str, ...]
    raw_response: str = ""


class PlanParseError(ValueError):
    """The planner returned something we can't use.

    Either malformed JSON, missing required fields, references a
    template not in :data:`ROLE_TEMPLATES`, or references an agent
    name in ``workflow`` that wasn't declared in ``agents``.
    """


# ---------------------------------------------------------------------------
# Planner prompt
# ---------------------------------------------------------------------------


_PLANNER_PROMPT_TEMPLATE = """You are the MDK Planner. The user describes an AI system at a \
high level, and you produce a structured project plan that names which role-template agents \
to scaffold.

# What is MDK

MDK (Movate Development Kit) is a declarative framework for AI agents.
Each agent has a `agent.yaml` spec, a `prompt.md` template, input + output
schemas, and an eval dataset. Agents compose into linear workflows.

# Available role templates

You may ONLY use templates from this list:

{role_catalog}

# User's request

{description}

# Your task

Emit ONE JSON object describing the project. Schema:

```json
{{
  "project_name": "<lowercase-hyphenated name, derived from the request>",
  "description": "<one sentence, paraphrasing the user's intent>",
  "agents": [
    {{
      "name": "<lowercase-hyphenated agent handle>",
      "template": "<MUST be one of the templates above>",
      "purpose": "<one sentence on what this agent does in this project>"
    }}
  ],
  "workflow": [
    "<agent name>",
    "<agent name>"
  ]
}}
```

Rules:
- Use ONLY templates from the list above. Picking a template that doesn't exist will be rejected.
- Workflow is a SEQUENTIAL list of agent names — first runs first, last runs last. \
Each name MUST match an entry in `agents`.
- Prefer 2-4 agents. Don't over-decompose; a single document-summarizer agent is often enough.
- Output ONLY the JSON object. No markdown fences, no prose around it.
"""


def build_planner_prompt(description: str) -> str:
    """Build the user-message prompt for the planner call.

    Inlines the role catalog (name + description) so the planner sees
    every available template's purpose. Catalog is read at call time
    so new role templates surface automatically.
    """
    return _PLANNER_PROMPT_TEMPLATE.format(
        role_catalog=_format_role_catalog(),
        description=description.strip(),
    )


def _format_role_catalog() -> str:
    """Format the ROLE_TEMPLATES registry as a markdown list for the prompt.

    Each entry is ``- <name>: <description>`` — minimal, structured
    enough for the model to pick correctly. We read the description
    from the template's agent.yaml lazily (cheap dict lookup; see
    :func:`movate.cli.add._read_role_metadata` for the same pattern).
    """
    from pathlib import Path  # noqa: PLC0415  -- avoid top-level import cost

    import yaml  # noqa: PLC0415

    from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

    lines: list[str] = []
    for name in sorted(ROLE_TEMPLATES.keys()):
        rel = ROLE_TEMPLATES[name]
        yaml_path = Path(TEMPLATES_DIR) / rel / "agent.yaml"
        description = ""
        if yaml_path.is_file():
            try:
                raw = yaml.safe_load(yaml_path.read_text()) or {}
            except yaml.YAMLError:
                raw = {}
            if isinstance(raw, dict):
                description = str(raw.get("description") or "").strip()
        lines.append(f"- **{name}**: {description}" if description else f"- **{name}**")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner call + parse
# ---------------------------------------------------------------------------


async def call_planner(
    *,
    description: str,
    planner_model: str,
    provider: BaseLLMProvider,
) -> ProjectPlan:
    """Run one planner call against the given provider.

    Returns a :class:`ProjectPlan`. Raises :class:`PlanParseError` on
    a malformed / invalid response — callers should catch this and
    surface a friendly error to the operator, not crash.

    Cost: a single LLM call. Estimated 2K input + 500 output tokens
    for a typical request.
    """
    prompt = build_planner_prompt(description)
    request = CompletionRequest(
        provider=planner_model,
        messages=[Message(role="user", content=prompt)],
        params={
            # Determinism — the planner should produce the same plan for
            # the same description. Operator confidence requires this.
            "temperature": 0.0,
            # Tight cap. A 4-agent plan is ~400 output tokens.
            "max_tokens": 1024,
        },
    )
    response = await provider.complete(request)
    return parse_plan(response.text)


def _parse_json_root(raw: str) -> dict:
    """Decode the planner response into a dict; reject non-dict roots."""
    cleaned = _strip_code_fences(raw)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"planner returned non-JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise PlanParseError(
            f"planner response must be a JSON object, got {type(obj).__name__}"
        )
    return obj


def _check_required_top_level_fields(obj: dict) -> None:
    """Verify every top-level key the planner contract requires."""
    for key in ("project_name", "description", "agents", "workflow"):
        if key not in obj:
            raise PlanParseError(f"planner response missing required field {key!r}")


def _parse_agents(raw_agents: object) -> list[PlannedAgent]:
    """Parse + validate the agents list.

    Per-agent checks: dict shape, required fields (name/template/purpose),
    template-in-catalog, no duplicate names. Each failure surfaces with
    the offending index for fast debugging.
    """
    if not isinstance(raw_agents, list) or not raw_agents:
        raise PlanParseError("'agents' must be a non-empty list")

    valid_templates = set(ROLE_TEMPLATES.keys())
    agents: list[PlannedAgent] = []
    declared_names: set[str] = set()

    for i, raw_agent in enumerate(raw_agents):
        if not isinstance(raw_agent, dict):
            raise PlanParseError(f"agents[{i}] must be an object")
        for key in ("name", "template", "purpose"):
            if key not in raw_agent:
                raise PlanParseError(f"agents[{i}] missing field {key!r}")
        name = str(raw_agent["name"]).strip()
        template = str(raw_agent["template"]).strip()
        purpose = str(raw_agent["purpose"]).strip()
        if template not in valid_templates:
            raise PlanParseError(
                f"agents[{i}].template={template!r} is not a known role; "
                f"valid: {sorted(valid_templates)}"
            )
        if name in declared_names:
            raise PlanParseError(
                f"agents[{i}].name={name!r} duplicates an earlier agent"
            )
        declared_names.add(name)
        agents.append(PlannedAgent(name=name, template=template, purpose=purpose))

    return agents


def _parse_workflow(raw_workflow: object, *, declared_names: set[str]) -> list[str]:
    """Parse + validate the workflow list (sequential chain of agent names)."""
    if not isinstance(raw_workflow, list):
        raise PlanParseError("'workflow' must be a list")
    workflow: list[str] = []
    for i, entry in enumerate(raw_workflow):
        node = str(entry).strip()
        if node not in declared_names:
            raise PlanParseError(
                f"workflow[{i}]={node!r} not in declared agents {sorted(declared_names)}"
            )
        workflow.append(node)
    return workflow


def parse_plan(raw: str) -> ProjectPlan:
    """Parse + validate a planner response.

    Permissive on form (strips markdown fences if the planner wrapped
    the JSON despite the instruction), strict on content (every
    template must be in :data:`ROLE_TEMPLATES`; every workflow entry
    must be a declared agent name). Subroutines split out to keep the
    main function within Ruff's branch limit and the failure modes
    grouped by category.
    """
    obj = _parse_json_root(raw)
    _check_required_top_level_fields(obj)

    project_name = str(obj["project_name"]).strip()
    description = str(obj["description"]).strip()

    agents = _parse_agents(obj["agents"])
    workflow = _parse_workflow(obj["workflow"], declared_names={a.name for a in agents})

    return ProjectPlan(
        project_name=project_name,
        description=description,
        agents=tuple(agents),
        workflow=tuple(workflow),
        raw_response=raw,
    )


def _strip_code_fences(raw: str) -> str:
    """Strip leading/trailing ```...``` if the planner wrapped its JSON."""
    cleaned = raw.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
