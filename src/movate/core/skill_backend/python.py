"""Python skill backend — resolves ``pkg.mod:func`` entries via importlib.

The entrypoint string is split on ``:`` into module + attribute, then
``importlib.import_module`` + ``getattr`` produce the callable. The
function is invoked with ``(input_dict, ctx)`` — sync or async, the
backend ``await``s either way. Output must be a dict matching the
skill's declared output schema (enforced one layer up in
:func:`dispatch_skill`).

Failure modes mapped to :class:`SkillError`:

* `ImportError` / `AttributeError` on resolve → ``backend_error``
* Function call exception → ``backend_error`` (preserves original message)
* Function returns non-dict → ``validation_failed`` (caught upstream by
  the output validator, which only accepts dicts)

Tracing (ADR 024):

When ``ctx.tracer`` is set the backend opens a ``python.call`` child span
under ``ctx.parent_span`` and closes it on success/error. The span carries
the resolved ``entry`` string so operators can find the exact callable.

This module's import side-effects register the backend with the
shared registry. Importing this module is the only thing needed to
"install" the Python backend.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import time
from pathlib import Path
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


class PythonSkillBackend:
    """Resolves the Python entrypoint and calls it.

    The backend object is stateless; one instance handles every
    Python-kind skill in the project. We do cache the resolved
    callable per ``entry`` string because importlib has non-trivial
    overhead on every call.

    Cache invalidation note: the resolved callable is cached for the
    lifetime of the process. Re-uploading a skill's ``impl.py`` via
    ``POST /api/v1/skills`` writes new bytes to disk but the cached
    callable still points at the old module. A process restart is
    required to pick up new skill code — this is an acceptable
    trade-off for the runtime's operational model (redeploy = new
    revision). Operators who need hot-reload during development should
    use ``mdk serve`` locally (fresh process per session).
    """

    kind = SkillImplementationKind.PYTHON

    def __init__(self) -> None:
        self._resolved: dict[str, Any] = {}

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        entry = skill.spec.implementation.entry
        func = self._resolve(entry, skill_dir=skill.skill_dir)

        # ADR 024 — open a ``python.call`` child span under the executor's
        # ``skill.<name>`` span (ctx.parent_span). No-op when tracer is None.
        _span = None
        _t0 = 0.0
        if ctx.tracer is not None:
            _t0 = time.monotonic()
            _span = ctx.tracer.start_span(
                "python.call",
                {"skill": skill.spec.name, "entry": entry},
                parent=ctx.parent_span,
            )

        try:
            result = func(input, ctx)
            # Tolerate both sync and async impls. Many simple skills
            # (calculator, JSON munging) are sync; HTTP-using ones are
            # async. Letting both work removes a footgun for skill authors
            # who'd otherwise be forced to make trivial things async.
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict):
                raise SkillError(
                    type=SkillErrorType.VALIDATION_FAILED,
                    message=(
                        f"python skill {skill.spec.name!r} returned a "
                        f"{type(result).__name__}, expected dict"
                    ),
                )
            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                ctx.tracer.end_span(_span, status="ok")
            return result
        except Exception:
            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                ctx.tracer.end_span(_span, status="error")
            raise

    def _resolve(self, entry: str, skill_dir: Path | None = None) -> Any:
        """Lazily resolve ``pkg.mod:func`` → the function object.

        Caches per-entry; importlib + getattr cost is measurable when
        a skill is invoked hundreds of times in a long-running worker.
        Validation of the ``:`` shape happens at SkillSpec parse time
        so by the time we get here it's already well-formed.

        ``skill_dir`` is the directory containing the skill's files
        (e.g. ``/app/agents/skills/kb-lookup/``). When provided, its
        parent is prepended to ``sys.path`` if not already present,
        enabling ``importlib.import_module('kb-lookup.impl')`` to
        resolve ``/app/agents/skills/kb-lookup/impl.py``. Python
        namespace packages (PEP 420) handle the hyphen in the
        directory name — the ``import`` *statement* would reject a
        hyphenated name, but ``importlib.import_module`` works fine.

        The ``sys.path`` entry is added once and persists for the
        process lifetime; repeated calls for the same parent are
        no-ops due to the membership check.
        """
        if entry in self._resolved:
            return self._resolved[entry]
        module_name, attr_name = entry.split(":", 1)
        if skill_dir is not None:
            parent = str(skill_dir.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"can't import {module_name!r}: {exc}",
            ) from exc
        if not hasattr(module, attr_name):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"module {module_name!r} has no attribute {attr_name!r}",
            )
        func = getattr(module, attr_name)
        if not callable(func):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"{entry!r} is not callable",
            )
        self._resolved[entry] = func
        return func


# Auto-register on import. CLI + executor both import this module from
# their _runtime initialization paths, so by the time any skill is
# dispatched the backend is wired up.
register_backend(PythonSkillBackend())


# Keep the loop visible to type checkers; not used at runtime here but
# referenced by tests that need a fresh event loop helper.
_ = asyncio  # silence "imported but unused" if no test references asyncio
