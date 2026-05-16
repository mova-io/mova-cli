"""Compile an inline shorthand schema (the dict form used in agent.yaml's
``input:`` / ``output:`` blocks, OR a standalone ``schema/*.yaml`` file)
into a JSON Schema dict that the rest of the codebase consumes unchanged.

The shorthand exists because most agents have trivial 2-3-field schemas
and don't want to look at a 40-line JSON Schema with ``minLength: 1``,
``additionalProperties: false``, and ``"type": "string"`` on every
property. For genuinely complex contracts (regex patterns, ``oneOf``,
conditional schemas) the path-string form pointing at a full JSON
Schema file stays first-class.

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
* **ranges** (numeric or length): ``integer(0..12)``,
  ``integer(0..)``, ``integer(..12)``, ``string(1..)``,
  ``string(..100)``. For strings the bounds map to
  ``minLength`` / ``maxLength``; for numerics they map to
  ``minimum`` / ``maximum``.
* **type aliases via $defs**: declare reusable types at the top with
  a ``$defs`` block, reference them with ``$<name>``:

  .. code-block:: yaml

      $defs:
        dim: { score: integer(0..3), rationale: string(1..) }

      bant:
        budget: $dim
        authority: $dim

  Each ``$<name>`` reference compiles to a JSON Schema
  ``{"$ref": "#/$defs/<name>"}``; the ``$defs`` block is materialized
  on the root schema so the runtime validator can resolve them.

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

    The top-level dict may include a ``$defs`` block to declare reusable
    types; siblings of ``$defs`` reference them via ``$<name>``. The
    ``$defs`` is hoisted onto the compiled root schema unchanged so
    JSON Schema's ``$ref`` resolution finds them at validation time.
    """
    if not isinstance(spec, dict):
        raise SchemaShorthandError(
            f"{root_label}: expected an object (dict of field-name → type); "
            f"got {type(spec).__name__}"
        )
    # Extract the top-level $defs block (if any) so it doesn't get
    # mistakenly treated as a property of the object schema.
    defs_raw = spec.get("$defs")
    fields = {k: v for k, v in spec.items() if k != "$defs"}
    compiled = _compile_object(fields, path=root_label)
    if defs_raw is not None:
        compiled["$defs"] = _compile_defs(defs_raw, path=f"{root_label}.$defs")
    return compiled


def _compile_defs(defs_raw: Any, *, path: str) -> dict[str, Any]:
    """Compile the top-level $defs block into a dict of JSON Schemas.

    Each value under $defs is itself a shorthand object (typically a
    nested record like ``dim: { score: integer(0..3), rationale: string }``)
    — we recursively compile it via the standard object path so $defs
    members support every shorthand feature (ranges, enums, arrays,
    optionality)."""
    if not isinstance(defs_raw, dict):
        raise SchemaShorthandError(
            f"{path}: expected a dict of type-name → shape; got {type(defs_raw).__name__}"
        )
    out: dict[str, Any] = {}
    for name, shape in defs_raw.items():
        if not isinstance(name, str) or not name:
            raise SchemaShorthandError(
                f"{path}: $defs entry name must be a non-empty string; got {name!r}"
            )
        if not isinstance(shape, dict):
            raise SchemaShorthandError(
                f"{path}.{name}: $defs entry must be a dict-of-fields; got {type(shape).__name__}"
            )
        out[name] = _compile_object(shape, path=f"{path}.{name}")
    return out


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
    """Compile a plain string like ``"integer"``, ``"string?"``,
    ``"pending|done|failed"``, ``"integer(0..12)"``, or ``"$dim"``
    into the appropriate JSON Schema fragment.

    The ``?`` suffix means optional — recorded as the first element of
    the return tuple. Other syntaxes:

    * ``a|b|c`` — string enum
    * ``$name`` — ref to ``#/$defs/<name>``
    * ``type(min..max)`` — bounded scalar (minimum/maximum for numeric
      types, minLength/maxLength for strings)
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

    # $defs reference — `$<name>` compiles to `{"$ref": "#/$defs/<name>"}`.
    # Check this before enums (so "$a|$b" can't smuggle two refs into a
    # string enum) and before ranges (so $foo(bar) isn't accidentally
    # parsed as a ranged type).
    if stripped.startswith("$"):
        ref_name = stripped[1:]
        if not ref_name or not _is_safe_ref_name(ref_name):
            raise SchemaShorthandError(
                f"{path}: $ref name must be a non-empty word-character identifier; got {stripped!r}"
            )
        return is_optional, {"$ref": f"#/$defs/{ref_name}"}

    # Enums — pipe-separated literals. Treated as strings.
    if "|" in stripped:
        members = [m.strip() for m in stripped.split("|")]
        empty_members = [i for i, m in enumerate(members) if not m]
        if empty_members:
            raise SchemaShorthandError(
                f"{path}: empty enum member in '{value}' (extra '|' separator?)"
            )
        return is_optional, {"type": "string", "enum": members}

    # Range form: `type(lo..hi)`, `type(lo..)`, `type(..hi)`.
    if "(" in stripped:
        return is_optional, _compile_ranged_scalar(stripped, original=value, path=path)

    if stripped not in _SCALAR_TYPES:
        raise SchemaShorthandError(
            f"{path}: unknown type {stripped!r}. Valid scalars: "
            f"{sorted(_SCALAR_TYPES.keys())}. For enums use 'a|b|c'; "
            "for arrays use '[type]'; for nested objects use a dict; "
            "for refs use '$<defs-name>'; for ranges use "
            "'<type>(<lo>..<hi>)'."
        )
    return is_optional, dict(_SCALAR_TYPES[stripped])


_REF_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


def _is_safe_ref_name(name: str) -> bool:
    """Whitelist word-character ref names so $defs/<name> resolves
    cleanly in JSON Pointer (no URL-encoding pitfalls)."""
    return bool(name) and all(c in _REF_NAME_CHARS for c in name)


def _compile_ranged_scalar(stripped: str, *, original: str, path: str) -> dict[str, Any]:
    """Compile ``type(lo..hi)`` / ``type(lo..)`` / ``type(..hi)``.

    For numeric types (``integer`` / ``number``) the bounds map to
    ``minimum`` / ``maximum``. For ``string`` they map to
    ``minLength`` / ``maxLength``. ``boolean(...)`` is rejected (a
    boolean has nothing to bound).
    """
    if not stripped.endswith(")"):
        raise SchemaShorthandError(
            f"{path}: malformed range in {original!r} "
            "(expected `type(lo..hi)`, missing closing paren?)"
        )
    type_name, _, inside = stripped[:-1].partition("(")
    type_name = type_name.strip()
    if type_name not in _SCALAR_TYPES:
        raise SchemaShorthandError(
            f"{path}: unknown ranged type {type_name!r} in {original!r}. "
            f"Valid: {sorted(_SCALAR_TYPES.keys())}"
        )
    if type_name == "boolean":
        raise SchemaShorthandError(
            f"{path}: boolean has no range; write 'boolean' (or 'boolean?') "
            f"without parens. Got {original!r}."
        )
    if ".." not in inside:
        raise SchemaShorthandError(
            f"{path}: range body must contain '..' (got {inside!r}). "
            f"Examples: '0..12', '1..', '..100'."
        )
    lo_raw, _, hi_raw = inside.partition("..")
    lo_raw, hi_raw = lo_raw.strip(), hi_raw.strip()
    if not lo_raw and not hi_raw:
        raise SchemaShorthandError(
            f"{path}: range needs at least one bound (got '{inside}'); "
            "write 'X..', '..X', or 'X..Y'."
        )

    lo = _parse_range_bound(lo_raw, type_name=type_name, path=path) if lo_raw else None
    hi = _parse_range_bound(hi_raw, type_name=type_name, path=path) if hi_raw else None

    schema: dict[str, Any] = dict(_SCALAR_TYPES[type_name])
    if type_name == "string":
        if lo is not None:
            schema["minLength"] = int(lo)
        if hi is not None:
            schema["maxLength"] = int(hi)
    else:
        # integer / number — numeric bounds.
        if lo is not None:
            schema["minimum"] = lo
        if hi is not None:
            schema["maximum"] = hi
    return schema


def _parse_range_bound(raw: str, *, type_name: str, path: str) -> int | float:
    """Parse a range-bound literal as the right numeric type.

    For ``integer`` and ``string`` lengths we require an int; for
    ``number`` we accept floats. Catches typos like ``integer(0..3.5)``
    early instead of silently truncating.
    """
    if type_name == "number":
        try:
            return float(raw)
        except ValueError as exc:
            raise SchemaShorthandError(f"{path}: range bound {raw!r} is not a number") from exc
    # integer / string — int only.
    try:
        return int(raw)
    except ValueError as exc:
        raise SchemaShorthandError(
            f"{path}: range bound {raw!r} must be an integer for {type_name}"
        ) from exc
