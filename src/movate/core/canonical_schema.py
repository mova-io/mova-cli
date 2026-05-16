"""Canonical schema format — business-readable schema DSL for AI agents.

Compiles into JSON Schema Draft 2020-12 (today) and — by design —
into Pydantic, OpenAPI, TypeScript, and form-definition outputs in
future versions. The compile-time contract is one-way (canonical →
target); round-tripping back from JSON Schema isn't supported in v1.

Why a third schema layer
------------------------

MDK ships three nested DSLs, each targeting a different author:

1. **Canonical** (this module) — what business analysts author.
   Rich semantic types (``email``, ``currency_usd``, ``markdown``),
   per-field ``description`` / ``example`` / ``ui`` annotations,
   top-level ``required`` list. Compiles to JSON Schema (or any
   other target the codegen suite supports).

2. **Shorthand** (:mod:`movate.core.schema_shorthand`) — what
   engineers author when they want terse. ``field: type`` pairs;
   ranges via ``integer(0..3)``; refs via ``$dim``. Compiles to
   JSON Schema. Stays first-class for power users + tests.

3. **JSON Schema** (Draft 2020-12) — what the runtime validates
   against. Both DSLs compile down to this. Hand-written JSON
   Schemas keep working unchanged.

The loader (:mod:`movate.core.loader`) detects which DSL each
``.yaml`` schema file uses (canonical has ``version: 1``; shorthand
doesn't; raw JSON Schema has ``$schema`` or ``type: object`` +
``properties``) and routes accordingly. Operators pick the layer
that fits their audience.

Format
------

The format is documented in detail in ``src/movate/templates/
*/schema/*.yaml`` examples and in ``mdk schema --help``. Top-level
keys:

* ``version`` (required, must be ``1``)
* ``type: object`` (root is always an object schema)
* ``title`` / ``description`` (free-form prose; carried into
  JSON Schema's same-name fields)
* ``fields`` (the field map — same key-value structure as JSON
  Schema's ``properties``)
* ``required`` (list of field names that must be present)
* ``types`` (named reusable types; compiled to JSON Schema
  ``$defs``)
* ``ui`` (form / display hints; carried as ``x-mdk-ui`` extension)
* ``contracts`` (cross-field invariants; carried as
  ``x-mdk-contracts`` extension in v1, enforced in v2)

Per-field keys (under ``fields:`` or ``types:``):

* ``type`` (required) — see semantic-type table below, or
  ``object`` + nested ``fields``, or ``list[<type>]`` for arrays,
  or a name from the top-level ``types`` block (compiles to
  ``$ref``)
* ``description`` (prose; passed through)
* ``example`` (single example value) / ``examples`` (list)
* ``min`` / ``max`` — numeric bounds (``minimum``/``maximum``)
  for numeric types; length bounds (``minLength``/``maxLength``)
  for ``string``/``text``/``markdown``
* ``min_length`` / ``max_length`` — explicit length bounds
  (preferred when the type is ambiguous, e.g. for ``markdown``)
* ``values`` (only with ``type: enum``) — the allowed values
* ``ui`` — per-field display hints

Semantic types
~~~~~~~~~~~~~~

============ ==================== ==============================
Canonical    JSON Schema          Notes
============ ==================== ==============================
string       string               Alias: ``text``
text         string               Same as ``string``
markdown     string               + ``x-mdk-format: markdown``
integer      integer
number       number               Float
boolean      boolean
email        string + format:email
url          string + format:uri
phone        string               + ``x-mdk-format: phone``
date         string + format:date ISO 8601 date
datetime     string + format:date-time ISO 8601 datetime
uuid         string + format:uuid
currency_usd integer              Amount in CENTS (no float)
enum         string + enum:[…]    Requires ``values:`` list
list[<T>]    array + items:<T>    ``T`` is any canonical type
object       object + properties  Requires nested ``fields:``
``<custom>`` $ref → #/$defs/…     Must be declared in ``types:``
============ ==================== ==============================
"""

from __future__ import annotations

from typing import Any

# ----------------------------------------------------------------------------
# Type registry — semantic name → JSON Schema fragment.
# ----------------------------------------------------------------------------
#
# Each entry is the BASE JSON Schema snippet a compile pass starts from
# before applying common modifiers (description / examples / bounds / ui).
# The ``x-mdk-format`` extension records semantic intent that JSON Schema
# doesn't carry on its own — form generators read this to pick the right
# input widget (markdown editor, phone input, currency picker, etc.).
_SEMANTIC_TYPES: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "text": {"type": "string"},
    "markdown": {"type": "string", "x-mdk-format": "markdown"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "email": {"type": "string", "format": "email"},
    "url": {"type": "string", "format": "uri"},
    "phone": {"type": "string", "x-mdk-format": "phone"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "uuid": {"type": "string", "format": "uuid"},
    "currency_usd": {
        "type": "integer",
        "x-mdk-format": "currency-usd-cents",
        "minimum": 0,
    },
}

# Whitelist for custom-type ref names (matches the shorthand compiler's
# rule). Same character class — JSON Pointer doesn't need escaping for
# any of these.
_REF_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

_SUPPORTED_VERSION = 1


class CanonicalSchemaError(ValueError):
    """Raised on malformed canonical-format schemas. Message includes the
    JSON-Pointer-like path so operators can spot the offending field."""


def compile_canonical(doc: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0912 — orchestrator; branch count reflects the format's surface
    """Compile a canonical-format dict into JSON Schema Draft 2020-12.

    The doc's top-level structure is validated first (version + type
    + fields); then each field is recursively compiled via
    :func:`_compile_field`. Reusable types from the optional
    ``types:`` block become ``$defs`` on the output. ``ui:`` and
    ``contracts:`` blocks are passed through as
    ``x-mdk-ui`` / ``x-mdk-contracts`` extensions so downstream
    tooling (form generators, contract enforcers) can read them
    without re-parsing the source YAML.

    Returns a JSON-Schema-compatible dict that
    :class:`jsonschema.Draft202012Validator` accepts unchanged.
    """
    if not isinstance(doc, dict):
        raise CanonicalSchemaError(
            f"<root>: schema must be a top-level object, got {type(doc).__name__}"
        )

    version = doc.get("version")
    if version != _SUPPORTED_VERSION:
        raise CanonicalSchemaError(
            f"<root>: unsupported `version: {version!r}`. This compiler "
            f"only handles version {_SUPPORTED_VERSION}; bump or pin "
            f"as needed."
        )

    root_type = doc.get("type", "object")
    if root_type != "object":
        raise CanonicalSchemaError(
            f"<root>: top-level `type` must be `object` "
            f"(v1 only supports object roots); got {root_type!r}"
        )

    fields_block = doc.get("fields")
    if not isinstance(fields_block, dict):
        raise CanonicalSchemaError(
            f"<root>: `fields` must be a dict of field-name → spec; got "
            f"{type(fields_block).__name__}"
        )

    properties: dict[str, Any] = {}
    for field_name, field_spec in fields_block.items():
        if not isinstance(field_name, str) or not field_name:
            raise CanonicalSchemaError(
                f"<root>.fields: field name must be a non-empty string; got {field_name!r}"
            )
        properties[field_name] = _compile_field(field_spec, path=f"fields.{field_name}")

    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }

    title = doc.get("title")
    if title:
        schema["title"] = str(title)
    description = doc.get("description")
    if description:
        schema["description"] = str(description).strip()

    required = doc.get("required") or []
    if not isinstance(required, list):
        raise CanonicalSchemaError(
            f"<root>: `required` must be a list of field names; got {type(required).__name__}"
        )
    # Validate every entry in `required` is a real declared field —
    # business users typo field names; catch the mismatch at compile.
    for name in required:
        if name not in properties:
            raise CanonicalSchemaError(
                f"<root>.required: {name!r} is not declared in `fields:`. "
                f"Declared fields: {sorted(properties.keys())}"
            )
    if required:
        schema["required"] = list(required)

    # Reusable types → $defs. Each compiled via the same field
    # compiler so they support every feature a regular field does
    # (nested objects, lists, refs, etc.).
    types_block = doc.get("types")
    if types_block is not None:
        if not isinstance(types_block, dict):
            raise CanonicalSchemaError(
                f"<root>.types: must be a dict of type-name → spec; got "
                f"{type(types_block).__name__}"
            )
        defs: dict[str, Any] = {}
        for type_name, type_spec in types_block.items():
            if not _is_safe_ref_name(type_name):
                raise CanonicalSchemaError(
                    f"<root>.types: type name {type_name!r} must be "
                    f"word-characters only (a-z A-Z 0-9 _ -)"
                )
            defs[type_name] = _compile_field(type_spec, path=f"types.{type_name}")
        if defs:
            schema["$defs"] = defs

    # UI block as JSON Schema extension. Preserved verbatim — the
    # form generator (future) is responsible for interpreting the
    # nested `order` / `groups` keys.
    ui_block = doc.get("ui")
    if ui_block is not None:
        schema["x-mdk-ui"] = ui_block

    # Contracts: v1 carries them through as an extension so authors
    # can write them today; enforcement (compiling to JSON Schema
    # `allOf` / `if-then-else`) lands in v2.
    contracts = doc.get("contracts")
    if contracts is not None:
        schema["x-mdk-contracts"] = contracts

    return schema


def is_canonical_format(doc: Any) -> bool:
    """Return True if ``doc`` looks like a canonical-format schema.

    The unambiguous marker is ``version: 1`` at the top level. Anything
    else falls through to shorthand or JSON Schema detection in the
    loader. Used by :mod:`movate.core.loader._load_schema_doc` to
    pick the right compiler at load time.
    """
    if not isinstance(doc, dict):
        return False
    return doc.get("version") == _SUPPORTED_VERSION


# ----------------------------------------------------------------------------
# Field compiler
# ----------------------------------------------------------------------------


def _compile_field(  # noqa: PLR0912 — type dispatcher; branch count reflects type-table
    spec: Any,
    *,
    path: str,
) -> dict[str, Any]:
    """Compile one field's spec dict into a JSON Schema fragment.

    Dispatches on ``type:`` — semantic atoms (``string``, ``email``,
    etc.), composite types (``list[X]``, ``object``, ``enum``), or
    a user-declared type from the top-level ``types:`` block (ref).
    Applies common modifiers (description, examples, bounds, ui)
    uniformly across all types.
    """
    if not isinstance(spec, dict):
        raise CanonicalSchemaError(f"{path}: field spec must be a dict, got {type(spec).__name__}")

    type_name = spec.get("type")
    if not type_name:
        raise CanonicalSchemaError(f"{path}: field must declare a `type`")
    if not isinstance(type_name, str):
        raise CanonicalSchemaError(
            f"{path}: `type` must be a string, got {type(type_name).__name__}"
        )

    # --- Composite: list[T] ---
    if type_name.startswith("list[") and type_name.endswith("]"):
        inner_name = type_name[len("list[") : -1].strip()
        if not inner_name:
            raise CanonicalSchemaError(
                f"{path}: `list[]` needs an inner type — write e.g. "
                f"`list[string]` or `list[scoring]`"
            )
        # Inner spec — only the type name; per-item annotations
        # aren't supported in v1 (carry through inline only if you
        # need per-element constraints, in v2).
        inner_schema = _compile_field({"type": inner_name}, path=f"{path}[]")
        schema: dict[str, Any] = {"type": "array", "items": inner_schema}

    # --- Composite: enum ---
    elif type_name == "enum":
        values = spec.get("values")
        if not isinstance(values, list) or not values:
            raise CanonicalSchemaError(f"{path}: `type: enum` requires a non-empty `values:` list")
        schema = {"type": "string", "enum": list(values)}

    # --- Composite: object (nested) ---
    elif type_name == "object":
        nested_fields = spec.get("fields")
        if not isinstance(nested_fields, dict):
            raise CanonicalSchemaError(f"{path}: `type: object` requires a `fields:` block")
        nested_properties: dict[str, Any] = {}
        for nf_name, nf_spec in nested_fields.items():
            nested_properties[nf_name] = _compile_field(nf_spec, path=f"{path}.fields.{nf_name}")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": nested_properties,
        }
        nested_required = spec.get("required") or []
        if not isinstance(nested_required, list):
            raise CanonicalSchemaError(f"{path}: nested `required` must be a list")
        for name in nested_required:
            if name not in nested_properties:
                raise CanonicalSchemaError(
                    f"{path}.required: {name!r} not in declared "
                    f"`fields:` ({sorted(nested_properties.keys())})"
                )
        if nested_required:
            schema["required"] = list(nested_required)

    # --- Atomic semantic types ---
    elif type_name in _SEMANTIC_TYPES:
        schema = dict(_SEMANTIC_TYPES[type_name])

    # --- Reference to a custom type from $defs ---
    else:
        if not _is_safe_ref_name(type_name):
            raise CanonicalSchemaError(
                f"{path}: unknown / unsafe type name {type_name!r}. "
                f"Built-in types: {sorted(_SEMANTIC_TYPES)} | "
                f"`enum` | `object` | `list[<type>]`. "
                f"For custom types, declare them in the top-level "
                f"`types:` block and reference by name."
            )
        schema = {"$ref": f"#/$defs/{type_name}"}

    # --- Common modifiers applied to all types ---
    description = spec.get("description")
    if description:
        # Strip trailing whitespace from multi-line YAML blocks (`>`
        # folded strings) so the output is clean.
        schema["description"] = str(description).strip()

    # `example:` (single) is normalized to `examples:` (list-of-one)
    # so the output is consistent with JSON Schema Draft 2020-12.
    if "example" in spec:
        schema["examples"] = [spec["example"]]
    if "examples" in spec:
        schema["examples"] = list(spec["examples"])

    # min / max — numeric bounds for numeric types, length bounds
    # for string-like types. Explicit `min_length` / `max_length`
    # are also accepted (preferred for ambiguous cases like
    # `currency_usd` where you might want numeric bounds AND string
    # bounds on the rendered form).
    underlying_type = schema.get("type")
    if "min" in spec:
        if underlying_type in ("integer", "number"):
            schema["minimum"] = spec["min"]
        elif underlying_type == "string":
            schema["minLength"] = int(spec["min"])
    if "max" in spec:
        if underlying_type in ("integer", "number"):
            schema["maximum"] = spec["max"]
        elif underlying_type == "string":
            schema["maxLength"] = int(spec["max"])
    if "min_length" in spec:
        schema["minLength"] = int(spec["min_length"])
    if "max_length" in spec:
        schema["maxLength"] = int(spec["max_length"])
    if "min_items" in spec and underlying_type == "array":
        schema["minItems"] = int(spec["min_items"])
    if "max_items" in spec and underlying_type == "array":
        schema["maxItems"] = int(spec["max_items"])

    # Per-field UI hints (e.g. `ui: textarea` or
    # `ui: { widget: textarea, rows: 8 }`). Preserved as extension.
    if "ui" in spec:
        schema["x-mdk-ui"] = spec["ui"]

    return schema


def _is_safe_ref_name(name: Any) -> bool:
    """Word-characters-only check for custom type names. Matches the
    shorthand compiler's policy so refs round-trip cleanly between
    DSLs."""
    if not isinstance(name, str) or not name:
        return False
    return all(c in _REF_NAME_CHARS for c in name)
