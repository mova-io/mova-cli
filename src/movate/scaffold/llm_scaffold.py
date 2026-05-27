# ruff: noqa: E501
# The few-shot exemplars below are deliberately long-form JSON strings.
# Wrapping them across multiple lines would force the model to deal
# with escaped continuations, which lowers fidelity in pilot runs.
# Disable line-length linting for this file; readability comes from
# the JSON structure, not from 100-column wrapping.
"""LLM-driven agent scaffolding.

Single-attempt generator + IO helpers. The caller (``movate.cli.init``)
owns the retry loop because retry policy is a CLI concern (it depends
on the user's debug-artifact path, the ``--dry-run`` flag, etc.).

Wire shape::

    provider = MockProvider() | LiteLLMProvider()
    generated: GeneratedAgent = await generate_agent_from_description(
        description="FAQ agent for our SaaS pricing",
        name="faq-agent",
        model="openai/gpt-4o-mini-2024-07-18",
        provider=provider,
    )
    write_agent_files(generated, target_dir=Path("./agents/faq-agent"))

The meta-prompt embeds two few-shot exemplars (FAQ + classifier) lifted
verbatim from the packaged templates. Inlined as string literals — at
1.5 KB each they don't justify a filesystem read at import time, and
they're the kind of thing you want to read while reviewing the prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from movate.core.models import TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    Message,
)


class LLMScaffoldError(Exception):
    """Raised when the LLM returns unparseable JSON, schema-violating
    JSON, or the provider call itself fails. The caller's retry loop
    decides whether to re-prompt or surface to the operator."""


class GeneratedAgent(BaseModel):
    """The complete agent payload an LLM scaffolder returns.

    All four file-bearing fields are required (the generator must
    produce a runnable agent, not a partial one). ``sample_evals`` is
    optional-ish: a 0-entry list is legal but the meta-prompt asks
    for 2-3, and a 0-entry list triggers a `missing-evals` finding in
    ``mdk audit current``.
    """

    model_config = ConfigDict(extra="forbid")

    agent_yaml: dict[str, Any] = Field(
        ...,
        description=(
            "The agent.yaml contents as a dict. Required keys: "
            "api_version='movate/v1', kind='Agent', name, version, "
            "model.provider, prompt, schema.{input,output}, evals.dataset."
        ),
    )
    prompt_md: str = Field(
        ...,
        description=(
            "The Jinja prompt template body. References to input fields "
            "use {{ input.<field> }}. Renders to the final system prompt."
        ),
    )
    input_schema: dict[str, Any] = Field(
        ...,
        description="JSON Schema 2020-12 for the agent's input contract.",
    )
    output_schema: dict[str, Any] = Field(
        ...,
        description="JSON Schema 2020-12 for the agent's output contract.",
    )
    sample_evals: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "2-3 sample dataset entries with 'input' and 'expected' keys "
            "matching the schemas above. Empty list is legal but produces "
            "a `missing-evals` audit finding."
        ),
    )


@dataclass(frozen=True)
class GenerationResult:
    """A successful generation attempt: the parsed agent + its token usage.

    Returned by :func:`generate_agent_from_description`. The caller
    rolls token usage across multiple attempts (attempt + retry) to
    compute total cost.
    """

    agent: GeneratedAgent
    tokens: TokenUsage


# ---------------------------------------------------------------------------
# Few-shot exemplars — embedded so the meta-prompt is self-contained.
# Lifted verbatim from src/movate/templates/{faq_agent,classifier_agent}/.
# When those templates change in a way that materially shifts what a
# "good" agent looks like, update these too.
# ---------------------------------------------------------------------------

_EXAMPLE_FAQ = """\
{
  "agent_yaml": {
    "api_version": "movate/v1",
    "kind": "Agent",
    "name": "faq-agent",
    "version": "0.1.0",
    "description": "An FAQ assistant. Answers questions concisely with a confidence score.",
    "owner": "",
    "model": {
      "provider": "openai/gpt-4o-mini-2024-07-18",
      "params": {"temperature": 0.0, "max_tokens": 512}
    },
    "prompt": "./prompt.md",
    "schema": {
      "input": "./schema/input.json",
      "output": "./schema/output.json"
    },
    "evals": {"dataset": "./evals/dataset.jsonl"},
    "timeouts": {"call_ms": 30000, "total_ms": 60000},
    "budget": {"max_cost_usd_per_run": 0.50},
    "tags": ["faq"]
  },
  "prompt_md": "You answer FAQ questions concisely. Question:\\n{{ input.question }}\\n\\nRespond as a single JSON object on one line:\\n{\\"answer\\": \\"<your answer>\\", \\"confidence\\": <0.0-1.0>}",
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["question"],
    "properties": {
      "question": {"type": "string", "minLength": 1}
    }
  },
  "output_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["answer", "confidence"],
    "properties": {
      "answer": {"type": "string", "minLength": 1},
      "confidence": {"type": "number", "minimum": 0, "maximum": 1}
    }
  },
  "sample_evals": [
    {"input": {"question": "What is your refund window?"}, "expected": {"answer": "30 days from purchase.", "confidence": 0.95}},
    {"input": {"question": "Do you support SAML SSO?"}, "expected": {"answer": "Yes, on the Enterprise tier.", "confidence": 0.9}}
  ]
}"""

_EXAMPLE_CLASSIFIER = """\
{
  "agent_yaml": {
    "api_version": "movate/v1",
    "kind": "Agent",
    "name": "sentiment-classifier",
    "version": "0.1.0",
    "description": "Classifies short text into one of a fixed label set.",
    "owner": "",
    "model": {
      "provider": "openai/gpt-4o-mini-2024-07-18",
      "params": {"temperature": 0.0, "max_tokens": 64}
    },
    "prompt": "./prompt.md",
    "schema": {
      "input": "./schema/input.json",
      "output": "./schema/output.json"
    },
    "evals": {"dataset": "./evals/dataset.jsonl"},
    "timeouts": {"call_ms": 30000, "total_ms": 60000},
    "budget": {"max_cost_usd_per_run": 0.10},
    "tags": ["classifier"]
  },
  "prompt_md": "You are a text classifier. Pick exactly one label from the provided list.\\n\\nText:\\n{{ input.text }}\\n\\nAvailable labels:\\n{% for label in input.labels %}- {{ label }}\\n{% endfor %}\\nRespond with a single JSON object: {\\"label\\": \\"<chosen label>\\"}",
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["text", "labels"],
    "properties": {
      "text": {"type": "string", "minLength": 1},
      "labels": {"type": "array", "items": {"type": "string"}, "minItems": 2}
    }
  },
  "output_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["label"],
    "properties": {"label": {"type": "string", "minLength": 1}}
  },
  "sample_evals": [
    {"input": {"text": "I loved this!", "labels": ["positive", "negative", "neutral"]}, "expected": {"label": "positive"}},
    {"input": {"text": "Worst experience ever.", "labels": ["positive", "negative", "neutral"]}, "expected": {"label": "negative"}}
  ]
}"""

# RAG / grounded-QA exemplar (F3, #112). The shape every "answer from a
# knowledge source" description must collapse to: `skills:
# [kb-vector-lookup]` + a `retrieval: {auto_into: context}` block (ADR
# 023 opt-in pre-retrieval) + an OPTIONAL `context: list[string]` input
# field + a grounded prompt that answers FROM input.context, cites by
# index, and declines when context is empty. Mirrors the packaged
# `rag_qa_agent` template (templates/rag_qa_agent/). The Executor
# auto-retrieves into `input.context` before the prompt renders, so the
# scaffolded agent is grounded end-to-end once a KB is ingested.
_EXAMPLE_RAG = """\
{
  "agent_yaml": {
    "api_version": "movate/v1",
    "kind": "Agent",
    "name": "docs-qa",
    "version": "0.1.0",
    "description": "Answers questions grounded in our product documentation. Cites the supporting chunks and declines when the docs don't cover the question.",
    "owner": "",
    "model": {
      "provider": "openai/gpt-4o-mini-2024-07-18",
      "params": {"temperature": 0.0, "max_tokens": 1024}
    },
    "prompt": "./prompt.md",
    "schema": {
      "input": "./schema/input.json",
      "output": "./schema/output.json"
    },
    "evals": {"dataset": "./evals/dataset.jsonl"},
    "timeouts": {"call_ms": 30000, "total_ms": 60000},
    "budget": {"max_cost_usd_per_run": 0.10},
    "tags": ["rag", "qa", "grounded"],
    "skills": ["kb-vector-lookup"],
    "retrieval": {"auto_into": "context", "query_from": "question"}
  },
  "prompt_md": "You are a grounded question-answering assistant. Answer ONLY from the retrieved context below — never from outside knowledge. Every claim must trace to a numbered context chunk.\\n\\n# Context\\n{% for chunk in input.context %}\\n[{{ loop.index }}] {{ chunk }}\\n{% endfor %}\\n\\n# Question\\n{{ input.question }}\\n\\nIf the context is empty or does not support an answer, set \\"grounded\\": false, return an empty \\"citations\\" list, and say what information is missing — do NOT fabricate.\\n\\nRespond with a single JSON object on one line:\\n{\\"answer\\": \\"<grounded answer>\\", \\"citations\\": [<1-based chunk indices>], \\"grounded\\": <true|false>, \\"confidence\\": <0.0-1.0>}",
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["question"],
    "properties": {
      "question": {"type": "string", "minLength": 1},
      "context": {"type": "array", "items": {"type": "string"}}
    }
  },
  "output_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["answer", "citations", "grounded", "confidence"],
    "properties": {
      "answer": {"type": "string", "minLength": 1},
      "citations": {"type": "array", "items": {"type": "integer"}},
      "grounded": {"type": "boolean"},
      "confidence": {"type": "number", "minimum": 0, "maximum": 1}
    }
  },
  "sample_evals": [
    {"input": {"question": "What is our refund window?", "context": ["Annual plans are refundable within 14 days of purchase, prorated by the unused portion."]}, "expected": {"answer": "Annual plans are refundable within 14 days of purchase, prorated by the unused portion.", "citations": [1], "grounded": true, "confidence": 0.95}},
    {"input": {"question": "Do you support SAML SSO?", "context": []}, "expected": {"answer": "The provided context does not cover SAML SSO support.", "citations": [], "grounded": false, "confidence": 0.0}}
  ]
}"""


# ---------------------------------------------------------------------------
# Meta-prompt — instructs the LLM how to map description → GeneratedAgent.
#
# STRUCTURE: a fully-static PREFIX followed by a short VARIABLE SUFFIX. The
# prefix (role/intro, schema, hard constraints, both few-shot examples) is
# byte-identical on every call — no interpolation — so it forms a stable
# cacheable prompt prefix (OpenAI auto-caches stable prefixes; this also
# sets up an explicit Anthropic ``cache_control`` breakpoint later). The
# per-call variables (description, name, target model) appear ONLY in the
# trailing suffix, so nothing before the suffix diverges between calls.
#
# Constraints live ABOVE the few-shot so the model anchors on rules first
# and examples second; reversing this in pilot runs produced more
# hallucinated JSON Schema types ("datetime", "uuid").
#
# The constraints reference name / model.provider GENERICALLY ("given
# below") rather than embedding the literal values — the literals live in
# the suffix. This is safe because the CLI coerces ``agent_yaml.name`` and
# ``agent_yaml.model.provider`` post-generation, so the exact strings need
# not appear mid-constraints. Keeping them out is what makes the prefix
# static (and therefore cacheable).
# ---------------------------------------------------------------------------

# Static, interpolation-free preamble. ``{example_faq}`` / ``{example_classifier}``
# are the only placeholders and they expand to byte-identical literals on
# every call, so the formatted prefix is itself constant. The detection
# markers the mock keys on ("scaffolding a movate AI agent",
# "GENERATEDAGENT SCHEMA") live here in the prefix.
_META_PROMPT_PREFIX = """\
You are scaffolding a movate AI agent from a natural-language description.

Your job is to generate a complete, runnable agent as a single JSON object
matching the GeneratedAgent schema below. The CLI will write the four files
(agent.yaml, prompt.md, schema/input.json, schema/output.json) plus an
evals/dataset.jsonl to disk and then validate the result by loading it. The
user description, agent name, and target model are given at the END of this
prompt.

GENERATEDAGENT SCHEMA:
{{
  "agent_yaml": <dict>,         // full agent.yaml contents
  "prompt_md": <string>,        // Jinja prompt template body
  "input_schema": <dict>,       // JSON Schema 2020-12 for input
  "output_schema": <dict>,      // JSON Schema 2020-12 for output
  "sample_evals": [             // 2-3 entries; each is {{input, expected}}
    {{"input": <dict>, "expected": <dict>}}
  ]
}}

HARD CONSTRAINTS — VIOLATIONS WILL FAIL VALIDATION:

1. agent_yaml MUST include:
   - api_version: "movate/v1"
   - kind: "Agent"
   - name: the exact agent name given below  (use it verbatim)
   - version: "0.1.0"
   - model.provider: the model id given below  (use that exact provider string)
   - model.params: {{"temperature": 0.0, "max_tokens": <256-2048>}}
   - prompt: "./prompt.md"
   - schema.input: "./schema/input.json"
   - schema.output: "./schema/output.json"
   - evals.dataset: "./evals/dataset.jsonl"
   - description: <one-line summary of what the agent does>
   - owner: ""

2. input_schema and output_schema MUST be valid JSON Schema 2020-12:
   - "$schema": "https://json-schema.org/draft/2020-12/schema"
   - "type": "object"
   - "additionalProperties": false
   - "required": [list of every property in "properties"]
   - "properties": {{...}}

3. JSON Schema "type" must be ONE OF: "string", "number", "integer",
   "boolean", "array", "object", "null". Do NOT invent types like
   "datetime", "date", "uuid", "email" — those go in "format" instead:
   {{"type": "string", "format": "date-time"}}

4. prompt_md uses Jinja syntax. Reference input fields as
   {{{{ input.<field-name> }}}}. Keep the prompt focused — explain
   the role, inject the input, instruct on JSON-only output, give
   one example response. Under 300 words.

5. sample_evals: 2-3 entries. Each entry's "input" must validate
   against input_schema; each "expected" must validate against
   output_schema. Pick representative real-world cases.

6. Output ONLY valid JSON. No markdown fences, no prose, no
   commentary before or after the JSON.

7. GROUNDING / RAG DETECTION. First decide whether the description asks
   the agent to ANSWER FROM A KNOWLEDGE SOURCE — e.g. "answer questions
   about our docs / help center / FAQ / policies / handbook", "based on
   our documentation", "from this website", a URL, or any "answer
   questions about <corpus>" phrasing. This is grounding/RAG intent.
   - If GROUNDING: emit a RAG-shaped agent (see EXAMPLE 3). It MUST have
     * agent_yaml.skills: ["kb-vector-lookup"]
     * agent_yaml.retrieval: {{"auto_into": "context", "query_from": "<the question/text input field>"}}
     * an input_schema with the primary question field PLUS an OPTIONAL
       "context" field of type array-of-string (do NOT put "context" in
       "required" — it is auto-filled by retrieval before the prompt
       renders).
     * a prompt that answers ONLY from input.context, cites chunks by
       1-based index, and DECLINES (sets a grounded=false signal) when
       the context is empty. Never answer from outside knowledge.
   - If NOT grounding (a classifier, summarizer, transformer, extractor,
     generator, or any task that operates purely on its input): emit a
     normal single-turn agent (EXAMPLE 1 or 2). Do NOT add skills or a
     retrieval block — those keys must be ABSENT for non-grounding agents.

EXAMPLE 1 (FAQ agent) — single-turn, NOT grounding:
{example_faq}

EXAMPLE 2 (Classifier agent) — single-turn, NOT grounding:
{example_classifier}

EXAMPLE 3 (Grounded RAG agent) — answers from a knowledge source:
{example_rag}
"""

# Variable suffix — the ONLY part that changes per call. Appended after the
# static prefix so the model sees the rules + examples first, then the
# concrete task. The ``AGENT NAME:`` line is preserved verbatim because the
# mock parses the requested name out of it (``_parse_scaffold_name``).
_META_PROMPT_SUFFIX = """\

USER DESCRIPTION:
\"\"\"
{description}
\"\"\"

AGENT NAME: {name}

TARGET MODEL (write this exact string into agent_yaml.model.provider): {target_model}

Now generate the GeneratedAgent JSON for the description above (name: {name}).
Respond with the JSON object only.
"""

# Full meta-prompt = static prefix + variable suffix. ``.format(...)`` only
# substitutes the suffix's placeholders (description/name/target_model) plus
# the prefix's example literals; every byte before USER DESCRIPTION is the
# same on every call.
_META_PROMPT = _META_PROMPT_PREFIX + _META_PROMPT_SUFFIX


# Retry prompt — used when validation fails. Feeds the error + the
# previous attempt back into the LLM so it can self-correct.
_RETRY_PROMPT = """\
Your previous attempt at scaffolding agent '{name}' failed validation.

PREVIOUS ATTEMPT:
{previous_json}

VALIDATION ERROR:
{error}

Fix the error above and return a corrected GeneratedAgent JSON object.
Output ONLY valid JSON — no markdown, no prose.
"""


async def generate_agent_from_description(
    *,
    description: str,
    name: str,
    model: str,
    provider: BaseLLMProvider,
    target_model: str | None = None,
    previous_attempt: GeneratedAgent | None = None,
    validation_error: str | None = None,
) -> GenerationResult:
    """Single LLM-driven generation attempt.

    Returns a :class:`GenerationResult` carrying the validated
    :class:`GeneratedAgent` plus the call's :class:`TokenUsage` so
    the caller can roll cost across attempts. Raises
    :class:`LLMScaffoldError` on any of:

    * Wire error from the provider.
    * LLM returned non-JSON.
    * LLM returned JSON that doesn't match GeneratedAgent's schema.

    ``model`` is the model used to DRIVE this scaffold call (the LLM
    doing the generating). ``target_model`` is the model string the
    GENERATED agent should declare in its ``agent_yaml.model.provider``
    — it's injected into the meta-prompt's hard constraints so the
    scaffolded agent runs with the key the operator actually has. When
    ``target_model`` is ``None`` the prompt falls back to the
    ``model`` value (back-compat: prior callers passed only ``model``).

    When ``previous_attempt`` and ``validation_error`` are both
    supplied, the meta-prompt switches to retry mode — the LLM is
    shown the prior attempt + the error and asked to self-correct.
    Used by the caller's one-shot retry loop.
    """
    if previous_attempt is not None and validation_error is not None:
        user_prompt = _RETRY_PROMPT.format(
            name=name,
            previous_json=previous_attempt.model_dump_json(indent=2),
            error=validation_error,
        )
    else:
        user_prompt = _META_PROMPT.format(
            description=description,
            name=name,
            target_model=target_model or model,
            example_faq=_EXAMPLE_FAQ,
            example_classifier=_EXAMPLE_CLASSIFIER,
            example_rag=_EXAMPLE_RAG,
        )

    request = CompletionRequest(
        provider=model,
        messages=[Message(role="user", content=user_prompt)],
        # LiteLLM-style JSON mode — passes ``response_format`` through
        # to the upstream provider where supported (OpenAI, Anthropic
        # via tool-call shim, Azure OpenAI). Native-SDK adapters that
        # ignore unknown params fall back to free-form output; the JSON
        # parse below catches the resulting failure and the retry loop
        # handles it.
        params={
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 4096,
        },
    )

    try:
        response = await provider.complete(request)
    except Exception as exc:
        raise LLMScaffoldError(f"provider call failed: {exc}") from exc

    raw = response.text.strip()
    # Some models still emit code fences despite response_format=json_object.
    # Strip them defensively — the alternative is a retry that costs $.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.removesuffix("```").strip()
        if raw.startswith("json"):
            raw = raw[4:].lstrip()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMScaffoldError(
            f"LLM returned invalid JSON: {exc}. First 200 chars of response: {raw[:200]!r}"
        ) from exc

    try:
        agent = GeneratedAgent.model_validate(payload)
    except ValidationError as exc:
        raise LLMScaffoldError(f"LLM output doesn't match GeneratedAgent schema:\n{exc}") from exc
    return GenerationResult(agent=agent, tokens=response.tokens)


def write_agent_files(generated: GeneratedAgent, *, target_dir: Path) -> None:
    """Materialize a :class:`GeneratedAgent` to disk.

    Writes the standard movate agent file layout::

        <target_dir>/
          ├── agent.yaml
          ├── prompt.md
          ├── schema/
          │     ├── input.json
          │     └── output.json
          └── evals/
                └── dataset.jsonl   (only if sample_evals is non-empty)

    Creates parent directories as needed; overwrites existing files
    (the caller is responsible for the ``--force`` / pre-existence
    check before calling).
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    # agent.yaml — block-style YAML for readability, not flow-style.
    # sort_keys=False preserves the order the LLM chose (api_version,
    # kind, name first is the convention every template uses).
    (target_dir / "agent.yaml").write_text(
        yaml.safe_dump(generated.agent_yaml, sort_keys=False, default_flow_style=False)
    )

    # prompt.md — verbatim. The LLM is responsible for Jinja correctness;
    # load_agent will catch unrenderable templates downstream.
    (target_dir / "prompt.md").write_text(generated.prompt_md)

    schema_dir = target_dir / "schema"
    schema_dir.mkdir(exist_ok=True)
    (schema_dir / "input.json").write_text(json.dumps(generated.input_schema, indent=2) + "\n")
    (schema_dir / "output.json").write_text(json.dumps(generated.output_schema, indent=2) + "\n")

    if generated.sample_evals:
        evals_dir = target_dir / "evals"
        evals_dir.mkdir(exist_ok=True)
        # JSONL: one entry per line, no trailing comma chaos.
        (evals_dir / "dataset.jsonl").write_text(
            "\n".join(json.dumps(e) for e in generated.sample_evals) + "\n"
        )
