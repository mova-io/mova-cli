"""``mdk eval-gen <agent>`` — LLM-generate eval dataset entries (Sprint R).

Lowers the #1 barrier to using evals: "I don't have a dataset." Given
an agent and (optionally) a sample input, asks an LLM to generate N
varied test cases that match the agent's input schema, then runs the
agent on each to capture the expected output. Operator reviews the
output (it's still LLM-generated — be skeptical) and commits the file.

  $ mdk eval-gen triage --num 20
  $ mdk eval-gen triage --num 5 --sample-input '{"text": "broken order"}'
  $ mdk eval-gen triage --num 10 --output evals/triage/generated.jsonl
  $ mdk eval-gen triage --num 3 --mock     # offline / hermetic / tests

[bold]Design call — BACKLOG slot:[/bold] BACKLOG sites this as
``mdk eval gen``. We ship as ``mdk eval-gen`` (hyphenated, sibling to
``mdk eval``) because the existing ``mdk eval`` is a single command,
not a Typer sub-app. Restructuring ``eval`` to a sub-app would risk
breaking ~30 test callsites for marginal ergonomic benefit. The two
commands link to each other in help text; future Sprint R+ can
consolidate if it becomes worth the breaking-change tax.

[bold]Design call — what we generate:[/bold] each entry is
``{"input": {...}, "expected": {...}}``. The ``expected`` field is
the agent's ACTUAL response — operators review + edit if the current
agent's behavior isn't yet correct (the dataset isn't the source of
truth on day 1). We mark generated entries with
``generated: true`` so a future ``mdk eval --strict`` can distinguish
generated-vs-curated.

What we DON'T do in MVP:

* No LLM-as-judge eval grading at generation time. The generated
  expected output IS the current agent's response; quality grading
  is the operator's first manual pass.
* No deduplication. Two LLM-generated inputs may be near-duplicates.
  Future: pass through a simple similarity filter (Sprint S+).
* No coverage analysis. We don't measure "do these cases exercise
  every branch of the prompt?" — that's a deeper static-analysis
  feature for later.
"""

from __future__ import annotations

import asyncio
import json
import random
import string
from pathlib import Path
from typing import Any

import typer
from jsonschema import Draft202012Validator, ValidationError
from rich.console import Console
from rich.panel import Panel

from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import RunRequest

console = Console()
err_console = Console(stderr=True)


# Default output path under the project. Operators override with
# --output. Keeps generated files distinct from human-curated ones
# (no fighting with merge conflicts on dataset.jsonl).
_DEFAULT_OUTPUT_SUFFIX = "evals/{name}/dataset.generated.jsonl"

# Hard cap on --num to prevent accidentally launching 10k LLM calls.
_MAX_GENERATE = 200


def _resolve_agent_path(name_or_path: str, project_root: Path) -> Path:
    """Same convention as `mdk inspect agent` / `mdk tune`."""
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "agent.yaml").is_file():
        return candidate.resolve()
    by_name = project_root / "agents" / name_or_path
    if by_name.is_dir() and (by_name / "agent.yaml").is_file():
        return by_name.resolve()
    err_console.print(
        f"[red]✗[/red] agent not found: [bold]{name_or_path}[/bold]. "
        "[dim]Looked under [bold]agents/[/bold] and as a literal path.[/dim]"
    )
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Mock-mode input synthesis (no LLM call)
# ---------------------------------------------------------------------------


def _mock_input_for_schema(schema: dict[str, Any], seed: int) -> dict[str, Any]:
    """Build a placeholder input dict that satisfies ``schema``.

    Used by ``--mock`` so the test suite + offline operators get a
    real working dataset without paying for tokens. Seeded by index so
    a re-run produces the same dataset for the same N.

    Walks the JSON Schema's ``properties`` and synthesizes one value
    per declared field, honoring ``type`` only — no enum / pattern /
    minLength logic. Real prod use should go through the LLM path.
    """
    rng = random.Random(seed)
    props = schema.get("properties", {}) or {}
    required = schema.get("required") or list(props.keys())
    out: dict[str, Any] = {}
    for name in required:
        prop = props.get(name) or {}
        out[name] = _mock_value(prop, rng)
    return out


def _mock_value(prop_schema: dict[str, Any], rng: random.Random) -> Any:
    """One placeholder value per JSON-Schema type."""
    t = prop_schema.get("type", "string")
    if t == "string":
        return "sample-" + "".join(rng.choices(string.ascii_lowercase, k=6))
    if t == "integer":
        return rng.randint(1, 100)
    if t == "number":
        return round(rng.uniform(0.0, 1.0), 4)
    if t == "boolean":
        return rng.choice([True, False])
    if t == "array":
        return []
    if t == "object":
        return {}
    return "sample"


# ---------------------------------------------------------------------------
# LLM-mode input generation
# ---------------------------------------------------------------------------


_GEN_SYSTEM_PROMPT = """\
You generate VARIED test inputs for an AI agent. The agent's behavior
is described by its prompt; the input schema describes the JSON shape
the inputs must match.

Your task: produce ONE realistic input that satisfies the schema and
that an end-user might plausibly send to this agent. Vary topics,
edge cases, tone, length.

Respond with a single JSON object — NO PROSE, NO MARKDOWN, NO CODE
FENCES. Just the bare JSON object.
"""


def _gen_user_message(
    bundle: AgentBundle,
    *,
    index: int,
    sample_input: dict[str, Any] | None,
) -> str:
    """Compose the user-side message asking the LLM to generate one input."""
    schema_json = json.dumps(bundle.input_schema, indent=2)
    parts = [
        f"Agent name: {bundle.spec.name}",
        f"Agent description: {bundle.spec.description or '(none)'}",
        "",
        "Input schema (JSON Schema):",
        schema_json,
        "",
    ]
    if sample_input is not None:
        parts.append("Reference example (vary off this style, don't copy verbatim):")
        parts.append(json.dumps(sample_input))
        parts.append("")
    parts.append(
        f"Produce test input #{index + 1}. Vary topic + tone vs. previous cases. "
        "Reply with ONE JSON object that matches the schema."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Generation engine
# ---------------------------------------------------------------------------


async def _generate_entries(
    bundle: AgentBundle,
    *,
    num: int,
    sample_input: dict[str, Any] | None,
    mock: bool,
) -> list[dict[str, Any]]:
    """Build ``num`` ``{input, expected, generated: true}`` entries.

    ``mock=True`` skips the LLM entirely — synthesizes inputs from the
    schema + runs the agent under MockProvider to fill ``expected``.
    Used by tests and offline operators.
    """
    rt = await build_local_runtime(mock=mock)
    validator = Draft202012Validator(bundle.input_schema)
    entries: list[dict[str, Any]] = []
    try:
        for i in range(num):
            if mock:
                generated_input = _mock_input_for_schema(bundle.input_schema, seed=i)
            else:
                generated_input = await _generate_one_input(
                    rt, bundle, index=i, sample_input=sample_input
                )
            # Validate before running the agent — a bad-schema input
            # blows up the executor with a confusing error. Skip + log.
            try:
                validator.validate(generated_input)
            except ValidationError as exc:
                err_console.print(
                    f"[yellow]⚠[/yellow] skipped generated input #{i + 1}: "
                    f"failed schema validation ({exc.message})"
                )
                continue
            # Execute to capture the expected output. Same path
            # the real eval runs would use.
            request = RunRequest(agent=bundle.spec.name, input=generated_input)
            response = await rt.executor.execute(bundle, request)
            entries.append(
                {
                    "input": generated_input,
                    "expected": response.data,
                    "generated": True,
                }
            )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    return entries


async def _generate_one_input(
    rt: Any,
    bundle: AgentBundle,
    *,
    index: int,
    sample_input: dict[str, Any] | None,
) -> dict[str, Any]:
    """Ask the LLM for one input. Provider call goes through the
    same registry the agent uses, so OPENAI_API_KEY etc. follow the
    operator's existing setup.
    """
    from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415

    provider = rt.provider
    request = CompletionRequest(
        provider=bundle.spec.model.provider,
        messages=[
            Message(role="system", content=_GEN_SYSTEM_PROMPT),
            Message(
                role="user",
                content=_gen_user_message(bundle, index=index, sample_input=sample_input),
            ),
        ],
        params={"temperature": 0.9, "max_tokens": 512},
    )
    try:
        response = await provider.complete(request)
    except Exception as exc:
        err_console.print(
            f"[yellow]⚠[/yellow] generator LLM call failed for case #{index + 1}: {exc}"
        )
        return {}

    text = (response.text or "").strip()
    # Strip code fences if the LLM ignored our "no markdown" instruction.
    if text.startswith("```"):
        text = text.strip("`")
        # After stripping backticks, leading "json" tag is common.
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        err_console.print(
            f"[yellow]⚠[/yellow] generator output for case #{index + 1} wasn't valid JSON: {exc}"
        )
        return {}

    if not isinstance(parsed, dict):
        err_console.print(
            f"[yellow]⚠[/yellow] generator output for case #{index + 1} wasn't a JSON object"
        )
        return {}
    return parsed


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def eval_gen(
    name: str = typer.Argument(
        ...,
        help=(
            "Agent name (resolved under [bold]agents/<name>[/bold]) or a "
            "literal path to an agent directory."
        ),
        metavar="AGENT",
    ),
    num: int = typer.Option(
        10,
        "--num",
        "-n",
        help=f"How many cases to generate. Default 10; max {_MAX_GENERATE}.",
    ),
    sample_input: str = typer.Option(
        "",
        "--sample-input",
        help=(
            "A reference input (JSON object) to bias generation. "
            "Useful when you want cases similar to a known good example."
        ),
    ),
    output: str = typer.Option(
        "",
        "--output",
        "-o",
        help=(
            "Where to write the JSONL. Default: "
            "[bold]evals/<agent>/dataset.generated.jsonl[/bold] under the "
            "project root."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help=(
            "Use MockProvider + synthetic schema-walking inputs — no LLM "
            "call. Useful for tests / offline / sanity-checking the "
            "schema before paying for tokens."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite OUTPUT if it already exists.",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd).",
        hidden=True,
    ),
) -> None:
    """Generate eval dataset entries for an agent via LLM.

    Each generated entry is ``{"input": {...}, "expected": {...},
    "generated": true}``. The ``expected`` field is the agent's
    ACTUAL response — review + edit if the current behavior isn't
    yet correct. Generated entries are flagged so a future
    [bold]mdk eval --strict[/bold] can distinguish them.

    [bold]Examples:[/bold]

      [dim]$ mdk eval-gen triage --num 20[/dim]
      [dim]$ mdk eval-gen triage --num 5 --sample-input '{"text": "x"}'[/dim]
      [dim]$ mdk eval-gen triage --num 10 -o evals/triage/cases.jsonl[/dim]
      [dim]$ mdk eval-gen triage --num 3 --mock     # offline test[/dim]
    """
    if num < 1:
        err_console.print(f"[red]✗[/red] --num must be ≥ 1; got {num}")
        raise typer.Exit(code=2)
    if num > _MAX_GENERATE:
        err_console.print(
            f"[red]✗[/red] --num {num} exceeds the safety cap of {_MAX_GENERATE}. "
            "[dim]Bump _MAX_GENERATE in the source if you really mean it; "
            "the cap is there to stop a typo from launching $$$$ of LLM calls.[/dim]"
        )
        raise typer.Exit(code=2)

    parsed_sample: dict[str, Any] | None = None
    if sample_input:
        try:
            parsed_sample = json.loads(sample_input)
        except json.JSONDecodeError as exc:
            err_console.print(f"[red]✗[/red] --sample-input is not valid JSON: {exc}")
            raise typer.Exit(code=2) from None
        if not isinstance(parsed_sample, dict):
            err_console.print("[red]✗[/red] --sample-input must be a JSON object")
            raise typer.Exit(code=2)

    root = Path(project_root).resolve()
    agent_path = _resolve_agent_path(name, root)
    try:
        bundle = load_agent(agent_path)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Resolve the output path. Default lives under evals/<agent-name>/.
    target = (
        Path(output).resolve()
        if output
        else root / _DEFAULT_OUTPUT_SUFFIX.format(name=bundle.spec.name)
    )
    if target.exists() and not force:
        err_console.print(
            f"[red]✗[/red] {target} already exists (pass [bold]--force[/bold] to overwrite)"
        )
        raise typer.Exit(code=2)

    console.print(
        f"[dim]Generating {num} case(s) for [bold]{bundle.spec.name}[/bold]"
        f"{' (mock)' if mock else ''}…[/dim]"
    )

    entries = asyncio.run(
        _generate_entries(
            bundle,
            num=num,
            sample_input=parsed_sample,
            mock=mock,
        )
    )

    if not entries:
        err_console.print(
            "[red]✗[/red] generated 0 valid entries. "
            "[dim]Check the agent's input schema; the generator's output "
            "may not be matching it cleanly. Try [bold]--mock[/bold] to "
            "confirm the schema accepts synthetic inputs.[/dim]"
        )
        raise typer.Exit(code=1)

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    body = (
        f"[bold]Wrote:[/bold]   [cyan]{target}[/cyan]\n"
        f"[bold]Cases:[/bold]   {len(entries)} (requested {num}"
        f"{', some skipped' if len(entries) < num else ''})\n\n"
        "[bold]Next:[/bold]\n"
        "  • Review the file — generated [bold]expected[/bold] fields\n"
        "    reflect the CURRENT agent's behavior; edit any that look wrong.\n"
        f"  • [cyan]mdk eval {bundle.spec.name}[/cyan] to run against this set."
    )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] eval-gen complete",
            title_align="left",
            border_style="green",
        )
    )
