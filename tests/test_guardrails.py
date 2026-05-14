"""Safe-AI guardrails (Phase J-0) — PII, topic, content modules + engine.

Three concentric layers of coverage:

1. **Module-level** — :mod:`movate.guardrails.pii`, ``topic``, ``content``
   each tested in isolation. Pure functions, deterministic, fast.
2. **Engine-level** — :func:`movate.guardrails.check_input` and
   ``check_output`` orchestrate the three modules behind a single
   :class:`GuardrailVerdict`. Tests cover mode combinations + ordering.
3. **Executor integration** — :class:`Executor` with a
   :class:`GuardrailsConfig` blocks / redacts / warns end-to-end.
   Exercises the ``safety_blocked`` propagation through the existing
   ``RunResponse`` shape.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from movate.core.config import (
    ContentGuardrailConfig,
    GuardrailDirection,
    GuardrailsConfig,
    PiiGuardrailConfig,
    TopicGuardrailConfig,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest
from movate.guardrails import (
    GuardrailVerdict,
    check_input,
    check_output,
)
from movate.guardrails import content as content_mod
from movate.guardrails import pii as pii_mod
from movate.guardrails import topic as topic_mod
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

# ---------------------------------------------------------------------------
# Module: PII
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPii:
    def test_email_detection(self) -> None:
        matches = pii_mod.scan("Contact me at jane@example.com please.")
        assert len(matches) == 1
        assert matches[0].pii_type == "email"
        assert matches[0].text == "jane@example.com"

    def test_phone_detection(self) -> None:
        matches = pii_mod.scan("Call +1 415-555-1234 today.")
        assert len(matches) == 1
        assert matches[0].pii_type == "phone"

    def test_ssn_detection(self) -> None:
        matches = pii_mod.scan("SSN: 123-45-6789")
        assert len(matches) == 1
        assert matches[0].pii_type == "ssn"

    def test_credit_card_detection(self) -> None:
        matches = pii_mod.scan("Card 4111 1111 1111 1111 on file.")
        assert len(matches) == 1
        assert matches[0].pii_type == "credit_card"

    def test_multiple_types_in_one_text(self) -> None:
        text = "Reach Sarah at sarah@acme.io or 415-555-1234."
        matches = pii_mod.scan(text)
        types = {m.pii_type for m in matches}
        assert "email" in types
        assert "phone" in types

    def test_redact_replaces_each_span(self) -> None:
        text = "Reach jane@example.com or 415-555-1234"
        matches = pii_mod.scan(text)
        out = pii_mod.redact(text, matches)
        assert "jane@example.com" not in out
        assert "415-555-1234" not in out
        assert "[REDACTED:email]" in out
        assert "[REDACTED:phone]" in out

    def test_type_filter_excludes_other_categories(self) -> None:
        text = "Email jane@example.com, phone 415-555-1234"
        only_email = pii_mod.scan(text, types=["email"])
        assert len(only_email) == 1
        assert only_email[0].pii_type == "email"

    def test_clean_text_returns_no_matches(self) -> None:
        assert pii_mod.scan("Just normal prose with no PII.") == []

    def test_redact_empty_matches_is_passthrough(self) -> None:
        assert pii_mod.redact("hello world", []) == "hello world"


# ---------------------------------------------------------------------------
# Module: Topic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTopic:
    def test_allowlist_pass_when_term_present(self) -> None:
        v = topic_mod.check(
            "Tell me about Sandisk SSDs",
            allowed_topics=["Sandisk", "Western Digital"],
        )
        assert v.status == "pass"
        assert "Sandisk" in v.matched_terms

    def test_allowlist_violation_when_no_term_present(self) -> None:
        v = topic_mod.check(
            "Tell me about Apple products",
            allowed_topics=["Sandisk", "Western Digital"],
        )
        assert v.status == "violation"

    def test_denylist_violation_when_term_present(self) -> None:
        v = topic_mod.check(
            "Confidential internal pricing data",
            banned_topics=["confidential", "internal pricing"],
        )
        assert v.status == "violation"
        assert "confidential" in v.matched_terms or "internal pricing" in v.matched_terms

    def test_no_constraints_is_pass(self) -> None:
        v = topic_mod.check("anything goes")
        assert v.status == "pass"

    def test_regex_prefix_enables_pattern_matching(self) -> None:
        # Match SKU-like patterns
        v = topic_mod.check(
            "Order SKU-9384",
            banned_topics=["re:SKU-\\d+"],
        )
        assert v.status == "violation"

    def test_substring_match_is_case_insensitive(self) -> None:
        v = topic_mod.check("SANDISK products", allowed_topics=["sandisk"])
        assert v.status == "pass"


# ---------------------------------------------------------------------------
# Module: Content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContent:
    def test_banned_term_triggers_violation(self) -> None:
        v = content_mod.check(
            "This document is INTERNAL ONLY",
            banned_terms=["internal only"],
        )
        assert v.status == "violation"
        assert "internal only" in v.matched_terms

    def test_clean_text_is_pass(self) -> None:
        v = content_mod.check("hello", banned_terms=["secret"])
        assert v.status == "pass"

    def test_empty_banned_list_is_permissive_pass(self) -> None:
        v = content_mod.check("any content")
        assert v.status == "pass"

    def test_multiple_hits_all_listed(self) -> None:
        v = content_mod.check(
            "This has foo and bar terms",
            banned_terms=["foo", "bar", "baz"],
        )
        assert v.status == "violation"
        assert set(v.matched_terms) == {"foo", "bar"}

    def test_regex_term_works(self) -> None:
        v = content_mod.check(
            "alpha-12345-omega",
            banned_terms=["re:\\d{5}"],
        )
        assert v.status == "violation"


# ---------------------------------------------------------------------------
# Engine: check_input / check_output
# ---------------------------------------------------------------------------


def _direction(
    *,
    pii_enabled: bool = False,
    pii_mode: str = "redact",
    pii_types: list[str] | None = None,
    topic_enabled: bool = False,
    allowed: list[str] | None = None,
    banned: list[str] | None = None,
    topic_action: str = "block",
    content_enabled: bool = False,
    content_terms: list[str] | None = None,
    content_action: str = "block",
) -> GuardrailDirection:
    """Compact builder so each engine test doesn't repeat 15 lines of config."""
    return GuardrailDirection(
        pii=PiiGuardrailConfig(
            enabled=pii_enabled,
            mode=pii_mode,  # type: ignore[arg-type]
            types=pii_types or [],  # type: ignore[arg-type]
        ),
        topic=TopicGuardrailConfig(
            enabled=topic_enabled,
            allowed_topics=allowed or [],
            banned_topics=banned or [],
            on_violation=topic_action,  # type: ignore[arg-type]
        ),
        content=ContentGuardrailConfig(
            enabled=content_enabled,
            banned_terms=content_terms or [],
            on_violation=content_action,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.unit
class TestEngine:
    def test_permissive_allows_everything(self) -> None:
        v = check_input("anything at all", _direction())
        assert v.action == "allow"

    def test_pii_redact_returns_modified_text(self) -> None:
        cfg = _direction(pii_enabled=True, pii_mode="redact")
        v = check_input("Email jane@example.com", cfg)
        assert v.action == "redact"
        assert v.redacted_text is not None
        assert "jane@example.com" not in v.redacted_text
        assert "pii" in v.triggered_by

    def test_pii_block_short_circuits(self) -> None:
        cfg = _direction(pii_enabled=True, pii_mode="block")
        v = check_input("Email jane@example.com", cfg)
        assert v.action == "block"
        assert v.triggered_by == ("pii",)

    def test_pii_warn_continues_with_no_modification(self) -> None:
        cfg = _direction(pii_enabled=True, pii_mode="warn")
        v = check_input("Email jane@example.com", cfg)
        assert v.action == "warn"
        assert v.redacted_text is None

    def test_topic_block_on_off_topic(self) -> None:
        cfg = _direction(topic_enabled=True, allowed=["sandisk"], topic_action="block")
        v = check_input("Tell me about Apple", cfg)
        assert v.action == "block"
        assert "topic" in v.triggered_by

    def test_content_block_on_banned_term(self) -> None:
        cfg = _direction(
            content_enabled=True,
            content_terms=["internal only"],
            content_action="block",
        )
        v = check_input("This is INTERNAL ONLY", cfg)
        assert v.action == "block"
        assert "content" in v.triggered_by

    def test_redact_then_topic_works_on_redacted_text(self) -> None:
        """PII redact must run BEFORE topic — otherwise the topic check
        would see the unredacted text. We rely on the engine running PII
        first so a `[REDACTED:email]` marker is what topic actually
        inspects."""
        cfg = _direction(
            pii_enabled=True,
            pii_mode="redact",
            topic_enabled=True,
            allowed=["sandisk"],
            topic_action="block",
        )
        # Off-topic AND has PII — engine returns block (because topic is
        # the offending guardrail, after PII redact); a pure "redact"
        # outcome would only happen on an on-topic text.
        v = check_input("Reach me at jane@example.com about Apple", cfg)
        assert v.action == "block"

    def test_multiple_warnings_accumulate(self) -> None:
        """Two non-blocking violations should produce a single warn
        verdict that names both modules in ``triggered_by``."""
        cfg = _direction(
            pii_enabled=True,
            pii_mode="warn",
            content_enabled=True,
            content_terms=["internal"],
            content_action="warn",
        )
        v = check_input("Internal note from jane@example.com", cfg)
        assert v.action == "warn"
        assert "pii" in v.triggered_by
        assert "content" in v.triggered_by

    def test_check_output_uses_same_logic_as_input(self) -> None:
        cfg = _direction(pii_enabled=True, pii_mode="redact")
        v = check_output("Sent jane@example.com the report", cfg)
        assert v.action == "redact"
        assert v.redacted_text is not None
        assert "jane@example.com" not in v.redacted_text


# ---------------------------------------------------------------------------
# Executor integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_blocks_input_with_safety_blocked_status() -> None:
    """A blocking input guardrail surfaces as RunResponse.status =
    'safety_blocked' — same plumbing the rest of the pipeline already
    uses for ContentFilterError."""
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "demo"
        scaffold_agent(dst, name="demo", template="default")
        bundle = load_agent(dst)

        # Build executor with guardrails that block any input
        # containing the word "secret".
        guardrails = GuardrailsConfig(
            input=_direction(
                content_enabled=True,
                content_terms=["secret"],
                content_action="block",
            )
        )

        # Build a fresh executor with guardrails wired.
        provider = MockProvider(response='{"message": "ok"}')
        storage = InMemoryStorage()
        await storage.init()
        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=storage,
            tracer=NullTracer(),
            guardrails=guardrails,
        )

        response = await executor.execute(
            bundle, RunRequest(agent="demo", input={"text": "tell me a secret"})
        )
        assert response.status == "safety_blocked", (
            f"expected safety_blocked, got {response.status}: {response.error}"
        )


@pytest.mark.unit
async def test_executor_blocks_output_with_safety_blocked_status() -> None:
    """A blocking output guardrail also surfaces as safety_blocked.
    The model's leaky text is caught BEFORE schema validation, so
    the failure category is content-filter, not schema-error."""
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "demo"
        scaffold_agent(dst, name="demo", template="default")
        bundle = load_agent(dst)

        # Output guardrail blocks if "leaked-token" appears anywhere
        # in the model's response.
        guardrails = GuardrailsConfig(
            output=_direction(
                content_enabled=True,
                content_terms=["leaked-token"],
                content_action="block",
            )
        )

        # Mock returns a valid JSON shape that contains the banned term.
        provider = MockProvider(response='{"message": "here is the leaked-token value"}')
        storage = InMemoryStorage()
        await storage.init()
        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=storage,
            tracer=NullTracer(),
            guardrails=guardrails,
        )

        response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hello"}))
        assert response.status == "safety_blocked"


@pytest.mark.unit
async def test_executor_permissive_guardrails_is_zero_overhead() -> None:
    """When guardrails are fully disabled (the default), the executor
    must fast-path skip the modules — no behavior change vs pre-J-0
    for projects that haven't opted in."""
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "demo"
        scaffold_agent(dst, name="demo", template="default")
        bundle = load_agent(dst)

        # Default GuardrailsConfig — every sub-block enabled=False.
        provider = MockProvider(response='{"message": "ok"}')
        storage = InMemoryStorage()
        await storage.init()
        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=storage,
            tracer=NullTracer(),
            guardrails=GuardrailsConfig(),
        )

        response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
        assert response.status == "success"


@pytest.mark.unit
def test_guardrails_config_permissive_default() -> None:
    """A freshly-constructed GuardrailsConfig has is_permissive=True so
    the executor's fast-path skip is engaged for projects that didn't
    opt in. Belt-and-braces: also assert each direction independently."""
    cfg = GuardrailsConfig()
    assert cfg.is_permissive()
    assert cfg.input.is_permissive()
    assert cfg.output.is_permissive()


@pytest.mark.unit
def test_guardrails_verdict_default_action_is_allow() -> None:
    v = GuardrailVerdict(action="allow")
    assert v.action == "allow"
    assert v.triggered_by == ()
    assert v.matched_terms == ()
    assert v.redacted_text is None
