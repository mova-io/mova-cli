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

The meta-prompt embeds one few-shot exemplar per canonical shape (F2,
#111): FAQ/QA, classifier, summarizer, extraction, and RAG/grounded
(F3, #112). A SHAPE-SELECTION instruction tells the model to classify
the description into exactly one shape and emit that shape's output
schema + prompt — instead of collapsing every agent to a generic
{answer, confidence}. Exemplars are lifted from the packaged templates
and inlined as string literals — they don't justify a filesystem read
at import time, and they're the kind of thing you want to read while
reviewing the prompt.
"""

from __future__ import annotations

import copy
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
      "input": "./schema/input.yaml",
      "output": "./schema/output.yaml"
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
      "input": "./schema/input.yaml",
      "output": "./schema/output.yaml"
    },
    "evals": {"dataset": "./evals/dataset.jsonl"},
    "timeouts": {"call_ms": 30000, "total_ms": 60000},
    "budget": {"max_cost_usd_per_run": 0.10},
    "tags": ["classifier"]
  },
  "prompt_md": "You are a text classifier. Pick exactly one label from the provided list.\\n\\nText:\\n{{ input.text }}\\n\\nAvailable labels:\\n{% for label in input.labels %}- {{ label }}\\n{% endfor %}\\nRespond with a single JSON object on one line:\\n{\\"label\\": \\"<chosen label>\\", \\"confidence\\": <0.0-1.0>}",
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
    "required": ["label", "confidence"],
    "properties": {
      "label": {"type": "string", "minLength": 1},
      "confidence": {"type": "number", "minimum": 0, "maximum": 1}
    }
  },
  "sample_evals": [
    {"input": {"text": "I loved this!", "labels": ["positive", "negative", "neutral"]}, "expected": {"label": "positive", "confidence": 0.97}},
    {"input": {"text": "Worst experience ever.", "labels": ["positive", "negative", "neutral"]}, "expected": {"label": "negative", "confidence": 0.95}}
  ]
}"""

# Summarizer exemplar (F2, #111). The shape every "summarize / condense /
# tl;dr / digest this text" description must collapse to:
# `{summary: string, key_points: array[string]}`. Lifted from the packaged
# `summarizer_agent` template's intent (a summary + the salient points),
# specialized to a list-of-bullets contract so the output is structured,
# not a single opaque blob. `max_words` is an OPTIONAL input knob (omitted
# from `required`) so callers can cap length without it being mandatory.
_EXAMPLE_SUMMARIZER = """\
{
  "agent_yaml": {
    "api_version": "movate/v1",
    "kind": "Agent",
    "name": "text-summarizer",
    "version": "0.1.0",
    "description": "Summarizes input text into a concise summary plus a list of key points.",
    "owner": "",
    "model": {
      "provider": "openai/gpt-4o-mini-2024-07-18",
      "params": {"temperature": 0.2, "max_tokens": 512}
    },
    "prompt": "./prompt.md",
    "schema": {
      "input": "./schema/input.yaml",
      "output": "./schema/output.yaml"
    },
    "evals": {"dataset": "./evals/dataset.jsonl"},
    "timeouts": {"call_ms": 30000, "total_ms": 60000},
    "budget": {"max_cost_usd_per_run": 0.50},
    "tags": ["summarizer"]
  },
  "prompt_md": "You are a summarization assistant. Read the text below and produce a concise summary plus the key points. Do not add facts that are not in the text.\\n\\nText:\\n{{ input.text }}\\n\\nRespond with a single JSON object on one line:\\n{\\"summary\\": \\"<concise summary>\\", \\"key_points\\": [\\"<point 1>\\", \\"<point 2>\\"]}",
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["text"],
    "properties": {
      "text": {"type": "string", "minLength": 1},
      "max_words": {"type": "integer", "minimum": 1}
    }
  },
  "output_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["summary", "key_points"],
    "properties": {
      "summary": {"type": "string", "minLength": 1},
      "key_points": {"type": "array", "items": {"type": "string"}}
    }
  },
  "sample_evals": [
    {"input": {"text": "Q3 revenue grew 18% YoY on enterprise renewals; operating margin expanded to 22% on cost optimization. Headcount held flat."}, "expected": {"summary": "Q3 revenue rose 18% YoY and margin expanded to 22%, with flat headcount.", "key_points": ["Revenue up 18% YoY on enterprise renewals", "Operating margin expanded to 22%", "Headcount held flat"]}},
    {"input": {"text": "The release fixes a login bug, adds dark mode, and improves export speed by 30%."}, "expected": {"summary": "The release fixes a login bug, adds dark mode, and speeds up exports.", "key_points": ["Login bug fixed", "Dark mode added", "Export speed improved 30%"]}}
  ]
}"""

# Extraction exemplar (F2, #111). The shape every "extract / pull out /
# parse <named fields> from text" description must collapse to: a structured
# output object whose properties ARE the named entities. Lifted from the
# packaged `extractor_agent` template — extraction wants determinism, so
# temperature 0.0 and a strict field contract. Fields the source may omit
# are nullable (a union with "null") and the prompt instructs the model to
# return null rather than fabricate; the field still appears in `required`
# (its VALUE may be null, but the KEY must be present). This is the canonical
# way to express "optional value" in a strict JSON Schema object — NEVER a
# key-suffix `?`, which is not JSON Schema and is not a value-optional marker.
_EXAMPLE_EXTRACTION = """\
{
  "agent_yaml": {
    "api_version": "movate/v1",
    "kind": "Agent",
    "name": "contact-extractor",
    "version": "0.1.0",
    "description": "Extracts named fields (contact name, email, organization, intent) from unstructured text.",
    "owner": "",
    "model": {
      "provider": "openai/gpt-4o-mini-2024-07-18",
      "params": {"temperature": 0.0, "max_tokens": 512}
    },
    "prompt": "./prompt.md",
    "schema": {
      "input": "./schema/input.yaml",
      "output": "./schema/output.yaml"
    },
    "evals": {"dataset": "./evals/dataset.jsonl"},
    "timeouts": {"call_ms": 30000, "total_ms": 60000},
    "budget": {"max_cost_usd_per_run": 0.05},
    "tags": ["extraction", "structured-output"]
  },
  "prompt_md": "You are a strict structured-field extractor. Read the text and pull out the requested fields. If a field is not present in the text, return null — do NOT invent or infer.\\n\\nFields:\\n- contact_name: the person's full name, or null.\\n- email: a valid email address, or null.\\n- organization: the company / org name, or null.\\n- intent: a short label for what the writer wants, or null.\\n\\nText:\\n{{ input.text }}\\n\\nRespond with a single JSON object on one line:\\n{\\"contact_name\\": \\"...\\", \\"email\\": \\"...\\", \\"organization\\": \\"...\\", \\"intent\\": \\"...\\"}",
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["text"],
    "properties": {
      "text": {"type": "string", "minLength": 1}
    }
  },
  "output_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "required": ["contact_name", "email", "organization", "intent"],
    "properties": {
      "contact_name": {"type": ["string", "null"]},
      "email": {"type": ["string", "null"], "format": "email"},
      "organization": {"type": ["string", "null"]},
      "intent": {"type": ["string", "null"]}
    }
  },
  "sample_evals": [
    {"input": {"text": "Hi, this is Sarah Chen from Acme Corp (sarah@acme.example). We'd like a demo of the Enterprise tier."}, "expected": {"contact_name": "Sarah Chen", "email": "sarah@acme.example", "organization": "Acme Corp", "intent": "demo_request"}},
    {"input": {"text": "Please cancel my subscription."}, "expected": {"contact_name": null, "email": null, "organization": null, "intent": "cancellation"}}
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
      "input": "./schema/input.yaml",
      "output": "./schema/output.yaml"
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
# prefix (role/intro, schema, hard constraints, the per-shape few-shot
# examples) is byte-identical on every call — no interpolation — so it forms a stable
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

# Static, interpolation-free preamble. The ``{example_*}`` placeholders
# (faq, classifier, summarizer, extraction, rag) are the only ones and they
# expand to byte-identical literals on every call, so the formatted prefix
# is itself constant. The detection markers the mock keys on ("scaffolding a
# movate AI agent", "GENERATEDAGENT SCHEMA") live here in the prefix.
_META_PROMPT_PREFIX = """\
You are scaffolding a movate AI agent from a natural-language description.

Your job is to generate a complete, runnable agent as a single JSON object
matching the GeneratedAgent schema below. The CLI will write the four files
(agent.yaml, prompt.md, schema/input.yaml, schema/output.yaml) plus an
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
   - schema.input: "./schema/input.yaml"
   - schema.output: "./schema/output.yaml"
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

7. SHAPE SELECTION (do this FIRST). The output schema + prompt MUST match
   what the description ASKS FOR — do not collapse every agent to a generic
   {{answer, confidence}}. Classify the description into exactly ONE of the
   shapes below (check them top-to-bottom; the FIRST match wins) and emit
   that shape's output_schema + prompt + sample_evals:

   a. GROUNDED / RAG — the agent must ANSWER FROM A KNOWLEDGE SOURCE: "answer
      questions about our docs / help center / FAQ / policies / handbook",
      "based on our documentation", "from this website", a URL, or any
      "answer questions about <corpus>" phrasing. → EXAMPLE 5 (RAG). It MUST
      have:
        * agent_yaml.skills: ["kb-vector-lookup"]
        * agent_yaml.retrieval: {{"auto_into": "context", "query_from": "<the question/text input field>"}}
        * an input_schema with the primary question field PLUS an OPTIONAL
          "context" field of type array-of-string (do NOT put "context" in
          "required" — it is auto-filled by retrieval before the prompt
          renders).
        * a prompt that answers ONLY from input.context, cites chunks by
          1-based index, and DECLINES (sets grounded=false) on empty context.
          Never answer from outside knowledge.
        * output {{answer, citations, grounded, confidence}}.

   b. CLASSIFIER — "classify / categorize / label / route / triage / detect
      sentiment / tag" into a fixed set of categories. → EXAMPLE 2. Output
      {{label, confidence}}.

   c. SUMMARIZER — "summarize / condense / tl;dr / digest / shorten / brief".
      → EXAMPLE 3. Output {{summary, key_points}} (key_points is an array of
      strings).

   d. EXTRACTION — "extract / pull out / parse / capture NAMED FIELDS"
      (entities, contact info, line items, dates, etc.) from text. → EXAMPLE
      4. The output_schema's properties ARE the named fields. Fields the
      source may omit get a nullable type ("type": ["string", "null"]) and
      the prompt returns null rather than fabricating — but the field KEY
      still appears in "required" (a present key with a null VALUE). NEVER
      append "?" to a property key — that is not JSON Schema.

   e. QA / FAQ (the default) — a free-text question answered from the model's
      own knowledge or the provided input, NOT from a retrieved corpus, and
      not any shape above. → EXAMPLE 1. Output {{answer, confidence}}.

   Shapes (b)-(e) are single-turn agents: they operate purely on their input.
   Do NOT add a skills or retrieval block to them — those keys must be ABSENT
   for every non-grounding shape.

EXAMPLE 1 (FAQ / QA agent) — single-turn, NOT grounding; {{answer, confidence}}:
{example_faq}

EXAMPLE 2 (Classifier agent) — single-turn; {{label, confidence}}:
{example_classifier}

EXAMPLE 3 (Summarizer agent) — single-turn; {{summary, key_points}}:
{example_summarizer}

EXAMPLE 4 (Extraction agent) — single-turn; structured named fields:
{example_extraction}

EXAMPLE 5 (Grounded RAG agent) — answers from a knowledge source:
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
            example_summarizer=_EXAMPLE_SUMMARIZER,
            example_extraction=_EXAMPLE_EXTRACTION,
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


# ---------------------------------------------------------------------------
# Canonical scaffold layout (#127, PR1).
#
# EVERY scaffolded agent uses ONE layout: schema is written as YAML files
# (`schema/input.yaml` + `schema/output.yaml`), and `agent.yaml` references
# them by FILE path. This unifies what `mdk init --llm` emits with the
# bundled-template layout (`schema/*.yaml` + a `judge.yaml.example`). The
# loader still accepts inline schema + `schema/*.json` for back-compat — we
# only standardize what NEW scaffolds emit. See `docs/agent-layout.md`.
# ---------------------------------------------------------------------------

# The path references written into the generated agent.yaml's `schema:`
# block. The writer FORCES these so the on-disk references always match the
# on-disk files, regardless of what the LLM / mock payload declared (a real
# LLM may still echo the exemplar's `./schema/input.json`).
_SCHEMA_INPUT_REF = "./schema/input.yaml"
_SCHEMA_OUTPUT_REF = "./schema/output.yaml"

# `evals/judge.yaml.example` — the optional LLM-as-judge config every
# bundled template ships (rename to `judge.yaml` to enable). Lifted from
# `src/movate/templates/agent_init/evals/judge.yaml.example` so a `--llm`
# scaffold looks like a hand-init'd one. Cross-family by default
# (agent=openai/* → judge=anthropic/*) since the eval engine rejects a
# same-family judge at parse time.
_JUDGE_YAML_EXAMPLE = """\
# Optional LLM-as-judge config. Rename to `judge.yaml` to enable.
#
# RULE: judge family MUST differ from agent family (e.g. agent=openai/* →
# judge cannot be openai/* or azure/*). The eval engine rejects same-family
# configs at parse time, not at run time, so misconfigs surface immediately.
#
# For variance defense, run `mdk eval` with --runs 3 (or 5) when using
# llm_judge so the per-case score is the mean of N independent judgments.

method: llm_judge

model:
  provider: anthropic/claude-sonnet-4-6
  params:
    temperature: 0.0

rubric: |
  Score the actual answer against the expected answer for semantic
  equivalence. Penalize hallucinations. A 1.0 = fully correct,
  0.5 = partially correct, 0.0 = wrong or unsafe.

threshold: 0.7
"""


def write_agent_files(generated: GeneratedAgent, *, target_dir: Path) -> None:
    """Materialize a :class:`GeneratedAgent` to disk in the canonical layout.

    Writes the ONE canonical movate agent layout (#127)::

        <target_dir>/
          ├── agent.yaml          # schema refs point at ./schema/*.yaml
          ├── prompt.md
          ├── schema/
          │     ├── input.yaml     # YAML, not JSON
          │     └── output.yaml
          └── evals/
                ├── dataset.jsonl   (only if sample_evals is non-empty)
                └── judge.yaml.example

    Schema is written as **YAML** (``yaml.safe_dump``) — the loader
    shape-sniffs a ``$schema``-bearing YAML doc as a verbatim JSON Schema,
    so the generated 2020-12 schemas load identically to the old ``.json``
    form. The ``agent.yaml`` ``schema:`` references are FORCED to
    ``./schema/input.yaml`` / ``./schema/output.yaml`` here so the on-disk
    references always match the on-disk files — the canonical writer is the
    single source of truth, regardless of whether the LLM (or a mock
    payload) declared ``./schema/input.json``.

    Creates parent directories as needed; overwrites existing files
    (the caller is responsible for the ``--force`` / pre-existence
    check before calling).
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    # Deep-copy so forcing the schema references never mutates the caller's
    # GeneratedAgent (the CLI re-uses the same object for the dry-run
    # preview, the validation tempdir, and the committed write).
    agent_yaml = copy.deepcopy(generated.agent_yaml)
    schema_block = agent_yaml.get("schema")
    if isinstance(schema_block, dict):
        # Force the canonical YAML file references. The LLM/mock may have
        # emitted `./schema/input.json`; we always write `.yaml` files, so
        # the references must match.
        schema_block["input"] = _SCHEMA_INPUT_REF
        schema_block["output"] = _SCHEMA_OUTPUT_REF

    # agent.yaml — block-style YAML for readability, not flow-style.
    # sort_keys=False preserves the order the LLM chose (api_version,
    # kind, name first is the convention every template uses).
    (target_dir / "agent.yaml").write_text(
        yaml.safe_dump(agent_yaml, sort_keys=False, default_flow_style=False)
    )

    # prompt.md — verbatim. The LLM is responsible for Jinja correctness;
    # load_agent will catch unrenderable templates downstream.
    (target_dir / "prompt.md").write_text(generated.prompt_md)

    # schema/*.yaml — block-style YAML. The 2020-12 schemas carry a
    # `$schema` key, so the loader uses them verbatim (no shorthand
    # compilation). sort_keys=False keeps `$schema`/`type`/`required`/
    # `properties` in a readable top-to-bottom order.
    schema_dir = target_dir / "schema"
    schema_dir.mkdir(exist_ok=True)
    (schema_dir / "input.yaml").write_text(
        yaml.safe_dump(generated.input_schema, sort_keys=False, default_flow_style=False)
    )
    (schema_dir / "output.yaml").write_text(
        yaml.safe_dump(generated.output_schema, sort_keys=False, default_flow_style=False)
    )

    if generated.sample_evals:
        evals_dir = target_dir / "evals"
        evals_dir.mkdir(exist_ok=True)
        # JSONL: one entry per line, no trailing comma chaos.
        (evals_dir / "dataset.jsonl").write_text(
            "\n".join(json.dumps(e) for e in generated.sample_evals) + "\n"
        )
        # judge.yaml.example — the optional LLM-as-judge config bundled
        # templates ship. Written alongside the dataset (an empty-evals
        # scaffold has nothing to judge, so the example stays out of it).
        (evals_dir / "judge.yaml.example").write_text(_JUDGE_YAML_EXAMPLE)
