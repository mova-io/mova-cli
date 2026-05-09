"""``movate serve`` — run the FastAPI runtime."""

from __future__ import annotations

import typer

from movate.cli._stub import not_yet_implemented


def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
) -> None:
    """Start the movate FastAPI runtime."""
    _ = (host, port)
    not_yet_implemented("serve", "v0.5")
