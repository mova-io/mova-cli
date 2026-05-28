"""Judge Engineer — author a ``judge.yaml`` for an agent (#judge-engineer).

Given an :class:`AgentBundle` (its spec + prompt + schemas + any
sample cases), generate a complete ``judge.yaml`` for ``mdk eval`` to
score the agent's output against a free-text reference.

The dominant constraint is **backward compatibility with the existing
judge.yaml schema** (CLAUDE.md rule 5; judge.yaml is a flagged
surface). The canonical, validated shape is the flat
:class:`~movate.core.models.JudgeConfig`::

    method: llm_judge
    model:
      provider: <cross-family>
      params: {temperature: 0.0}
    rubric: |
      <free-text rubric, the scoring guide>
    threshold: 0.7

The task spec talks about "rubric dimensions" (accuracy, tone, etc.).
We DO NOT add a new top-level ``dimensions:`` YAML key — that would
break ``JudgeConfig.model_validate`` for everything downstream (eval
engine, panel mode, workflow eval). Instead, the dimensions are
expressed *inside the existing ``rubric:`` text* as a structured,
authored-by-Claude markdown rubric with a per-dimension scoring
breakdown. The dimension list itself surfaces on the API response
(``rubric_dimensions``) so the caller can render / edit them, but the
YAML stays in the canonical shape — old eval code keeps working
byte-for-byte.

This module is pure transform — no I/O, no filesystem, no env
inspection. It takes a :class:`BaseLLMProvider`, a configured model,
the agent spec + prompt, and returns a :class:`GeneratedJudge`. The
runtime endpoint + the ``mdk judge`` CLI each compose it identically
(CLAUDE.md rule 6, ``cli ⊥ runtime``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from movate.core.models import JudgeConfig, JudgeMethod, ModelConfig
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    Message,
)

if TYPE_CHECKING:
    from movate.core.loader import AgentBundle
    from movate.providers.pricing import PricingTable


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class JudgeEngineerError(Exception):
    """Raised on any judge-author failure (LLM mis-behavior, malformed
    YAML, budget exceeded). The caller maps to an HTTP status.

    ``status_code`` mirrors :class:`AgentCreationError`'s convention:

    * **400** — bad request shape (unknown dimension, etc.)
    * **402** — budget exceeded before / during generation
    * **422** — generated / supplied YAML is malformed (won't load
      as :class:`JudgeConfig`)
    * **500** — unexpected LLM / dependency failure
    """

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class GeneratedJudge:
    """The result of :func:`generate_judge` — what the endpoint returns.

    ``judge_yaml`` is the full YAML body (a string) that, on commit,
    lands at ``<agent_dir>/evals/judge.yaml``. Already validated against
    :class:`JudgeConfig`.

    ``rubric_dimensions`` is the dimension list the rubric covers. This
    mirrors the dimensions baked INTO the rubric text — surfaced on the
    response so the caller can render an edit UI / cite the inferred
    shape without re-parsing the markdown.

    ``rationale`` is one or two sentences explaining WHY these
    dimensions were picked (e.g. "tone is included because the agent's
    description mentions 'empathetic'"). Useful in the UI; not persisted.

    ``tokens_used`` / ``cost_usd`` surface generation cost so the caller
    can show / log it. Cost is 0.0 when the pricing lookup misses.
    """

    judge_yaml: str
    rubric_dimensions: list[str]
    rationale: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    raw_response: str = field(default="", repr=False)


# ---------------------------------------------------------------------------
# Default dimensions per agent shape
# ---------------------------------------------------------------------------

# These dimensions are the *default* set when the caller doesn't pass an
# explicit ``rubric_dimensions``. Inference order matters — first match
# wins. RAG is checked first because a RAG agent that ALSO declares
# skills (kb-vector-lookup is itself a skill) should score on groundedness,
# not on tool_appropriateness.
_RAG_DIMENSIONS: tuple[str, ...] = (
    "accuracy",
    "groundedness",
    "citation_quality",
    "completeness",
)
_TOOL_USE_DIMENSIONS: tuple[str, ...] = (
    "accuracy",
    "tool_appropriateness",
    "error_handling",
    "schema_adherence",
)
_WORKFLOW_DIMENSIONS: tuple[str, ...] = (
    "accuracy",
    "step_adherence",
    "escalation_judgment",
    "completion",
)
_DEFAULT_DIMENSIONS: tuple[str, ...] = (
    "accuracy",
    "tone",
    "schema_adherence",
    "completeness",
)


def default_dimensions_for(bundle: AgentBundle) -> list[str]:
    """Infer a sensible default ``rubric_dimensions`` list from an agent.

    Inference rules (first match wins):

    1. **RAG / knowledge-grounded** — the agent declares ``knowledge:``
       OR a built-in retrieval skill (``kb-vector-lookup``) is in
       ``skills`` OR ``retrieval.auto_into`` is set. Groundedness +
       citation quality dominate.
    2. **Tool-use** — the agent declares any non-retrieval ``skills``.
       Tool appropriateness + error handling dominate.
    3. **Workflow-shaped** — the agent's description / prompt mentions
       multi-step / escalation language. Step adherence + completion
       dominate. Conservative: catches the ``mdk init`` workflow
       templates; misses bespoke workflows (which fall through to the
       generic shape, fine — operator can override).
    4. **Generic single-shot** — accuracy + tone + schema adherence.

    Returns a fresh list every call (caller may mutate).
    """
    spec = bundle.spec

    # ---- RAG / grounding shape ----
    if spec.knowledge is not None:
        return list(_RAG_DIMENSIONS)
    if getattr(spec.retrieval, "auto_into", "") or getattr(spec.retrieval, "query_from", ""):
        return list(_RAG_DIMENSIONS)
    if "kb-vector-lookup" in spec.skills:
        return list(_RAG_DIMENSIONS)

    # ---- Tool-use shape (any non-retrieval skill) ----
    non_retrieval_skills = [s for s in spec.skills if s != "kb-vector-lookup"]
    if non_retrieval_skills:
        return list(_TOOL_USE_DIMENSIONS)

    # ---- Workflow shape — conservative keyword match on description ----
    # Bare-agent workflows (the ``mdk init --workflow`` templates) ship
    # descriptions / prompts with these markers. A false negative is
    # fine — the operator pastes their own dimensions; a false positive
    # would be more confusing because the rubric would score steps the
    # agent doesn't have.
    desc = (spec.description or "").lower()
    prompt = (bundle.prompt_template or "").lower()
    workflow_markers = ("step ", "escalate", "escalation", "handoff", "workflow", "multi-step")
    if any(marker in desc for marker in workflow_markers) or any(
        marker in prompt for marker in workflow_markers
    ):
        return list(_WORKFLOW_DIMENSIONS)

    return list(_DEFAULT_DIMENSIONS)


# ---------------------------------------------------------------------------
# Dimension validation
# ---------------------------------------------------------------------------

_DIMENSION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_dimensions(dimensions: list[str]) -> list[str]:
    """Validate + dedupe a caller-supplied dimension list.

    Each entry must be lowercase snake_case, 1-64 chars, leading letter.
    Order is preserved; duplicates after normalization are dropped.
    Empty list → :class:`JudgeEngineerError` (caller should call
    :func:`default_dimensions_for` instead of passing empty).
    """
    if not dimensions:
        raise JudgeEngineerError(
            "rubric_dimensions cannot be empty — omit the field to use defaults",
            status_code=400,
        )
    seen: set[str] = set()
    out: list[str] = []
    for raw in dimensions:
        if not isinstance(raw, str):
            raise JudgeEngineerError(
                f"rubric_dimensions entries must be strings, got {type(raw).__name__}",
                status_code=400,
            )
        name = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if not _DIMENSION_NAME_RE.match(name):
            raise JudgeEngineerError(
                f"rubric_dimensions entry {raw!r} is not a valid identifier "
                "(lowercase snake_case, 1-64 chars, leading letter)",
                status_code=400,
            )
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# YAML validation
# ---------------------------------------------------------------------------


def validate_judge_yaml(judge_yaml: str) -> JudgeConfig:
    """Parse + validate ``judge_yaml`` against :class:`JudgeConfig`.

    Used by both the generation path (sanity-check the LLM's output
    before returning it to the caller) and the commit path (defense
    against an operator's hand edit landing a broken judge.yaml).

    Raises :class:`JudgeEngineerError` (status 422) on YAML parse
    failure or schema mismatch.
    """
    try:
        data = yaml.safe_load(judge_yaml)
    except yaml.YAMLError as exc:
        raise JudgeEngineerError(
            f"generated judge YAML is not valid YAML: {exc}",
            status_code=422,
        ) from exc
    if not isinstance(data, dict):
        raise JudgeEngineerError(
            "judge YAML must be a top-level mapping (got "
            f"{type(data).__name__})",
            status_code=422,
        )
    try:
        return JudgeConfig.model_validate(data)
    except ValidationError as exc:
        raise JudgeEngineerError(
            f"judge YAML failed JudgeConfig validation:\n{exc}",
            status_code=422,
        ) from exc


# ---------------------------------------------------------------------------
# Prompt + generation
# ---------------------------------------------------------------------------


_JUDGE_ENGINEER_PROMPT_TEMPLATE = """You are a senior eval engineer authoring an LLM-as-judge rubric for an AI \
agent. Your output will be saved as `evals/judge.yaml` and used by `mdk \
eval` to score the agent's runs.

# Agent under evaluation

Name:        {name}
Description: {description}
Persona:     {persona}
Role:        {role}
Goals:       {goals}

# Agent prompt template

```
{prompt}
```

# Input schema (the cases the agent receives)

```json
{input_schema}
```

# Output schema (what the agent must return)

```json
{output_schema}
```

{examples_block}

# Rubric dimensions to cover

{dimensions_block}

# Your task

Write a complete rubric covering EXACTLY these dimensions. For each \
dimension:

* Open with the dimension name as an H3 markdown heading.
* Describe what the dimension measures in 1-2 sentences.
* Give a 1-5 numeric scoring scale with concrete anchors at 1, 3, and 5 \
(say what behavior earns that score).
{examples_instruction}

The rubric must be self-contained — a fresh judge model with NO prior \
context must be able to score correctly using only the rubric text plus \
the agent's actual output + the case's expected output.

# Output format

Respond with ONE JSON object on a single line, no markdown fences, no \
prose outside the JSON:

{{"rubric_markdown": "<the rubric body as markdown>", "rationale": \
"<one or two sentences on why these dimensions fit this agent>"}}

Rules:
- The `rubric_markdown` is the full rubric — every dimension covered, \
no placeholders, no TODOs.
- The `rationale` is for the human reviewer; keep it concise and \
specific to THIS agent.
"""

_EXAMPLES_BLOCK_HEADER = "# Sample cases from the agent's dataset\n\n"
_EXAMPLES_INSTRUCTION_ON = (
    "* Anchor with 2-3 concrete scored examples drawn from the agent's "
    "domain (use the sample cases above when relevant). Format each as "
    "`> input → output | score: X.Y — <reason>`."
)
_EXAMPLES_INSTRUCTION_OFF = ""


def _build_examples_block(samples: list[dict[str, Any]] | None) -> str:
    """Render up to 3 sample cases into a markdown block for the prompt.

    ``samples`` is the list of ``{"input": ..., "expected": ...}`` rows
    from the agent's ``evals/dataset.jsonl`` (already loaded by the
    caller — keeping I/O out of this module).
    """
    if not samples:
        return ""
    rows: list[str] = [_EXAMPLES_BLOCK_HEADER]
    for i, row in enumerate(samples[:3], start=1):
        in_repr = json.dumps(row.get("input", {}), ensure_ascii=False)
        out_repr = json.dumps(row.get("expected", row.get("output", {})), ensure_ascii=False)
        rows.append(f"Case {i}:\n  input:    {in_repr}\n  expected: {out_repr}\n")
    return "\n".join(rows) + "\n"


def _build_dimensions_block(dimensions: list[str]) -> str:
    """Render the dimension list for the prompt — one per line."""
    return "\n".join(f"- {d}" for d in dimensions)


def _build_meta_prompt(
    *,
    bundle: AgentBundle,
    dimensions: list[str],
    samples: list[dict[str, Any]] | None,
    include_examples: bool,
) -> str:
    """Compose the prompt the engineer LLM receives. Pure string transform."""
    spec = bundle.spec
    examples_block = _build_examples_block(samples) if include_examples else ""
    examples_instruction = _EXAMPLES_INSTRUCTION_ON if include_examples else _EXAMPLES_INSTRUCTION_OFF
    return _JUDGE_ENGINEER_PROMPT_TEMPLATE.format(
        name=spec.name,
        description=spec.description or "(no description)",
        persona=spec.persona or "(none)",
        role=spec.role or "(none)",
        goals=", ".join(spec.goals) if spec.goals else "(none)",
        prompt=bundle.prompt_template.strip(),
        input_schema=json.dumps(bundle.input_schema, indent=2, ensure_ascii=False),
        output_schema=json.dumps(bundle.output_schema, indent=2, ensure_ascii=False),
        examples_block=examples_block,
        dimensions_block=_build_dimensions_block(dimensions),
        examples_instruction=examples_instruction,
    )


def _parse_engineer_response(raw: str) -> tuple[str, str]:
    """Pull ``rubric_markdown`` + ``rationale`` from the LLM's JSON line.

    Permissive — strips markdown fences if the LLM added them despite
    the instruction. Raises :class:`JudgeEngineerError` (422) on a
    response we can't recover from.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JudgeEngineerError(
            f"judge engineer LLM did not return parseable JSON: {exc}; "
            f"raw response (truncated): {raw[:200]!r}",
            status_code=422,
        ) from exc
    if not isinstance(data, dict):
        raise JudgeEngineerError(
            f"judge engineer JSON must be a mapping, got {type(data).__name__}",
            status_code=422,
        )
    rubric_markdown = data.get("rubric_markdown")
    rationale = data.get("rationale", "")
    if not isinstance(rubric_markdown, str) or not rubric_markdown.strip():
        raise JudgeEngineerError(
            "judge engineer JSON missing non-empty 'rubric_markdown' field",
            status_code=422,
        )
    if not isinstance(rationale, str):
        rationale = ""
    return rubric_markdown.strip(), rationale.strip()


# Default judge-model picks (cross-family from the most common agent
# providers). The endpoint accepts an explicit override; this is only
# the fallback. ADR 011's cross-family-by-default applies to RUNNING
# the judge against an agent, not to AUTHORING the rubric — but using a
# strong general-purpose model here keeps the rubrics good. Defaults to
# Anthropic Sonnet since most demo agents run on OpenAI.
DEFAULT_ENGINEER_MODEL = "anthropic/claude-sonnet-4-6"

# Default for the judge MODEL inside the generated YAML (separate from
# the engineer model that AUTHORS the rubric). Same cross-family
# reasoning — most demo agents are openai/*, so the judge MODEL inside
# the YAML should be from a different family. The eval engine's
# cross-family validator will catch a same-family pick at eval time.
_DEFAULT_JUDGE_MODEL_IN_YAML = "anthropic/claude-sonnet-4-6"


def _build_judge_yaml(
    *,
    bundle: AgentBundle,
    rubric_markdown: str,
    dimensions: list[str],
    judge_model: str | None,
) -> str:
    """Compose the final ``judge.yaml`` body from the LLM's rubric.

    Wraps the rubric markdown in the canonical flat :class:`JudgeConfig`
    shape — ``method: llm_judge``, ``model:``, ``rubric:``, ``threshold:``.
    No new keys. The dimension list is reflected at the TOP of the
    rubric text as a "Dimensions covered" preamble so a human reading
    the file sees them at a glance, but it stays inside the existing
    ``rubric:`` field — preserving load compat.

    Picks a judge model that's cross-family from the agent's own
    provider when the caller didn't supply one (uses a Sonnet fallback;
    the eval-time cross-family check will reject a same-family pick if
    the operator overrides poorly).
    """
    agent_provider = bundle.spec.model.provider
    judge_provider = judge_model or _pick_cross_family_judge(agent_provider)

    dimensions_preamble = "Dimensions covered:\n" + "\n".join(
        f"- {d}" for d in dimensions
    )
    full_rubric = f"{dimensions_preamble}\n\n{rubric_markdown.strip()}\n"

    config = JudgeConfig(
        method=JudgeMethod.LLM_JUDGE,
        model=ModelConfig(provider=judge_provider, params={"temperature": 0.0}),
        rubric=full_rubric,
        threshold=0.7,
    )

    # Hand-author the YAML rather than ``yaml.safe_dump(config.model_dump())``
    # so the ``rubric:`` block uses a block-literal (`|`) scalar — the
    # canonical shape (see templates/hr_policy_agent/evals/judge.yaml).
    # safe_dump emits a folded scalar for multi-line strings which is
    # harder to read AND can mangle markdown intent.
    lines: list[str] = [
        f"# Auto-generated by mdk judge generate for agent {bundle.spec.name!r}.",
        "# Reviewable: edit and commit via `mdk judge commit` or",
        "# POST /api/v1/agents/{name}/judge/commit.",
        "",
        "method: llm_judge",
        "",
        "model:",
        f"  provider: {judge_provider}",
        "  params:",
        "    temperature: 0.0",
        "",
        "rubric: |",
    ]
    for rubric_line in full_rubric.splitlines():
        lines.append(f"  {rubric_line}".rstrip())
    lines.append("")
    lines.append(f"threshold: {config.threshold}")
    lines.append("")
    return "\n".join(lines)


def _pick_cross_family_judge(agent_provider: str) -> str:
    """Pick a sensible judge provider from a *different family* than the agent.

    Conservative: openai → anthropic, anthropic → openai, anything else
    → anthropic. The eval engine re-validates cross-family at eval time
    (:func:`movate.core.eval.assert_cross_family`); this is just the
    sensible default, not the authoritative check.
    """
    family = agent_provider.split("/", 1)[0].lower() if "/" in agent_provider else ""
    if family == "openai":
        return _DEFAULT_JUDGE_MODEL_IN_YAML
    if family == "anthropic":
        return "openai/gpt-4o-mini-2024-07-18"
    return _DEFAULT_JUDGE_MODEL_IN_YAML


# ---------------------------------------------------------------------------
# Top-level generator
# ---------------------------------------------------------------------------


async def generate_judge(
    *,
    bundle: AgentBundle,
    provider: BaseLLMProvider,
    engineer_model: str = DEFAULT_ENGINEER_MODEL,
    rubric_dimensions: list[str] | None = None,
    include_examples: bool = True,
    samples: list[dict[str, Any]] | None = None,
    budget_usd: float = 0.10,
    pricing: PricingTable | None = None,
    judge_model_in_yaml: str | None = None,
) -> GeneratedJudge:
    """Author a complete ``judge.yaml`` for ``bundle``.

    The :class:`BaseLLMProvider` is called once with a meta-prompt that
    embeds the agent's spec + prompt + schemas + (optional) sample
    cases. The LLM returns one JSON object with the rubric markdown +
    a rationale; we wrap it in the canonical :class:`JudgeConfig` shape
    and validate before returning.

    ``rubric_dimensions``:
      * ``None`` (default) — infer from the agent shape via
        :func:`default_dimensions_for`.
      * Explicit list — normalized (lowercased snake_case, deduped); an
        empty list is rejected with status 400.

    ``include_examples`` — when True (default) the rubric includes
    2-3 anchor scored examples drawn from the agent's domain (and from
    ``samples`` when supplied).

    ``samples`` is the optional list of ``{"input": ..., "expected": ...}``
    dataset rows. Pass at most 3 — the prompt will truncate. None or
    empty is fine (the LLM invents domain-appropriate anchors instead).

    ``budget_usd`` is a hard ceiling on the generation call. When
    ``pricing`` is provided and the computed cost exceeds the budget,
    raises :class:`JudgeEngineerError` (402) AFTER the call has
    completed — we don't pre-charge tokens because the response size
    isn't predictable. Typical generation is <$0.01 so this is a safety
    valve, not a tight cap.

    ``judge_model_in_yaml`` is the model the GENERATED judge.yaml will
    use to score the agent at eval time. Defaults to a cross-family
    fallback (see :func:`_pick_cross_family_judge`). Distinct from the
    ``engineer_model`` that authors the rubric.

    Returns a :class:`GeneratedJudge` — the YAML body + dimensions +
    rationale + token/cost telemetry.
    """
    dimensions = (
        normalize_dimensions(rubric_dimensions)
        if rubric_dimensions is not None
        else default_dimensions_for(bundle)
    )

    meta_prompt = _build_meta_prompt(
        bundle=bundle,
        dimensions=dimensions,
        samples=samples,
        include_examples=include_examples,
    )

    request = CompletionRequest(
        provider=engineer_model,
        messages=[Message(role="user", content=meta_prompt)],
        params={
            # Determinism — same agent → same rubric is the property the
            # human reviewer wants. A temperature-0 author also makes the
            # generated YAML byte-stable across regeneration, useful for
            # tracking edits.
            "temperature": 0.0,
            # Plenty of headroom for a 4-dimension rubric with anchor
            # examples; the actual response is typically ~1500 tokens.
            "max_tokens": 4096,
        },
    )

    try:
        response = await provider.complete(request)
    except Exception as exc:
        raise JudgeEngineerError(
            f"judge engineer LLM call failed: {exc}",
            status_code=500,
        ) from exc

    rubric_markdown, rationale = _parse_engineer_response(response.text)
    judge_yaml = _build_judge_yaml(
        bundle=bundle,
        rubric_markdown=rubric_markdown,
        dimensions=dimensions,
        judge_model=judge_model_in_yaml,
    )

    # Sanity check — never return a YAML the eval engine can't load.
    validate_judge_yaml(judge_yaml)

    cost_usd = 0.0
    if pricing is not None:
        try:
            cost_usd = pricing.cost_for(provider=engineer_model, tokens=response.tokens)
        except Exception:
            # Pricing miss is non-fatal — return cost 0.0 and let the
            # caller log. A missing price for the engineer model is far
            # less serious than refusing to author the rubric.
            cost_usd = 0.0

    if cost_usd > budget_usd:
        raise JudgeEngineerError(
            f"generation cost ${cost_usd:.4f} exceeded budget "
            f"${budget_usd:.4f} — pass a higher budget_usd or a "
            "cheaper engineer_model",
            status_code=402,
        )

    tokens_used = response.tokens.input + response.tokens.output
    return GeneratedJudge(
        judge_yaml=judge_yaml,
        rubric_dimensions=dimensions,
        rationale=rationale,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        raw_response=response.text,
    )


__all__ = [
    "DEFAULT_ENGINEER_MODEL",
    "GeneratedJudge",
    "JudgeEngineerError",
    "default_dimensions_for",
    "generate_judge",
    "normalize_dimensions",
    "validate_judge_yaml",
]
