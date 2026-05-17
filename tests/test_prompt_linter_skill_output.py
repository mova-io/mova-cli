"""Unit tests for the SKILL_OUTPUT_REF_MISMATCH lint rule.

Rule: when a prompt references ``{{ <skill_name>_output.X }}`` and field
``X`` is not in the skill's declared output schema, emit a warning.

Also verifies that:
* no false positives on correct field refs
* no issues when the agent has no skills
* no issues when the skill has an open output schema (no ``properties``)
* hyphenated skill names normalize to underscored var names
  (``web-search`` → ``web_search_output``)
* multiple bad refs on the same skill each get their own issue
* the rule is quiet when the template doesn't reference the skill var at all
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from movate.core.prompt_linter import LintIssue, _skill_output_var_name, lint_prompt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(name: str, output_props: dict[str, Any] | None) -> SimpleNamespace:
    output_schema: dict[str, Any] = {"type": "object"}
    if output_props is not None:
        output_schema["properties"] = output_props
    return SimpleNamespace(
        spec=SimpleNamespace(name=name),
        output_schema=output_schema,
    )


def _make_bundle(
    prompt: str,
    *,
    skills: list[SimpleNamespace] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_template=prompt,
        skills=skills or [],
        input_schema=input_schema or {
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        output_schema=output_schema or {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
        },
    )


def _codes(issues: list[LintIssue]) -> set[str]:
    return {i.code for i in issues}


# ---------------------------------------------------------------------------
# _skill_output_var_name helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkillOutputVarName:
    def test_simple_name(self) -> None:
        assert _skill_output_var_name("summarize") == "summarize_output"

    def test_hyphenated_name(self) -> None:
        assert _skill_output_var_name("web-search") == "web_search_output"

    def test_multi_hyphen(self) -> None:
        assert _skill_output_var_name("kb-lookup-v2") == "kb_lookup_v2_output"

    def test_already_underscored(self) -> None:
        assert _skill_output_var_name("kb_lookup") == "kb_lookup_output"


# ---------------------------------------------------------------------------
# SKILL_OUTPUT_REF_MISMATCH rule
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkillOutputRefMismatch:
    def test_no_issues_when_no_skills(self) -> None:
        bundle = _make_bundle("Say {{ input.text }}")
        assert "SKILL_OUTPUT_REF_MISMATCH" not in _codes(lint_prompt(bundle))

    def test_no_issues_when_template_doesnt_reference_skill_var(self) -> None:
        skill = _make_skill("web-search", {"results": {}, "warning": {}})
        bundle = _make_bundle("Say {{ input.text }}", skills=[skill])
        assert "SKILL_OUTPUT_REF_MISMATCH" not in _codes(lint_prompt(bundle))

    def test_no_issues_on_correct_field_ref(self) -> None:
        skill = _make_skill("web-search", {"results": {}, "warning": {}})
        bundle = _make_bundle(
            "Results: {{ web_search_output.results }}", skills=[skill]
        )
        assert "SKILL_OUTPUT_REF_MISMATCH" not in _codes(lint_prompt(bundle))

    def test_mismatch_fires_for_unknown_field(self) -> None:
        skill = _make_skill("web-search", {"results": {}, "warning": {}})
        bundle = _make_bundle(
            "Data: {{ web_search_output.hits }}", skills=[skill]
        )
        issues = lint_prompt(bundle)
        mismatch = [i for i in issues if i.code == "SKILL_OUTPUT_REF_MISMATCH"]
        assert mismatch, "expected SKILL_OUTPUT_REF_MISMATCH"
        assert mismatch[0].severity == "warning"
        assert "hits" in mismatch[0].message
        assert "web-search" in mismatch[0].message

    def test_hint_mentions_skill_yaml(self) -> None:
        skill = _make_skill("web-search", {"results": {}})
        bundle = _make_bundle("{{ web_search_output.bad_field }}", skills=[skill])
        issues = [i for i in lint_prompt(bundle) if i.code == "SKILL_OUTPUT_REF_MISMATCH"]
        assert issues
        assert "skill.yaml" in issues[0].hint or "web-search" in issues[0].hint

    def test_hyphenated_skill_name_maps_to_underscored_var(self) -> None:
        skill = _make_skill("kb-lookup", {"answer": {}, "citations": {}})
        bundle = _make_bundle(
            "Answer: {{ kb_lookup_output.answer }}", skills=[skill]
        )
        assert "SKILL_OUTPUT_REF_MISMATCH" not in _codes(lint_prompt(bundle))

    def test_hyphenated_skill_bad_field_caught(self) -> None:
        skill = _make_skill("kb-lookup", {"answer": {}})
        bundle = _make_bundle(
            "Score: {{ kb_lookup_output.score }}", skills=[skill]
        )
        issues = [i for i in lint_prompt(bundle) if i.code == "SKILL_OUTPUT_REF_MISMATCH"]
        assert issues
        assert "score" in issues[0].message

    def test_multiple_bad_refs_each_reported(self) -> None:
        skill = _make_skill("web-search", {"results": {}})
        bundle = _make_bundle(
            "{{ web_search_output.hits }} {{ web_search_output.count }}", skills=[skill]
        )
        issues = [i for i in lint_prompt(bundle) if i.code == "SKILL_OUTPUT_REF_MISMATCH"]
        bad_attrs = {i.message.split(".")[1].split(" ")[0] for i in issues}
        assert "hits" in bad_attrs
        assert "count" in bad_attrs

    def test_duplicate_ref_reported_once(self) -> None:
        skill = _make_skill("web-search", {"results": {}})
        bundle = _make_bundle(
            "{{ web_search_output.hits }} {{ web_search_output.hits }}", skills=[skill]
        )
        issues = [i for i in lint_prompt(bundle) if i.code == "SKILL_OUTPUT_REF_MISMATCH"]
        assert len(issues) == 1

    def test_open_schema_no_properties_skipped(self) -> None:
        skill = _make_skill("my-skill", None)  # no properties → open schema
        bundle = _make_bundle(
            "{{ my_skill_output.anything }}", skills=[skill]
        )
        assert "SKILL_OUTPUT_REF_MISMATCH" not in _codes(lint_prompt(bundle))

    def test_multiple_skills_only_mismatch_flagged(self) -> None:
        skill_a = _make_skill("searcher", {"result": {}})
        skill_b = _make_skill("formatter", {"html": {}})
        bundle = _make_bundle(
            "{{ searcher_output.result }} {{ formatter_output.bad_field }}",
            skills=[skill_a, skill_b],
        )
        issues = [i for i in lint_prompt(bundle) if i.code == "SKILL_OUTPUT_REF_MISMATCH"]
        assert len(issues) == 1
        assert "formatter" in issues[0].message

    def test_unrelated_var_not_flagged(self) -> None:
        skill = _make_skill("web-search", {"results": {}})
        bundle = _make_bundle(
            "{{ loop.index }}: {{ item.title }}", skills=[skill]
        )
        assert "SKILL_OUTPUT_REF_MISMATCH" not in _codes(lint_prompt(bundle))


# ---------------------------------------------------------------------------
# Integration: load a real rag-qa bundle and verify no false positives
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rag_qa_scaffold_clean(tmp_path: Path) -> None:
    """The rag-qa template's prompt does not use <skill>_output vars at
    all — so the rule should not fire, even with web-search skill present."""

    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.testing import scaffold_agent  # noqa: PLC0415

    agent_dir = scaffold_agent(tmp_path / "rag-qa", name="rag-qa", template="rag-qa")
    bundle = load_agent(agent_dir)
    issues = [i for i in lint_prompt(bundle) if i.code == "SKILL_OUTPUT_REF_MISMATCH"]
    assert not issues, f"unexpected SKILL_OUTPUT_REF_MISMATCH on rag-qa: {issues}"
