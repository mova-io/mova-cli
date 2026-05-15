"""``mdk menu`` — guided next-step UX (Sprint P onboarding polish).

Inspects the workspace, renders a compact status panel (similar to
``mdk doctor`` but action-oriented), and offers 5-7 contextual
suggestions for what to do next.

The menu is **educational** — every suggestion shows the literal
command, so users learn ``mdk``'s surface area instead of getting
stuck inside an interactive prompt.

The menu is **composable** — picking an action shells out to the
real ``mdk <subcmd>`` so output matches running directly. No hidden
state, no different code path.

Public API:

    inspect_workspace(root) -> WorkspaceStatus
    build_actions(status)   -> list[Action]
"""

from __future__ import annotations

from movate.menu.actions import Action, build_actions
from movate.menu.status import (
    AgentInfo,
    EnvVarStatus,
    WorkspaceStatus,
    inspect_workspace,
)

__all__ = [
    "Action",
    "AgentInfo",
    "EnvVarStatus",
    "WorkspaceStatus",
    "build_actions",
    "inspect_workspace",
]
