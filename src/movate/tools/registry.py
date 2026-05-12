"""Registry of tools agents can invoke during a run.

The registry is a module-level singleton — same shape as
:class:`movate.providers.pricing.PricingTable` but for callables instead
of price tables. Operator code registers tools at import time (typically
inside an ``agents/<name>/tools.py`` that the framework auto-imports
via ``movate.yaml: tools_paths: [...]`` — agent-side discovery is the
follow-up PR's job).

For now register via the ``@tool`` decorator at any import-time
location reachable by the executor.

Concurrency note: the registry is a plain dict guarded by Python's
GIL. Tools should never be registered after the executor starts —
register at import time, then run. Mutating the registry under load
is undefined behaviour; if a multi-process layout demands per-process
tool sets, build separate registries via ``ToolRegistry()`` directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from movate.tools._schema import SchemaError, build_tool_schema


class ToolError(Exception):
    """Raised on registration / lookup / schema-generation failures.

    Wraps :class:`SchemaError` from the schema generator so callers
    have a single exception type to handle for "this tool can't be
    used as a tool."
    """


# ---------------------------------------------------------------------------
# Tool dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One registered tool — the callable + the metadata LiteLLM needs.

    Attributes:

    * ``name`` — what the model sees and what the agent.yaml references.
      Defaults to the function's ``__name__``.
    * ``callable`` — the Python function the executor invokes when the
      model emits a tool call with this name.
    * ``schema`` — OpenAI / LiteLLM-format tool schema (built by
      :func:`movate.tools._schema.build_tool_schema`).
    * ``side_effects`` — true if the tool mutates external state
      (writes to a DB, sends an email, posts to an API). The
      checkpointer consults this when resuming a workflow paused
      mid-tool-loop: side-effecting tools MUST NOT replay.
    * ``description`` — short string the model sees. Pulled from the
      function's docstring by default; overridable at register time.
    """

    name: str
    callable: Callable[..., Any]
    schema: dict[str, Any]
    side_effects: bool
    description: str

    def to_openai_tool(self) -> dict[str, Any]:
        """Return the OpenAI / LiteLLM ``tools=[...]`` entry shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema["parameters"],
            },
        }


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, Tool] = {}


def register_tool(t: Tool) -> Tool:
    """Add a :class:`Tool` to the global registry.

    Raises :class:`ToolError` if a tool with the same name is already
    registered — explicit failure beats silent overwrite. Use a
    different ``name=`` on the decorator if you need to register
    multiple versions side-by-side.
    """
    if t.name in _REGISTRY:
        existing = _REGISTRY[t.name]
        if existing.callable is t.callable:
            # Re-registration of the same callable (e.g. a module
            # imported twice in tests) — idempotent silent skip.
            return existing
        raise ToolError(
            f"tool {t.name!r} is already registered to {existing.callable!r}; "
            f"register {t.callable!r} under a different name via "
            f"`@tool(name=...)`"
        )
    _REGISTRY[t.name] = t
    return t


def get_tool(name: str) -> Tool:
    """Return the registered :class:`Tool` for ``name``.

    Raises :class:`ToolError` if not found. Callers building the
    LiteLLM ``tools=[...]`` list should catch and surface to the
    operator — "agent.yaml references unknown tool 'foo'" is a
    common typo to recover from."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ToolError(
            f"no tool registered under {name!r}. Available: "
            f"{', '.join(sorted(_REGISTRY)) or '(empty)'}"
        ) from exc


def list_tools() -> list[Tool]:
    """Snapshot of every registered tool, sorted by name. Used by
    ``movate doctor`` / ``movate show`` to enumerate the agent's
    callable surface."""
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def _clear_registry_for_tests() -> None:
    """Test helper — wipe the global registry. Production code must
    not call this; the registry is meant to be append-only after
    import time."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Decorator — the operator-facing entry point
# ---------------------------------------------------------------------------


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    side_effects: bool = False,
    description: str | None = None,
) -> Any:
    """Register ``fn`` as a tool an agent can invoke.

    Usable as either ``@tool`` (no args) or ``@tool(side_effects=True)``
    (with args). Returns the original callable unchanged so the function
    is still callable normally in Python — only the registry side-effect
    is added.

    Examples:

    >>> @tool
    ... def search(query: str) -> list[str]:
    ...     '''Look up matching documents.'''
    ...     return [...]

    >>> @tool(side_effects=True, name="post_comment")
    ... def comment(thread_id: str, body: str) -> str:
    ...     '''Post a comment to a thread.'''
    ...     return external_api.post(thread_id, body).id
    """

    def _wrap(target: Callable[..., Any]) -> Callable[..., Any]:
        try:
            schema = build_tool_schema(target)
        except SchemaError as exc:
            raise ToolError(f"can't register {target.__name__!r}: {exc}") from exc

        tool_name = name or target.__name__
        desc = description or schema["description"]
        # Re-use the generated schema but pin the resolved name + desc on
        # it so caller-visible fields are consistent.
        schema_copy = {**schema, "name": tool_name, "description": desc}
        register_tool(
            Tool(
                name=tool_name,
                callable=target,
                schema=schema_copy,
                side_effects=side_effects,
                description=desc,
            )
        )
        return target

    # `@tool` (no args) — fn is the target.
    if fn is not None and callable(fn):
        return _wrap(fn)
    # `@tool(...)` (with args) — return the wrapper.
    return _wrap


__all__ = [
    "Tool",
    "ToolError",
    "get_tool",
    "list_tools",
    "register_tool",
    "tool",
]
