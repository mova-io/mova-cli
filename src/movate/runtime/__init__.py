"""HTTP runtime — FastAPI app + auth middleware + wire schemas.

The runtime is intentionally a *thin* layer over the storage Protocol
and ``core/auth``. Nothing here re-implements business logic; the
handlers translate between HTTP wire types (``runtime/schemas.py``)
and the persisted models (``core/models.py``).

Public surface:

* :func:`build_app` — factory that returns a FastAPI app bound to a
  given storage backend. Tests pass an :class:`InMemoryStorage`;
  ``movate serve`` passes the configured :class:`SqliteProvider`.
* :class:`AuthContext` — what the auth dependency yields to handlers.

Wire schemas live separately from DB models on purpose — the API can
evolve (e.g. add ``priority`` to /run requests) without forcing a
schema migration, and vice versa.
"""

from __future__ import annotations

# Both ``build_app`` and ``AuthContext`` live in modules that import
# FastAPI, which is an optional [runtime] dep. Eagerly importing them
# here would crash every non-serve mdk command (kb ingest, eval, run,
# …) when FastAPI isn't installed — e.g. in a plain uv tool install
# that only includes core deps.
#
# Solution: __getattr__ defers the import until the name is actually
# accessed, so ``import movate.runtime`` (triggered by any
# ``from movate.runtime.schemas import …`` in the codebase) no longer
# pulls in FastAPI transitively.

__all__ = ["AuthContext", "build_app"]


def __getattr__(name: str) -> object:
    if name == "build_app":
        from movate.runtime.app import build_app  # noqa: PLC0415

        return build_app
    if name == "AuthContext":
        from movate.runtime.middleware import AuthContext  # noqa: PLC0415

        return AuthContext
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
