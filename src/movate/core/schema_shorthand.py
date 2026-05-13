"""Compile an inline shorthand schema (the dict form used in agent.yaml's
``input:`` / ``output:`` blocks) into a JSON Schema dict that the rest of
the codebase consumes unchanged.

The shorthand exists because most agents have trivial 2-3-field schemas
and don't want a `schema/*.json` subfolder for ``{"message": "string"}``.
For genuinely complex contracts (refs, ``oneOf``, regex constraints,
nested ``$defs``) keep using the path-string form pointing at a full
JSON Schema file — it stays first-class.

Syntax
------

Each field's *value* is a single short string that names a type:

* ``string``, ``number``, ``integer``, ``boolean``: scalar
* ``string?`` (any-type with ``?`` suffix): field is optional
* ``a|b|c``: string enum (e.g. ``status: pending|done|failed``);
  ``a|b?`` is an optional enum
* ``[T]``: array whose items use the shorthand ``T`` (e.g.
  ``tags: [string]``, ``users: [{ name: string }]``)
* nested object dict: ``user: { name: string, age: integer }``

Every compiled object schema gets ``additionalProperties: false`` and a
``required`` list of every non-``?`` field, so the contract is strict
by default. Operators who want a permissive object can fall back to the
path-string form, where they own the full schema dict.

Errors
------

Unknown or malformed shorthand raises :class:`SchemaShorthandError` with
the offending key path and value baked into the message — operators see
which field tripped, not a generic "validation failed".
"""

from __future__ import annotations

from typing import Any

_SCALAR_TYPES: dict[str, dict[str, str]] = {
    # Maps the shorthand type name → the JSON Schema fragment for it.
    # Object / array containers are handled separately (they have
    # structure, not just a primitive type name).
    "string": {"type": "string"},
    "number": {"type": "number"},
    "integer": {"type": "integer"},
    "boolean": {"type": "boolean"},
}


class SchemaShorthandError(ValueError):
    """Raised on malformed shorthand. Message includes the key path so
    operators can spot the offending field in agent.yaml."""


def compile_shorthand(spec: dict[str, Any], *, root_label: str = "<root>") -> dict[str, Any]:
    """Turn a shorthand dict into a strict JSON Schema object.

    ``root_label`` is the field name used in error messages — pass
    ``"input"`` or ``"output"`` from the loader so error messages read
    like "input.message: unknown type 'strng'" rather than "<root>.message".
    """
    if not isinstance(spec, dict):
        raise SchemaShorthandError(
            f"{root_label}: expected an object (dict of field-name → type); "
            f"got {type(spec).__name__}"
        )
    return _compile_object(spec, path=root_label)


def _compile_object(spec: dict[str, Any], *, path: str) -> dict[str, Any]:
    """Compile a dict-shape into ``{"type": "object", "properties": ...}``.

    Every non-``?`` field lands in ``required``. ``additionalProperties``
    is locked to ``False`` so unknown keys at runtime fail
    validation — the inline shorthand is for tight contracts; loose
    contracts use the path-string form.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field_name, raw_value in spec.items():
        if not isinstance(field_name, str):
            raise SchemaShorthandError(
                f"{path}: field names must be strings; got "
                f"{type(field_name).__name__} ({field_name!r})"
            )
        sub_path = f"{path}.{field_name}"
        is_optional, schema_fragment = _compile_value(raw_value, path=sub_path)
        properties[field_name] = schema_fragment
        if not is_optional:
            required.append(field_name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _compile_value(value: Any, *, path: str) -> tuple[bool, dict[str, Any]]:
    """Compile a single field value. Returns ``(is_optional, schema)``.

    The ``is_optional`` flag is hoisted up so the caller (parent object
    compiler) can decide whether to add the field to ``required``.
    Optionality lives on the *parent*'s ``required`` list in JSON
    Schema, not on the field itself — so we return it separately from
    the schema fragment.
    """
    if isinstance(value, dict):
        return False, _compile_object(value, path=path)
    if isinstance(value, list):
        return False, _compile_array(value, path=path)
    if isinstance(value, str):
        return _compile_scalar_or_enum(value, path=path)
    raise SchemaShorthandError(
        f"{path}: unsupported shorthand value of type {type(value).__name__} "
        f"({value!r}); expected a type name like 'string', a list "
        "[T] for arrays, or a dict {field: type, ...} for nested objects"
    )


def _compile_array(items: list[Any], *, path: str) -> dict[str, Any]:
    """Compile a list-shape into ``{"type": "array", "items": ...}``.

    Only single-element lists are supported in v1 — the element is the
    item schema. We don't accept ``[]`` (would need to either reject
    or default to ``items: {}``); we don't accept multi-element lists
    (would imply tuple semantics or union, both of which are clearer
    as a full JSON Schema in path form). The error message points the
    operator at that escape hatch.
    """
    if len(items) != 1:
        raise SchemaShorthandError(
            f"{path}: array shorthand expects exactly one element describing the "
            f"item schema (e.g. tags: [string]); got {len(items)} elements. For "
            "tuple or union arrays use a path-string and a full JSON Schema file."
        )
    _, item_schema = _compile_value(items[0], path=f"{path}[]")
    return {
        "type": "array",
        "items": item_schema,
    }


def _compile_scalar_or_enum(value: str, *, path: str) -> tuple[bool, dict[str, Any]]:
    """Compile a plain string like ``"integer"``, ``"string?"``, or
    ``"pending|done|failed"`` into the appropriate JSON Schema fragment.

    The ``?`` suffix means optional — recorded as the first element of
    the return tuple. The ``|`` separator means string enum. Combining
    both (``"a|b?"``) is allowed and means "optional string enum".
    """
    stripped = value.strip()
    if not stripped:
        raise SchemaShorthandError(f"{path}: empty type string is not a valid shorthand")

    is_optional = stripped.endswith("?")
    if is_optional:
        stripped = stripped[:-1].strip()
        if not stripped:
            raise SchemaShorthandError(
                f"{path}: '?' alone is not a type — write e.g. 'string?' to mark "
                "a string field as optional"
            )

    # Enums first — pipe-separated literals. We treat them as strings
    # rather than mixed types because the shorthand has no syntax for
    # "this enum value is an integer."
    if "|" in stripped:
        members = [m.strip() for m in stripped.split("|")]
        empty_members = [i for i, m in enumerate(members) if not m]
        if empty_members:
            raise SchemaShorthandError(
                f"{path}: empty enum member in '{value}' (extra '|' separator?)"
            )
        return is_optional, {"type": "string", "enum": members}

    if stripped not in _SCALAR_TYPES:
        raise SchemaShorthandError(
            f"{path}: unknown type {stripped!r}. Valid scalars: "
            f"{sorted(_SCALAR_TYPES.keys())}. For enums use 'a|b|c'; for arrays "
            "use '[type]'; for nested objects use a dict."
        )
    return is_optional, dict(_SCALAR_TYPES[stripped])
