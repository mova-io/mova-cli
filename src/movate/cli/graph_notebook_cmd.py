"""``mdk graph notebook`` — generate a ready-to-run KB-graph notebook.

The data-science / Jupyter viewer option in the graph-viz bake-off. This
command writes (or prints) a ``.ipynb`` that wires up the
:mod:`movate.graph.notebook` ipysigma helper against the operator's
resolved target + project, so an analyst can open it and immediately
explore the knowledge graph interactively.

Distinct from the other viewer options by design:

* ``notebook`` (this command) — generates a Jupyter notebook (ipysigma).
* (future) sigma ``serve`` — a standalone sigma.js web server.
* (future) dash ``serve-dash`` — a Dash app.
* (future) pyvis ``export`` — a static HTML file.

Security
--------

The generated notebook reads the API key from
``os.environ["MOVATE_API_KEY"]`` at runtime. The key is **never** written
into the ``.ipynb`` file — only the env-var *name* and a placeholder
reminder appear. Everything goes through the graph API; no direct storage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from movate.cli._console import error, hint
from movate.core.user_config import (
    UserConfigError,
    resolve_target,
)

console = Console()
err_console = Console(stderr=True)

graph_app = typer.Typer(
    name="graph",
    help=(
        "Explore the knowledge graph. [bold]graph notebook[/bold] generates a "
        "ready-to-run Jupyter notebook (ipysigma) for interactive exploration."
    ),
    no_args_is_help=True,
)

# The env var the GENERATED notebook reads its bearer token from. Never
# the value — only this name is written into the file.
_API_KEY_ENV = "MOVATE_API_KEY"

_DEFAULT_OUTPUT = "explore-graph.ipynb"


def build_notebook(*, base_url: str, project_id: str, target: str) -> dict[str, Any]:
    """Build the nbformat-v4 notebook structure (a plain dict).

    Hand-constructed (no ``nbformat`` dependency) so this works on a core
    install. The shape matches the nbformat v4 schema: a top-level
    ``nbformat``/``nbformat_minor``/``metadata``/``cells`` envelope with
    ``markdown`` and ``code`` cells.

    SECURITY: the bearer token is read at notebook-runtime from
    ``os.environ[%r]`` — only the env-var NAME and a placeholder hint are
    embedded; the key value is never written into the returned structure.
    """
    intro_md = [
        "# Explore the knowledge graph\n",
        "\n",
        "Interactive viewer (ipysigma) for the Movate knowledge graph.\n",
        "\n",
        f"- **Target:** `{target}`\n",
        f"- **Project:** `{project_id}`\n",
        f"- **API base URL:** `{base_url}`\n",
        "\n",
        "## Setup\n",
        "\n",
        "Install the optional extra and set your API key **in the shell"
        " before launching Jupyter** (the key is never stored in this file):\n",
        "\n",
        "```bash\n",
        "pip install 'movate-cli[graph-notebook]'\n",
        f"export {_API_KEY_ENV}='<your-bearer-token>'\n",
        "```\n",
    ]

    # The api key is resolved from the environment at runtime — NOT baked in.
    setup_code = [
        "import os\n",
        "\n",
        "from movate.graph.notebook import load_graph, node_detail, show_graph\n",
        "\n",
        f"BASE_URL = {base_url!r}\n",
        f"TARGET = {target!r}\n",
        f"PROJECT_ID = {project_id!r}\n",
        "\n",
        "# The bearer token is read from the environment at runtime; it is\n",
        "# never written into this notebook file. Set it before launching:\n",
        f"#   export {_API_KEY_ENV}='<your-bearer-token>'\n",
        f"API_KEY = os.environ[{_API_KEY_ENV!r}]\n",
    ]

    load_code = [
        "graph = load_graph(\n",
        "    TARGET,\n",
        "    PROJECT_ID,\n",
        "    base_url=BASE_URL,\n",
        "    api_key=API_KEY,\n",
        ")\n",
        "print(f'loaded {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges')\n",
    ]

    show_code = [
        "# Interactive sigma.js widget: color by `type`, size by degree,\n",
        "# label from `label`, edge thickness from `weight`. Click a node,\n",
        "# then drill in with node_detail(<id>) below.\n",
        "show_graph(graph)\n",
    ]

    detail_md = [
        "## Drill into a node\n",
        "\n",
        "Copy a node id from the widget and fetch its full properties / provenance:\n",
    ]

    detail_code = [
        "# node_detail('<node-id>', base_url=BASE_URL, api_key=API_KEY)\n",
    ]

    def _code_cell(source: list[str]) -> dict[str, Any]:
        return {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": source,
        }

    def _md_cell(source: list[str]) -> dict[str, Any]:
        return {"cell_type": "markdown", "metadata": {}, "source": source}

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
            "movate": {
                "generated_by": "mdk graph notebook",
                "target": target,
                "project_id": project_id,
                "api_key_env": _API_KEY_ENV,
            },
        },
        "cells": [
            _md_cell(intro_md),
            _code_cell(setup_code),
            _code_cell(load_code),
            _code_cell(show_code),
            _md_cell(detail_md),
            _code_cell(detail_code),
        ],
    }


# Bind the env-var name into the docstring's %r placeholder once at import.
build_notebook.__doc__ = (build_notebook.__doc__ or "") % _API_KEY_ENV


@graph_app.command("notebook")
def notebook(
    project: str = typer.Option(
        ...,
        "--project",
        "-p",
        help="Project id whose knowledge graph to explore.",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target/env (resolves the API base URL from your "
            "config). Falls back to the active target."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write the notebook here (default: ./explore-graph.ipynb). "
            "Pass '-' to print the notebook JSON to stdout instead."
        ),
    ),
) -> None:
    """Generate a ready-to-run Jupyter notebook for graph exploration.

    Wires the [bold]movate.graph.notebook[/bold] ipysigma helper against
    your resolved target + project. The notebook reads your API key from
    [bold]$MOVATE_API_KEY[/bold] at runtime — the key is never written
    into the file.

    [bold]Examples:[/bold]

      [dim]# Default output: ./explore-graph.ipynb against the active target[/dim]
      $ mdk graph notebook --project my-kb

      [dim]# Against prod, to a custom path[/dim]
      $ mdk graph notebook --project my-kb -t prod -o kb-explore.ipynb

      [dim]# Print the notebook JSON (e.g. to pipe into a tool)[/dim]
      $ mdk graph notebook --project my-kb -o -
    """
    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    nb = build_notebook(
        base_url=target_cfg.url,
        project_id=project,
        target=target_name,
    )
    # Trailing newline matches Jupyter's own on-disk convention.
    nb_json = json.dumps(nb, indent=1, ensure_ascii=False) + "\n"

    # `-o -` prints to stdout (snippet mode) rather than writing a file.
    if output is not None and str(output) == "-":
        console.print(nb_json, soft_wrap=True, highlight=False)
        return

    dest = output if output is not None else Path(_DEFAULT_OUTPUT)
    dest.write_text(nb_json, encoding="utf-8")

    console.print(f"[green]✓[/green] wrote {dest}")
    hint(
        "Install the viewer extra and set your key, then open the notebook:\n"
        "  pip install 'movate-cli[graph-notebook]'\n"
        f"  export {_API_KEY_ENV}='<your-bearer-token>'\n"
        f"  jupyter lab {dest}"
    )
