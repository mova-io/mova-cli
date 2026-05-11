"""Shared ``--output`` format enums for every CLI command.

Before this module existed each command declared its own
``output_format: str = typer.Option("table", ...)`` with a bespoke
help string ("table | json", "table | json | markdown", "json | text")
and the handlers did stringly-typed comparisons (``if output_format
== "json"``). That meant:

* Typer never auto-validated the value — ``-o foo`` silently fell
  through to whatever branch had the default behaviour.
* Adding a new format required hunting through 7 modules.
* The vocabulary drifted (e.g. ``run`` used ``text`` for raw scalar
  output; everyone else used ``table`` for human summaries).

The fix: typed string enums, one per "shape" of command, used as the
Typer option type. Typer then renders the choices in ``--help``,
validates at parse time, and offers shell tab-completion of values.
The enums are ``StrEnum`` so existing ``if fmt == "json"`` comparisons
keep working unchanged (StrEnum members ``==`` their string value).

Naming convention:

* ``TableJson`` — commands that summarize one or more records for a
  human (``jobs show``, ``pricing``, ``submit``, ``trace replay``).
* ``Report`` — commands that produce a tabular *report* the user
  might paste into a CI annotation (``bench``, ``eval``). Adds
  ``markdown``.
* ``Run`` — the ``movate run`` command, which prints either the agent's
  full JSON output or just the scalar inner value. ``table`` doesn't
  apply (there's no row structure) so this enum has only ``json`` and
  ``text``.

If a future command needs a different shape, add a new enum here —
do NOT re-introduce ``output_format: str``.
"""

from __future__ import annotations

from enum import StrEnum


class TableJson(StrEnum):
    """Default for human-summary commands. ``table`` for interactive
    use; ``json`` to pipe to ``jq`` / scripts."""

    TABLE = "table"
    JSON = "json"


class Report(StrEnum):
    """``bench`` / ``eval`` — adds ``markdown`` for CI annotation
    output (GitHub Actions step summaries, PR comments)."""

    TABLE = "table"
    JSON = "json"
    MARKDOWN = "markdown"


class Run(StrEnum):
    """``movate run`` — the result IS the payload, so we default to
    JSON (pipe-friendly) and offer ``text`` to print the raw scalar
    value of a single-field output. No ``table`` because there's no
    row structure to render."""

    JSON = "json"
    TEXT = "text"


__all__ = ["Report", "Run", "TableJson"]
