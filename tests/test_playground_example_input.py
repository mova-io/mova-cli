"""Copy-paste-ready example input for the Chainlit playground (#768).

When an agent is picked, the playground shows a concrete example built from its
input schema (not just the field names) — agents reject generic input, which was
the #1 "it errored" confusion. ``example_input_for_schema`` is the pure builder.
"""

from __future__ import annotations

import pytest

# The playground app imports chainlit at module scope; skip when absent.
pytest.importorskip("chainlit")

from movate.playground.app import example_input_for_schema


@pytest.mark.unit
def test_empty_schema_yields_empty_example() -> None:
    assert example_input_for_schema({}) == {}
    assert example_input_for_schema({"type": "object"}) == {}


@pytest.mark.unit
def test_name_aware_string_examples() -> None:
    """Known field names get a natural sample, not a bare placeholder."""
    ex = example_input_for_schema(
        {"properties": {"request": {"type": "string"}}, "required": ["request"]}
    )
    assert "request" in ex
    assert isinstance(ex["request"], str) and "refund" in ex["request"].lower()


@pytest.mark.unit
def test_schema_provided_example_wins() -> None:
    ex = example_input_for_schema(
        {"properties": {"text": {"type": "string", "example": "hello world"}}}
    )
    assert ex["text"] == "hello world"


@pytest.mark.unit
def test_examples_list_and_default_fallbacks() -> None:
    ex = example_input_for_schema(
        {
            "properties": {
                "a": {"type": "string", "examples": ["from-list"]},
                "b": {"type": "string", "default": "from-default"},
            }
        }
    )
    assert ex["a"] == "from-list"
    assert ex["b"] == "from-default"


@pytest.mark.unit
def test_type_placeholders() -> None:
    ex = example_input_for_schema(
        {
            "properties": {
                "n": {"type": "integer"},
                "flag": {"type": "boolean"},
                "items": {"type": "array"},
                "obj": {"type": "object"},
            }
        }
    )
    assert ex == {"n": 1, "flag": True, "items": [], "obj": {}}


@pytest.mark.unit
def test_required_fields_come_first_and_cap_applies() -> None:
    props = {f"f{i}": {"type": "string"} for i in range(10)}
    schema = {"properties": props, "required": ["f7"]}
    ex = example_input_for_schema(schema)
    keys = list(ex)
    assert keys[0] == "f7"  # required first
    assert len(keys) <= 6  # _SCHEMA_HINT_MAX_FIELDS cap
