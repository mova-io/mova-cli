"""KB + skills + contexts + output-schema awareness in ``mdk eval-gen``.

Before this PR the generator's prompt only carried the agent's
**input** schema. Generated cases therefore tended to ignore:

* The agent's **skills** — routing-flavored cases never showed up
  unless the operator hand-crafted a sample input.
* The agent's **contexts** (rubrics, tone guides, policies) — cases
  rarely probed the constraints the agent is supposed to honor.
* The **output schema** — generated ``expected`` outputs would
  occasionally violate the schema, causing the eval engine to
  reject cases at validation time.
* The **10-category scorecard dimensions** — there was no way to ask
  "give me cases that stress safety" or "cases that exercise
  tool_usage routing."

This file tests the new prompt-injection helpers and the
``--target-dim`` flag end-to-end through the wizardless CLI path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from movate.cli.eval_gen_cmd import (
    _DIMENSION_GEN_HINTS,
    _dim_generation_hint,
    _gen_user_message,
)


def _bundle(
    *,
    skills: list[SimpleNamespace] | None = None,
    contexts: list[tuple[str, str]] | None = None,
    output_schema: dict | None = None,
) -> object:
    """Duck-typed bundle exposing exactly what _gen_user_message reads."""
    return SimpleNamespace(
        spec=SimpleNamespace(name="triage", description="Triages tickets"),
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        output_schema=output_schema,
        skills=skills or [],
        contexts=contexts or [],
    )


def _skill(name: str, description: str) -> SimpleNamespace:
    return SimpleNamespace(spec=SimpleNamespace(name=name, description=description))


# ---------------------------------------------------------------------------
# Output schema injection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutputSchemaInPrompt:
    def test_output_schema_appears_in_prompt(self) -> None:
        b = _bundle(
            output_schema={
                "type": "object",
                "properties": {
                    "priority": {"enum": ["p0", "p1", "p2"]},
                    "category": {"type": "string"},
                },
            }
        )
        msg = _gen_user_message(b, index=0, sample_input=None)
        assert "Output schema" in msg
        assert "p0" in msg
        assert "category" in msg

    def test_no_output_schema_in_bundle_omits_section(self) -> None:
        b = _bundle(output_schema=None)
        msg = _gen_user_message(b, index=0, sample_input=None)
        assert "Output schema" not in msg

    def test_include_output_schema_false_opts_out(self) -> None:
        b = _bundle(output_schema={"type": "object"})
        msg = _gen_user_message(b, index=0, sample_input=None, include_output_schema=False)
        assert "Output schema" not in msg


# ---------------------------------------------------------------------------
# Skills injection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkillsInPrompt:
    def test_skills_appear_with_name_and_description(self) -> None:
        b = _bundle(
            skills=[
                _skill("kb-lookup", "Search internal knowledge base by query"),
                _skill("ticket-create", "Create a Jira ticket"),
            ]
        )
        msg = _gen_user_message(b, index=0, sample_input=None)
        assert "Declared skills" in msg
        assert "kb-lookup" in msg
        assert "Search internal knowledge base" in msg
        assert "ticket-create" in msg
        assert "Create a Jira ticket" in msg

    def test_no_description_falls_back_to_placeholder(self) -> None:
        b = _bundle(skills=[_skill("bare-skill", "")])
        msg = _gen_user_message(b, index=0, sample_input=None)
        assert "bare-skill" in msg
        assert "(no description)" in msg

    def test_empty_skills_list_omits_section(self) -> None:
        b = _bundle(skills=[])
        msg = _gen_user_message(b, index=0, sample_input=None)
        assert "Declared skills" not in msg

    def test_include_skills_false_opts_out(self) -> None:
        b = _bundle(skills=[_skill("kb-lookup", "search")])
        msg = _gen_user_message(b, index=0, sample_input=None, include_skills=False)
        assert "Declared skills" not in msg
        assert "kb-lookup" not in msg


# ---------------------------------------------------------------------------
# Contexts injection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContextsInPrompt:
    def test_contexts_appear_with_name_and_body(self) -> None:
        b = _bundle(
            contexts=[
                ("tone-guide", "Be concise and professional. Never use emojis."),
                ("escalation-policy", "Always escalate to L2 for billing issues."),
            ]
        )
        msg = _gen_user_message(b, index=0, sample_input=None)
        assert "Declared contexts" in msg
        assert "tone-guide" in msg
        assert "Be concise and professional" in msg
        assert "escalation-policy" in msg
        assert "billing issues" in msg

    def test_long_context_body_is_truncated_at_600_chars(self) -> None:
        long_body = "X" * 1000
        b = _bundle(contexts=[("verbose-rubric", long_body)])
        msg = _gen_user_message(b, index=0, sample_input=None)
        # Full body NOT included.
        assert "X" * 1000 not in msg
        # Truncation marker present.
        assert "truncated" in msg

    def test_empty_contexts_list_omits_section(self) -> None:
        b = _bundle(contexts=[])
        msg = _gen_user_message(b, index=0, sample_input=None)
        assert "Declared contexts" not in msg

    def test_include_contexts_false_opts_out(self) -> None:
        b = _bundle(contexts=[("rubric", "policy text")])
        msg = _gen_user_message(b, index=0, sample_input=None, include_contexts=False)
        assert "Declared contexts" not in msg
        assert "policy text" not in msg


# ---------------------------------------------------------------------------
# target_dims (10-category dimension biasing)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTargetDimsInPrompt:
    def test_single_target_dim_injects_dimension_hint(self) -> None:
        b = _bundle()
        msg = _gen_user_message(b, index=0, sample_input=None, target_dims=["safety"])
        assert "Target evaluation dimension" in msg
        assert "safety" in msg
        # The hint string for safety mentions guardrails / unsafe output.
        assert "unsafe" in msg.lower() or "guardrails" in msg.lower()

    def test_multiple_target_dims_inject_all_hints(self) -> None:
        b = _bundle()
        msg = _gen_user_message(
            b,
            index=0,
            sample_input=None,
            target_dims=["faithfulness", "completeness"],
        )
        assert "faithfulness" in msg
        assert "completeness" in msg
        # Both hints landed.
        assert _DIMENSION_GEN_HINTS["faithfulness"] in msg
        assert _DIMENSION_GEN_HINTS["completeness"] in msg

    def test_unknown_dim_silently_dropped(self) -> None:
        """The generator must not crash on a typo — the eval engine
        will surface unknown-dim errors at scoring time. Falling back
        cleanly keeps eval-gen robust."""
        b = _bundle()
        msg = _gen_user_message(b, index=0, sample_input=None, target_dims=["totally-not-a-dim"])
        # The header line still appears (operator sees what we did),
        # but no per-dim hint is added.
        assert "totally-not-a-dim" in msg

    def test_no_target_dims_omits_section(self) -> None:
        b = _bundle()
        msg = _gen_user_message(b, index=0, sample_input=None, target_dims=None)
        assert "Target evaluation dimension" not in msg

    def test_dim_generation_hint_returns_empty_for_unknown_dim(self) -> None:
        assert _dim_generation_hint(["nonsense"]) == ""

    def test_dim_generation_hint_concats_known_dims(self) -> None:
        h = _dim_generation_hint(["safety", "tool_usage"])
        assert _DIMENSION_GEN_HINTS["safety"] in h
        assert _DIMENSION_GEN_HINTS["tool_usage"] in h


# ---------------------------------------------------------------------------
# Backward-compat: callers that pass no new flags get pre-PR behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_legacy_caller_gets_same_prompt_shape_without_new_data() -> None:
    """A bundle with no skills/contexts/output_schema produces a prompt
    that doesn't include any of the new sections. This pins the
    backwards-compat surface."""
    b = _bundle()
    msg = _gen_user_message(b, index=0, sample_input=None)
    assert "Declared skills" not in msg
    assert "Declared contexts" not in msg
    assert "Output schema" not in msg
    assert "Target evaluation dimension" not in msg
    # The pre-existing pieces are still there.
    assert "Agent name: triage" in msg
    assert "Input schema" in msg
