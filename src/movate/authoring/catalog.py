"""The authoring action catalog — a ``name -> action`` registry (ADR 025 D1).

This is the single source of truth all three future surfaces share
(conversational ``mdk dev``, the ``AGENTS.md`` CLI, the MCP server). It is
self-describing: :func:`describe_catalog` serializes every action's metadata +
arg schema so the planner system prompt / MCP tool manifest is generated, not
hand-maintained — the lesson this codebase keeps relearning (one shared path,
no drift).

Actions register themselves at import time via :func:`register`; importing this
module imports the :mod:`movate.authoring.actions` package, which populates the
registry as a side effect.
"""

from __future__ import annotations

from typing import Any

from movate.authoring.base import AuthoringAction, self_description

_REGISTRY: dict[str, AuthoringAction] = {}


class UnknownActionError(KeyError):
    """Raised when a requested action name is not in the catalog."""


def register(action: AuthoringAction) -> AuthoringAction:
    """Register ``action`` under its ``name``. Returns it (usable as a hook).

    Raises ``ValueError`` on a duplicate name so two actions can't silently
    shadow each other — a registration collision is a programming error.
    """
    if action.name in _REGISTRY:
        raise ValueError(f"duplicate authoring action name: {action.name!r}")
    _REGISTRY[action.name] = action
    return action


def get_action(name: str) -> AuthoringAction:
    """Look up an action by name. Raises :class:`UnknownActionError` if absent."""
    _ensure_loaded()
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise UnknownActionError(f"unknown authoring action {name!r}; known: {known}") from None


def list_actions() -> list[AuthoringAction]:
    """Every registered action, sorted by name (deterministic ordering)."""
    _ensure_loaded()
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def action_names() -> list[str]:
    """Sorted list of registered action names."""
    _ensure_loaded()
    return sorted(_REGISTRY)


def describe_catalog() -> list[dict[str, Any]]:
    """Serialize the whole catalog (name, description, side effects, arg schema).

    The self-describing manifest PR3's planner + PR4's MCP server consume.
    """
    return [self_description(a) for a in list_actions()]


def _ensure_loaded() -> None:
    """Import the actions package so all actions self-register.

    Idempotent: a second import is a no-op. Kept lazy (not a top-level import)
    to avoid a circular import — the actions import :mod:`catalog.register`.
    """
    if not _REGISTRY:
        import movate.authoring.actions  # noqa: F401, PLC0415  (import for side effects)
