"""``mdk policy export | import`` — round-trip policy.yaml across environments.

Three subcommands:

* ``mdk policy export`` — read the active policy.yaml from cwd, normalize
  it (drop comments, sort keys deterministically, fill defaults), and
  emit it as YAML on stdout (or to ``--output <path>``).
* ``mdk policy import <file>`` — read a policy doc from a file (JSON or
  YAML), validate it against :class:`ProjectConfig`, and write it back
  to ``policy.yaml`` (or wherever ``--target`` points). Use this in
  promotion pipelines to copy a vetted policy from one env to another.
* ``mdk policy diff <file>`` — compare ``<file>`` against the active
  policy.yaml, showing what would change if you imported. Read-only —
  doesn't touch disk. Promotion safety check before ``import``.

The export format is deterministic: sorted keys, no comments, no
defaults that match the empty :class:`ProjectConfig`. The import
respects the same shape — round-tripping ``export | import`` produces
the same file on disk modulo whitespace.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from movate.core.config import ProjectConfig, load_project_config

console = Console()
err_console = Console(stderr=True)


policy_app = typer.Typer(
    name="policy",
    help=(
        "Export / import / diff project policy.yaml — promote a vetted "
        "policy from dev → staging → prod without copy-paste."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


class _PolicyIOError(Exception):
    """Raised on missing files or unparseable input. Bubbles up to the
    Typer entry point which prints a friendly error + exits 2."""


def _serialize(config: ProjectConfig, *, fmt: str) -> str:
    """Render ``config`` as the requested format, deterministically.

    ``exclude_defaults`` strips fields equal to their Pydantic default
    so the exported document only contains operator-set values — the
    diff between two exports stays signal-only without noise from
    default-valued fields appearing on every line.
    """
    payload = config.model_dump(mode="json", exclude_defaults=True, by_alias=True)
    if fmt == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    # YAML default. sort_keys=True for deterministic output across runs;
    # default_flow_style=False so the result reads like hand-written YAML.
    return yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)


def _read_policy_file(path: Path) -> dict[str, Any]:
    """Parse a policy doc from disk as either JSON or YAML.

    We sniff by extension first (``.json`` vs anything else), then fall
    back to YAML — which is a superset of JSON, so YAML parsing of a
    JSON file works either way. The strict checking is at the
    Pydantic step.
    """
    if not path.exists():
        raise _PolicyIOError(f"policy file not found: {path}")
    text = path.read_text()
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise _PolicyIOError(f"failed to parse {path}: {exc}") from exc
    if data is None:
        # Empty file — treat as empty document.
        return {}
    if not isinstance(data, dict):
        raise _PolicyIOError(f"{path} must contain a top-level object; got {type(data).__name__}")
    return data


def _validate(data: dict[str, Any]) -> ProjectConfig:
    """Validate a raw dict against :class:`ProjectConfig`. Surfaces
    validation errors with the offending field paths so the operator
    sees ``defaults.model.params`` rather than a stack trace."""
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise _PolicyIOError(f"policy validation failed:\n{exc}") from exc


# ---------------------------------------------------------------------------
# Subcommand: export
# ---------------------------------------------------------------------------


@policy_app.command("export")
def export(
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write the exported policy to this file instead of stdout. "
            "File extension determines the format if --format isn't passed "
            "(``.json`` ⇒ json, anything else ⇒ yaml)."
        ),
    ),
    fmt: str = typer.Option(
        None,
        "--format",
        "-f",
        help=(
            "Output format: 'yaml' (default) or 'json'. Inferred from "
            "--output's extension when both are passed and --format isn't."
        ),
    ),
) -> None:
    """Emit the active policy.yaml as deterministic YAML or JSON.

    Reads ``policy.yaml`` from cwd (or ``movate.yaml`` for the legacy
    name), normalizes it (sorted keys, defaults stripped), and writes
    to stdout — or to ``--output <path>`` for piping into a vault /
    git-commit / cross-env promotion script.

    [bold]Examples:[/bold]

      [dim]# Print the active policy to stdout[/dim]
      $ mdk policy export

      [dim]# Promote dev's policy to staging by exporting it to JSON[/dim]
      $ mdk policy export -o /tmp/dev-policy.json --format json

      [dim]# Drop into a release artifact[/dim]
      $ mdk policy export -o release/policy.yaml
    """
    # Format resolution: explicit --format wins, else infer from --output
    # extension, else default to yaml.
    resolved_fmt = (fmt or _infer_format(output) or "yaml").lower()
    if resolved_fmt not in ("yaml", "json"):
        err_console.print(
            f"[red]✗[/red] unsupported --format {resolved_fmt!r}; use 'yaml' or 'json'"
        )
        raise typer.Exit(code=2)

    config = load_project_config()
    rendered = _serialize(config, fmt=resolved_fmt)

    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    console.print(f"[green]✓[/green] wrote {len(rendered)} bytes to {output}")


def _infer_format(output: Path | None) -> str | None:
    """Map a --output file extension to a format. Returns None when no
    --output was passed so the caller falls through to the explicit
    --format option or the default."""
    if output is None:
        return None
    if output.suffix.lower() == ".json":
        return "json"
    if output.suffix.lower() in (".yaml", ".yml"):
        return "yaml"
    return None


# ---------------------------------------------------------------------------
# Subcommand: import
# ---------------------------------------------------------------------------


@policy_app.command("import")
def import_(
    source: Path = typer.Argument(..., help="Policy file to import (JSON or YAML)."),
    target: Path = typer.Option(
        Path("policy.yaml"),
        "--target",
        "-t",
        help=(
            "Path to write the imported policy. Defaults to policy.yaml "
            "in cwd. Use --target to write to a non-standard location "
            "(e.g. an alternate environment's checked-in policy file)."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite ``--target`` if it already exists (default refuses).",
    ),
) -> None:
    """Read a policy doc and write it to policy.yaml.

    Validates against :class:`ProjectConfig` before writing, so a
    malformed input never overwrites a working policy. Refuses to
    overwrite an existing target unless ``--force`` is passed.

    [bold]Examples:[/bold]

      [dim]# Adopt a policy exported from another environment[/dim]
      $ mdk policy import /tmp/dev-policy.json

      [dim]# Overwrite the current policy.yaml with a new one[/dim]
      $ mdk policy import release/policy.yaml --force

      [dim]# Stage a policy at a non-standard path[/dim]
      $ mdk policy import shared-policy.yaml -t infra/staging-policy.yaml
    """
    try:
        data = _read_policy_file(source)
    except _PolicyIOError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        config = _validate(data)
    except _PolicyIOError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    if target.exists() and not force:
        err_console.print(f"[red]✗[/red] {target} already exists; pass --force to overwrite")
        raise typer.Exit(code=2)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_serialize(config, fmt="yaml"))
    console.print(f"[green]✓[/green] imported policy to {target}")


# ---------------------------------------------------------------------------
# Subcommand: diff
# ---------------------------------------------------------------------------


@policy_app.command("diff")
def diff(
    source: Path = typer.Argument(
        ...,
        help="Candidate policy file to compare against the active policy.yaml.",
    ),
) -> None:
    """Show what would change if you imported ``source``.

    Reads the active policy.yaml from cwd, reads ``source``, and prints
    a side-by-side line diff of their normalized YAML representations.
    Read-only — nothing on disk changes. Run this in a promotion
    pipeline as a safety check before ``mdk policy import``.

    Exits 0 if the documents match (including when both are absent /
    empty), 1 if they differ. Useful in CI: ``mdk policy diff
    release/policy.yaml`` fails the build when prod has drifted from
    what's checked in.
    """
    try:
        candidate_raw = _read_policy_file(source)
    except _PolicyIOError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Validate both sides so the diff compares apples-to-apples — same
    # normalization (defaults stripped, keys sorted) on both.
    try:
        candidate = _validate(candidate_raw)
    except _PolicyIOError as exc:
        err_console.print(f"[red]✗ candidate policy invalid:[/red] {exc}")
        raise typer.Exit(code=2) from None

    active = load_project_config()
    candidate_yaml = _serialize(candidate, fmt="yaml")
    active_yaml = _serialize(active, fmt="yaml")

    if candidate_yaml == active_yaml:
        console.print("[green]✓[/green] policies are identical")
        return

    import difflib  # noqa: PLC0415

    diff_lines = list(
        difflib.unified_diff(
            active_yaml.splitlines(keepends=True),
            candidate_yaml.splitlines(keepends=True),
            fromfile="active (policy.yaml)",
            tofile=f"candidate ({source})",
            n=3,
        )
    )
    sys.stdout.write("".join(diff_lines))
    raise typer.Exit(code=1)
