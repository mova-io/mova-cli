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
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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


# ---------------------------------------------------------------------------
# Meta-prompt — instructs the LLM how to map description → GeneratedAgent.
# Constraints live ABOVE the few-shot so the model anchors on rules first
# and examples second; reversing this in pilot runs produced more
# hallucinated JSON Schema types ("datetime", "uuid").
# ---------------------------------------------------------------------------

_META_PROMPT = """\
You are scaffolding a movate AI agent from a natural-language description.

USER DESCRIPTION:
\"\"\"
{description}
\"\"\"

AGENT NAME: {name}

Your job is to generate a complete, runnable agent as a single JSON object
matching the GeneratedAgent schema below. The CLI will write the four files
(agent.yaml, prompt.md, schema/input.json, schema/output.json) plus an
evals/dataset.jsonl to disk and then validate the result by loading it.

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
   - name: "{name}"  (exactly this value)
   - version: "0.1.0"
   - model.provider: "openai/gpt-4o-mini-2024-07-18"  (or another valid LiteLLM string)
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

EXAMPLE 1 (FAQ agent):
{example_faq}

EXAMPLE 2 (Classifier agent):
{example_classifier}

Now generate the GeneratedAgent JSON for: \"{description}\" (name: {name}).
Respond with the JSON object only.
"""


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
    previous_attempt: GeneratedAgent | None = None,
    validation_error: str | None = None,
) -> GeneratedAgent:
    """Single LLM-driven generation attempt.

    Returns a validated :class:`GeneratedAgent`. Raises
    :class:`LLMScaffoldError` on any of:

    * Wire error from the provider.
    * LLM returned non-JSON.
    * LLM returned JSON that doesn't match GeneratedAgent's schema.

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
            example_faq=_EXAMPLE_FAQ,
            example_classifier=_EXAMPLE_CLASSIFIER,
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
            f"LLM returned invalid JSON: {exc}. "
            f"First 200 chars of response: {raw[:200]!r}"
        ) from exc

    try:
        return GeneratedAgent.model_validate(payload)
    except ValidationError as exc:
        raise LLMScaffoldError(
            f"LLM output doesn't match GeneratedAgent schema:\n{exc}"
        ) from exc


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
