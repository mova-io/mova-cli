"""Tool registry + @tool decorator + JSON Schema generation.

Coverage matrix:

* Registry: register, get, list, clear; duplicate-name rejection;
  idempotent re-registration of the same callable.
* Decorator: bare `@tool` and `@tool(...)` forms; explicit name /
  description / side_effects override the defaults.
* Schema generation: each supported type (primitives, list/dict,
  Optional, Literal, Any) produces the documented JSON Schema shape;
  unannotated params + *args + **kwargs are rejected at register time.

The OpenAI / LiteLLM ``tools=[...]`` shape is asserted on the final
tool object so a refactor that breaks compatibility surfaces here,
not at first model call.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest

from movate.tools import Tool, ToolError, get_tool, list_tools, tool
from movate.tools._schema import SchemaError, build_tool_schema
from movate.tools.registry import _clear_registry_for_tests


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test starts with a clean registry. The registry is a
    module-level singleton, so tests would otherwise contend for
    tool names."""
    _clear_registry_for_tests()
    yield
    _clear_registry_for_tests()


# ---------------------------------------------------------------------------
# Decorator — both forms
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bare_decorator_registers_with_function_name() -> None:
    @tool
    def search(query: str) -> list[str]:
        """Look up matching documents."""
        return [query]

    t = get_tool("search")
    assert t.name == "search"
    assert t.callable is search
    assert t.description == "Look up matching documents."
    assert t.side_effects is False  # default


@pytest.mark.unit
def test_decorator_with_args_overrides_name_and_side_effects() -> None:
    @tool(name="post_comment", side_effects=True, description="Posts a comment.")
    def _impl(thread_id: str, body: str) -> str:
        return "comment-id"

    t = get_tool("post_comment")
    assert t.name == "post_comment"
    assert t.side_effects is True
    assert t.description == "Posts a comment."
    # The decorated function is unchanged — still callable as Python.
    assert _impl("t1", "hi") == "comment-id"


@pytest.mark.unit
def test_decorator_preserves_the_original_callable() -> None:
    @tool
    def double(x: int) -> int:
        """Return ``x * 2``."""
        return x * 2

    assert double(3) == 6  # still callable as Python
    assert get_tool("double").callable(7) == 14  # registry holds the same fn


# ---------------------------------------------------------------------------
# Registry — get / list / duplicate handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_tool_raises_with_known_tools_list() -> None:
    @tool
    def a(x: str) -> str:
        """A."""
        return x

    @tool
    def b(x: str) -> str:
        """B."""
        return x

    with pytest.raises(ToolError, match="a, b"):
        get_tool("c")


@pytest.mark.unit
def test_list_tools_returns_sorted_snapshot() -> None:
    @tool
    def zeta(x: str) -> str:
        """Z."""
        return x

    @tool
    def alpha(x: str) -> str:
        """A."""
        return x

    @tool
    def mu(x: str) -> str:
        """M."""
        return x

    names = [t.name for t in list_tools()]
    assert names == ["alpha", "mu", "zeta"]


@pytest.mark.unit
def test_duplicate_name_with_different_callable_raises() -> None:
    @tool(name="same")
    def first(x: str) -> str:
        """First."""
        return x

    with pytest.raises(ToolError, match="already registered"):

        @tool(name="same")
        def second(x: str) -> str:
            """Second."""
            return x


@pytest.mark.unit
def test_reregistration_of_same_callable_is_idempotent() -> None:
    """Importing a tool module twice (e.g. in pytest re-import order)
    shouldn't crash. The registry detects same-callable re-registration
    and silently skips."""

    def fn(x: str) -> str:
        """fn."""
        return x

    from movate.tools.registry import Tool as ToolCls  # noqa: PLC0415 — narrow scope
    from movate.tools.registry import register_tool  # noqa: PLC0415 — narrow scope

    t = ToolCls(
        name="fn",
        callable=fn,
        schema=build_tool_schema(fn),
        side_effects=False,
        description="fn.",
    )
    register_tool(t)
    register_tool(t)  # second register — no raise
    assert get_tool("fn") is t


# ---------------------------------------------------------------------------
# OpenAI / LiteLLM tool-shape conversion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_openai_tool_produces_function_shape() -> None:
    @tool
    def search(query: str, k: int = 5) -> list[str]:
        """Search the KB."""
        return [query]

    payload = get_tool("search").to_openai_tool()
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "search"
    assert payload["function"]["description"] == "Search the KB."
    params = payload["function"]["parameters"]
    assert params["type"] == "object"
    assert params["additionalProperties"] is False
    # `query` is required (no default); `k` is not (has default).
    assert params["required"] == ["query"]
    assert set(params["properties"]) == {"query", "k"}
    assert params["properties"]["query"]["type"] == "string"
    assert params["properties"]["k"]["type"] == "integer"


# ---------------------------------------------------------------------------
# Schema generation — type coverage
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_schema_handles_primitive_types() -> None:
    def fn(a: str, b: int, c: float, d: bool) -> None:
        """primitive types."""

    schema = build_tool_schema(fn)
    props = schema["parameters"]["properties"]
    assert props["a"] == {"type": "string"}
    assert props["b"] == {"type": "integer"}
    assert props["c"] == {"type": "number"}
    assert props["d"] == {"type": "boolean"}


@pytest.mark.unit
def test_schema_handles_list_and_dict() -> None:
    def fn(xs: list[str], counts: dict[str, int]) -> None:
        """lists + dicts."""

    schema = build_tool_schema(fn)
    props = schema["parameters"]["properties"]
    assert props["xs"] == {"type": "array", "items": {"type": "string"}}
    assert props["counts"] == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }


@pytest.mark.unit
def test_schema_handles_optional_and_union_with_none() -> None:
    def fn(x: str | None = None, y: int | None = None) -> None:
        """optional fields."""

    schema = build_tool_schema(fn)
    props = schema["parameters"]["properties"]
    # Both should be a union with null — order may vary by Python version
    # but the set is stable.
    for name in ("x", "y"):
        type_field = props[name]["type"]
        assert isinstance(type_field, list)
        assert "null" in type_field
    # Neither is required (both have defaults).
    assert schema["parameters"]["required"] == []


@pytest.mark.unit
def test_schema_handles_literal_as_enum() -> None:
    def fn(decision: Literal["approve", "reject", "review"]) -> None:
        """enum field."""

    schema = build_tool_schema(fn)
    assert schema["parameters"]["properties"]["decision"] == {
        "enum": ["approve", "reject", "review"]
    }


@pytest.mark.unit
def test_schema_handles_any_as_empty_schema() -> None:
    def fn(blob: Any) -> None:
        """passthrough."""

    schema = build_tool_schema(fn)
    # JSON Schema's "accept anything" is the empty schema {}.
    assert schema["parameters"]["properties"]["blob"] == {}


@pytest.mark.unit
def test_schema_rejects_unannotated_param() -> None:
    def fn(query) -> None:  # type: ignore[no-untyped-def]
        """missing annotation."""

    with pytest.raises(SchemaError, match="no type annotation"):
        build_tool_schema(fn)


@pytest.mark.unit
def test_schema_rejects_varargs() -> None:
    def fn(*args: str) -> None:
        """varargs."""

    with pytest.raises(SchemaError, match=r"\*args / \*\*kwargs"):
        build_tool_schema(fn)


@pytest.mark.unit
def test_schema_rejects_kwargs() -> None:
    def fn(**kwargs: int) -> None:
        """kwargs."""

    with pytest.raises(SchemaError, match=r"\*args / \*\*kwargs"):
        build_tool_schema(fn)


@pytest.mark.unit
def test_decorator_wraps_schemaerror_as_toolerror() -> None:
    """Operators see ToolError uniformly. SchemaError is an internal
    detail."""
    with pytest.raises(ToolError, match="no type annotation"):

        @tool
        def bad(query):  # type: ignore[no-untyped-def]
            """missing annotation."""


# ---------------------------------------------------------------------------
# side_effects flag — declared, retrievable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_side_effects_flag_stored_on_tool() -> None:
    @tool(side_effects=True)
    def write(x: str) -> str:
        """mutating."""
        return x

    @tool(side_effects=False)
    def read(x: str) -> str:
        """idempotent."""
        return x

    assert get_tool("write").side_effects is True
    assert get_tool("read").side_effects is False


# ---------------------------------------------------------------------------
# Dataclass invariants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_dataclass_is_frozen() -> None:
    """Frozen dataclass — mutating a registered tool's fields should
    fail rather than silently corrupting the registry."""

    @tool
    def fn(x: str) -> str:
        """fn."""
        return x

    t = get_tool("fn")
    assert isinstance(t, Tool)
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        t.name = "renamed"  # type: ignore[misc]
