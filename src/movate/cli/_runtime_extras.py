"""Friendly preflight for the ``[runtime]`` extras (Monday-demo polish).

Several CLI commands (``mdk serve``, ``mdk worker``, ...) lazily import
``uvicorn``/``fastapi`` only when invoked, so a missing ``[runtime]``
extra surfaces as a raw ``ModuleNotFoundError`` traceback — confusing
for operators who just want to know which install command to run.

This module wraps the optional-dep failure path in a small helper that:

* checks the imports the runtime commands need, and
* prints a copy-pasteable install hint via the shared Rich stderr console
  before raising ``typer.Exit(code=2)``.

Kept tiny + import-light on purpose: it's called from the hot path of
``mdk serve``/``mdk worker`` and must not itself need any optional dep.
"""

from __future__ import annotations

import importlib.util

import typer
from rich.console import Console

err = Console(stderr=True)

# Modules required by `mdk serve` (and `mdk worker`'s dispatch wiring).
# Probing rather than importing avoids any partial side effects from a
# half-installed extra and keeps the check cheap.
_REQUIRED_RUNTIME_MODULES: tuple[str, ...] = ("uvicorn", "fastapi")


def ensure_runtime_extras() -> None:
    """Verify the ``[runtime]`` extra is installed; print a hint + exit if not.

    Iterates :data:`_REQUIRED_RUNTIME_MODULES` and exits with code 2 on
    the first missing one. The message is identical regardless of which
    module is missing — they all install together via the same extra,
    so a granular per-module hint would only confuse operators.
    """
    # Rich treats ``[runtime]`` / ``[voice]`` / ``[otel]`` as markup tags
    # and silently drops them, so escape every literal extra-bracket
    # before interpolating. ``r"\[runtime]"`` renders as ``[runtime]``.
    for module_name in _REQUIRED_RUNTIME_MODULES:
        if importlib.util.find_spec(module_name) is None:
            err.print(
                r"[red]✗[/red] mdk serve requires the [bold]\[runtime][/bold] "
                "extra (uvicorn, fastapi, …)."
                "\n\nInstall with:"
                r"  [bold]uv tool install --editable '.\[runtime,playground]' --force[/bold]"
                "\n\nOr, from inside an active venv:"
                r"  [bold]uv pip install -e '.\[runtime,playground]'[/bold]"
                "\n\nThe "
                r"[bold]\[voice][/bold] and [bold]\[otel][/bold] extras are also recommended:"
                r"  [bold]uv tool install --editable '.\[runtime,playground,voice,otel]' "
                r"--force[/bold]"
            )
            raise typer.Exit(code=2)
