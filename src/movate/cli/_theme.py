"""Shared visual idioms for the movate CLI.

One source of truth for colors, icons, and the recurring Rich constructs
(verdict panels, key/value summary tables). Subcommand modules import
from here so the look stays consistent and a single edit retunes the
whole CLI.

Conventions:

* **Status colors** — green=success, yellow=warning, red=failure.
* **Icons** — single Unicode glyph each; ASCII fallbacks not provided
  because every modern terminal we target handles them. If that
  changes, swap at the constants only.
* **Panels** — always rounded, padded (1, 2), title left-aligned, border
  color matched to status. The title carries the icon + name + version
  so the panel reads as one verdict line.
* **kv_table** — two columns, dim right-aligned label + value. No box,
  small padding. Rich handles label alignment automatically; callers
  don't compute whitespace.
"""

from __future__ import annotations

from typing import Any

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---- Colors ----------------------------------------------------------------

SUCCESS = "green"
WARN = "yellow"
ERROR = "red"
ACCENT = "cyan"
DIM = "dim"

# ---- Icons -----------------------------------------------------------------

OK = "✓"
FAIL = "✗"
WARNING = "!"
INFO = "i"
ARROW = "→"
BULLET = "·"


# ---- Building blocks -------------------------------------------------------


def kv_table() -> Table:
    """Two-column key/value table — dim right-aligned labels, value second.

    Default config matches the look used in ``movate validate``: no
    outer box, generous horizontal padding, no edge pad. Caller adds
    rows with ``add_row(label, value)``.
    """
    t = Table(show_header=False, box=None, padding=(0, 2), pad_edge=False)
    t.add_column(style=DIM, no_wrap=True, justify="right")
    t.add_column(no_wrap=False)
    return t


def verdict_title(
    *,
    icon: str,
    icon_color: str,
    name: str,
    version: str | None = None,
    kind: str = "",
) -> Text:
    """Build the standard verdict-title Rich Text.

    Looks like ``✓ faq-agent  v0.1.0  ·  agent`` with the icon
    colored, name bold, version + kind dim. ``version`` and ``kind``
    are optional — omit either to drop that segment.
    """
    parts: list[tuple[str, str]] = [
        (f"{icon} ", f"bold {icon_color}"),
        (name, "bold"),
    ]
    if version:
        parts.append(("  v", DIM))
        parts.append((version, DIM))
    if kind:
        parts.append((f"  {BULLET}  {kind}", DIM))
    return Text.assemble(*parts)


def _status_panel(body: Any, *, title: Text, color: str) -> Panel:
    """Internal: rounded panel with status-colored border + left-aligned title."""
    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=color,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def success_panel(
    body: Any,
    *,
    name: str,
    version: str | None = None,
    kind: str = "",
) -> Panel:
    """Green-bordered verdict panel: ``✓ name vX · kind``."""
    title = verdict_title(
        icon=OK, icon_color=SUCCESS, name=name, version=version, kind=kind
    )
    return _status_panel(body, title=title, color=SUCCESS)


def warn_panel(
    body: Any,
    *,
    name: str,
    version: str | None = None,
    kind: str = "",
) -> Panel:
    """Yellow-bordered verdict panel: ``! name · kind``."""
    title = verdict_title(
        icon=WARNING, icon_color=WARN, name=name, version=version, kind=kind
    )
    return _status_panel(body, title=title, color=WARN)


def error_panel(
    body: Any,
    *,
    name: str,
    version: str | None = None,
    kind: str = "",
) -> Panel:
    """Red-bordered verdict panel: ``✗ name · kind``."""
    title = verdict_title(
        icon=FAIL, icon_color=ERROR, name=name, version=version, kind=kind
    )
    return _status_panel(body, title=title, color=ERROR)


# ---- Inline status badges (used inside kv rows) ----------------------------


def ok_badge(label: str) -> str:
    """Inline ``✓ label`` markup, e.g. for the ``checks`` row."""
    return f"[{SUCCESS}]{OK}[/{SUCCESS}] {label}"


def warn_badge(label: str) -> str:
    """Inline ``! label`` markup."""
    return f"[{WARN}]{WARNING}[/{WARN}] {label}"


def fail_badge(label: str) -> str:
    """Inline ``✗ label`` markup."""
    return f"[{ERROR}]{FAIL}[/{ERROR}] {label}"
