"""``mdk guardrails`` — manage + test the Safe-AI guardrails (Phase J-0).

Companion CLI to the engine shipped in PR #8 (Phase J-0). Without
this wrapper, an operator who wants to test or toggle guardrails has
to hand-edit ``movate.yaml`` — clumsy and demo-unfriendly. With the
wrapper, the same workflow is:

  $ mdk guardrails test "leak: jane@acme.com"      # dry-run a string
  $ mdk guardrails list                            # show current config
  $ mdk guardrails enable input.pii                # toggle a module on
  $ mdk guardrails disable output.content          # toggle a module off

Subcommands operate on the project's ``movate.yaml`` (or
``policy.yaml``) — same precedence the executor already uses. Writes
are minimal-diff (PyYAML round-trip preserves existing keys / order
where it can; un-touched sections are left as-is).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from movate.core.config import GuardrailsConfig, load_project_config
from movate.guardrails import GuardrailVerdict, check_input, check_output

console = Console()
err_console = Console(stderr=True)


guardrails_app = typer.Typer(
    name="guardrails",
    help=(
        "Manage + test the Safe-AI guardrails (PII / topic / content) "
        "configured in [bold]movate.yaml: guardrails:[/bold]. The engine "
        "ships in v0.7 (Phase J-0); this command surfaces it ergonomically."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Allowed direction.module paths for enable/disable
# ---------------------------------------------------------------------------

# ``<direction>.<module>`` paths the operator can toggle. Stored as a
# constant so a typo (``mdk guardrails enable inpit.pii``) surfaces as
# a clean error with the valid set listed.
_VALID_PATHS: tuple[str, ...] = (
    "input.pii",
    "input.topic",
    "input.content",
    "output.pii",
    "output.topic",
    "output.content",
)


# ---------------------------------------------------------------------------
# Subcommand: test
# ---------------------------------------------------------------------------


@guardrails_app.command("test")
def test(
    text: str = typer.Argument(
        ...,
        help=(
            "Text to dry-run against the configured guardrails. The "
            "text is NOT sent to any provider — this is a pure local "
            "check using the regex / substring engines."
        ),
    ),
    direction: str = typer.Option(
        "input",
        "--direction",
        "-d",
        help=(
            "Which direction's config to test against: ``input`` "
            "(default — checks what the agent would see before "
            "calling the model) or ``output`` (what the model's "
            "response would go through before reaching the caller)."
        ),
    ),
) -> None:
    """Dry-run a string against the configured guardrails.

    [bold]Examples:[/bold]

      [dim]# Test an obvious PII leak (input direction)[/dim]
      $ mdk guardrails test "reach me at jane@example.com"

      [dim]# Same string, output direction[/dim]
      $ mdk guardrails test "...sensitive..." --direction output

      [dim]# Topic restriction check[/dim]
      $ mdk guardrails test "tell me about Apple products"
    """
    if direction not in {"input", "output"}:
        err_console.print(
            f"[red]✗[/red] --direction must be 'input' or 'output' (got {direction!r})"
        )
        raise typer.Exit(code=2)

    cfg = load_project_config()
    guardrails = cfg.guardrails
    direction_cfg = guardrails.input if direction == "input" else guardrails.output

    if direction_cfg.is_permissive():
        console.print(
            f"[yellow]⚠[/yellow] no guardrails enabled for direction "
            f"[bold]{direction}[/bold]. Configure them in [bold]movate.yaml: "
            f"guardrails.{direction}[/bold] or use "
            f"[bold]mdk guardrails enable {direction}.<module>[/bold]."
        )
        raise typer.Exit(code=0)

    verdict = (
        check_input(text, direction_cfg)
        if direction == "input"
        else check_output(text, direction_cfg)
    )
    _emit_verdict(verdict, text=text, direction=direction)
    # Exit non-zero when the verdict would BLOCK the request — useful
    # for CI scenarios (`mdk guardrails test "$(cat input.txt)" || exit`).
    if verdict.action == "block":
        raise typer.Exit(code=1)


def _emit_verdict(verdict: GuardrailVerdict, *, text: str, direction: str) -> None:
    """Render a :class:`GuardrailVerdict` as a friendly Rich panel.

    Color-coded by action (block=red, redact=yellow, warn=yellow,
    allow=green) so the operator scans the result in one glance.
    """
    style = {
        "allow": "green",
        "warn": "yellow",
        "redact": "yellow",
        "block": "red",
    }[verdict.action]
    icon = {
        "allow": "✓",
        "warn": "⚠",
        "redact": "✂",
        "block": "✗",
    }[verdict.action]

    console.print()
    console.print(
        f"[bold]{direction.title()} guardrails verdict:[/bold] "
        f"[{style}]{icon} {verdict.action.upper()}[/{style}]"
    )
    if verdict.triggered_by:
        console.print(f"  triggered by: [bold]{', '.join(verdict.triggered_by)}[/bold]")
    if verdict.reason:
        console.print(f"  reason: [dim]{verdict.reason}[/dim]")
    if verdict.matched_terms:
        console.print(f"  matched terms: [dim]{list(verdict.matched_terms)}[/dim]")
    if verdict.action == "redact" and verdict.redacted_text is not None:
        console.print()
        console.print("[bold]Original input:[/bold]")
        console.print(f"  [dim]{text}[/dim]")
        console.print("[bold]Redacted output (what the model would see):[/bold]")
        console.print(f"  [green]{verdict.redacted_text}[/green]")


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


@guardrails_app.command("list")
def list_(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table.",
    ),
) -> None:
    """Show the currently-configured guardrails per direction + module."""
    import json as _json  # noqa: PLC0415  -- only on json-output path

    cfg = load_project_config()
    g = cfg.guardrails

    if json_output:
        console.print_json(_json.dumps(g.model_dump(mode="json")))
        return

    table = Table(title="Configured guardrails", title_style="bold")
    table.add_column("Path", style="cyan", no_wrap=True)
    table.add_column("Enabled", no_wrap=True)
    table.add_column("Mode / action", style="dim", no_wrap=True)
    table.add_column("Detail", style="dim")

    for direction_name, direction_cfg in (("input", g.input), ("output", g.output)):
        for module_name, module_cfg in (
            ("pii", direction_cfg.pii),
            ("topic", direction_cfg.topic),
            ("content", direction_cfg.content),
        ):
            enabled = "[green]✓[/green]" if module_cfg.enabled else "[dim]✗[/dim]"
            mode = _module_mode(module_name, module_cfg)
            detail = _module_detail(module_name, module_cfg)
            table.add_row(f"{direction_name}.{module_name}", enabled, mode, detail)

    console.print(table)

    if g.is_permissive():
        console.print()
        console.print(
            "[dim]All guardrails disabled. Enable one with "
            "[bold]mdk guardrails enable <direction>.<module>[/bold] "
            "or edit movate.yaml: guardrails.[/dim]"
        )


def _module_mode(module_name: str, module_cfg: object) -> str:
    """Surface the per-module action mode for the list table.

    PII has a tri-state ``mode`` (redact/block/warn); topic + content
    have an ``on_violation`` binary (block/warn). Returns ``"-"`` for
    disabled modules so the column stays visually flat.
    """
    if not module_cfg.enabled:  # type: ignore[attr-defined]
        return "-"
    if module_name == "pii":
        return module_cfg.mode  # type: ignore[attr-defined, no-any-return]
    return module_cfg.on_violation  # type: ignore[attr-defined, no-any-return]


def _module_detail(module_name: str, module_cfg: object) -> str:
    """One-line detail string for the list table — what's actually
    configured under this module (types, term counts, etc).
    """
    if not module_cfg.enabled:  # type: ignore[attr-defined]
        return ""
    if module_name == "pii":
        types = list(module_cfg.types) or ["(all)"]  # type: ignore[attr-defined]
        return f"types: {', '.join(types)}"
    if module_name == "topic":
        a = len(module_cfg.allowed_topics)  # type: ignore[attr-defined]
        b = len(module_cfg.banned_topics)  # type: ignore[attr-defined]
        return f"{a} allowed, {b} banned"
    if module_name == "content":
        n = len(module_cfg.banned_terms)  # type: ignore[attr-defined]
        return f"{n} banned term(s)"
    return ""


# ---------------------------------------------------------------------------
# Subcommands: enable + disable
# ---------------------------------------------------------------------------


@guardrails_app.command("enable")
def enable(
    path: str = typer.Argument(
        ...,
        help=(
            "``<direction>.<module>`` to enable. Valid paths: "
            "input.pii, input.topic, input.content, output.pii, "
            "output.topic, output.content."
        ),
    ),
    config_file: Path = typer.Option(
        Path("movate.yaml"),
        "--config",
        "-c",
        help="Project config file to update. Defaults to ./movate.yaml.",
    ),
) -> None:
    """Flip a guardrail's ``enabled: true`` in the project config.

    Minimal-diff write — only the targeted module's ``enabled`` flag
    changes; all other config is preserved. The change is written to
    disk; the executor picks it up on the next ``mdk run``.

    Doesn't fill in surrounding fields (``rubric``, ``banned_terms``,
    etc.) — those are operator decisions; we just flip the bit. After
    enable, run [bold]mdk guardrails list[/bold] to see what's missing.
    """
    _toggle(path, enabled=True, config_file=config_file)


@guardrails_app.command("disable")
def disable(
    path: str = typer.Argument(
        ...,
        help="``<direction>.<module>`` to disable.",
    ),
    config_file: Path = typer.Option(
        Path("movate.yaml"),
        "--config",
        "-c",
        help="Project config file to update. Defaults to ./movate.yaml.",
    ),
) -> None:
    """Flip a guardrail's ``enabled: false`` in the project config.

    Same minimal-diff semantics as ``enable``. Other config under the
    module (types, banned_terms, etc.) is preserved — re-enabling
    later restores the previous state without reconfiguration.
    """
    _toggle(path, enabled=False, config_file=config_file)


def _toggle(path: str, *, enabled: bool, config_file: Path) -> None:
    """Read config, flip enabled, write back. Validates the resulting
    config through :class:`GuardrailsConfig` so an invalid toggle
    (e.g. ``input.bogus``) fails loud instead of silently corrupting
    the YAML.
    """
    if path not in _VALID_PATHS:
        err_console.print(f"[red]✗[/red] invalid path {path!r}; valid: {list(_VALID_PATHS)}")
        raise typer.Exit(code=2)

    direction_key, module_key = path.split(".", 1)
    config_path = config_file.resolve()

    raw: dict[str, Any] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            err_console.print(
                f"[red]✗[/red] {config_path} root must be a mapping; got {type(loaded).__name__}"
            )
            raise typer.Exit(code=2)
        raw = loaded

    # Navigate to guardrails.<direction>.<module>, creating intermediate
    # nodes as we go. The operator doesn't have to scaffold the whole
    # block up front.
    guardrails_block = raw.setdefault("guardrails", {})
    direction_block = guardrails_block.setdefault(direction_key, {})
    module_block = direction_block.setdefault(module_key, {})
    module_block["enabled"] = enabled

    # Validate through GuardrailsConfig so a malformed surrounding
    # block (e.g. typo in another field) surfaces here rather than at
    # the next executor invocation.
    try:
        GuardrailsConfig.model_validate(raw["guardrails"])
    except Exception as exc:
        err_console.print(f"[red]✗[/red] resulting guardrails config invalid: {exc}")
        raise typer.Exit(code=2) from exc

    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    verb = "enabled" if enabled else "disabled"
    console.print(f"[green]✓[/green] {path} {verb} in [bold]{config_path}[/bold]")
