"""LangChain tool skill backend — wraps LangChain ``BaseTool`` as mdk skills.

Resolves a LangChain tool class or factory from a dotted import path
(``langchain_community.tools.wikipedia:WikipediaQueryRun``), instantiates
it once, and delegates ``execute`` to the tool's ``invoke`` / ``ainvoke``.

The ``mdk[langchain]`` extra (``langchain-core>=0.3``) is imported LAZILY
inside the ``execute`` method — a runtime without the extra never triggers
the import (same posture as the LangGraph backend).

Skill authors write a standard ``skill.yaml``::

    implementation:
      kind: langchain
      entry: "langchain_community.tools.wikipedia:WikipediaQueryRun"

The tool's ``invoke()`` receives the input dict; its return value is
coerced to a dict (string results become ``{"result": <str>}``).

Mock mode: when ``ctx.mock`` is True, the backend short-circuits to
``{"result": "(mock) tool result for <skill>"}`` without calling the
real tool — deterministic for eval/CI.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from movate.core.models import SkillImplementationKind
from movate.core.skill_backend.base import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    register_backend,
)

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle

_log = logging.getLogger(__name__)


class LangChainSkillBackend:
    """Wraps LangChain ``BaseTool`` subclasses as mdk skills.

    Resolves the tool class from the ``entry`` string on first call,
    instantiates it, and caches the instance. Subsequent calls reuse
    the cached tool (LangChain tools are typically stateless).

    Supports both sync (``invoke``) and async (``ainvoke``) tools.
    """

    kind = SkillImplementationKind.LANGCHAIN

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}  # entry → instantiated BaseTool

    async def execute(  # noqa: PLR0912
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        # Mock mode — deterministic stub for eval/CI.
        if ctx.mock:
            return {"result": f"(mock) tool result for {skill.spec.name}"}

        entry = skill.spec.implementation.entry
        tool = self._resolve_tool(entry, skill)

        # Tracing (ADR 024) — open a child span under the executor's skill span.
        _span = None
        _t0 = 0.0
        if ctx.tracer is not None:
            _t0 = time.monotonic()
            _span = ctx.tracer.start_span(
                "langchain.tool",
                {"skill": skill.spec.name, "entry": entry, "tool_name": getattr(tool, "name", "")},
                parent=ctx.parent_span,
            )

        try:
            # LangChain tools accept either a dict or a string. If the
            # input schema has a single required field, pass just that
            # value (many LC tools expect a bare string). Otherwise pass
            # the full dict.
            tool_input: Any = input
            props = skill.input_schema.get("properties", {})
            required = skill.input_schema.get("required", [])
            if len(required) == 1 and len(props) == 1:
                # Single-field shorthand: pass the value directly.
                tool_input = input.get(required[0], input)

            # Prefer async if available, fall back to sync.
            if hasattr(tool, "ainvoke"):
                raw_result = await tool.ainvoke(tool_input)
            elif hasattr(tool, "invoke"):
                result_maybe = tool.invoke(tool_input)
                if inspect.isawaitable(result_maybe):
                    raw_result = await result_maybe
                else:
                    raw_result = result_maybe
            else:
                raise SkillError(
                    type=SkillErrorType.BACKEND_ERROR,
                    message=f"LangChain tool {entry!r} has no invoke/ainvoke method",
                )

            # Coerce result to dict. LangChain tools often return bare strings.
            if isinstance(raw_result, dict):
                output = raw_result
            elif isinstance(raw_result, str):
                output = {"result": raw_result}
            else:
                output = {"result": str(raw_result)}

            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                ctx.tracer.end_span(_span, status="ok")

            return output

        except SkillError:
            raise  # preserve structured errors
        except Exception as exc:
            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                ctx.tracer.end_span(_span, status="error")
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"LangChain tool {skill.spec.name!r} failed: {exc}",
            ) from exc

    def _resolve_tool(self, entry: str, skill: SkillBundle) -> Any:
        """Resolve a dotted import path to a LangChain tool instance.

        ``entry`` format: ``module.path:ClassName`` or ``module.path:factory_func``.
        The class/function is imported via importlib, instantiated (if a class),
        and cached for the process lifetime.

        Config from ``skill.yaml`` metadata is passed as kwargs to the constructor
        if present (e.g. ``implementation.config: {max_results: 5}``).
        """
        if entry in self._tools:
            return self._tools[entry]

        if ":" not in entry:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"langchain entry {entry!r} must be 'module.path:ClassName' "
                    "(e.g. 'langchain_community.tools.wikipedia:WikipediaQueryRun')"
                ),
            )

        module_name, attr_name = entry.split(":", 1)
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"can't import LangChain tool module {module_name!r}: {exc}. "
                    "Is the LangChain package installed? "
                    "Try: pip install langchain-community"
                ),
            ) from exc

        if not hasattr(module, attr_name):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"module {module_name!r} has no attribute {attr_name!r}",
            )

        obj = getattr(module, attr_name)

        # If obj is a class, instantiate it with optional config kwargs.
        config = getattr(skill.spec.implementation, "config", None) or {}
        if isinstance(config, dict) and inspect.isclass(obj):
            try:
                tool = obj(**config)
            except Exception as exc:
                raise SkillError(
                    type=SkillErrorType.BACKEND_ERROR,
                    message=f"failed to instantiate LangChain tool {entry!r}: {exc}",
                ) from exc
        elif inspect.isclass(obj):
            try:
                tool = obj()
            except Exception as exc:
                raise SkillError(
                    type=SkillErrorType.BACKEND_ERROR,
                    message=f"failed to instantiate LangChain tool {entry!r}: {exc}",
                ) from exc
        elif callable(obj):
            # Factory function — call it to get the tool instance.
            try:
                tool = obj() if not config else obj(**config)
            except Exception as exc:
                raise SkillError(
                    type=SkillErrorType.BACKEND_ERROR,
                    message=f"failed to call LangChain tool factory {entry!r}: {exc}",
                ) from exc
        else:
            # Already an instance (module-level singleton).
            tool = obj

        self._tools[entry] = tool
        _log.info(
            "langchain: resolved tool %s → %s (name=%s)",
            entry,
            type(tool).__name__,
            getattr(tool, "name", "?"),
        )
        return tool


# Auto-register on import. The executor imports all backend modules from
# its initialization path, so by the time any skill is dispatched the
# backend is wired up.
register_backend(LangChainSkillBackend())
