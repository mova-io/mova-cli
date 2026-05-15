"""Parse an OpenAPI 3.x spec and lower each operation to a movate skill.

Public API:

* :func:`parse_openapi` — accepts JSON or YAML text, returns a list of
  :class:`OperationSpec` (one per path-and-method pair).
* :func:`skill_yaml_for` — pure function: OperationSpec + server URL
  → dict ready to be ``yaml.safe_dump``-ed into ``skill.yaml``.

Design notes
------------

* **One skill per operation.** A single OpenAPI spec for, say, the
  Stripe API would produce ~500 skills. Agents reference the subset
  they need; the rest sit alongside unused. Cheaper than trying to
  guess what the agent will actually use.
* **Operation IDs become skill names** when present (the spec author
  already chose them). Fall back to ``<method>-<path-slug>`` when
  absent.
* **Path / query / body parameters all collapse into one input dict**
  (mirrors how LLM tool-use frames inputs). Path params are interpolated
  into the URL via ``{{ input.<name> }}`` Jinja syntax — same as the
  HTTP skill backend already understands.
* **side_effects derives from HTTP method.** GET/HEAD/OPTIONS →
  ``read-only``; everything else → ``mutates-state``. The auth-policy
  layer (Sprint Q+) gates mutating skills behind an explicit allow-list.
* **Auth is a placeholder.** We emit
  ``auth: bearer-from-env:OPENAPI_TOKEN`` so operators get a working
  shape; they edit the env var name (or replace with their own scheme)
  before running the skill.

What we DON'T do in MVP:

* Recursive ``$ref`` resolution — we follow one level into
  ``#/components/schemas`` then bail out to ``type: object``. Deep
  schemas with cycles aren't worth the complexity here.
* OpenAPI 2.0 (Swagger) — operators run a 2-to-3 converter first.
* OAuth flows / scopes — operators wire those into the skill's
  ``auth`` block after import.
* Server selection beyond the first ``servers[0].url``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import yaml


class OpenAPIParseError(Exception):
    """Raised when the OpenAPI spec is malformed or unsupported.

    Carries an operator-facing message; the CLI maps this to exit-2
    so the operator knows the spec needs manual cleanup (the import
    didn't half-fail mid-generation).
    """


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterSpec:
    """One input parameter (path / query / header / body field)."""

    name: str
    location: str  # "path" | "query" | "header" | "body"
    type_: str = "string"
    required: bool = False
    description: str = ""


@dataclass(frozen=True)
class OperationSpec:
    """One operation lifted from the spec — enough to generate skill.yaml.

    ``path_template`` keeps the raw ``/pets/{petId}`` so the lowerer can
    interpolate ``{{ input.petId }}`` placeholders into the URL.
    """

    operation_id: str
    method: str  # "GET" | "POST" | ...
    path_template: str
    summary: str = ""
    description: str = ""
    parameters: tuple[ParameterSpec, ...] = ()
    output_type: str = "object"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


# HTTP methods we recognize. OpenAPI also defines `trace` but it's rare
# enough that ignoring it keeps the operation enumeration cleaner.
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def parse_openapi(  # noqa: PLR0912 — branch count is inherent to multi-validation parse
    text: str,
) -> list[OperationSpec]:
    """Parse a JSON-or-YAML OpenAPI 3.x spec.

    Tries JSON first (cheaper / stricter parse), falls back to YAML.
    Raises :class:`OpenAPIParseError` for anything we can't lower —
    missing ``paths``, OpenAPI 2.0, etc.
    """
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise OpenAPIParseError(f"not valid JSON or YAML: {exc}") from exc

    if not isinstance(spec, dict):
        raise OpenAPIParseError(f"spec root must be a mapping, got {type(spec).__name__}")

    # Sanity: refuse Swagger 2.0 with a clear hint. Operators run
    # swagger2openapi or similar before importing.
    if "swagger" in spec and spec.get("swagger", "").startswith("2"):
        raise OpenAPIParseError(
            "OpenAPI 2.0 (Swagger) not supported. "
            "Convert to 3.x first (e.g. `npx swagger2openapi spec.yaml`)."
        )
    openapi_version = str(spec.get("openapi") or "")
    if not openapi_version.startswith("3"):
        raise OpenAPIParseError(
            f"unsupported openapi version {openapi_version!r}; this importer accepts 3.x specs only"
        )

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise OpenAPIParseError("spec missing required field 'paths'")
    if not paths:
        raise OpenAPIParseError("spec has no operations under 'paths' — nothing to import")

    components = spec.get("components", {}) or {}
    schemas = (components.get("schemas") or {}) if isinstance(components, dict) else {}

    operations: list[OperationSpec] = []
    seen_ids: set[str] = set()

    for path_template, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        # Path-level parameters apply to every operation under the path.
        path_level_params = _coerce_parameter_list(methods.get("parameters"))

        for method_lower, op in methods.items():
            if method_lower not in _HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            op_id = _operation_id(op, method_lower, path_template, seen_ids)
            seen_ids.add(op_id)

            params = path_level_params + _coerce_parameter_list(op.get("parameters"))
            params += _parameters_from_request_body(op.get("requestBody"), schemas)
            output_type = _output_type_from_responses(op.get("responses"), schemas)

            operations.append(
                OperationSpec(
                    operation_id=op_id,
                    method=method_lower.upper(),
                    path_template=str(path_template),
                    summary=str(op.get("summary") or ""),
                    description=str(op.get("description") or ""),
                    parameters=tuple(params),
                    output_type=output_type,
                )
            )

    if not operations:
        raise OpenAPIParseError("no usable operations found in spec")
    return operations


def _operation_id(
    op: dict[str, Any],
    method: str,
    path_template: str,
    seen: set[str],
) -> str:
    """Pick a stable skill name for the operation.

    If ``operationId`` is present, preserve it verbatim (subject only to
    a minimal sanitization that strips characters our filesystem layer
    can't handle). The spec author chose camelCase / snake_case
    deliberately — flattening to slug-case would confuse operators
    referencing the skill by the name they're used to.

    If absent, synthesize a slugified ``<method>-<path-slug>``.

    Disambiguates on collision with a ``-2`` suffix so a single import
    never produces two skills with the same name.
    """
    raw = op.get("operationId")
    if raw and isinstance(raw, str) and raw.strip():
        base = _sanitize_id(raw)
    else:
        base = _slugify(f"{method}-{path_template}")

    if base not in seen:
        return base
    n = 2
    while f"{base}-{n}" in seen:
        n += 1
    return f"{base}-{n}"


def _sanitize_id(raw: str) -> str:
    """Preserve case + structure, strip filesystem-hostile characters.

    Used for explicit operationIds. The OpenAPI spec allows pretty much
    anything in an operationId, but our skill directories can't have
    slashes, dots, or whitespace.
    """
    # Replace anything outside [A-Za-z0-9_-] with a hyphen, collapse
    # consecutive hyphens, strip edge hyphens.
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", raw)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "operation"


def _slugify(raw: str) -> str:
    """Lowercase, hyphen-separated, identifier-safe. Used for synthesized ids."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-").lower()
    return s or "operation"


def _coerce_parameter_list(raw: Any) -> list[ParameterSpec]:
    """Lift the OpenAPI ``parameters`` array into our shape."""
    if not isinstance(raw, list):
        return []
    out: list[ParameterSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        location = str(entry.get("in") or "query")
        schema = entry.get("schema") or {}
        type_ = (
            str(schema.get("type")) if isinstance(schema, dict) and schema.get("type") else "string"
        )
        out.append(
            ParameterSpec(
                name=name,
                location=location,
                type_=_openapi_to_movate_type(type_),
                required=bool(entry.get("required")),
                description=str(entry.get("description") or ""),
            )
        )
    return out


def _parameters_from_request_body(body: Any, schemas: dict[str, Any]) -> list[ParameterSpec]:
    """Flatten a JSON request body's top-level properties into parameters.

    For ``application/json`` with a flat object schema, each property
    becomes one input parameter. For ``$ref`` schemas, we follow one
    level into ``#/components/schemas/<Name>``. Anything more complex
    collapses to a single ``body: object`` parameter.
    """
    if not isinstance(body, dict):
        return []
    content = body.get("content") or {}
    if not isinstance(content, dict):
        return []
    json_block = content.get("application/json") or {}
    if not isinstance(json_block, dict):
        return []
    schema = _resolve_schema(json_block.get("schema"), schemas)
    if not isinstance(schema, dict):
        return []

    props = schema.get("properties")
    required = set(schema.get("required") or [])
    body_required = bool(body.get("required"))

    if isinstance(props, dict) and props:
        out: list[ParameterSpec] = []
        for name, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                continue
            type_ = _openapi_to_movate_type(str(prop_schema.get("type") or "string"))
            out.append(
                ParameterSpec(
                    name=str(name),
                    location="body",
                    type_=type_,
                    required=str(name) in required or body_required,
                    description=str(prop_schema.get("description") or ""),
                )
            )
        return out

    # Fall-through: collapse to a single body parameter.
    return [
        ParameterSpec(
            name="body",
            location="body",
            type_="object",
            required=body_required,
        )
    ]


def _output_type_from_responses(responses: Any, schemas: dict[str, Any]) -> str:
    """Pick a movate-typed string for the operation's output.

    Prefers 200, then 201, then any 2xx, then ``default``. If the
    matching response has a complex schema, we collapse to ``object``
    rather than try to mirror every possible deep type — operators who
    want strict typing can edit the generated schema after import.
    """
    if not isinstance(responses, dict):
        return "object"
    candidate_codes = ("200", "201", "default")
    chosen: dict[str, Any] | None = None
    for code in candidate_codes:
        if code in responses and isinstance(responses[code], dict):
            chosen = responses[code]
            break
    if chosen is None:
        # Last-chance scan for any 2xx.
        for code, resp in responses.items():
            if isinstance(code, str) and code.startswith("2") and isinstance(resp, dict):
                chosen = resp
                break
    if chosen is None:
        return "object"

    content = chosen.get("content") or {}
    if not isinstance(content, dict):
        return "object"
    json_block = content.get("application/json") or {}
    if not isinstance(json_block, dict):
        return "object"
    schema = _resolve_schema(json_block.get("schema"), schemas)
    if isinstance(schema, dict):
        if "properties" in schema or schema.get("type") == "object":
            return "object"
        if schema.get("type") == "array":
            return "array"
        return _openapi_to_movate_type(str(schema.get("type") or "object"))
    return "object"


def _resolve_schema(schema: Any, schemas: dict[str, Any]) -> Any:
    """Follow one level of ``$ref`` into ``#/components/schemas``.

    Deep $ref chains are rare in MVP-targeted specs; if the operator
    has a recursive schema they'll edit the generated input/output
    by hand. One-level follow gets us 95% of the common case (response
    body is a $ref to a single component) for ~10 lines of code.
    """
    if isinstance(schema, dict) and "$ref" in schema:
        ref = str(schema["$ref"])
        if ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", maxsplit=1)[-1]
            return schemas.get(name, schema)
    return schema


_OPENAPI_TO_MOVATE_TYPE = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def _openapi_to_movate_type(t: str) -> str:
    """Map the OpenAPI primitive name to the inline-schema name we emit."""
    return _OPENAPI_TO_MOVATE_TYPE.get(t.lower(), "string")


# ---------------------------------------------------------------------------
# Generator — pure function from OperationSpec to skill.yaml dict
# ---------------------------------------------------------------------------


_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_GENERATED_AUTH_ENV_VAR = "OPENAPI_TOKEN"
"""Placeholder env var name for the generated auth block.

Operators rename this in the skill.yaml after import. We use a
single canonical default rather than per-operation so a multi-skill
import doesn't sprinkle 50 different env var names across the
project — way too easy to mistype one and chase a 401 for hours.
"""


def skill_yaml_for(op: OperationSpec, *, server_url: str = "") -> dict[str, Any]:
    """Lower one :class:`OperationSpec` to a skill.yaml-shaped dict.

    Output is YAML-serializable. The caller does the file write +
    directory layout — keeping this pure makes it trivial to ``--dry-run``
    by serializing in-memory and printing.

    ``server_url`` is the OpenAPI ``servers[0].url`` value. We
    concatenate it with the operation's path template; if empty, the
    skill.yaml's ``implementation.entry`` is a relative path the
    operator fills in manually.
    """
    description = op.summary or op.description or f"{op.method} {op.path_template}"
    # First line only — skill descriptions render in the LLM's tool
    # catalog; long blocks are noise to the model.
    description = description.split("\n", 1)[0].strip()

    input_schema = _input_schema_dict(op.parameters)
    output_schema = {"result": op.output_type}

    full_url = (server_url.rstrip("/") + op.path_template) if server_url else op.path_template

    impl: dict[str, Any] = {
        "kind": "http",
        "entry": full_url,
        "method": op.method,
        "auth": f"bearer-from-env:{_GENERATED_AUTH_ENV_VAR}",
    }
    # Header parameters become headers in the implementation block.
    header_params = [p for p in op.parameters if p.location == "header"]
    if header_params:
        impl["headers"] = {p.name: f"{{{{ input.{p.name} }}}}" for p in header_params}

    side_effects = "read-only" if op.method in _READ_ONLY_METHODS else "mutates-state"

    return {
        "api_version": "movate/v1",
        "kind": "Skill",
        "name": op.operation_id,
        "version": "0.1.0",
        "description": description,
        "schema": {
            "input": input_schema,
            "output": output_schema,
        },
        "implementation": impl,
        "cost": {"per_call_usd": 0.0},
        "side_effects": side_effects,
    }


def _input_schema_dict(params: tuple[ParameterSpec, ...]) -> dict[str, str]:
    """Build the inline input schema dict (name → type with `?` for optional).

    Matches the inline-shorthand convention from the existing skill
    template: ``query: string`` for required, ``query?: string`` for
    optional. Operators who need a full JSON Schema with refs / regex
    swap the inline block for ``input: ./schema/input.json`` after
    import.
    """
    result: dict[str, str] = {}
    for p in params:
        key = p.name if p.required else f"{p.name}?"
        result[key] = p.type_
    return result
