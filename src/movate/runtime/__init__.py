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

from typing import TYPE_CHECKING, Any

# Lazy re-exports via PEP 562 ``__getattr__``. Importing
# ``movate.runtime.app`` pulls FastAPI; importing
# ``movate.runtime.middleware`` does not. Pre-lazy, even ``movate
# --help`` would crash if the ``[serve]`` extra wasn't installed
# because the CLI transitively imported this module (via worker's
# ``from movate.runtime.dispatch import …``, whose package init
# ran ``from movate.runtime.app import build_app``).
#
# The public API is unchanged: ``from movate.runtime import build_app``
# still works, but the import only fires when the attribute is
# accessed — i.e. when the operator actually runs ``movate serve``.

if TYPE_CHECKING:  # pragma: no cover - type-checker only
    from movate.runtime.app import build_app
    from movate.runtime.middleware import AuthContext

__all__ = ["AuthContext", "build_app"]


def __getattr__(name: str) -> Any:
    if name == "build_app":
        from movate.runtime.app import build_app  # noqa: PLC0415

        return build_app
    if name == "AuthContext":
        from movate.runtime.middleware import AuthContext  # noqa: PLC0415

        return AuthContext
    raise AttributeError(f"module 'movate.runtime' has no attribute {name!r}")
