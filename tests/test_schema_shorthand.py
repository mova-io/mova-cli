"""Tests for the agent.yaml schema-shorthand compiler.

The compiler turns the inline form used in ``input:`` / ``output:``
blocks into JSON Schema dicts; the rest of the codebase consumes
those unchanged. This module covers:

* every scalar / container shape the shorthand supports;
* the ``?`` optional suffix and ``|`` enum sugar;
* every error path (unknown type, malformed array, bad keys) — error
  messages must include the field's key path so operators spot the
  bad field without paging through stack traces;
* a roundtrip step: every compiled schema is checked against
  ``Draft202012Validator.check_schema`` so we catch any shape we'd
  ship that the actual validator rejects.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from movate.core.schema_shorthand import SchemaShorthandError, compile_shorthand

# ---------------------------------------------------------------------------
# Helper: every compiled schema must itself be a valid JSON Schema.
# ---------------------------------------------------------------------------


def _assert_valid_schema(schema: dict) -> None:
    """Fail loudly if a compiler output isn't a valid JSON Schema —
    catches typos in our compiler that would otherwise silently slip
    through to runtime."""
    Draft202012Validator.check_schema(schema)


# ---------------------------------------------------------------------------
# Happy path — scalars + objects
# ---------------------------------------------------------------------------


def test_compile_minimal_object() -> None:
    schema = compile_shorthand({"message": "string"})
    _assert_valid_schema(schema)
    assert schema == {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }


def test_compile_all_scalar_types() -> None:
    schema = compile_shorthand(
        {
            "name": "string",
            "age": "integer",
            "score": "number",
            "is_admin": "boolean",
        }
    )
    _assert_valid_schema(schema)
    assert schema["properties"]["name"] == {"type": "string"}
    assert schema["properties"]["age"] == {"type": "integer"}
    assert schema["properties"]["score"] == {"type": "number"}
    assert schema["properties"]["is_admin"] == {"type": "boolean"}
    assert sorted(schema["required"]) == ["age", "is_admin", "name", "score"]


def test_compile_strict_by_default() -> None:
    """Inline shorthand objects are strict — additionalProperties=False.
    Operators wanting permissive contracts use a path-string + full
    JSON Schema."""
    schema = compile_shorthand({"x": "string"})
    assert schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Optional fields
# ---------------------------------------------------------------------------


def test_optional_suffix_removes_from_required() -> None:
    schema = compile_shorthand({"name": "string", "nickname": "string?"})
    assert schema["required"] == ["name"]
    # The optional field still appears in properties — JSON Schema
    # carries optionality at the parent level, not on the field itself.
    assert schema["properties"]["nickname"] == {"type": "string"}


def test_optional_on_every_scalar_type() -> None:
    schema = compile_shorthand({"s": "string?", "n": "number?", "i": "integer?", "b": "boolean?"})
    assert schema["required"] == []
    assert len(schema["properties"]) == 4


def test_all_required_empty_required_list() -> None:
    schema = compile_shorthand({"a": "string?", "b": "integer?"})
    assert schema["required"] == []


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_pipe_separated_string_enum() -> None:
    schema = compile_shorthand({"status": "pending|done|failed"})
    _assert_valid_schema(schema)
    assert schema["properties"]["status"] == {
        "type": "string",
        "enum": ["pending", "done", "failed"],
    }
    assert schema["required"] == ["status"]


def test_optional_enum() -> None:
    schema = compile_shorthand({"status": "pending|done?"})
    assert schema["properties"]["status"]["enum"] == ["pending", "done"]
    assert schema["required"] == []


def test_enum_trims_whitespace() -> None:
    schema = compile_shorthand({"x": "a | b | c"})
    assert schema["properties"]["x"]["enum"] == ["a", "b", "c"]


def test_two_member_enum() -> None:
    """Smallest useful enum — `yes|no`."""
    schema = compile_shorthand({"answer": "yes|no"})
    assert schema["properties"]["answer"]["enum"] == ["yes", "no"]


# ---------------------------------------------------------------------------
# Arrays
# ---------------------------------------------------------------------------


def test_array_of_scalar() -> None:
    schema = compile_shorthand({"tags": ["string"]})
    _assert_valid_schema(schema)
    assert schema["properties"]["tags"] == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_array_of_enum() -> None:
    schema = compile_shorthand({"levels": ["low|med|high"]})
    assert schema["properties"]["levels"]["items"]["enum"] == ["low", "med", "high"]


def test_array_of_nested_object() -> None:
    schema = compile_shorthand({"users": [{"name": "string", "age": "integer"}]})
    _assert_valid_schema(schema)
    user_schema = schema["properties"]["users"]["items"]
    assert user_schema["type"] == "object"
    assert user_schema["properties"]["name"] == {"type": "string"}
    assert sorted(user_schema["required"]) == ["age", "name"]


# ---------------------------------------------------------------------------
# Nested objects
# ---------------------------------------------------------------------------


def test_nested_object() -> None:
    schema = compile_shorthand({"user": {"name": "string", "age": "integer"}})
    _assert_valid_schema(schema)
    user = schema["properties"]["user"]
    assert user["type"] == "object"
    assert user["properties"]["name"] == {"type": "string"}
    assert sorted(user["required"]) == ["age", "name"]
    assert user["additionalProperties"] is False


def test_three_level_deep_nesting() -> None:
    """Pathological-ish nesting — the recursion should just work."""
    schema = compile_shorthand({"a": {"b": {"c": {"d": "string"}}}})
    _assert_valid_schema(schema)
    inner = schema["properties"]["a"]["properties"]["b"]["properties"]["c"]
    assert inner["properties"]["d"] == {"type": "string"}


# ---------------------------------------------------------------------------
# Round-trip: compiled schema validates expected runtime payloads
# ---------------------------------------------------------------------------


def test_compiled_schema_accepts_conforming_payload() -> None:
    """End-to-end: compile, then validate a real payload against the
    result. This is what runtime does at request-validation time."""
    schema = compile_shorthand({"message": "string", "priority": "integer?"})
    validator = Draft202012Validator(schema)
    # Required-only — passes.
    validator.validate({"message": "hi"})
    # With optional — passes.
    validator.validate({"message": "hi", "priority": 5})


def test_compiled_schema_rejects_missing_required() -> None:
    schema = compile_shorthand({"message": "string"})
    validator = Draft202012Validator(schema)
    with pytest.raises(Exception):
        validator.validate({})


def test_compiled_schema_rejects_extra_properties() -> None:
    """Strict-by-default: unknown keys at runtime fail validation."""
    schema = compile_shorthand({"message": "string"})
    validator = Draft202012Validator(schema)
    with pytest.raises(Exception):
        validator.validate({"message": "hi", "smuggled_field": "boom"})


def test_compiled_enum_rejects_unknown_member() -> None:
    schema = compile_shorthand({"status": "pending|done|failed"})
    validator = Draft202012Validator(schema)
    with pytest.raises(Exception):
        validator.validate({"status": "blocked"})


# ---------------------------------------------------------------------------
# Error path — every error message must include the key path
# ---------------------------------------------------------------------------


def test_non_dict_root_errors() -> None:
    with pytest.raises(SchemaShorthandError, match="expected an object"):
        compile_shorthand("string")  # type: ignore[arg-type]


def test_unknown_scalar_type_includes_field_path() -> None:
    with pytest.raises(SchemaShorthandError, match=r"input\.message"):
        compile_shorthand({"message": "strng"}, root_label="input")


def test_unknown_scalar_lists_valid_types() -> None:
    with pytest.raises(SchemaShorthandError, match=r"\['boolean'.*'string'\]"):
        compile_shorthand({"x": "garbage"})


def test_empty_array_rejected() -> None:
    with pytest.raises(SchemaShorthandError, match="exactly one element"):
        compile_shorthand({"tags": []})


def test_multi_element_array_rejected_with_escape_hatch() -> None:
    """Multi-element shorthand arrays are ambiguous (tuple? union?) —
    error tells the operator how to express either case."""
    with pytest.raises(SchemaShorthandError, match="path-string"):
        compile_shorthand({"x": ["string", "integer"]})


def test_array_path_includes_brackets() -> None:
    """A nested error inside an array reports the field path including
    the array marker so operators can find it."""
    with pytest.raises(SchemaShorthandError, match=r"items\[\]"):
        compile_shorthand({"items": ["bogus"]})


def test_unsupported_value_type_errors() -> None:
    """Numbers, bools, None as field values are nonsensical at parse
    time — the shorthand is a *type description*, not a *default*."""
    with pytest.raises(SchemaShorthandError, match="unsupported shorthand value"):
        compile_shorthand({"x": 42})  # type: ignore[dict-item]


def test_non_string_field_name_errors() -> None:
    with pytest.raises(SchemaShorthandError, match="field names must be strings"):
        compile_shorthand({1: "string"})  # type: ignore[dict-item]


def test_empty_type_string_errors() -> None:
    with pytest.raises(SchemaShorthandError, match="empty type string"):
        compile_shorthand({"x": ""})


def test_lone_question_mark_errors() -> None:
    """'?' alone isn't a type — must be a suffix on something."""
    with pytest.raises(SchemaShorthandError, match=r"'\?' alone is not a type"):
        compile_shorthand({"x": "?"})


def test_double_pipe_enum_errors() -> None:
    """Stray '|' produces an empty member — operator gets a hint."""
    with pytest.raises(SchemaShorthandError, match="empty enum member"):
        compile_shorthand({"status": "a||b"})


def test_trailing_pipe_enum_errors() -> None:
    with pytest.raises(SchemaShorthandError, match="empty enum member"):
        compile_shorthand({"status": "a|b|"})
