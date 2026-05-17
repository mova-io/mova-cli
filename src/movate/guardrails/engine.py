"""Guardrails orchestrator + the verdict type the executor consumes.

Pulls together :mod:`movate.guardrails.pii`,
:mod:`movate.guardrails.topic`, and :mod:`movate.guardrails.content`
into two top-level entry points the executor calls:

* :func:`check_input` — runs all enabled INPUT guardrails against
  the rendered prompt OR the raw input text. Called from
  :meth:`movate.core.executor.Executor.execute` after policy / budget
  / skill checks but BEFORE prompt rendering and provider call.
* :func:`check_output` — runs all enabled OUTPUT guardrails against
  the model's completion text. Called after the model responds but
  BEFORE schema validation and persist.

The orchestrator returns a unified :class:`GuardrailVerdict`:

* ``action="allow"`` — text untouched, run proceeds
* ``action="redact"`` — text was modified (PII replaced with markers),
  run proceeds with the modified text in ``redacted_text``
* ``action="warn"`` — text untouched but a non-fatal violation
  occurred; caller may log/trace but should let the run proceed
* ``action="block"`` — caller MUST raise
  :class:`movate.core.failures.ContentFilterError` with ``reason``
  so the rest of the pipeline propagates ``safety_blocked``

Module is fully synchronous (no I/O, no LLM calls in the MVP). The
hot-path cost is dominated by Python regex matching — well below the
runtime budget for a typical agent input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from movate.guardrails import content as content_module
from movate.guardrails import pii as pii_module
from movate.guardrails import topic as topic_module

if TYPE_CHECKING:
    from movate.core.config import GuardrailDirection


Action = Literal["allow", "redact", "warn", "block"]


@dataclass(frozen=True)
class GuardrailVerdict:
    """Unified outcome of a guardrails check.

    ``action`` is what the caller should do:

    * ``allow`` — proceed unchanged.
    * ``redact`` — proceed using ``redacted_text`` in place of the
      original input. Set when PII was scrubbed.
    * ``warn`` — proceed unchanged, but log/trace the ``reason`` so an
      operator can review offline.
    * ``block`` — raise :class:`ContentFilterError` with ``reason``;
      do NOT proceed.

    ``triggered_by`` lists which guardrail(s) fired
    (``"pii" | "topic" | "content"`` — possibly more than one), so the
    operator-facing error names every contributing rule rather than
    surface only the first one. ``matched_terms`` is supplied by the
    underlying module (banned terms, allowed-topic hits) for the
    error message; empty for PII (which speaks in counts, not terms).
    """

    action: Action
    reason: str = ""
    redacted_text: str | None = None
    triggered_by: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()


def check_input(text: str, config: GuardrailDirection) -> GuardrailVerdict:
    """Run enabled INPUT guardrails over ``text``.

    Evaluation order (failing fast on the first ``block``):

    1. PII — may redact (preserves the run with scrubbed text) or
       block. Modes: ``redact``, ``block``, ``warn``.
    2. Topic — checks the (possibly redacted) text against the
       allow/deny list. Modes: ``block``, ``warn``.
    3. Content — banned-terms scan. Modes: ``block``, ``warn``.

    A single ``warn`` accumulates rather than short-circuits; the
    aggregated verdict's ``triggered_by`` lists every warning rule
    so the operator can configure them all in one pass. A ``block``
    short-circuits (no point checking further rules if we're
    blocking anyway).

    Returns ``GuardrailVerdict(action="allow")`` if all guardrails
    pass or are disabled. The caller decides what to do with
    ``redact`` (use ``redacted_text``) vs ``warn`` (log + continue)
    vs ``block`` (raise).
    """
    return _orchestrate(text, config)


def check_output(text: str, config: GuardrailDirection) -> GuardrailVerdict:
    """Run enabled OUTPUT guardrails over the model's completion.

    Same logic as :func:`check_input`; we keep two entry points so
    each direction's config can be tuned independently (you typically
    want STRICTER output guardrails than input — a leaky output
    reaches the customer, a leaky input only reaches the model).

    The redact path is supported on output too — useful for
    "the model regurgitated the email I asked it not to mention"
    style mishaps.
    """
    return _orchestrate(text, config)


# ---------------------------------------------------------------------------
# Internal orchestration
# ---------------------------------------------------------------------------


@dataclass
class _ModuleOutcome:
    """One module's contribution to the aggregated verdict.

    Either ``terminate`` is set (caller returns it as a final
    ``block`` verdict, short-circuiting later modules) or the
    accumulator fields are populated and the loop continues with
    the optional ``new_text`` (set when PII redacted in-place).
    """

    terminate: GuardrailVerdict | None = None
    new_text: str | None = None
    action_upgrade: Action | None = None
    triggered: str | None = None
    matched_terms: tuple[str, ...] = ()
    reason: str = ""


def _check_pii(text: str, cfg: object) -> _ModuleOutcome:
    """Run the PII module; return outcome for the orchestrator."""
    # Empty `types` list = "all supported categories" (see
    # PiiGuardrailConfig docstring) — pass None to ``scan`` so the
    # default-all path engages.
    types_filter = list(cfg.types) if cfg.types else None  # type: ignore[attr-defined]
    matches = pii_module.scan(text, types=types_filter)
    if not matches:
        return _ModuleOutcome()
    if cfg.mode == "block":  # type: ignore[attr-defined]
        return _ModuleOutcome(
            terminate=GuardrailVerdict(
                action="block",
                reason=f"input contains {len(matches)} PII match(es); policy=block",
                triggered_by=("pii",),
            )
        )
    if cfg.mode == "redact":  # type: ignore[attr-defined]
        return _ModuleOutcome(
            new_text=pii_module.redact(text, matches),
            action_upgrade="redact",
            triggered="pii",
            reason=f"redacted {len(matches)} PII match(es)",
        )
    # warn
    return _ModuleOutcome(
        action_upgrade="warn",
        triggered="pii",
        reason=f"warning: {len(matches)} PII match(es)",
    )


def _check_topic(text: str, cfg: object) -> _ModuleOutcome:
    verdict = topic_module.check(
        text,
        allowed_topics=list(cfg.allowed_topics) or None,  # type: ignore[attr-defined]
        banned_topics=list(cfg.banned_topics) or None,  # type: ignore[attr-defined]
    )
    if verdict.status != "violation":
        return _ModuleOutcome()
    if cfg.on_violation == "block":  # type: ignore[attr-defined]
        return _ModuleOutcome(
            terminate=GuardrailVerdict(
                action="block",
                reason=f"topic violation: {verdict.reason}",
                triggered_by=("topic",),
                matched_terms=tuple(verdict.matched_terms),
            )
        )
    return _ModuleOutcome(
        action_upgrade="warn",
        triggered="topic",
        matched_terms=tuple(verdict.matched_terms),
        reason=f"topic warning: {verdict.reason}",
    )


def _check_content(text: str, cfg: object) -> _ModuleOutcome:
    verdict = content_module.check(
        text,
        banned_terms=list(cfg.banned_terms) or None,  # type: ignore[attr-defined]
    )
    if verdict.status != "violation":
        return _ModuleOutcome()
    if cfg.on_violation == "block":  # type: ignore[attr-defined]
        return _ModuleOutcome(
            terminate=GuardrailVerdict(
                action="block",
                reason=f"content violation: {len(verdict.matched_terms)} banned term(s)",
                triggered_by=("content",),
                matched_terms=tuple(verdict.matched_terms),
            )
        )
    return _ModuleOutcome(
        action_upgrade="warn",
        triggered="content",
        matched_terms=tuple(verdict.matched_terms),
        reason=f"content warning: {len(verdict.matched_terms)} banned term(s)",
    )


def _orchestrate(text: str, config: GuardrailDirection) -> GuardrailVerdict:
    """Run all three modules; aggregate to a single verdict.

    Pure function. Sequence: PII → topic → content. ``block`` from
    any module short-circuits via ``_ModuleOutcome.terminate``;
    ``redact`` from PII mutates the working text so subsequent
    modules see the scrubbed version; ``warn`` accumulates.
    """
    working_text = text
    triggered: list[str] = []
    matched_terms: list[str] = []
    reasons: list[str] = []
    final_action: Action = "allow"
    redacted_text_out: str | None = None

    # Each tuple: (enabled-predicate, callable-on-current-text).
    checks: list[tuple[bool, object, object]] = [
        (config.pii.enabled, config.pii, _check_pii),
        (config.topic.enabled, config.topic, _check_topic),
        (config.content.enabled, config.content, _check_content),
    ]

    for enabled, cfg, check_fn in checks:
        if not enabled:
            continue
        outcome = check_fn(working_text, cfg)  # type: ignore[operator]
        if outcome.terminate is not None:
            return outcome.terminate  # type: ignore[no-any-return]
        if outcome.new_text is not None:
            working_text = outcome.new_text
            redacted_text_out = outcome.new_text
        if outcome.action_upgrade is not None:
            final_action = _max_action(final_action, outcome.action_upgrade)
        if outcome.triggered:
            triggered.append(outcome.triggered)
        if outcome.matched_terms:
            matched_terms.extend(outcome.matched_terms)
        if outcome.reason:
            reasons.append(outcome.reason)

    return GuardrailVerdict(
        action=final_action,
        reason="; ".join(reasons),
        redacted_text=redacted_text_out,
        triggered_by=tuple(triggered),
        matched_terms=tuple(matched_terms),
    )


_ACTION_RANK: dict[Action, int] = {"allow": 0, "warn": 1, "redact": 2, "block": 3}


def _max_action(a: Action, b: Action) -> Action:
    """Return the more-severe of two actions.

    Severity: ``allow < warn < redact < block``. Ensures that a
    ``warn`` from one module never downgrades a prior ``redact``,
    and a later ``redact`` doesn't downgrade an already-``warn``
    state (which only happens through programming error — but
    cheap to be explicit).
    """
    return a if _ACTION_RANK[a] >= _ACTION_RANK[b] else b
