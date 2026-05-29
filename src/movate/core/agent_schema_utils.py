"""Pure agent-schema introspection helpers (ADR 032 D1 factor-out).

The runtime never imports ``cli`` (``cli ⊥ runtime``), so the load-time
ADR-023 retrieval check and the two JSON-Schema introspection helpers it
depends on live in ``core`` — the single source of truth shared by:

* :mod:`movate.cli.validate` (``mdk validate``) — the existing operator-facing
  validation command.
* :mod:`movate.cli.init` — the ``--llm`` scaffold's validation loop.
* :mod:`movate.core.scaffold_preview` — the backend-agnostic preview pipeline
  (ADR 032 D1) shared by ``mdk init --llm`` and ``POST /api/v1/agents/preview``.

Everything here is pure (no I/O, no ``cli`` import, no concrete backend) so
both the CLI and the runtime can call it without dragging unrelated layers in.
"""

from __future__ import annotations

import json as _json
from typing import Any


def field_accepts_string_list(field_schema: dict[str, Any]) -> bool:
    """True when ``field_schema`` (a compiled JSON Schema fragment) can hold a
    ``list[string]`` — i.e. ``type: array`` with string items (or
    unconstrained / string items).

    Lifted verbatim from ``cli/validate.py`` so the runtime can call the same
    check; ``cli/validate.py`` now imports this. Behavior is byte-identical.
    """
    if not isinstance(field_schema, dict):
        return False
    if field_schema.get("type") != "array":
        return False
    items = field_schema.get("items")
    # No item constraint → accepts anything incl. strings.
    if items is None:
        return True
    if isinstance(items, dict):
        itype = items.get("type")
        return itype is None or itype == "string"
    return False


def primary_string_input_fields(input_schema: dict[str, Any]) -> list[str]:
    """Names of top-level string-typed input fields, for the query_from
    ambiguity check.

    Lifted verbatim from ``cli/validate.py`` so the runtime + scaffold-preview
    pipeline share one source of truth.
    """
    props = input_schema.get("properties", {})
    return [
        name for name, sub in props.items() if isinstance(sub, dict) and sub.get("type") == "string"
    ]


def check_adr023_retrieval(bundle: Any) -> str | None:
    """Run the ADR-023 load-time retrieval checks against a loaded bundle.

    Returns ``None`` when the agent either didn't opt into pre-retrieval
    (``retrieval.auto_into`` unset — the non-grounding path) or opted in
    correctly; otherwise an error string describing the first
    misconfiguration. The string is suitable for feeding back into a
    self-correcting LLM retry prompt OR for surfacing to an operator.

    Lifted from ``cli/init.py`` so both the CLI (``mdk init --llm``) and the
    runtime (``POST /api/v1/agents/preview``) reject the same set of
    misconfigured RAG-shape candidates. The bundle type is loose (Any) so this
    module stays import-light — it only needs ``bundle.spec.retrieval``,
    ``bundle.skills``, and ``bundle.input_schema``.
    """
    cfg = bundle.spec.retrieval
    if not cfg.auto_retrieval_enabled:
        return None

    auto_into = cfg.auto_into

    # (1) retrieval.skill must be declared on the agent + resolvable.
    declared = {s.spec.name for s in bundle.skills}
    if cfg.auto_skill not in declared:
        return (
            f"retrieval.skill {cfg.auto_skill!r} is not declared in the agent's "
            f"skills: {sorted(declared) or 'none'}. Add it to the skills list."
        )

    # (2) auto_into must name an input field that accepts list[string].
    props = bundle.input_schema.get("properties", {})
    field_schema = props.get(auto_into)
    if field_schema is None:
        return (
            f"retrieval.auto_into {auto_into!r} is not a field in the input schema — "
            f"add a {auto_into}: list[string] field for the retrieved chunks."
        )
    if not field_accepts_string_list(field_schema):
        return (
            f"retrieval.auto_into {auto_into!r} must accept a list[string] "
            f"(the chunk shape), but its schema is {_json.dumps(field_schema)[:160]}."
        )

    # (3) query_from must be unambiguous when left to the default.
    if not cfg.query_from:
        candidates = primary_string_input_fields(bundle.input_schema)
        canonical = [c for c in ("query", "question", "text", "message") if c in candidates]
        if not canonical and len(candidates) != 1:
            return (
                "retrieval.query_from is unset and the primary text input field is "
                f"ambiguous (string fields: {candidates or 'none'}). Set retrieval.query_from."
            )
    return None
