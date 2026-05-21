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

# ``AuthContext`` is lightweight (no FastAPI dep) — import eagerly so
# handlers can do ``from movate.runtime import AuthContext``.
from movate.runtime.middleware import AuthContext

# ``build_app`` is NOT imported eagerly — it pulls in FastAPI which is
# an optional [runtime] dep. Any code that actually needs build_app
# (i.e. ``mdk serve``) imports it directly from ``movate.runtime.app``.
# Lazy __getattr__ preserves the public API for callers that do
# ``from movate.runtime import build_app`` without forcing the import
# at package-load time (which would break non-serve commands like
# ``mdk kb ingest`` when the [runtime] extra isn't installed).

__all__ = ["AuthContext", "build_app"]


def __getattr__(name: str) -> object:
    if name == "build_app":
        from movate.runtime.app import build_app  # noqa: PLC0415
        return build_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
