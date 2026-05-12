"""Synthesize a JSON Schema for a tool from its Python signature.

Walks ``inspect.signature(fn)`` + the function's type annotations + the
docstring to produce the parameter schema LiteLLM passes to the model
in OpenAI's ``tools=[...]`` format.

Coverage is deliberately narrow (v1.1):

* ``str`` / ``int`` / ``float`` / ``bool`` map to their JSON Schema
  equivalents.
* ``list[T]`` / ``tuple[T, ...]`` map to ``{type: array, items: ...}``.
* ``dict[str, T]`` maps to ``{type: object, additionalProperties: ...}``.
* ``Optional[T]`` / ``T | None`` adds ``null`` to the type union.
* ``Literal["a", "b"]`` maps to ``{enum: ["a", "b"]}``.
* Unannotated parameters fail at registration so missing annotations
  surface immediately, not at first tool call.

Defaults make a parameter optional (``required: []`` excludes it);
unannotated defaults still require an annotation on the param itself.

Docstrings are parsed at a very shallow level — first line becomes
the tool's description. Per-parameter descriptions are NOT extracted
from docstrings in v1.1; the parameter name carries the semantic.
Defer richer docstring parsing (Google / NumPy / reST) until a real
use case demands it.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints


class SchemaError(Exception):
    """Raised when a function's signature can't be converted to a
    JSON Schema. Bubbles to :class:`ToolError` at registration time."""


# ---------------------------------------------------------------------------
# Type → JSON Schema mapping
# ---------------------------------------------------------------------------


_PRIMITIVE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _type_to_schema(  # noqa: PLR0912 — one branch per type-kind; flat dispatch
    annotation: Any, *, param_name: str
) -> dict[str, Any]:
    """Convert a single Python type annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty:
        raise SchemaError(
            f"parameter {param_name!r} has no type annotation. "
            f"Annotate it (e.g. `query: str`) so the tool's schema "
            f"can be generated."
        )

    # Primitives — fast path.
    if annotation in _PRIMITIVE_MAP:
        return {"type": _PRIMITIVE_MAP[annotation]}

    origin = get_origin(annotation)

    # Union / Optional — collect the non-None members and union them.
    if origin in (Union, types.UnionType):
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        nullable = len(non_none) != len(get_args(annotation))
        if len(non_none) == 1:
            inner = _type_to_schema(non_none[0], param_name=param_name)
            if nullable:
                # Merge null into the type list.
                inner_type = inner.get("type")
                if isinstance(inner_type, str):
                    inner["type"] = [inner_type, "null"]
                elif isinstance(inner_type, list):
                    inner["type"] = [*inner_type, "null"]
            return inner
        raise SchemaError(
            f"parameter {param_name!r}: union of more than one non-None type "
            f"({non_none}) isn't supported yet. Pick one or use ``Any``."
        )

    # Literal["a", "b"] → enum
    if origin is Literal:
        members = list(get_args(annotation))
        return {"enum": members}

    # list[T] / tuple[T, ...]
    if origin in (list, tuple):
        args = get_args(annotation)
        if not args:
            return {"type": "array"}
        return {"type": "array", "items": _type_to_schema(args[0], param_name=param_name)}

    # dict[str, T]
    if origin is dict:
        args = get_args(annotation)
        dict_arity = 2  # (key_type, value_type)
        if len(args) == dict_arity and args[0] is str:
            return {
                "type": "object",
                "additionalProperties": _type_to_schema(args[1], param_name=param_name),
            }
        return {"type": "object"}

    # `Any` — accept anything.
    if annotation is Any:
        return {}  # JSON Schema "any" = empty schema

    raise SchemaError(
        f"parameter {param_name!r}: unsupported type annotation {annotation!r}. "
        f"Supported: str / int / float / bool / Literal[...] / list[T] / "
        f"dict[str, T] / Optional[T] / Any."
    )


# ---------------------------------------------------------------------------
# Public — function → tool schema
# ---------------------------------------------------------------------------


def build_tool_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """Return the OpenAI / LiteLLM tool-format schema for ``fn``.

    Output shape::

        {
            "name": "<fn.__name__>",
            "description": "<first line of __doc__>",
            "parameters": {
                "type": "object",
                "properties": { ... },
                "required": [ ... ],
                "additionalProperties": false,
            }
        }

    Required params are those WITHOUT a default. Self / cls / *args /
    **kwargs are rejected — tools are flat functions.
    """
    sig = inspect.signature(fn)
    # Resolve string-form annotations (PEP 563 / ``from __future__ import
    # annotations``) at registration time so the generator works on
    # callers that opted into deferred-annotation evaluation. Falls back
    # to raw signature annotations if get_type_hints fails (e.g. forward
    # references the resolver can't see).
    try:
        resolved_hints = get_type_hints(fn, include_extras=False)
    except Exception:
        resolved_hints = {}

    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise SchemaError(
                f"tool {fn.__name__!r}: *args / **kwargs are not supported. "
                f"Declare each parameter explicitly so the model sees a "
                f"deterministic argument list."
            )
        if name in ("self", "cls"):
            raise SchemaError(
                f"tool {fn.__name__!r}: don't register methods directly. "
                f"Wrap with a module-level function that captures the "
                f"receiver."
            )
        # Prefer the resolved hint (handles PEP 563 string annotations);
        # fall back to the raw signature annotation otherwise. Then the
        # error path stays exactly the same.
        annotation = resolved_hints.get(name, param.annotation)
        properties[name] = _type_to_schema(annotation, param_name=name)
        if param.default is inspect.Parameter.empty:
            required.append(name)

    description = (fn.__doc__ or "").strip().split("\n", 1)[0].strip()

    schema: dict[str, Any] = {
        "name": fn.__name__,
        "description": description or f"Call {fn.__name__}",
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }
    return schema


__all__ = ["SchemaError", "build_tool_schema"]
