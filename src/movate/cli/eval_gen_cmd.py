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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from jsonschema import Draft202012Validator, ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

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
    with_dimensions: bool = True,
) -> list[dict[str, Any]]:
    """Build ``num`` ``{input, expected, generated: true}`` entries.

    ``mock=True`` skips the LLM entirely — synthesizes inputs from the
    schema + runs the agent under MockProvider to fill ``expected``.
    Used by tests and offline operators.

    When ``with_dimensions=True`` (the default), each entry also gets
    ``grounding`` and ``expected_coverage`` fields populated via a
    second LLM call. These fields activate faithfulness and coverage
    scoring in ``mdk eval``.
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
            entry: dict[str, Any] = {
                "input": generated_input,
                "expected": response.data,
                "generated": True,
            }
            if with_dimensions:
                dims = await _enrich_with_dimensions(
                    rt, bundle, generated_input, response.data, mock=mock
                )
                entry.update(dims)
            entries.append(entry)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    return entries


_RETRY_NUDGE = (
    "\n\nReminder: respond with ONE JSON object only — no prose, no "
    "markdown, no code fences. Just the bare {...}. Your previous "
    "response did not parse as JSON."
)

# ---------------------------------------------------------------------------
# Dimensional annotation (grounding + expected_coverage)
# ---------------------------------------------------------------------------

_DIM_SYSTEM_PROMPT = """\
You annotate AI agent responses with grounding context and coverage topics.
Given an input and the agent's response, produce a JSON object with exactly
two keys:
  "grounding": one sentence of factual context that anchors the answer
               (e.g. "The return policy allows 30-day returns with receipt.")
  "expected_coverage": a list of 2-4 lowercase hyphenated topic slugs that
               a correct answer should address
               (e.g. ["return-window", "receipt-required"])

Respond with ONE bare JSON object — no prose, no markdown, no code fences.
"""


async def _enrich_with_dimensions(
    rt: Any,
    bundle: AgentBundle,
    input_data: dict[str, Any],
    output_data: dict[str, Any],
    *,
    mock: bool,
) -> dict[str, Any]:
    """Return grounding + expected_coverage annotations for one entry.

    Under mock, returns stubs so offline / test flows produce valid
    eval datasets without a real LLM call.
    """
    if mock:
        return {"grounding": "mock grounding context", "expected_coverage": ["mock-topic"]}

    from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415

    request = CompletionRequest(
        provider=bundle.spec.model.provider,
        messages=[
            Message(role="system", content=_DIM_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"Input: {json.dumps(input_data)}\n"
                    f"Response: {json.dumps(output_data)}\n\n"
                    "Annotate this response with grounding and expected_coverage."
                ),
            ),
        ],
        params={"temperature": 0.2, "max_tokens": 256},
    )
    try:
        response = await rt.provider.complete(request)
        text = (response.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        if (
            isinstance(parsed, dict)
            and "grounding" in parsed
            and "expected_coverage" in parsed
        ):
            return {
                "grounding": str(parsed["grounding"]),
                "expected_coverage": list(parsed["expected_coverage"]),
            }
    except Exception:
        pass
    return {}


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

    Retries once on JSON parse failure with a stricter prompt.
    Lifts yield on flaky-output models from ~80% to ~95% in
    practice — one extra call is cheap relative to the case it saves.
    """
    parsed = await _attempt_generate(rt, bundle, index=index, sample_input=sample_input, nudge="")
    if parsed is not None:
        return parsed

    # One retry with a stricter system-prompt nudge.
    parsed = await _attempt_generate(
        rt, bundle, index=index, sample_input=sample_input, nudge=_RETRY_NUDGE
    )
    if parsed is not None:
        return parsed

    err_console.print(
        f"[yellow]⚠[/yellow] generator failed for case #{index + 1} after retry — skipping"
    )
    return {}


async def _attempt_generate(
    rt: Any,
    bundle: AgentBundle,
    *,
    index: int,
    sample_input: dict[str, Any] | None,
    nudge: str,
) -> dict[str, Any] | None:
    """One LLM call + parse. Returns None on any failure so the caller
    can decide whether to retry."""
    from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415

    provider = rt.provider
    system = _GEN_SYSTEM_PROMPT + nudge
    request = CompletionRequest(
        provider=bundle.spec.model.provider,
        messages=[
            Message(role="system", content=system),
            Message(
                role="user",
                content=_gen_user_message(bundle, index=index, sample_input=sample_input),
            ),
        ],
        params={"temperature": 0.9, "max_tokens": 512},
    )
    try:
        response = await provider.complete(request)
    except Exception:
        return None

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
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ---------------------------------------------------------------------------
# Guided wizard — `mdk eval-gen --guided`  (PR #108)
# ---------------------------------------------------------------------------


@dataclass
class _EvalGenWizardChoices:
    """Resolved answers from the interactive eval-gen wizard.

    Maps 1:1 to the CLI flags the dispatch path already handles, so
    the wizard's only job is collecting choices — execution stays in
    the existing code paths.
    """

    name: str
    num: int
    sample_input: str
    mock: bool
    output: str | None
    force: bool


def _run_eval_gen_wizard() -> _EvalGenWizardChoices | None:  # noqa: PLR0912 — orchestrator; 4 prompts each with try/except adds linear branch count
    """Interactive Rich-prompted eval-gen setup. Returns None on Ctrl-C.

    Walks the operator through the four most common decisions a
    generation invocation needs: which agent, how many cases, mock vs
    real provider, and whether to bias generation with a sample input
    from the existing dataset. The full surface of ``mdk eval-gen``
    has 6 flags; the wizard intentionally covers the 4 a casual
    operator cares about and leaves --output / --force to the
    explicit CLI path (sensible defaults work for the wizard flow).

    Same visual style as ``mdk eval --guided`` (Rich Panel + numbered
    Prompt.ask choices) so operators see one consistent UX language
    across the guided commands.
    """
    from movate.core.config import is_project_root  # noqa: PLC0415

    cwd = Path.cwd()
    if not is_project_root(cwd):
        err_console.print(
            "[red]✗[/red] guided eval-gen needs a project (project.yaml / "
            "policy.yaml / movate.yaml). None found in cwd."
        )
        return None

    console.print()
    console.print(
        Panel(
            "[bold]mdk eval-gen — guided setup[/bold]\n"
            "[dim]Four questions; press Ctrl-C any time to quit. "
            "The resolved command is shown before it runs so you can "
            "copy-paste it next time.[/dim]",
            border_style="cyan",
            title_align="left",
        )
    )

    # Q1: Which agent? (Unlike `mdk eval --guided`, no "all" here —
    # that's PR #109's `--all` flag, a separate sweep path. Operators
    # who want the sweep type `mdk eval-gen --all` instead of taking
    # the wizard route.)
    agents_dir = cwd / "agents"
    agent_names: list[str] = []
    if agents_dir.is_dir():
        agent_names = sorted(
            d.name for d in agents_dir.iterdir() if d.is_dir() and (d / "agent.yaml").is_file()
        )
    if not agent_names:
        err_console.print(
            "[red]✗[/red] no agents in [bold]./agents/[/bold]. "
            "Run [bold]mdk add <template>[/bold] first."
        )
        return None
    console.print()
    console.print("[bold]Which agent?[/bold]")
    for i, agent_name in enumerate(agent_names, start=1):
        console.print(f"  [bold cyan][{i}][/bold cyan] {agent_name}")
    try:
        agent_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=[str(i) for i in range(1, len(agent_names) + 1)],
            default="1",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    chosen_agent = agent_names[int(agent_idx) - 1]

    # Q2: How many cases? Common defaults — most demo flows want 5-10
    # for quick iteration. 50+ is "I'm building a real eval set" — give
    # it a dedicated option so operators don't have to type a number.
    num_choices = {
        "1": (5, "quick — 5 cases, sub-minute LLM cost"),
        "2": (10, "default — 10 cases, balanced coverage"),
        "3": (20, "deeper — 20 cases, catches more edge behaviors"),
        "4": (50, "comprehensive — 50 cases, real eval set"),
    }
    console.print()
    console.print("[bold]How many cases to generate?[/bold]")
    for key, (value, label) in num_choices.items():
        console.print(f"  [bold cyan][{key}][/bold cyan] {value}  [dim]{label}[/dim]")
    try:
        num_idx = Prompt.ask(
            "\n[bold]Pick[/bold]",
            choices=list(num_choices.keys()),
            default="2",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    chosen_num = num_choices[num_idx][0]

    # Q3: Mock or real provider? Same shape as `mdk eval --guided`.
    # Mock = synthetic schema-walking inputs, no LLM call (free,
    # offline). Real = LLM-generated inputs (better variety, costs
    # tokens). Demo path: mock-default keeps the cost-of-curiosity at
    # $0 for first-time operators.
    console.print()
    try:
        use_mock = Confirm.ask(
            "[bold]Use mock provider?[/bold] [dim](no LLM call; synthesizes "
            "inputs from the schema. Free + offline; lower variety than "
            "the real generator. Recommended for first-look.)[/dim]",
            default=True,
        )
    except (KeyboardInterrupt, EOFError):
        return None

    # Q4: Sample-input strategy. Skipped under mock (no LLM means the
    # sample doesn't influence anything — synthetic inputs are
    # schema-driven). Under real LLM gen, seeding with the first
    # dataset row significantly improves topic/tone variety.
    sample_input_arg = ""
    if not use_mock:
        existing_dataset = _find_existing_dataset(cwd / "agents" / chosen_agent)
        sample_choices: dict[str, tuple[str, str]] = {
            "1": (
                "none",
                "let the LLM choose freely — broadest variety, less anchoring",
            ),
            "2": (
                "first_row",
                "seed with first row from existing dataset"
                + (
                    " [yellow](no dataset found — would skip)[/yellow]"
                    if existing_dataset is None
                    else ""
                ),
            ),
        }
        console.print()
        console.print("[bold]Sample-input strategy?[/bold]")
        for key, (_, label) in sample_choices.items():
            console.print(f"  [bold cyan][{key}][/bold cyan] {label}")
        try:
            sample_idx = Prompt.ask(
                "\n[bold]Pick[/bold]",
                choices=list(sample_choices.keys()),
                default="2" if existing_dataset is not None else "1",
                show_choices=False,
            )
        except (KeyboardInterrupt, EOFError):
            return None
        if sample_choices[sample_idx][0] == "first_row" and existing_dataset is not None:
            sample_input_arg = json.dumps(existing_dataset)

    # Preview the equivalent CLI command so the operator learns the
    # flag-form for next time. Same affordance as `mdk eval --guided`.
    parts: list[str] = ["mdk", "eval-gen", chosen_agent, "--num", str(chosen_num)]
    if use_mock:
        parts.append("--mock")
    if sample_input_arg:
        parts.extend(["--sample-input", sample_input_arg])

    console.print()
    console.print(
        Panel(
            "[dim]Running:[/dim] [bold cyan]" + " ".join(parts) + "[/bold cyan]",
            border_style="green",
            title="[green]✓[/green] Configured",
            title_align="left",
        )
    )

    return _EvalGenWizardChoices(
        name=chosen_agent,
        num=chosen_num,
        sample_input=sample_input_arg,
        mock=use_mock,
        output=None,  # let the dispatch path use the default location
        force=False,
    )


def _find_existing_dataset(agent_dir: Path) -> dict[str, Any] | None:
    """Return the first row's ``input`` from an existing dataset.jsonl,
    or None when there's no dataset / it's empty / the row isn't shape-
    correct. Used by the wizard's sample-input prompt to bias generation
    when the operator already has a curated example."""
    dataset_path = agent_dir / "evals" / "dataset.jsonl"
    if not dataset_path.is_file():
        return None
    try:
        text = dataset_path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        first_row = json.loads(text.splitlines()[0])
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(first_row, dict):
        return None
    input_field = first_row.get("input")
    if not isinstance(input_field, dict):
        return None
    return input_field


# ---------------------------------------------------------------------------
# `--all` project sweep  (PR #109)
# ---------------------------------------------------------------------------


def _eval_gen_all_in_project(  # noqa: PLR0912 — orchestrator; per-agent state machine reads clearer flat
    *,
    num: int,
    sample_input: str,
    mock: bool,
    force: bool,
    with_dimensions: bool,
    project_root: str,
) -> None:
    """Generate eval cases for every agent in the project.

    Walks ``<project>/agents/*/agent.yaml``, invokes the standard
    per-agent generator once per agent, aggregates results into a
    Rich summary table, and emits a greppable
    ``mdk_eval_gen_all_summary:`` line. Same shape as
    ``mdk_eval_all_summary:`` so CI workflows can scrape one line
    format whether they're running eval or eval-gen.

    Idempotent by default: agents that already have a
    ``evals/<agent>/dataset.generated.jsonl`` are SKIPPED, not
    overwritten. Pass ``--force`` to regenerate (matches the
    per-agent ``mdk eval-gen --force`` semantics).

    Why a separate sweep helper instead of looping over the
    per-agent eval_gen command itself: the per-agent path raises
    typer.Exit on file-already-exists, which would abort the whole
    sweep on the first idempotent skip. We need clean per-agent
    branching (generated / skipped / failed) which the standalone
    helper provides.
    """
    from rich.table import Table  # noqa: PLC0415

    from movate.core.config import is_project_root  # noqa: PLC0415

    if num < 1:
        err_console.print(f"[red]✗[/red] --num must be ≥ 1; got {num}")
        raise typer.Exit(code=2)
    if num > _MAX_GENERATE:
        err_console.print(
            f"[red]✗[/red] --num {num} exceeds the safety cap of "
            f"{_MAX_GENERATE}. [dim]Bump _MAX_GENERATE in the source "
            f"if you really mean it.[/dim]"
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

    # Walk up to find the project root. Same convention `mdk eval --all`
    # uses — operators can run `mdk eval-gen --all` from any subdir
    # of the project, not just the root.
    root = Path(project_root).resolve()
    current = root
    found_root: Path | None = None
    while True:
        if is_project_root(current):
            found_root = current
            break
        if current.parent == current:
            break
        current = current.parent
    if found_root is None:
        err_console.print(
            "[red]✗[/red] not inside a movate project. "
            "[dim]Run [bold]mdk init <name>[/bold] first, or pass an "
            "AGENT name to generate for one agent.[/dim]"
        )
        raise typer.Exit(code=2)
    root = found_root

    agents_dir = root / "agents"
    agent_dirs = (
        sorted(p.parent for p in agents_dir.glob("*/agent.yaml")) if agents_dir.is_dir() else []
    )
    if not agent_dirs:
        err_console.print(
            "[yellow]⚠[/yellow] no agents found under "
            f"[dim]{agents_dir}[/dim]. "
            "[dim]Add agents via [bold]mdk add <template>[/bold] first.[/dim]"
        )
        # Not an error — empty project is valid. Greppable line fires
        # so CI can branch on it cleanly.
        console.print(
            "[dim]mdk_eval_gen_all_summary: "
            "agents_total=0 generated=0 skipped=0 failed=0 ok=true[/dim]"
        )
        return

    # Per-agent state.
    rows: list[tuple[str, str, str]] = []  # (name, status, detail)
    generated_count = 0
    skipped_count = 0
    failed_count = 0

    for agent_dir in agent_dirs:
        agent_name = agent_dir.name
        target = root / _DEFAULT_OUTPUT_SUFFIX.format(name=agent_name)
        if target.exists() and not force:
            rows.append(
                (
                    agent_name,
                    "[yellow]⊝ skipped[/yellow]",
                    "already exists — pass [bold]--force[/bold] to regenerate",
                )
            )
            skipped_count += 1
            continue

        try:
            bundle = load_agent(agent_dir)
        except AgentLoadError as exc:
            rows.append((agent_name, "[red]✗ load failed[/red]", str(exc)[:80]))
            failed_count += 1
            continue

        try:
            entries = asyncio.run(
                _generate_entries(
                    bundle,
                    num=num,
                    sample_input=parsed_sample,
                    mock=mock,
                    with_dimensions=with_dimensions,
                )
            )
        except Exception as exc:  # last-resort guard for one-agent failures
            rows.append((agent_name, "[red]✗ generator failed[/red]", str(exc)[:80]))
            failed_count += 1
            continue

        if not entries:
            rows.append(
                (
                    agent_name,
                    "[red]✗ 0 valid entries[/red]",
                    "schema mismatch or LLM failure — try --mock first",
                )
            )
            failed_count += 1
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        rows.append(
            (
                agent_name,
                "[green]✓ generated[/green]",
                f"{len(entries)}/{num} cases → {target.relative_to(root)}",
            )
        )
        generated_count += 1

    # Render summary table.
    table = Table(
        title=(f"Project eval-gen — [bold]{root.name}[/bold] [dim]({len(rows)} agent(s))[/dim]"),
        title_style="bold",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Agent", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail")
    for name, status, detail in rows:
        table.add_row(name, status, detail)
    console.print()
    console.print(table)

    ok = failed_count == 0
    console.print(
        f"[dim]mdk_eval_gen_all_summary: "
        f"agents_total={len(rows)} "
        f"generated={generated_count} skipped={skipped_count} "
        f"failed={failed_count} "
        f"ok={'true' if ok else 'false'}[/dim]"
    )

    if not ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def eval_gen(  # noqa: PLR0912 — orchestrator; wizard + validation + dispatch flat reads clearer
    name: str = typer.Argument(
        None,
        help=(
            "Agent name (resolved under [bold]agents/<name>[/bold]) or a "
            "literal path to an agent directory. Omit with [bold]--guided[/bold] "
            "to pick interactively, or with [bold]--all[/bold] to sweep "
            "every agent in the project."
        ),
        metavar="AGENT",
    ),
    num: int = typer.Option(
        10,
        "--num",
        "-n",
        help=f"How many cases to generate. Default 10; max {_MAX_GENERATE}.",
    ),
    guided: bool = typer.Option(
        False,
        "--guided",
        "-g",
        help=(
            "Interactive wizard: walks the operator through agent "
            "selection, case count, provider (mock/real), and sample-"
            "input strategy — then runs the generator. Auto-triggered "
            "when [bold]mdk eval-gen[/bold] is invoked with no agent "
            "from a TTY inside a project."
        ),
    ),
    all_in_project: bool = typer.Option(
        False,
        "--all",
        help=(
            "Sweep every agent under [bold]./agents/[/bold] in the "
            "current project, generating [bold]--num[/bold] cases for "
            "each. Skips agents that already have a generated dataset "
            "unless [bold]--force[/bold] is passed. Emits "
            "[bold]mdk_eval_gen_all_summary:[/bold] for CI scrapers."
        ),
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
    with_dimensions: bool = typer.Option(
        True,
        "--with-dimensions/--no-with-dimensions",
        help=(
            "Annotate each entry with [bold]grounding[/bold] (one-sentence context) "
            "and [bold]expected_coverage[/bold] (2-4 topic slugs) via a second LLM "
            "call. These fields activate faithfulness and coverage scoring in "
            "[bold]mdk eval[/bold]. Pass [bold]--no-with-dimensions[/bold] to skip "
            "the extra call and generate accuracy-only entries."
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
      [dim]$ mdk eval-gen --guided                  # interactive wizard[/dim]
      [dim]$ mdk eval-gen --all --num 10 --mock     # sweep every agent[/dim]
    """
    # --all sweep: handle BEFORE --guided / single-agent dispatch since
    # the sweep is its own code path (no per-agent name resolution, no
    # wizard). Mutex with explicit AGENT — passing both is ambiguous.
    if all_in_project:
        if name is not None:
            err_console.print(
                "[red]✗[/red] [bold]--all[/bold] and an explicit AGENT "
                "argument are mutually exclusive."
            )
            raise typer.Exit(code=2)
        if guided:
            err_console.print(
                "[red]✗[/red] [bold]--all[/bold] and [bold]--guided[/bold] "
                "are mutually exclusive — guided picks one agent; --all "
                "sweeps every agent."
            )
            raise typer.Exit(code=2)
        _eval_gen_all_in_project(
            num=num,
            sample_input=sample_input,
            mock=mock,
            force=force,
            with_dimensions=with_dimensions,
            project_root=project_root,
        )
        return

    # Guided wizard — explicit `--guided`, OR auto-trigger when an
    # operator typed bare `mdk eval-gen` with no agent name from a TTY
    # inside a project. CI / pipe / no-args-outside-project paths still
    # fall through to the existing "agent required" error.
    if not guided and name is None:
        from movate.core.config import is_project_root  # noqa: PLC0415

        if sys.stdin.isatty() and sys.stdout.isatty() and is_project_root(Path.cwd()):
            guided = True
    if guided:
        wizard = _run_eval_gen_wizard()
        if wizard is None:  # operator hit Ctrl-C / quit
            raise typer.Exit(code=0)
        # Apply wizard's answers as if they were CLI flags, then fall
        # through to the standard dispatch below — no duplicated logic.
        name = wizard.name
        num = wizard.num
        sample_input = wizard.sample_input
        mock = wizard.mock
        output = wizard.output or ""
        force = wizard.force

    if name is None:
        err_console.print(
            "[red]✗[/red] AGENT required. Pass an agent name, or use "
            "[bold]--guided[/bold] to pick interactively, or "
            "[bold]--all[/bold] to sweep every agent."
        )
        raise typer.Exit(code=2)

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
            with_dimensions=with_dimensions,
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
