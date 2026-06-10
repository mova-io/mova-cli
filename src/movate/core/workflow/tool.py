"""Shared TOOL-node input/output mapping (ADR 097 D3).

A TOOL workflow node executes ONE registered skill as a deterministic workflow
step — no LLM, no prompt. The skill executes through the one shared
:func:`movate.core.skill_backend.base.dispatch_skill` path on both backends;
what each backend adds *around* that dispatch is identical and lives here, so
the native runner and the Temporal activity (``call_skill_activity``) cannot
disagree on what the skill saw or what state became — the parity guarantee
this codebase enforces for every node type (the :mod:`decision` /
:mod:`judge` precedent, ADR 094 D3 / ADR 056 D2).

Two pure helpers:

* :func:`build_skill_input` — the skill's input dict. An explicit ``input:``
  map (dotted state paths + ``{literal: …}`` constants) when the author gave
  one, else the **schema projection** — state narrowed to the skill's declared
  input-schema ``properties`` keys, byte-for-byte the rule
  ``call_skill_activity`` has always applied (and the same rule agent nodes
  get via ``_project_state``).
* :func:`merge_tool_output` — the state **delta** the caller merges:
  ``{output_key: output}`` when ``output_key`` is set (collision-safe
  namespacing), else the output dict unchanged (raw merge — the agent-node
  convention, ADR 097 D1).

Dependency-free + side-effect-free by design (no IO, no time, no random): the
only import is the sibling :mod:`decision` module's dotted-path reader, so the
``order.id``-style path semantics here are identical to a decision node's
``field:`` semantics — one dotted-path rule across the workflow surface.
Tracing/policy stay wired at the edges (runner / activity), never in here.
"""

from __future__ import annotations

from typing import Any

# One dotted-path rule across the workflow surface: a tool node's input map
# reads state exactly like a decision node's ``field:`` does (ADR 094), missing
# segments included. ``_MISSING`` keeps a stored ``None`` distinguishable from
# an absent key — a mapped path that is missing from state is OMITTED from the
# skill input (the skill's own ``required`` schema then fails the call loudly
# via dispatch_skill's input validation, ADR 097 D1).
from movate.core.workflow.decision import _MISSING, _read_field


def build_skill_input(
    state: dict[str, Any],
    input_map: dict[str, Any] | None,
    input_schema_props: Any,
) -> dict[str, Any]:
    """Build the input dict for one tool-node skill dispatch (ADR 097 D1/D3).

    With an explicit ``input_map`` (the node's ``input:`` block) the map is
    **exclusive** — only the mapped keys are sent, no implicit projection
    underneath, so the skill's input is fully readable from ``workflow.yaml``:

    * a ``str`` value is a dotted path into ``state`` (``order.id``); a path
      missing from state is omitted from the input (the skill's ``required``
      schema fails the call loudly downstream);
    * a ``{"literal": value}`` dict injects the constant verbatim.

    Without a map, the default is the established **schema projection**: state
    narrowed to ``input_schema_props`` keys (the skill's input-schema
    ``properties``); a skill with no/empty ``properties`` receives the whole
    state — exactly the rule ``call_skill_activity`` already implemented, so
    the no-map path is byte-for-byte backward compatible.
    """
    if input_map:
        built: dict[str, Any] = {}
        for key, src in input_map.items():
            if isinstance(src, dict):
                # {"literal": <value>} wrapper — a constant, not a state read.
                # Shape is enforced by ToolNodeSpec at parse time; ``get`` keeps
                # this total for hand-built graphs.
                built[key] = src.get("literal")
            else:
                value = _read_field(state, str(src))
                if value is not _MISSING:
                    built[key] = value
        return built
    if isinstance(input_schema_props, dict) and input_schema_props:
        return {k: state[k] for k in input_schema_props if k in state}
    return dict(state)


def merge_tool_output(output: dict[str, Any], output_key: str | None) -> dict[str, Any]:
    """The state DELTA for one tool-node result (ADR 097 D1/D3).

    ``output_key`` set ⇒ ``{output_key: <output dict>}`` (the collision-safe
    opt-in for skills with generic output keys); unset ⇒ the output dict
    unchanged (raw merge — the agent-node / ``call_skill_activity`` default).
    Callers apply the delta with ``state.update(...)`` on both backends.
    """
    if output_key:
        return {output_key: dict(output)}
    return dict(output)
