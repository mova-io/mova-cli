"""``movate run <path>`` — execute an agent or a workflow locally.

Auto-detects which: a path containing ``workflow.yaml`` runs as a workflow
(input parsed as JSON for ``initial_state``); otherwise runs as an agent
with the existing string/JSON/file/stdin input coercion.

v0.3 supports local execution only. Remote (`--remote`) hits the FastAPI
runtime and lands in v0.5 alongside `movate serve`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.cli import _console
from movate.cli._completion import complete_agent_path
from movate.cli._output import Run
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.cli._workflow_path import is_workflow_path
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import RunRequest, WorkflowStatus
from movate.core.run_replay import (
    AgentReplayDiff,
    ReplayMismatchError,
    render_replay_json,
    replay_agent_run,
)
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowGraph,
    WorkflowResult,
    WorkflowRunError,
    WorkflowRunner,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.spec import WorkflowSpecLoadError

console = Console(stderr=True)

# Auto-select output format: text for interactive terminals, JSON for pipes/CI.
# `--output json` / `--output text` always override.
_default_output_format: Run = Run.TEXT if sys.stdout.isatty() else Run.JSON


def run(
    path: Path = typer.Argument(
        ...,
        help="Path to an agent or workflow directory.",
        shell_complete=complete_agent_path,
    ),
    input_arg: str = typer.Argument(
        None,
        metavar="INPUT",
        help=(
            "Input: agent mode accepts a plain string (auto-wraps), JSON, file, "
            "or '-' for stdin. Workflow mode requires JSON, a file, or '-'."
        ),
    ),
    input_flag: str = typer.Option(
        None, "--input", "-i", help="Alternative way to pass input (preferred for explicit JSON)."
    ),
    replay_id: str = typer.Option(
        None,
        "--replay",
        help=(
            "Re-run a recorded RunRecord by id against the current agent code. "
            "Pins the original input; everything else (prompt, model, schemas) "
            "comes from disk. Mutually exclusive with positional INPUT/--input."
        ),
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic MockProvider (no API keys; for smoke tests)."
    ),
    target: str = typer.Option(
        None,
        "--target",
        help=(
            "Run against a deployed runtime instead of locally. "
            "Resolves to a target from ~/.movate/config.yaml (URL + key_env). "
            "POSTs the input to /api/v1/agents/<name>/runs?wait=true and "
            "renders the resulting RunView the same way as a local run. "
            "Mutually exclusive with --replay and --stream (remote runtime "
            "owns persistence + provider; no local stream callback)."
        ),
    ),
    stream: bool = typer.Option(
        False,
        "--stream",
        help=(
            "Render model tokens to stderr as they arrive (preview). "
            "Final output is still schema-validated + persisted normally; "
            "this flag only adds a live preview. Workflow + replay modes ignore it."
        ),
    ),
    trace: bool = typer.Option(
        False,
        "--trace",
        help=(
            "After the run, print a table of KB chunks retrieved by any "
            "kb-vector-lookup skill calls. Useful for debugging retrieval quality."
        ),
    ),
    output_format: Run = typer.Option(
        _default_output_format,
        "--output",
        "-o",
        case_sensitive=False,
        help=(
            "Output format. Defaults to [bold]text[/bold] on interactive terminals "
            "and [bold]json[/bold] when stdout is piped. Use [bold]--output json[/bold] "
            "to force JSON from a terminal (e.g. for scripting)."
        ),
    ),
) -> None:
    """Run an agent or workflow against the given input.

    [bold]Agent examples:[/bold]

      [dim]# Plain string — auto-wraps to the agent's single required string field[/dim]
      $ movate run ./faq-agent "What is movate?"

      [dim]# See tokens as they arrive (dev-loop preview)[/dim]
      $ movate run ./faq-agent "hi" --stream

      [dim]# Mock mode (no API calls)[/dim]
      $ movate run ./faq-agent "hello" --mock

      [dim]# Replay a stored run against current code (regression debug)[/dim]
      $ movate run ./faq-agent --replay 4f8a-...

      [dim]# Hit a deployed runtime (Azure ACA / local-serve)[/dim]
      $ mdk run faq "hello world" --target dev

    [bold]Workflow examples:[/bold]

      [dim]# Initial state as JSON[/dim]
      $ movate run ./returns-workflow '{"order_id": "ord-123"}' --mock

      [dim]# Initial state from a file[/dim]
      $ movate run ./returns-workflow --input initial_state.json

    [bold]Inside a project[/bold] you can pass a bare agent or workflow
    name (resolved under ``./agents/<name>/`` or ``./workflows/<name>/``):

      [dim]# Both forms work — full path or just the name[/dim]
      $ mdk run rag-qa '{"question":"..."}'
      $ mdk run ./agents/rag-qa '{"question":"..."}'
    """
    # Remote dispatch wins early — when --target is set, we don't need
    # the local bundle (the runtime has it). Mutex with --replay (replay
    # rebuilds from a local RunRecord; nothing remote about it) and
    # --stream (no token callback path through the runtime in v0.5).
    if target is not None:
        if replay_id is not None:
            console.print(
                "[red]✗[/red] --target is mutually exclusive with --replay; "
                "remote runtime owns the RunRecord history."
            )
            raise typer.Exit(code=2)
        if stream:
            console.print(
                "[red]✗[/red] --target does not support --stream in v0.7; "
                "the runtime returns the final RunView once execution completes."
            )
            raise typer.Exit(code=2)
        _dispatch_remote_agent(
            agent_name=str(path),
            raw=input_flag or input_arg,
            target=target,
            mock=mock,
            output_format=output_format,
        )
        return

    # Bare-name resolution: inside a project, `mdk run rag-qa` resolves
    # to ./agents/rag-qa. Full paths + URLs pass through unchanged.
    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415

    path = Path(resolve_agent_or_workflow_arg(str(path)))

    if replay_id is not None:
        if input_arg is not None or input_flag is not None:
            console.print(
                "[red]✗[/red] --replay is mutually exclusive with positional INPUT / --input"
            )
            raise typer.Exit(code=2)
        if is_workflow_path(path):
            console.print(
                "[red]✗[/red] --replay supports agents only in v0.4; "
                "workflow replay lands in a follow-up"
            )
            raise typer.Exit(code=2)
        _dispatch_replay(path, replay_id, mock=mock, output_format=output_format)
        return

    if is_workflow_path(path):
        if stream:
            # Workflows are multi-step; streaming one node's tokens
            # interleaved with another's would be confusing. Out of scope
            # for v0.5 — surface the limitation explicitly rather than
            # silently ignoring the flag.
            console.print("[red]✗[/red] --stream supports agents only; workflow streaming is TBD")
            raise typer.Exit(code=2)
        _dispatch_workflow(path, input_flag or input_arg, mock=mock, output_format=output_format)
    else:
        _dispatch_agent(
            path,
            input_flag or input_arg,
            mock=mock,
            stream=stream,
            trace=trace,
            output_format=output_format,
        )


# ---------------------------------------------------------------------------
# Agent dispatch (unchanged behaviour)
# ---------------------------------------------------------------------------


def _dispatch_agent(
    path: Path,
    raw: str | None,
    *,
    mock: bool,
    stream: bool,
    trace: bool = False,
    output_format: Run,
) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        _maybe_suggest_fuzzy(path)
        raise typer.Exit(code=2) from None

    if raw is None:
        _suggest_dataset_example(bundle)
        raise typer.Exit(code=2)
    payload = _coerce_agent_input(raw, bundle)

    asyncio.run(
        _run_local_agent(
            bundle, payload, output_format=output_format, mock=mock, stream=stream, trace=trace
        )
    )


def _maybe_suggest_fuzzy(path: Path) -> None:
    """When ``load_agent`` fails AND the original arg was a bare name
    (no path separator), surface a fuzzy-match suggestion from the
    project's actual agents directory.

    Same `did you mean rag-qa?` UX the doctor-agent path emits.
    Silent no-op when path has separators (operator passed a full
    path; the error is more likely a real filesystem issue).
    """
    arg_str = str(path)
    if "/" in arg_str or "\\" in arg_str:
        return
    from movate.cli._resolve import suggest_similar_agent  # noqa: PLC0415

    suggestion = suggest_similar_agent(arg_str)
    if suggestion:
        console.print(f"[dim]→ did you mean [bold]{suggestion}[/bold]?[/dim]")


def _suggest_dataset_example(bundle: AgentBundle) -> None:
    """Print a "no input" error PLUS a copy-pasteable example from the
    agent's evals/dataset.jsonl.

    Customers on first `mdk run` don't know the agent's input schema.
    The dataset has real, valid example inputs — surface the first one
    so they can copy-paste a working command instead of reading the
    schema and constructing JSON manually.
    """
    console.print("[red]✗[/red] provide input as a positional arg or via --input.")

    # Try to read the first dataset row. The path lives in the agent
    # spec; resolve it relative to agent_dir. Failures are non-fatal —
    # we already printed the primary error.
    sample_input: dict[str, Any] | None = None
    try:
        dataset_path = (bundle.agent_dir / bundle.spec.evals.dataset).resolve()
        if dataset_path.is_file():
            text = dataset_path.read_text().strip()
            if text:
                first_row = json.loads(text.splitlines()[0])
                if isinstance(first_row, dict) and "input" in first_row:
                    sample_input = first_row["input"]
    except (OSError, json.JSONDecodeError, AttributeError):
        sample_input = None

    if sample_input is not None:
        # Compact JSON keeps the suggested command on one line for
        # easy copy-paste. Agent name comes from the resolved bundle —
        # operators get the SAME shape they'd type at the prompt.
        sample_json = json.dumps(sample_input, separators=(", ", ": "))
        console.print(
            f"[dim]→ try the first example from the dataset:[/dim]\n"
            f"[bold cyan]  mdk run {bundle.spec.name} '{sample_json}'[/bold cyan]"
        )
    else:
        console.print(
            "[dim]→ no dataset sample available. Inspect the input schema "
            "via [bold]mdk show "
            f"{bundle.spec.name}[/bold] before composing the input.[/dim]"
        )


def _run_hint(run_short: str, metrics: Any, output_format: Run) -> str:
    """Build the dim footer printed to stderr after a successful agent run.

    Text mode (TTY): run-id · latency · cost · tokens · mdk explain hint.
    JSON / pipe mode: minimal hint (metadata already in the JSON body).
    """
    if output_format != Run.TEXT:
        return (
            f"[dim]→ run [bold]{run_short}[/bold] · "
            f"[cyan]mdk explain {run_short}[/cyan][/dim]"
        )
    parts: list[str] = [f"[bold]{run_short}[/bold]"]
    if metrics is not None:
        latency = getattr(metrics, "latency_ms", 0) or 0
        cost = getattr(metrics, "cost_usd", 0.0) or 0.0
        tokens_obj = getattr(metrics, "tokens", None)
        tok_in = getattr(tokens_obj, "input", 0) if tokens_obj else 0
        tok_out = getattr(tokens_obj, "output", 0) if tokens_obj else 0
        if latency:
            parts.append(f"{latency:,}ms")
        if cost:
            parts.append(f"${cost:.4f}")
        total = tok_in + tok_out
        if total:
            parts.append(f"{total:,} tok ({tok_in}↑ {tok_out}↓)")
    parts.append(f"[cyan]mdk explain {run_short}[/cyan]")
    return "[dim]→ " + " · ".join(parts) + "[/dim]"


def _coerce_agent_input(arg: str, bundle: AgentBundle) -> dict[str, Any]:
    """Best-effort interpretation of an agent's positional input.

    Order:
      1. ``-`` → read JSON from stdin
      2. existing file path → read JSON from file
      3. parses as a JSON object → use as-is
      4. plain string AND agent has exactly one required string field →
         wrap as ``{<field>: arg}``
      5. otherwise → raise with a helpful message
    """
    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))

    p = Path(arg)
    if p.is_file():
        return _ensure_dict(json.loads(p.read_text()))

    try:
        parsed = json.loads(arg)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    schema = bundle.input_schema
    required = list(schema.get("required", []))
    properties = schema.get("properties", {}) or {}
    string_required = [
        name for name in required if properties.get(name, {}).get("type") == "string"
    ]
    if len(string_required) == 1 and len(required) == 1:
        return {string_required[0]: arg}

    raise typer.BadParameter(
        f"input is not valid JSON and cannot be auto-wrapped — agent "
        f"{bundle.spec.name!r} requires {required}. Pass JSON via --input or "
        f"as a JSON-formatted positional arg."
    )


async def _run_local_agent(
    bundle: AgentBundle,
    payload: dict[str, Any],
    *,
    output_format: Run,
    mock: bool,
    stream: bool = False,
    trace: bool = False,
) -> None:
    rt = await build_local_runtime(mock=mock)
    # Dataset-aware mock (PR #104): when running --mock against an
    # agent that ships an evals dataset, configure the MockProvider
    # to return dataset.jsonl[*].expected on each call. This makes
    # `mdk run --mock` produce a schema-conforming response instead
    # of the canned `{"message": "mock"}` that fails validation for
    # any non-trivial output schema. Single-shot run → returns
    # dataset[0].expected.
    if mock:
        _configure_mock_for_bundle(rt.provider, bundle)
    try:
        request = RunRequest(agent=bundle.spec.name, input=payload)
        # Streaming preview goes to stderr so it doesn't poison stdout
        # (which carries the schema-validated final JSON). The MockProvider
        # has no real stream path, so silently skip streaming under --mock.
        on_token = _streaming_callback() if stream and not mock else None
        # Show a spinner while the LLM responds (text mode + interactive + no streaming).
        # Cleared automatically by the Status context when the await returns.
        _spin = output_format == Run.TEXT and not stream and sys.stderr.isatty()
        if _spin:
            with console.status(
                f"Running [bold]{bundle.spec.name}[/bold]…",
                spinner="dots",
            ):
                response = await rt.executor.execute(bundle, request, on_token=on_token)
        else:
            response = await rt.executor.execute(bundle, request, on_token=on_token)
        if on_token is not None:
            # End the streamed preview with a newline so the JSON output
            # below starts on its own line.
            sys.stderr.write("\n")
            sys.stderr.flush()
        if trace and response.run_id:
            try:
                record = await rt.storage.get_run(response.run_id, tenant_id="local")
                if record and record.skill_calls:
                    _print_kb_trace(record.skill_calls)
            except Exception:
                pass  # never break the run for trace display failures
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Run.TEXT:
        sys.stdout.write(response.human_readable + "\n")
    else:
        sys.stdout.write(response.model_dump_json(indent=2) + "\n")

    # Echo the run_id on stderr so it survives stdout-piping (jq, `>` to a
    # file, etc.) without corrupting the captured JSON. Closes the dev
    # loop: operators see exactly which RunRecord to feed into
    # `mdk replay` / `mdk explain` next. Honors --quiet via _console.hint.
    if response.run_id:
        short = response.run_id[:8]
        _console.hint(_run_hint(short, response.metrics, output_format))

    # Greppable summary line — mirrors mdk_init_summary / mdk_add_summary
    # / mdk_validate_summary / mdk_eval_*_summary so CI workflows can
    # scrape one shape for every command. Goes to stderr so it doesn't
    # corrupt stdout JSON output. Always fires — even on error — so CI
    # gates can branch on `ok=true|false`.
    ok = response.status != "error"
    cost_usd = getattr(response.metrics, "cost_usd", None) if response.metrics else None
    latency_ms = getattr(response.metrics, "latency_ms", None) if response.metrics else None
    run_short = (response.run_id or "")[:8]
    sys.stderr.write(
        f"mdk_run_summary: kind=agent agent={bundle.spec.name} "
        f"run_id={run_short or '-'} "
        f"cost_usd={cost_usd if cost_usd is not None else '-'} "
        f"latency_ms={latency_ms if latency_ms is not None else '-'} "
        f"ok={'true' if ok else 'false'}\n"
    )
    sys.stderr.flush()

    if response.status == "error":
        raise typer.Exit(code=1)


def _print_kb_trace(skill_calls: list[Any]) -> None:
    """Print a Rich dim table of KB chunks retrieved during a run.

    Filters for skill calls whose name contains 'kb' (case-insensitive),
    extracts the ``chunks`` list from each call's output dict, and renders
    a compact table per matching call to stderr.

    Never raises — all failures are silently suppressed so ``--trace`` can
    never break a run.
    """
    try:
        kb_calls = [sc for sc in skill_calls if "kb" in sc.skill.lower()]
        if not kb_calls:
            return

        sys.stderr.write("─── KB retrieval trace " + "─" * 37 + "\n")
        sys.stderr.flush()

        for sc in kb_calls:
            output = sc.output or {}
            chunks = output.get("chunks") or []
            if not chunks:
                continue

            latency = f"{int(sc.latency_ms)}ms" if sc.latency_ms else "?ms"
            sys.stderr.write(f"  skill: {sc.skill}  latency: {latency}\n")
            sys.stderr.flush()

            table = Table(style="dim", show_header=True, header_style="bold dim")
            table.add_column("#", width=4)
            table.add_column("score", width=6)
            table.add_column("source / content preview")

            for i, chunk in enumerate(chunks, start=1):
                score_val = chunk.get("score")
                score_str = f"{score_val:.2f}" if isinstance(score_val, (int, float)) else "?"
                source = str(chunk.get("source") or "")
                content = str(chunk.get("content") or "").replace("\n", " ")[:80]
                preview = f'{source} · "{content}"' if source else f'"{content}"'
                table.add_row(str(i), score_str, preview)

            console.print(table)
    except Exception:
        pass  # trace display must never fail


def _streaming_callback() -> Callable[[str], None]:
    """Return a token callback that writes each chunk to stderr,
    unbuffered, so the preview reads as a live stream.

    Lives here (not in _console.py) because it touches sys.stderr
    directly rather than going through Rich — Rich would buffer and
    apply markup, which is wrong for incremental tokens."""

    def _emit(text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    return _emit


# ---------------------------------------------------------------------------
# Workflow dispatch
# ---------------------------------------------------------------------------


def _dispatch_workflow(path: Path, raw: str | None, *, mock: bool, output_format: Run) -> None:
    try:
        spec, parent = load_workflow_spec(path)
    except WorkflowSpecLoadError as exc:
        console.print(f"[red]✗ workflow.yaml load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    try:
        graph = compile_workflow(spec, parent)
        validate_linear(graph)
    except WorkflowCompileError as exc:
        console.print(f"[red]✗ workflow validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if raw is None:
        # Default to empty initial state — convenient when state_schema has
        # no `required` fields.
        initial_state: dict[str, Any] = {}
    else:
        initial_state = _coerce_workflow_input(raw)

    asyncio.run(_run_local_workflow(graph, initial_state, output_format=output_format, mock=mock))


def _coerce_workflow_input(arg: str) -> dict[str, Any]:
    """Workflows take a JSON object for ``initial_state`` — no auto-wrap.

    Accepts ``-`` (stdin), a file path, or a JSON string literal.
    """
    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    p = Path(arg)
    if p.is_file():
        return _ensure_dict(json.loads(p.read_text()))
    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"workflow input must be JSON (object), file path, or '-': {exc}"
        ) from exc
    return _ensure_dict(parsed)


async def _run_local_workflow(
    graph: WorkflowGraph,
    initial_state: dict[str, Any],
    *,
    output_format: Run,
    mock: bool,
) -> None:
    rt = await build_local_runtime(mock=mock)
    runner = WorkflowRunner(executor=rt.executor, storage=rt.storage)
    try:
        try:
            result = await runner.run(graph, initial_state=initial_state)
        except WorkflowRunError as exc:
            console.print(f"[red]✗ workflow failed:[/red] {exc}")
            raise typer.Exit(code=2) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Run.JSON:
        _emit_workflow_json(result)
    else:
        _emit_workflow_text(result)

    if result.status is WorkflowStatus.ERROR:
        raise typer.Exit(code=1)


def _emit_workflow_json(result: WorkflowResult) -> None:
    payload = {
        "workflow_run_id": result.workflow_run_id,
        "status": result.status.value,
        "initial_state": result.initial_state,
        "final_state": result.final_state,
        "duration_ms": result.duration_ms,
        "error_node_id": result.error_node_id,
        "error": result.error.model_dump() if result.error else None,
        "nodes": [
            {
                "node_id": r.node_id,
                "agent": r.agent,
                "status": r.status.value,
                "cost_usd": r.metrics.cost_usd,
                "latency_ms": r.metrics.latency_ms,
                "tokens": r.metrics.tokens.model_dump(),
                "output": r.output,
                "error": r.error.model_dump() if r.error else None,
            }
            for r in result.runs
        ],
    }
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


def _emit_workflow_text(result: WorkflowResult) -> None:
    """Pretty Rich summary on stderr; final state JSON on stdout for piping."""
    head = Table(title=f"workflow run {result.workflow_run_id[:8]}…", show_header=False)
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("status", _status_badge(result))
    head.add_row("duration", f"{result.duration_ms} ms")
    if result.error_node_id:
        head.add_row("error_node", result.error_node_id)
        if result.error:
            head.add_row("error", f"{result.error.type}: {result.error.message}")
    head.add_row("workflow_run_id", result.workflow_run_id)
    console.print(head)

    if result.runs:
        rows = Table(title="Nodes", show_header=True, header_style="bold")
        rows.add_column("#", style="dim", width=3)
        rows.add_column("node")
        rows.add_column("agent")
        rows.add_column("status")
        rows.add_column("ms")
        rows.add_column("cost")
        for i, r in enumerate(result.runs, start=1):
            rows.add_row(
                str(i),
                r.node_id or "?",
                r.agent,
                r.status.value,
                str(r.metrics.latency_ms),
                f"${r.metrics.cost_usd:.6f}",
            )
        console.print(rows)

    sys.stdout.write(json.dumps(result.final_state, indent=2) + "\n")


def _status_badge(result: WorkflowResult) -> str:
    if result.status is WorkflowStatus.SUCCESS:
        return "[green]SUCCESS[/green]"
    return "[red]ERROR[/red]"


# ---------------------------------------------------------------------------
# Replay dispatch
# ---------------------------------------------------------------------------


def _dispatch_replay(path: Path, run_id: str, *, mock: bool, output_format: Run) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    asyncio.run(_run_replay(bundle, run_id, output_format=output_format, mock=mock))


async def _run_replay(
    bundle: AgentBundle,
    run_id: str,
    *,
    output_format: Run,
    mock: bool,
) -> None:
    rt = await build_local_runtime(mock=mock)
    try:
        try:
            diff = await replay_agent_run(
                storage=rt.storage,
                executor=rt.executor,
                bundle=bundle,
                run_id=run_id,
            )
        except ReplayMismatchError as exc:
            console.print(f"[red]✗ replay failed:[/red] {exc}")
            raise typer.Exit(code=2) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Run.TEXT:
        _emit_replay_text(diff)
    else:
        sys.stdout.write(json.dumps(render_replay_json(diff), indent=2, default=str) + "\n")

    # Output changes are normal — surfacing them IS the goal. Only fail
    # the gate when the replay itself errored (regression in the agent).
    if diff.current.status == "error":
        raise typer.Exit(code=1)


def _emit_replay_text(diff: AgentReplayDiff) -> None:
    head = Table(
        title=f"agent replay vs run {diff.original.run_id[:8]}…",
        show_header=False,
    )
    head.add_column("field", style="dim")
    head.add_column("value")
    head.add_row("agent", diff.original.agent)
    head.add_row(
        "agent_version",
        f"{diff.original.agent_version} (recorded)",
    )
    head.add_row("recorded_at", diff.original.created_at.isoformat())
    head.add_row(
        "status",
        f"{diff.original.status.value} → {diff.current.status}"
        + ("  [yellow](changed)[/yellow]" if diff.status_changed else ""),
    )
    head.add_row(
        "output_changed",
        "[yellow]YES[/yellow]" if diff.output_changed else "[green]no[/green]",
    )
    if diff.changed_keys:
        head.add_row("changed_keys", ", ".join(diff.changed_keys))
    head.add_row(
        "cost_delta",
        f"${diff.original.metrics.cost_usd:.6f} → ${diff.current.metrics.cost_usd:.6f}  "
        f"({diff.cost_delta_usd:+.6f})",
    )
    head.add_row(
        "latency_delta",
        f"{diff.original.metrics.latency_ms} → {diff.current.metrics.latency_ms} ms  "
        f"({diff.latency_delta_ms:+d})",
    )
    console.print(head)

    sys.stdout.write(
        json.dumps(
            {
                "input": diff.original.input,
                "recorded_output": diff.original.output,
                "current_output": diff.current.data,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise typer.BadParameter(f"input must be a JSON object, got {type(value).__name__}")
    return value


def _configure_mock_for_bundle(provider: Any, bundle: AgentBundle) -> None:
    """If ``provider`` is a :class:`MockProvider` and ``bundle`` ships
    an evals dataset, configure the mock to cycle through the
    dataset's ``expected`` outputs.

    Best-effort: silently no-ops when the provider isn't a mock, the
    bundle has no dataset declared, or the dataset file can't be
    read. The mock just falls back to its default canned response.

    Why this helper lives in ``run.py`` instead of ``_runtime.py``:
    the build_local_runtime() path doesn't have the bundle yet — it
    only knows ``mock`` as a bool. The CLI dispatch (here + in
    eval.py) is the first point both rt.provider AND bundle exist.
    """
    from movate.providers.mock import MockProvider, load_dataset_expecteds  # noqa: PLC0415

    if not isinstance(provider, MockProvider):
        return
    dataset_decl = getattr(bundle.spec.evals, "dataset", None) if bundle.spec.evals else None
    if not dataset_decl:
        return
    dataset_path = (bundle.agent_dir / dataset_decl).resolve()
    expecteds = load_dataset_expecteds(dataset_path)
    if expecteds:
        provider.configure_dataset(expecteds)


# ---------------------------------------------------------------------------
# Remote dispatch (--target) — PR #107
# ---------------------------------------------------------------------------


_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404
_HTTP_UNPROCESSABLE = 422


def _dispatch_remote_agent(  # noqa: PLR0912 — flat HTTP error mapping reads better than nested helpers
    *,
    agent_name: str,
    raw: str | None,
    target: str,
    mock: bool,
    output_format: Run,
) -> None:
    """Run an agent against a deployed runtime.

    Closes the loop: ``init → add → eval → deploy → run --target``.
    Resolves the target's URL + bearer-token env var from
    ``~/.movate/config.yaml`` (same plumbing ``mdk deploy --target``
    uses), POSTs the input to ``/api/v1/agents/<name>/runs?wait=true``,
    and renders the resulting :class:`RunView` the same way as a local
    run — JSON to stdout, summary line on stderr, exit code mirrors
    the run's status.

    The agent NAME (not a local path) is what we put in the URL. If
    the caller happened to pass ``./agents/faq``, we strip to the
    final segment — operators inside a project shouldn't have to think
    about whether they typed the bare name or the path.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

    # Normalize agent path → bare name. The runtime indexes by name,
    # not filesystem path. ``./agents/faq`` and ``faq`` both resolve
    # to ``faq``. Trailing slash tolerant.
    name = Path(agent_name).name or agent_name

    # Resolve target → URL + key_env. Same error UX as `mdk deploy`.
    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Read bearer from env. Matches the deploy preflight — separate
    # error from "target not configured" so the operator sees exactly
    # which knob to turn.
    api_key = os.environ.get(target_cfg.key_env, "").strip()
    if not api_key:
        console.print(
            f"[red]✗[/red] env var ${target_cfg.key_env} is empty. "
            f"Run [bold]mdk auth save-runtime-key {target_name} <key>[/bold] "
            f"to persist + autoload, or [bold]export {target_cfg.key_env}=mvt_live_...[/bold]."
        )
        raise typer.Exit(code=2)

    if raw is None:
        console.print(
            "[red]✗[/red] provide input as a positional arg or via --input "
            "(remote runs require JSON input; no schema available client-side)."
        )
        raise typer.Exit(code=2)

    payload = _coerce_remote_agent_input(raw, name)

    base_url = target_cfg.url.rstrip("/")
    body = {"input": payload, "mock": mock}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Long-ish timeout — inline mode blocks for the full agent run.
    # Typical LLM call is a few seconds; tool-use loops can stretch.
    # 120s gives us headroom without hanging forever on a stuck pod.
    timeout = httpx.Timeout(120.0, connect=10.0)

    console.print(
        f"[dim]→ running [bold]{name}[/bold] on [bold]{target_name}[/bold] "
        f"({target_cfg.url}) …[/dim]"
    )

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/api/v1/agents/{name}/runs",
                params={"wait": "true"},
                json=body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        console.print(f"[red]✗ network error:[/red] {exc}")
        _emit_remote_summary(
            agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
        )
        raise typer.Exit(code=2) from None

    # Friendly error mapping. The runtime's error envelope is
    # `{detail: {...}}` for FastAPI 422 + `{detail: str}` for our typed
    # raises (`not_found`, etc.). Surface the operator-friendly hint
    # first, then the raw body for power users.
    if response.status_code == _HTTP_UNAUTHORIZED:
        prefix = api_key[:16]
        console.print(
            f"[red]✗ runtime rejected the bearer token[/red] "
            f"(value starts with: '{prefix}…').\n"
            f"  Check your env: [bold]echo ${target_cfg.key_env}[/bold] — "
            f"likely stale from .zshrc or a prior tenant.\n"
            f"  Fix: [bold]mdk auth save-runtime-key {target_name} <new-key>[/bold] "
            f"to persist + autoload across shells."
        )
        _emit_remote_summary(
            agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
        )
        raise typer.Exit(code=1)
    if response.status_code == _HTTP_NOT_FOUND:
        console.print(
            f"[red]✗ agent [bold]{name}[/bold] not found on [bold]{target_name}[/bold].[/red]\n"
            f"  Did you forget to [bold]mdk deploy --target {target_name}[/bold] first?\n"
            f"  List deployed agents: [bold]curl -H 'Authorization: Bearer "
            f"${target_cfg.key_env}' {target_cfg.url}/api/v1/agents[/bold]"
        )
        _emit_remote_summary(
            agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
        )
        raise typer.Exit(code=1)
    if response.status_code == _HTTP_UNPROCESSABLE:
        try:
            detail = response.json()
        except ValueError:
            detail = {"raw": response.text[:300]}
        console.print(
            f"[red]✗ input rejected by runtime (422):[/red]\n"
            f"  {detail}\n"
            f"  Inspect the deployed agent's schema: "
            f"[bold]mdk show {name}[/bold] (local) or via the runtime."
        )
        _emit_remote_summary(
            agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
        )
        raise typer.Exit(code=1)
    if response.status_code != _HTTP_OK:
        try:
            body_json = response.json()
        except ValueError:
            body_json = {"raw": response.text[:300]}
        console.print(
            f"[red]✗ HTTP {response.status_code}[/red] from {target_cfg.url}: {body_json}"
        )
        _emit_remote_summary(
            agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
        )
        raise typer.Exit(code=1)

    # Parse the response. The runtime returns a RunView shape; we
    # don't strictly validate (extra fields are forward-compat) — just
    # surface what we got. Output goes to stdout; metadata to stderr.
    try:
        run_view = response.json()
    except ValueError as exc:
        console.print(f"[red]✗ runtime returned non-JSON body:[/red] {exc}")
        _emit_remote_summary(
            agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
        )
        raise typer.Exit(code=1) from None

    _render_remote_run(run_view, output_format=output_format)

    status = (run_view.get("status") or "").lower()
    ok = status == "success"
    metrics = run_view.get("metrics") or {}
    cost = metrics.get("cost_usd")
    latency = metrics.get("latency_ms")
    run_id = run_view.get("run_id")
    _emit_remote_summary(
        agent=name,
        target=target_name,
        run_id=run_id,
        cost=cost,
        latency=latency,
        ok=ok,
    )

    if not ok:
        # Surface the runtime's error envelope on stderr so the
        # operator sees what went wrong without re-parsing JSON.
        err = run_view.get("error") or {}
        if err:
            console.print(
                f"[red]✗ run errored:[/red] {err.get('type', 'unknown')}: {err.get('message', '')}"
            )
        raise typer.Exit(code=1)


def _coerce_remote_agent_input(raw: str, agent_name: str) -> dict[str, Any]:
    """Coerce the positional input for a remote run.

    Same precedence as the local path (``-``/file/JSON) but without a
    local bundle to consult for auto-wrap. As a convenience for inside-
    project demos, we DO try to load the local bundle by name — if it
    exists, we can auto-wrap a plain string into ``{<field>: arg}``;
    if it doesn't (e.g. running against a runtime whose agent isn't
    checked out locally), we require JSON.
    """
    if raw == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    p = Path(raw)
    if p.is_file():
        return _ensure_dict(json.loads(p.read_text()))
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Auto-wrap fallback: if the agent exists locally, use its input
    # schema to wrap a plain string the same way the local path does.
    # Silent skip on any load failure — caller gets a clean error.
    from movate.cli._resolve import resolve_agent_or_workflow_arg  # noqa: PLC0415

    try:
        local_path = Path(resolve_agent_or_workflow_arg(agent_name))
        bundle = load_agent(local_path)
    except (AgentLoadError, FileNotFoundError, ValueError):
        raise typer.BadParameter(
            f"remote input must be JSON (no local bundle for {agent_name!r} to "
            f"auto-wrap from). Pass JSON via --input or as a JSON positional arg."
        ) from None

    schema = bundle.input_schema
    required = list(schema.get("required", []))
    properties = schema.get("properties", {}) or {}
    string_required = [nm for nm in required if properties.get(nm, {}).get("type") == "string"]
    if len(string_required) == 1 and len(required) == 1:
        return {string_required[0]: raw}

    raise typer.BadParameter(
        f"remote input is not valid JSON and cannot be auto-wrapped — agent "
        f"{agent_name!r} requires {required}. Pass JSON via --input or as a "
        f"JSON-formatted positional arg."
    )


def _render_remote_run(run_view: dict[str, Any], *, output_format: Run) -> None:
    """Render the RunView the runtime returned.

    Mirrors the local _run_local_agent rendering:
      - JSON mode: dump the output (or the full RunView if no output).
      - TEXT mode: pretty-print the output as JSON (closest analog to
        ``response.human_readable`` — the local mode's TEXT is the
        Pydantic ``model.human_readable`` property which the runtime
        doesn't emit over the wire).

    Echo the run_id on stderr so it survives stdout-piping (jq, `>`).
    """
    output = run_view.get("output")
    if output_format == Run.TEXT:
        # The runtime doesn't ship the human_readable rendering — that's
        # a Pydantic property on RunResponse. Fall back to pretty JSON.
        sys.stdout.write(json.dumps(output, indent=2, default=str) + "\n")
    else:
        # JSON mode → emit the full RunView so callers can pipe to jq
        # and pick out cost/latency/output. This is symmetric with the
        # local run's `response.model_dump_json(indent=2)` which dumps
        # the full RunResponse model.
        sys.stdout.write(json.dumps(run_view, indent=2, default=str) + "\n")

    run_id = run_view.get("run_id")
    if run_id:
        short = str(run_id)[:8]
        _console.hint(
            f"[dim]→ remote run_id [bold]{short}[/bold] "
            f"(persisted on the runtime, not locally)[/dim]"
        )


def _emit_remote_summary(
    *,
    agent: str,
    target: str,
    run_id: str | None,
    cost: float | None,
    latency: int | None,
    ok: bool,
) -> None:
    """Greppable summary — same shape as the local ``mdk_run_summary``
    with an extra ``target=`` field so CI workflows can branch on
    local-vs-remote runs.
    """
    run_short = (run_id or "")[:8] or "-"
    sys.stderr.write(
        f"mdk_run_summary: kind=agent agent={agent} target={target} "
        f"run_id={run_short} "
        f"cost_usd={cost if cost is not None else '-'} "
        f"latency_ms={latency if latency is not None else '-'} "
        f"ok={'true' if ok else 'false'}\n"
    )
    sys.stderr.flush()
