"""MockProvider — deterministic, network-free implementation of BaseLLMProvider.

Used by the smoke test suite and the ``--mock`` flag. Default response is a
minimal JSON object that satisfies the scaffolded agent template's output
schema. Override with ``MOVATE_MOCK_RESPONSE`` or the ``response=`` arg.

**Dataset-aware mode** (PR #104, May 2026): when configured with an
agent's ``evals/dataset.jsonl[*].expected`` outputs via
:meth:`configure_dataset`, the mock cycles through those expected
outputs on each ``complete()`` call. Because the eval engine
iterates the dataset in order, eval-with-mock produces
schema-conforming responses that PASS validation — closes the
demo-day annoyance where every ``mdk eval --mock <agent>`` failed
with "model output failed schema." For single-shot ``mdk run
--mock`` the mock returns the FIRST dataset row's expected (still
schema-conforming).

Special case: when the prompt looks like an LLM-as-judge prompt (contains
``Rubric:``), the mock returns a deterministic ``{"score": ..., "rationale":
"mock"}`` payload so ``--mock`` works end-to-end through ``movate eval`` and
``movate bench`` without a second env var. The judge-response path is
NOT subject to the dataset-cycle — judge prompts are independent of
the agent's own dataset rows.

**Scaffold-aware mode**: when the prompt is the ``mdk init --llm``
scaffold meta-prompt (or its retry variant), the mock synthesizes a
valid :class:`movate.scaffold.GeneratedAgent` JSON payload so ``mdk
init --llm --mock`` produces a runnable agent offline (no API key). It
classifies the operator's description into a canonical SHAPE (F2, #111
— QA, classifier, summarizer, extraction; F3, #112 — grounded/RAG) and
emits a shape-appropriate output schema + prompt, mirroring the
meta-prompt's SHAPE-SELECTION. Like dataset-aware mode, this fires ONLY
when no explicit ``MOVATE_MOCK_RESPONSE`` / ``response=`` was set — an
explicit override always wins.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from movate.core.models import TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)

_DEFAULT_RESPONSE = '{"message": "mock response"}'
_DEFAULT_JUDGE_RESPONSE = '{"score": 0.5, "rationale": "mock judge"}'
_RESPONSE_ENV = "MOVATE_MOCK_RESPONSE"
_JUDGE_RESPONSE_ENV = "MOVATE_MOCK_JUDGE_RESPONSE"

# --- Scaffold-aware mode (offline `mdk init --llm --mock`) ----------------
#
# `mdk init <name> --llm "<desc>" --mock` is advertised as the no-key /
# hermetic-CI path, but the canned `{"message": "mock response"}` fails
# `GeneratedAgent` validation (Extra inputs not permitted / agent_yaml
# required) → hard exit. To make `--mock` actually produce a runnable
# agent offline, the mock detects the scaffold meta-prompt (and its retry
# variant) and synthesizes a minimal but valid `GeneratedAgent` JSON
# payload: a generic text-in → message-out agent, mirroring the default
# template. The synthesized payload is engineered to pass BOTH
# `GeneratedAgent.model_validate` AND `load_agent()`.
#
# Detection markers are stable substrings of the scaffold prompts (see
# `movate.scaffold.llm_scaffold._META_PROMPT` / `_RETRY_PROMPT`). We key
# on phrases that no judge/dataset/user prompt would contain, so this
# never mis-fires on the existing judge (`Rubric:`) or dataset paths.
#
# CRITICAL: this synthesis ONLY fires when `self._response_is_default`
# (no explicit `MOVATE_MOCK_RESPONSE` / `response=`). An operator who set
# an explicit response — including the phase-3 tests that force-feed a
# valid `GeneratedAgent` — always wins, exactly like dataset-aware mode.
_SCAFFOLD_PROMPT_MARKERS = (
    "scaffolding a movate AI agent",
    "GENERATEDAGENT SCHEMA",
)
_SCAFFOLD_RETRY_MARKERS = (
    "failed validation",
    "GeneratedAgent JSON",
)
# The agent-name line the meta-prompt emits — we parse the requested name
# back out so the synthesized agent.yaml carries it (the CLI also coerces
# the name post-generation, so a parse miss is harmless).
_SCAFFOLD_NAME_PREFIX = "AGENT NAME:"

# The meta-prompt wraps the operator's description between these markers
# (`USER DESCRIPTION:` then a triple-quote fence). The offline scaffold
# path parses the description back out so it can classify grounding /
# RAG intent (F3, #112) deterministically — mirroring the classification
# the real LLM does via the meta-prompt's GROUNDING DETECTION constraint.
_SCAFFOLD_DESC_PREFIX = "USER DESCRIPTION:"

# Substrings that signal the agent should ANSWER FROM A KNOWLEDGE SOURCE
# (grounding / RAG intent). Kept deliberately broad — false positives in
# `--mock` just produce a grounded scaffold (still valid); the real LLM
# path does the nuanced classification. Mirrors the phrasing the
# meta-prompt's constraint #7 keys on (docs/FAQ/policies/"answer
# questions about X"/URLs).
_GROUNDING_MARKERS = (
    "knowledge base",
    "knowledge-base",
    "documentation",
    " docs",
    "help center",
    "help docs",
    "help articles",
    "faq",
    "frequently asked",
    "policy",
    "policies",
    "handbook",
    "knowledge source",
    "answer questions about",
    "answer questions from",
    "questions about our",
    "based on our",
    "based on the",
    "grounded",
    "retrieval",
    "rag ",
    "wiki",
    "http://",
    "https://",
    "www.",
)


def _looks_like_grounding_description(description: str) -> bool:
    """True when ``description`` implies a grounded / RAG agent (F3, #112).

    The agent should answer from a knowledge source (docs, FAQ, policy
    corpus, a website / URL, "answer questions about X"). Used by the
    offline ``--mock`` scaffold path to deterministically emit a
    RAG-shaped agent for grounding descriptions — matching the
    classification the real LLM does from the meta-prompt. Substring
    match on a lowercased description; deliberately lenient.
    """
    haystack = description.lower()
    return any(marker in haystack for marker in _GROUNDING_MARKERS)


# --- Per-shape detection (F2, #111) ---------------------------------------
#
# The offline `--mock` scaffold path classifies a non-grounding description
# into one of the canonical SHAPES so it can emit a shape-appropriate output
# schema + prompt — mirroring the SHAPE-SELECTION instruction the real LLM
# follows from the meta-prompt. Grounding/RAG is checked FIRST elsewhere
# (`_looks_like_grounding_description`); these markers cover the remaining
# shapes. Deliberately lenient substring matching: a `--mock` misfire just
# yields a different (still valid) scaffold, and the real LLM does the
# nuanced classification. The shape names match the meta-prompt's taxonomy.
_CLASSIFIER_MARKERS = (
    "classify",
    "classifier",
    "categorize",
    "categorise",
    "categorization",
    "label ",
    "labeling",
    "labelling",
    "route ",
    "routing",
    "triage",
    "sentiment",
    "tag ",
    "tagging",
    "detect ",
)
_SUMMARIZER_MARKERS = (
    "summarize",
    "summarise",
    "summary",
    "summarization",
    "summarisation",
    "condense",
    "tl;dr",
    "tldr",
    "digest",
    "shorten",
    "brief ",
    "briefing",
    "recap",
)
_EXTRACTION_MARKERS = (
    "extract",
    "extraction",
    "pull out",
    "parse ",
    "capture ",
    "named field",
    "named entit",
    "structured field",
    "line item",
)


def _detect_shape(description: str) -> str:
    """Classify a NON-grounding ``description`` into a canonical shape (F2).

    Returns one of ``"classifier"``, ``"summarizer"``, ``"extraction"``,
    or ``"qa"`` (the default). Mirrors the meta-prompt's SHAPE-SELECTION
    order for the non-grounding shapes — grounding/RAG is decided BEFORE
    this is called (see :func:`_looks_like_grounding_description`), so it
    is intentionally absent here. First marker group to match wins;
    nothing matching falls through to ``"qa"`` (today's default shape).

    Substring match on a lowercased description; deliberately lenient —
    a misfire under ``--mock`` just produces a different valid scaffold.
    """
    haystack = description.lower()
    if any(marker in haystack for marker in _CLASSIFIER_MARKERS):
        return "classifier"
    if any(marker in haystack for marker in _SUMMARIZER_MARKERS):
        return "summarizer"
    if any(marker in haystack for marker in _EXTRACTION_MARKERS):
        return "extraction"
    return "qa"


def _parse_scaffold_description(body: str) -> str:
    """Pull the operator's description out of the scaffold meta-prompt.

    The meta-prompt emits::

        USER DESCRIPTION:
        \"\"\"
        <description>
        \"\"\"

    Best-effort: returns the text between the triple-quote fences after
    the ``USER DESCRIPTION:`` marker, or ``""`` on any parse miss (which
    classifies as non-grounding → the generic scaffold, a safe default).
    The retry prompt has no ``USER DESCRIPTION:`` block, so a retry of a
    RAG scaffold falls back to generic — acceptable, since the offline
    mock's first attempt already validates (no retry fires).
    """
    marker_idx = body.find(_SCAFFOLD_DESC_PREFIX)
    if marker_idx == -1:
        return ""
    rest = body[marker_idx + len(_SCAFFOLD_DESC_PREFIX) :]
    fence = '"""'
    open_idx = rest.find(fence)
    if open_idx == -1:
        return ""
    after_open = rest[open_idx + len(fence) :]
    close_idx = after_open.find(fence)
    if close_idx == -1:
        return ""
    return after_open[:close_idx].strip()


def _looks_like_scaffold_prompt(body: str) -> bool:
    """True if ``body`` is the LLM-scaffold meta-prompt or its retry form.

    Matches either: ALL of the meta-prompt markers, OR all of the retry
    markers. Both groups use phrases unique to the scaffold prompts so
    this never collides with judge (``Rubric:``) or agent-run prompts.
    """
    if all(marker in body for marker in _SCAFFOLD_PROMPT_MARKERS):
        return True
    return all(marker in body for marker in _SCAFFOLD_RETRY_MARKERS)


def _parse_scaffold_name(body: str, *, default: str = "mock-agent") -> str:
    """Pull the requested agent name out of the scaffold prompt.

    The meta-prompt emits an ``AGENT NAME: <name>`` line. Best-effort:
    on any parse miss we return ``default`` — the CLI coerces the
    generated ``agent_yaml.name`` to the real ``<name>`` argument anyway.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(_SCAFFOLD_NAME_PREFIX):
            candidate = stripped[len(_SCAFFOLD_NAME_PREFIX) :].strip()
            if candidate:
                return candidate
    return default


def _build_rag_scaffold_response(name: str) -> str:
    """Return a valid RAG-shaped ``GeneratedAgent`` JSON payload (F3, #112).

    Emitted by the offline ``--mock`` path when the description implies
    grounding (see :func:`_looks_like_grounding_description`). The shape
    mirrors the meta-prompt's RAG exemplar + the packaged
    ``rag_qa_agent`` template:

    * ``agent_yaml.skills = ["kb-vector-lookup"]`` — the built-in
      retrieval skill the Executor pre-invokes.
    * ``agent_yaml.retrieval = {auto_into: context, query_from: question}``
      — ADR 023 opt-in pre-retrieval. The Executor auto-fills
      ``input.context`` before the prompt renders.
    * an OPTIONAL ``context: list[string]`` input field (NOT required —
      retrieval populates it) alongside the required ``question`` field.
    * a grounded prompt that answers FROM ``input.context``, cites by
      1-based index, and declines (``grounded: false``) on empty context.

    Engineered to pass ``GeneratedAgent.model_validate`` AND
    ``load_agent()`` once the ``kb-vector-lookup`` skill is provisioned
    alongside the agent (the CLI does this for both the validation
    tempdir and the committed scaffold).
    """
    payload: dict[str, Any] = {
        "agent_yaml": {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": name,
            "version": "0.1.0",
            "description": (
                "Answers questions grounded in a retrieved knowledge source. "
                "Cites the supporting context and declines when the source "
                "does not cover the question."
            ),
            "owner": "",
            "model": {
                "provider": "openai/gpt-4o-mini-2024-07-18",
                "params": {"temperature": 0.0, "max_tokens": 1024},
            },
            "prompt": "./prompt.md",
            "schema": {
                "input": "./schema/input.json",
                "output": "./schema/output.json",
            },
            "evals": {"dataset": "./evals/dataset.jsonl"},
            "tags": ["rag", "qa", "grounded"],
            "skills": ["kb-vector-lookup"],
            "retrieval": {"auto_into": "context", "query_from": "question"},
        },
        "prompt_md": (
            "You are a grounded question-answering assistant. Answer ONLY "
            "from the retrieved context below — never from outside "
            "knowledge. Every claim must trace to a numbered context "
            "chunk.\n\n"
            "# Context\n"
            "{% for chunk in input.context %}\n"
            "[{{ loop.index }}] {{ chunk }}\n"
            "{% endfor %}\n\n"
            "# Question\n"
            "{{ input.question }}\n\n"
            "If the context is empty or does not support an answer, set "
            '"grounded": false, return an empty "citations" list, and say '
            "what information is missing — do NOT fabricate.\n\n"
            "Respond with a single JSON object on one line:\n"
            '{"answer": "<grounded answer>", "citations": [<1-based chunk '
            'indices>], "grounded": <true|false>, "confidence": <0.0-1.0>}'
        ),
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            # NB: `context` is intentionally NOT in `required` — ADR 023
            # pre-retrieval auto-fills it before the prompt renders.
            "required": ["question"],
            "properties": {
                "question": {"type": "string", "minLength": 1},
                "context": {"type": "array", "items": {"type": "string"}},
            },
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["answer", "citations", "grounded", "confidence"],
            "properties": {
                "answer": {"type": "string", "minLength": 1},
                "citations": {"type": "array", "items": {"type": "integer"}},
                "grounded": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "sample_evals": [
            {
                "input": {
                    "question": "What is the refund window?",
                    "context": [
                        "Annual plans are refundable within 14 days of "
                        "purchase, prorated by the unused portion."
                    ],
                },
                "expected": {
                    "answer": (
                        "Annual plans are refundable within 14 days of "
                        "purchase, prorated by the unused portion."
                    ),
                    "citations": [1],
                    "grounded": True,
                    "confidence": 0.95,
                },
            },
            {
                "input": {"question": "Do you support SAML SSO?", "context": []},
                "expected": {
                    "answer": "The provided context does not cover SAML SSO support.",
                    "citations": [],
                    "grounded": False,
                    "confidence": 0.0,
                },
            },
        ],
    }
    return json.dumps(payload)


def _agent_yaml_base(name: str, *, description: str, max_tokens: int = 512) -> dict[str, Any]:
    """Shared ``agent_yaml`` skeleton for the single-turn shapes (F2, #111).

    Every non-grounding shape shares the same required-key spine (api_version,
    kind, name, version, model, prompt, schema, evals); only ``description``
    and ``max_tokens`` vary. Factored out so each shape builder only declares
    what makes it distinct (its output contract + prompt). Note: NO ``skills``
    or ``retrieval`` keys — those are exclusive to the RAG shape.
    """
    return {
        "api_version": "movate/v1",
        "kind": "Agent",
        "name": name,
        "version": "0.1.0",
        "description": description,
        "owner": "",
        "model": {
            "provider": "openai/gpt-4o-mini-2024-07-18",
            "params": {"temperature": 0.0, "max_tokens": max_tokens},
        },
        "prompt": "./prompt.md",
        "schema": {
            "input": "./schema/input.json",
            "output": "./schema/output.json",
        },
        "evals": {"dataset": "./evals/dataset.jsonl"},
    }


def _build_qa_scaffold_response(name: str) -> str:
    """The QA / FAQ shape — ``{answer, confidence}`` (F2, #111).

    Today's default shape: a free-text question answered from the model's
    own knowledge, NOT from a retrieved corpus. Mirrors the meta-prompt's
    FAQ exemplar + the packaged ``faq_agent`` template. Engineered to pass
    ``GeneratedAgent.model_validate`` AND ``load_agent()`` offline.
    """
    payload: dict[str, Any] = {
        "agent_yaml": _agent_yaml_base(
            name,
            description="Answers questions concisely with a confidence score.",
        ),
        "prompt_md": (
            "You answer questions concisely.\n\n"
            "Question:\n{{ input.question }}\n\n"
            "Respond with a single JSON object on one line:\n"
            '{"answer": "<your answer>", "confidence": <0.0-1.0>}'
        ),
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["question"],
            "properties": {"question": {"type": "string", "minLength": 1}},
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["answer", "confidence"],
            "properties": {
                "answer": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "sample_evals": [
            {
                "input": {"question": "What is your refund window?"},
                "expected": {"answer": "30 days from purchase.", "confidence": 0.95},
            },
            {
                "input": {"question": "Do you support SAML SSO?"},
                "expected": {"answer": "Yes, on the Enterprise tier.", "confidence": 0.9},
            },
        ],
    }
    return json.dumps(payload)


def _build_classifier_scaffold_response(name: str) -> str:
    """The classifier shape — ``{label, confidence}`` (F2, #111).

    For "classify / categorize / label / route / triage / sentiment"
    descriptions. The input carries the text plus the candidate ``labels``;
    the output is the chosen label with a confidence. Mirrors the
    meta-prompt's classifier exemplar + the packaged ``classifier_agent``
    template.
    """
    payload: dict[str, Any] = {
        "agent_yaml": _agent_yaml_base(
            name,
            description="Classifies input text into one of a fixed list of labels.",
            max_tokens=64,
        ),
        "prompt_md": (
            "You are a text classifier. Pick exactly one label from the "
            "provided list.\n\n"
            "Text:\n{{ input.text }}\n\n"
            "Available labels:\n"
            "{% for label in input.labels %}- {{ label }}\n{% endfor %}\n"
            "Respond with a single JSON object on one line:\n"
            '{"label": "<chosen label>", "confidence": <0.0-1.0>}'
        ),
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "labels"],
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "labels": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            },
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["label", "confidence"],
            "properties": {
                "label": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "sample_evals": [
            {
                "input": {"text": "I loved this!", "labels": ["positive", "negative", "neutral"]},
                "expected": {"label": "positive", "confidence": 0.97},
            },
            {
                "input": {
                    "text": "Worst experience ever.",
                    "labels": ["positive", "negative", "neutral"],
                },
                "expected": {"label": "negative", "confidence": 0.95},
            },
        ],
    }
    return json.dumps(payload)


def _build_summarizer_scaffold_response(name: str) -> str:
    """The summarizer shape — ``{summary, key_points}`` (F2, #111).

    For "summarize / condense / tl;dr / digest / shorten" descriptions.
    ``key_points`` is an array of strings (the salient bullets);
    ``max_words`` is an OPTIONAL input knob (absent from ``required``).
    Mirrors the meta-prompt's summarizer exemplar + the packaged
    ``summarizer_agent`` template's intent.
    """
    payload: dict[str, Any] = {
        "agent_yaml": _agent_yaml_base(
            name,
            description="Summarizes input text into a concise summary plus key points.",
        ),
        "prompt_md": (
            "You are a summarization assistant. Read the text below and "
            "produce a concise summary plus the key points. Do not add facts "
            "that are not in the text.\n\n"
            "Text:\n{{ input.text }}\n\n"
            "Respond with a single JSON object on one line:\n"
            '{"summary": "<concise summary>", "key_points": ["<point 1>", '
            '"<point 2>"]}'
        ),
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            # `max_words` is intentionally NOT required — an optional knob.
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "max_words": {"type": "integer", "minimum": 1},
            },
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "key_points"],
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "key_points": {"type": "array", "items": {"type": "string"}},
            },
        },
        "sample_evals": [
            {
                "input": {
                    "text": (
                        "Q3 revenue grew 18% YoY on enterprise renewals; "
                        "operating margin expanded to 22% on cost "
                        "optimization. Headcount held flat."
                    )
                },
                "expected": {
                    "summary": (
                        "Q3 revenue rose 18% YoY and margin expanded to 22%, with flat headcount."
                    ),
                    "key_points": [
                        "Revenue up 18% YoY on enterprise renewals",
                        "Operating margin expanded to 22%",
                        "Headcount held flat",
                    ],
                },
            },
            {
                "input": {
                    "text": (
                        "The release fixes a login bug, adds dark mode, and "
                        "improves export speed by 30%."
                    )
                },
                "expected": {
                    "summary": (
                        "The release fixes a login bug, adds dark mode, and speeds up exports."
                    ),
                    "key_points": [
                        "Login bug fixed",
                        "Dark mode added",
                        "Export speed improved 30%",
                    ],
                },
            },
        ],
    }
    return json.dumps(payload)


def _build_extraction_scaffold_response(name: str) -> str:
    """The extraction shape — structured named fields (F2, #111).

    For "extract / pull out / parse named fields" descriptions. The output
    properties ARE the named entities; fields the source may omit get a
    nullable type (``["string", "null"]``) and the prompt returns null
    rather than fabricating — but the KEY stays in ``required`` (a present
    key, possibly-null value). This is the validate-safe way to express
    "optional value"; NEVER a key-suffix ``?``. Mirrors the meta-prompt's
    extraction exemplar + the packaged ``extractor_agent`` template.
    """
    payload: dict[str, Any] = {
        "agent_yaml": _agent_yaml_base(
            name,
            description=(
                "Extracts named fields (contact name, email, organization, "
                "intent) from unstructured text."
            ),
        ),
        "prompt_md": (
            "You are a strict structured-field extractor. Read the text and "
            "pull out the requested fields. If a field is not present in the "
            "text, return null — do NOT invent or infer.\n\n"
            "Fields:\n"
            "- contact_name: the person's full name, or null.\n"
            "- email: a valid email address, or null.\n"
            "- organization: the company / org name, or null.\n"
            "- intent: a short label for what the writer wants, or null.\n\n"
            "Text:\n{{ input.text }}\n\n"
            "Respond with a single JSON object on one line:\n"
            '{"contact_name": "...", "email": "...", "organization": "...", '
            '"intent": "..."}'
        ),
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            # Every field is REQUIRED (the key must be present) but
            # nullable (the VALUE may be null when the source omits it).
            "required": ["contact_name", "email", "organization", "intent"],
            "properties": {
                "contact_name": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"], "format": "email"},
                "organization": {"type": ["string", "null"]},
                "intent": {"type": ["string", "null"]},
            },
        },
        "sample_evals": [
            {
                "input": {
                    "text": (
                        "Hi, this is Sarah Chen from Acme Corp "
                        "(sarah@acme.example). We'd like a demo of the "
                        "Enterprise tier."
                    )
                },
                "expected": {
                    "contact_name": "Sarah Chen",
                    "email": "sarah@acme.example",
                    "organization": "Acme Corp",
                    "intent": "demo_request",
                },
            },
            {
                "input": {"text": "Please cancel my subscription."},
                "expected": {
                    "contact_name": None,
                    "email": None,
                    "organization": None,
                    "intent": "cancellation",
                },
            },
        ],
    }
    return json.dumps(payload)


# Dispatch table for the non-grounding shapes (F2, #111). Keyed by the shape
# name :func:`_detect_shape` returns; grounding/RAG is handled separately in
# :func:`_build_scaffold_response` (it has its own detector + builder).
_SHAPE_BUILDERS = {
    "classifier": _build_classifier_scaffold_response,
    "summarizer": _build_summarizer_scaffold_response,
    "extraction": _build_extraction_scaffold_response,
    "qa": _build_qa_scaffold_response,
}


def _build_scaffold_response(
    name: str, *, grounding: bool = False, shape: str | None = None
) -> str:
    """Return a valid ``GeneratedAgent`` JSON payload for the right SHAPE.

    Selection order (mirrors the meta-prompt's SHAPE-SELECTION):

    * ``grounding=True`` (F3, #112) → the RAG-shaped scaffold
      (:func:`_build_rag_scaffold_response`). Checked FIRST and unchanged.
    * otherwise (F2, #111) the ``shape`` arg picks a single-turn shape:
      ``"classifier"`` → ``{label, confidence}``, ``"summarizer"`` →
      ``{summary, key_points}``, ``"extraction"`` → structured named
      fields, ``"qa"`` (default) → ``{answer, confidence}``.

    ``shape`` defaults to ``"qa"`` when unset, so a caller that passes
    neither keyword gets today's QA shape — back-compat for any code that
    called ``_build_scaffold_response(name)`` before F2. An unrecognized
    ``shape`` likewise falls back to QA.

    Every branch is engineered to satisfy the meta-prompt's HARD
    CONSTRAINTS so the result passes both ``GeneratedAgent.model_validate``
    and ``load_agent()`` — i.e. ``mdk init --llm --mock`` yields a runnable
    agent offline regardless of shape.
    """
    if grounding:
        return _build_rag_scaffold_response(name)
    builder = _SHAPE_BUILDERS.get(shape or "qa", _build_qa_scaffold_response)
    return builder(name)


class MockProvider(BaseLLMProvider):
    name = "mock"
    version = "0.0.1"

    def __init__(
        self,
        response: str | None = None,
        *,
        judge_response: str | None = None,
        tool_script: list[tuple[str, dict[str, object]]] | None = None,
    ) -> None:
        """Construct a deterministic mock.

        ``tool_script`` lets tests script a tool-use loop. Each entry
        is ``(tool_name, tool_input_dict)`` — when ``complete()`` is
        called with non-empty ``tools``, the mock returns the next
        entry as a ``kind="tool_use"`` response. After the script is
        exhausted, ``complete()`` returns the final ``response`` as a
        regular ``kind="final"`` reply. This mirrors how a real LLM
        decides "I need to call a tool" → "I have the result, here's
        my final answer."
        """
        # Track whether the response was EXPLICITLY overridden. Explicit
        # overrides defeat dataset-aware mode below — operators using
        # `MOVATE_MOCK_RESPONSE` (or the `response=` constructor arg)
        # clearly want a fixed canned response; respecting that lets
        # tests force-fail scenarios still work after PR #104.
        explicit_response = response is not None or _RESPONSE_ENV in os.environ
        self._response = response or os.environ.get(_RESPONSE_ENV, _DEFAULT_RESPONSE)
        self._response_is_default = not explicit_response
        self._judge_response = judge_response or os.environ.get(
            _JUDGE_RESPONSE_ENV, _DEFAULT_JUDGE_RESPONSE
        )
        # Sanity check at construction time so tests fail loud, not at runtime.
        json.loads(self._response)
        json.loads(self._judge_response)
        self._tool_script: list[tuple[str, dict[str, object]]] = list(tool_script or [])
        self._tool_calls_emitted = 0
        # Dataset-aware mode (PR #104). Populated post-construction
        # via :meth:`configure_dataset`. When non-empty AND no
        # explicit response was set, ``complete()`` cycles through
        # these on each call instead of returning the default response.
        # Explicit `MOVATE_MOCK_RESPONSE` / `response=` overrides win.
        self._dataset_expecteds: list[Any] = []
        self._dataset_call_index = 0

    def configure_dataset(self, expecteds: list[Any]) -> None:
        """Switch the mock into dataset-aware mode.

        ``expecteds`` is the list of ``dataset.jsonl[*].expected``
        outputs, in dataset order. After this call, each invocation
        of :meth:`complete` returns the next entry (cycling at end).
        Pass an empty list to reset back to the canned ``response``.

        Used by ``mdk run --mock`` and ``mdk eval --mock`` to make
        the mock produce schema-conforming outputs that match what
        the dataset says the agent SHOULD return. Without this, the
        mock returns the canned ``{"message": "mock response"}``
        which fails validation against any non-trivial output
        schema (the previous demo annoyance).
        """
        self._dataset_expecteds = list(expecteds)
        self._dataset_call_index = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content if request.messages else ""
        prompt_chars = sum(len(m.content) for m in request.messages)

        # Tool-use scripting: when the request has tools AND the script
        # still has entries, emit the next tool call. Each call gets a
        # deterministic id ``mock-tool-<n>`` so test assertions can
        # match by index. After the script is exhausted, fall through
        # to the final response below.
        if request.tools and self._tool_calls_emitted < len(self._tool_script):
            name, args = self._tool_script[self._tool_calls_emitted]
            call_id = f"mock-tool-{self._tool_calls_emitted}"
            self._tool_calls_emitted += 1
            return CompletionResponse(
                text="",
                tokens=TokenUsage(
                    input=max(1, prompt_chars // 4),
                    output=1,
                ),
                raw={"mock": True, "provider": request.provider, "tool_use": True},
                kind="tool_use",
                tool_name=name,
                tool_id=call_id,
                tool_input=args,
            )

        # Four-way choice for the response text:
        # 1. Judge prompt → canned judge-response (rubric-aware)
        # 2. Scaffold prompt → synthesized valid GeneratedAgent JSON
        #    (offline `mdk init --llm --mock`); default-response only
        # 3. Dataset-aware mode (PR #104) → next expected from dataset
        # 4. Default → canned _response
        is_judge_prompt = "Rubric:" in body
        if is_judge_prompt:
            text = self._judge_response
        elif self._response_is_default and _looks_like_scaffold_prompt(body):
            # Offline scaffold path. Only when the response wasn't
            # explicitly overridden — phase-3 tests that force-feed a
            # GeneratedAgent via MOVATE_MOCK_RESPONSE must still win.
            #
            # Classify the description into a canonical SHAPE so the offline
            # scaffold matches the described intent (mirrors the meta-prompt's
            # SHAPE-SELECTION). Grounding/RAG (F3, #112) is checked FIRST; if
            # it doesn't match, F2 (#111) picks a single-turn shape
            # (classifier / summarizer / extraction / qa). Deterministic +
            # offline.
            description = _parse_scaffold_description(body)
            scaffold_name = _parse_scaffold_name(body)
            if _looks_like_grounding_description(description):
                text = _build_scaffold_response(scaffold_name, grounding=True)
            else:
                text = _build_scaffold_response(scaffold_name, shape=_detect_shape(description))
        elif self._dataset_expecteds and self._response_is_default:
            # Cycle through dataset rows in order. Wraps at the end so
            # callers that exceed dataset length still get valid output
            # (rather than IndexError or fallback to non-conforming
            # default). Skipped when the operator explicitly overrode
            # the response (env var or constructor arg) — they wanted
            # a fixed value, respect that.
            expected = self._dataset_expecteds[
                self._dataset_call_index % len(self._dataset_expecteds)
            ]
            self._dataset_call_index += 1
            # Serialize the expected dict to JSON — that's how the
            # provider's text body crosses into the schema validator.
            text = json.dumps(expected)
        else:
            text = self._response
        return CompletionResponse(
            text=text,
            tokens=TokenUsage(
                input=max(1, prompt_chars // 4),
                output=max(1, len(text) // 4),
            ),
            raw={"mock": True, "provider": request.provider},
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Deterministic streaming for tests: chunk the canned response
        into ~10-char slices, then emit a final usage-only chunk so
        cost accounting downstream sees real numbers."""
        body = request.messages[0].content if request.messages else ""
        text = self._judge_response if "Rubric:" in body else self._response
        prompt_chars = sum(len(m.content) for m in request.messages)
        # Yield in small slices so test code observing the chunks
        # actually sees a stream (more than one chunk).
        slice_size = 10
        for i in range(0, len(text), slice_size):
            yield StreamChunk(text=text[i : i + slice_size])
        # Final chunk: zero text, populated tokens (mirrors LiteLLM's
        # include_usage=True behaviour).
        yield StreamChunk(
            text="",
            tokens=TokenUsage(
                input=max(1, prompt_chars // 4),
                output=max(1, len(text) // 4),
            ),
        )

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError


def load_dataset_expecteds(dataset_path: Any) -> list[Any]:
    """Read an agent's ``evals/dataset.jsonl`` and return its
    ``expected`` outputs in order.

    Used by the CLI to switch :class:`MockProvider` into dataset-aware
    mode just before running an agent or eval. Best-effort: a
    missing / malformed dataset yields an empty list — the mock then
    falls back to its canned response.

    ``dataset_path`` is duck-typed (``pathlib.Path``-shaped object
    expected). Lazy-typed because :mod:`movate.providers.mock`
    shouldn't import pathlib just for this helper.
    """
    if dataset_path is None:
        return []
    try:
        text = dataset_path.read_text()
    except (OSError, AttributeError):
        return []
    expecteds: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            # Malformed row — skip silently. The eval engine's own
            # dataset loader surfaces the canonical error elsewhere.
            continue
        if isinstance(row, dict) and "expected" in row:
            expecteds.append(row["expected"])
    return expecteds
