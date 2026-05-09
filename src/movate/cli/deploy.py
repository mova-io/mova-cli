"""``movate deploy`` — push a runtime image and update Azure Container Apps."""

from __future__ import annotations

import typer

from movate.cli._stub import not_yet_implemented


def deploy(
    env: str = typer.Argument(..., help="Target environment (dev, staging, prod)."),
) -> None:
    """Deploy the movate runtime to Azure Container Apps."""
    _ = env
    not_yet_implemented("deploy", "v1.0")
