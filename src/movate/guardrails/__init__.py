"""Safe-AI guardrails for input + output content.

The MVP (Phase J-0) ships three deterministic, dependency-free
guardrail modules:

* :mod:`movate.guardrails.pii` — regex-based PII detection
  (email, phone, SSN, credit-card). Modes: redact / block / warn.
* :mod:`movate.guardrails.topic` — keyword / regex allow- or deny-list
  on conversation topic. Catches "only talk about Sandisk" style
  restrictions without an LLM call.
* :mod:`movate.guardrails.content` — banned-terms filter. The simple
  defense layer for profanity, leaked internal terms, etc.

A higher-fidelity successor (semantic topic match, moderation API,
spaCy-backed PII NER) can swap in behind the same
:class:`GuardrailVerdict` interface without breaking callers. See
``docs/adr/`` for the design discussion (forthcoming).

Wiring lives in :mod:`movate.core.executor`:

* Input check fires AFTER policy + budget + skill checks but BEFORE
  prompt rendering — so a blocked request never bills latency or
  triggers any side effect.
* Output check fires AFTER the completion lands but BEFORE schema
  validation + persist — so a leaky output is caught before the
  caller sees it.

A blocking verdict raises :class:`movate.core.failures.ContentFilterError`
which propagates as :class:`FailureType.CONTENT_FILTER` and surfaces in
the RunRecord with ``status="safety_blocked"`` — the same status code
the rest of the pipeline (workflow runner / worker / RemoteExecutor)
already treats as terminal-non-retryable.

A redaction verdict mutates the text in-place (input PII redaction
replaces matched spans with ``[REDACTED:type]`` markers) and lets the
run proceed.

A warn verdict leaves the text untouched but emits a tracer event +
log line for offline review.
"""

from __future__ import annotations

from movate.guardrails.engine import (
    GuardrailVerdict,
    check_input,
    check_output,
)

__all__ = [
    "GuardrailVerdict",
    "check_input",
    "check_output",
]
