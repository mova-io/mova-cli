"""Input guardrails for movate agents.

Today ships the ``prompt_injection`` detector — a heuristic regex scanner that
blocks known injection patterns before they reach the LLM, incurring zero token
cost.

Configured per-agent via ``movate.yaml``::

    policy:
      input_guardrails:
        - prompt_injection

The detector is invoked by :class:`movate.core.executor.Executor` at the top of
``execute()``, BEFORE any provider call, when the agent's
:attr:`~movate.core.config.AgentPolicy.input_guardrails` list contains the
``"prompt_injection"`` token.

Raises :class:`movate.core.failures.GuardrailViolationError` on detection, which
propagates the same path as other typed failures (persisted
:class:`~movate.core.models.FailureRecord` with
``failure_type=FailureType.GUARDRAIL_VIOLATION``, non-retryable, response status
``"error"``).
"""

from __future__ import annotations

from movate.core.guardrails.prompt_injection import DetectionResult, PromptInjectionDetector

__all__ = [
    "DetectionResult",
    "PromptInjectionDetector",
]
