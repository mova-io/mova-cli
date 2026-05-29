"""``mdk judge`` — author + commit an ``evals/judge.yaml`` for an agent.

Thin CLI parity for the Judge Engineer runtime endpoints
(``POST /api/v1/agents/{name}/judge/{generate,commit}``). The CLI
never imports ``movate.runtime`` directly (``cli ⊥ runtime``,
CLAUDE.md rule 6) — it calls them over HTTP via
:class:`~movate.core.client.MovateClient` against the configured
``--target``.

Two sub-commands:

* ``mdk judge generate <agent>`` — sync; Claude authors the rubric.
  Prints the generated YAML to stdout (or ``-o <file>``). The user
  reviews, edits if desired, then runs ``mdk judge commit``.
* ``mdk judge commit <agent> --yaml <file>`` — POSTs the reviewed YAML
  to the runtime, which validates against ``JudgeConfig`` before
  persisting to ``<agent_dir>/evals/judge.yaml``.

By design, ``generate`` alone never modifies the agent. ``commit`` is
the explicit human-review gate — same pattern as ``mdk eval harvest``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from movate.cli._completion import complete_agent_name
from movate.cli._console import error, get_global_target
from movate.cli._output import TableJson
from movate.cli._progress import spinner
from movate.core.client import MovateClient, MovateClientError
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import JudgeCommitResponse, JudgeGenerateResponse

stdout = Console()
err = Console(stderr=True)

judge_app = typer.Typer(
    name="judge",
    help=(
        "Author + commit an LLM-as-judge rubric (evals/judge.yaml) for "
        "an agent. Claude reads the agent's spec and writes a rubric "
        "covering the requested (or inferred) scoring dimensions."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _parse_dimensions(raw: str | None) -> list[str] | None:
    """Split a comma-separated ``--dimensions`` flag into a list.

    ``None`` / empty → ``None`` (lets the server infer defaults).
    Whitespace around entries is stripped. Empty entries dropped.
    """
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    parts = [p for p in parts if p]
    return parts or None


@judge_app.command("generate")
def generate(
    agent: str = typer.Argument(
        ...,
        help="Agent name registered on the target runtime.",
        shell_complete=complete_agent_name,
    ),
    dimensions: str | None = typer.Option(
        None,
        "--dimensions",
        "-d",
        help=(
            "Comma-separated list of rubric dimensions (e.g. "
            "[bold]accuracy,tone,schema_adherence[/bold]). Omit to let "
            "the engineer infer a sensible set from the agent's shape."
        ),
    ),
    include_examples: bool = typer.Option(
        True,
        "--include-examples/--no-examples",
        help=(
            "Anchor the rubric with 2-3 concrete scored examples. "
            "Default on; pass [bold]--no-examples[/bold] for a leaner "
            "rubric when the agent has no dataset yet."
        ),
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help=(
            "Optional LiteLLM-style provider/model for the ENGINEER "
            "model that authors the rubric (distinct from the judge "
            "model the generated YAML uses at eval time)."
        ),
    ),
    budget_usd: float = typer.Option(
        0.10,
        "--budget-usd",
        min=0.0,
        max=10.0,
        help="Hard ceiling on the generation call's cost in USD.",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target name (from `mdk config list-targets`). "
            "Omit to use the active target."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write the generated YAML here. Use [bold]-[/bold] for stdout. "
            "Defaults to stdout — review, edit, then [bold]mdk judge "
            "commit[/bold]."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help=(
            "Use the deterministic MockProvider on the server (no API "
            "key needed). For hermetic CI + offline demos — the "
            "resulting rubric is a canned anchor, not authored from "
            "the agent's spec."
        ),
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE,
        "--format",
        "-f",
        case_sensitive=False,
        help="Summary output format (yaml body is always printed/written).",
    ),
) -> None:
    """Author a complete [bold]evals/judge.yaml[/bold] for [bold]<agent>[/bold].

    [bold]Examples:[/bold]

      [dim]# Let Claude infer dimensions; print to stdout[/dim]
      $ mdk judge generate rag-qa

      [dim]# Specific dimensions; write to file for review[/dim]
      $ mdk judge generate rag-qa -d accuracy,tone -o judge.yaml

      [dim]# Hermetic / offline (MockProvider on the server)[/dim]
      $ mdk judge generate rag-qa --mock

    [bold]This does NOT modify the agent.[/bold] Review the output, edit
    if needed, then commit with [bold]mdk judge commit[/bold].
    """
    dims_list = _parse_dimensions(dimensions)

    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    resp = asyncio.run(
        _generate(
            target_name=target_name,
            base_url=target_cfg.url,
            token=token,
            agent=agent,
            dimensions=dims_list,
            include_examples=include_examples,
            model=model,
            budget_usd=budget_usd,
            mock=mock,
        )
    )

    # Always emit the YAML body — operators pipe to a file or paste it
    # into their editor. The summary line goes to stderr so the YAML on
    # stdout can be redirected cleanly.
    if output is None or str(output) == "-":
        stdout.print(resp.judge_yaml, soft_wrap=True, highlight=False, end="")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(resp.judge_yaml, encoding="utf-8")

    _print_generate_summary(resp, output_format=output_format, wrote_to=output)


@judge_app.command("commit")
def commit(
    agent: str = typer.Argument(
        ...,
        help="Agent name registered on the target runtime.",
        shell_complete=complete_agent_name,
    ),
    yaml_file: Path = typer.Option(
        ...,
        "--yaml",
        "-y",
        help=(
            "Path to the reviewed [bold]judge.yaml[/bold] body to commit. "
            "Use [bold]-[/bold] to read from stdin."
        ),
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name. Omit to use the active target.",
    ),
) -> None:
    """Commit a reviewed [bold]judge.yaml[/bold] to the agent.

    The runtime re-validates the YAML against [bold]JudgeConfig[/bold]
    before any byte hits disk — a malformed hand edit is rejected with
    422 and the agent's existing [bold]judge.yaml[/bold] stays put.

    [bold]Example:[/bold]

      [dim]# Round-trip: generate, edit, commit[/dim]
      $ mdk judge generate rag-qa -o judge.yaml
      $ vim judge.yaml
      $ mdk judge commit rag-qa --yaml judge.yaml
    """
    import sys  # noqa: PLC0415

    if str(yaml_file) == "-":
        judge_yaml = sys.stdin.read()
    else:
        if not yaml_file.is_file():
            error(f"--yaml path {yaml_file} is not a file")
            raise typer.Exit(code=2)
        judge_yaml = yaml_file.read_text(encoding="utf-8")
    if not judge_yaml.strip():
        error("judge_yaml is empty — nothing to commit")
        raise typer.Exit(code=2)

    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    resp = asyncio.run(
        _commit(
            target_name=target_name,
            base_url=target_cfg.url,
            token=token,
            agent=agent,
            judge_yaml=judge_yaml,
        )
    )

    verb = "Updated" if resp.updated else "Created"
    stdout.print(
        f"[green]✓[/green] {verb} [bold]{resp.judge_path}[/bold] "
        f"for agent [bold]{resp.agent_name}[/bold] on target [bold]{target_name}[/bold]."
    )


# ---------------------------------------------------------------------------
# Async cores
# ---------------------------------------------------------------------------


async def _generate(
    *,
    target_name: str,
    base_url: str,
    token: str,
    agent: str,
    dimensions: list[str] | None,
    include_examples: bool,
    model: str | None,
    budget_usd: float,
    mock: bool,
) -> JudgeGenerateResponse:
    async with MovateClient(base_url=base_url, api_key=token) as client:
        try:
            with spinner(f"authoring judge.yaml on {target_name}..."):
                return await client.generate_judge(
                    agent,
                    rubric_dimensions=dimensions,
                    include_examples=include_examples,
                    model=model,
                    budget_usd=budget_usd,
                    mock=mock,
                )
        except MovateClientError as exc:
            error(str(exc), context="judge generate")
            raise typer.Exit(code=1) from None


async def _commit(
    *,
    target_name: str,
    base_url: str,
    token: str,
    agent: str,
    judge_yaml: str,
) -> JudgeCommitResponse:
    async with MovateClient(base_url=base_url, api_key=token) as client:
        try:
            with spinner(f"committing judge.yaml to {target_name}..."):
                return await client.commit_judge(agent, judge_yaml=judge_yaml)
        except MovateClientError as exc:
            error(str(exc), context="judge commit")
            raise typer.Exit(code=1) from None


def _print_generate_summary(
    resp: JudgeGenerateResponse,
    *,
    output_format: TableJson,
    wrote_to: Path | None,
) -> None:
    """Render the post-generation summary to stderr so the YAML on stdout
    can be redirected cleanly."""
    if output_format == TableJson.JSON:
        import json  # noqa: PLC0415

        err.print_json(
            json.dumps(
                {
                    "rubric_dimensions": resp.rubric_dimensions,
                    "rationale": resp.rationale,
                    "tokens_used": resp.tokens_used,
                    "cost_usd": resp.cost_usd,
                    "wrote_to": str(wrote_to) if wrote_to else None,
                }
            )
        )
        return

    where = "stdout" if wrote_to is None else f"[bold]{wrote_to}[/bold]"
    err.print(
        f"\n[green]✓[/green] Generated rubric with dimensions: "
        f"[bold]{', '.join(resp.rubric_dimensions)}[/bold] "
        f"({resp.tokens_used} tokens, ${resp.cost_usd:.4f}) → {where}"
    )
    if resp.rationale:
        err.print(f"[dim]{resp.rationale}[/dim]")
    err.print(
        "[dim]Review the YAML, then run [bold]mdk judge commit "
        "<agent> --yaml <file>[/bold] to persist it. Nothing was "
        "written to the agent yet.[/dim]"
    )


__all__ = ["judge_app"]
