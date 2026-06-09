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
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
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
    validate_graph,
)
from movate.core.workflow.spec import WorkflowSpecLoadError

console = Console(stderr=True)

# Auto-select output format: text for interactive terminals, JSON for pipes/CI.
# `--output json` / `--output text` always override.
_default_output_format: Run = Run.TEXT if sys.stdout.isatty() else Run.JSON


def run(  # noqa: PLR0912 — flat mode-dispatch (remote/replay/estimate/workflow/agent) reads better than nested helpers
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
            "With --stream, POSTs to /api/v1/agents/<name>/runs/stream and "
            "renders tokens live over SSE. Mutually exclusive with --replay "
            "(remote runtime owns the RunRecord history)."
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
    estimate: bool = typer.Option(
        False,
        "--estimate",
        help=(
            "Predict the cost + latency of this run WITHOUT executing it. "
            "Prints an estimate table (no LLM call, no job enqueued, no charge). "
            "The estimate reflects this agent's real assembled prompt + real "
            "historical runs. Works locally and against a deployed --target. "
            "RAG agents exclude retrieved-chunk tokens unless "
            "--estimate-retrieval is also passed (small embedding cost)."
        ),
    ),
    estimate_retrieval: bool = typer.Option(
        False,
        "--estimate-retrieval",
        help=(
            "With --estimate, run the agent's retrieval so retrieved-chunk "
            "tokens are folded into the estimate. Embeds the query (small "
            "cost). No effect on non-RAG agents or without --estimate."
        ),
    ),
    runtime: str = typer.Option(
        None,
        "--runtime",
        help=(
            "Override the workflow execution backend for THIS run only (ADR 055 D3): "
            "[bold]native[/bold] (in-process, default), [bold]temporal[/bold] (durable/"
            "deterministic, needs mdk[temporal] + TEMPORAL_HOST), or [bold]langgraph[/bold] "
            "(not yet wired — fails loud). Precedence: this flag > workflow.yaml 'runtime:' "
            "field > native. Read-only — never mutates the spec. Agent runs ignore it."
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
    # rebuilds from a local RunRecord; nothing remote about it). --stream
    # IS supported remotely: it POSTs to the runtime's SSE endpoint and
    # renders tokens as they arrive (BACKLOG #75).
    if target is not None:
        if replay_id is not None:
            console.print(
                "[red]✗[/red] --target is mutually exclusive with --replay; "
                "remote runtime owns the RunRecord history."
            )
            raise typer.Exit(code=2)
        if estimate:
            _dispatch_remote_agent_estimate(
                agent_name=str(path),
                raw=input_flag or input_arg,
                target=target,
                estimate_retrieval=estimate_retrieval,
                output_format=output_format,
            )
            return
        if stream:
            _dispatch_remote_agent_stream(
                agent_name=str(path),
                raw=input_flag or input_arg,
                target=target,
                mock=mock,
                output_format=output_format,
            )
            return
        _dispatch_remote_agent(
            agent_name=str(path),
            raw=input_flag or input_arg,
            target=target,
            mock=mock,
            output_format=output_format,
        )
        return

    # Name/path resolution (ADR 026 D2): existing path wins, else a bare
    # name resolves under the project's ``agents/`` / ``workflows/``, else a
    # friendly not-found error. `mdk run .` + a standalone agent dir are
    # first-class. The shared resolver backs run / validate / dev.
    from movate.cli._resolve import resolve_agent_arg  # noqa: PLC0415

    try:
        path = resolve_agent_arg(str(path))
    except FileNotFoundError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

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

    if estimate:
        if is_workflow_path(path):
            console.print(
                "[red]✗[/red] --estimate supports agents only; workflow cost "
                "prediction is not available."
            )
            raise typer.Exit(code=2)
        _dispatch_agent_estimate(
            path,
            input_flag or input_arg,
            estimate_retrieval=estimate_retrieval,
            output_format=output_format,
        )
        return

    if is_workflow_path(path):
        if stream:
            # Workflows are multi-step; streaming one node's tokens
            # interleaved with another's would be confusing. Out of scope
            # for v0.5 — surface the limitation explicitly rather than
            # silently ignoring the flag.
            console.print("[red]✗[/red] --stream supports agents only; workflow streaming is TBD")
            raise typer.Exit(code=2)
        _dispatch_workflow(
            path,
            input_flag or input_arg,
            mock=mock,
            output_format=output_format,
            runtime_override=runtime,
        )
    else:
        if runtime is not None:
            # --runtime selects a *workflow* execution backend; it has no
            # meaning for a single agent run. Fail loud rather than silently
            # ignoring it (the operator clearly expected it to do something).
            console.print(
                "[red]✗[/red] --runtime applies to workflows only; "
                "this path is an agent. Drop --runtime."
            )
            raise typer.Exit(code=2)
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


# ---------------------------------------------------------------------------
# Cost prediction (--estimate) — no run executes
# ---------------------------------------------------------------------------


def _dispatch_agent_estimate(
    path: Path,
    raw: str | None,
    *,
    estimate_retrieval: bool,
    output_format: Run,
) -> None:
    """Estimate the cost + latency of a local agent run WITHOUT executing it.

    Loads the bundle, coerces the input the same way a real local run
    does, then calls :func:`movate.core.run_estimator.estimate_run` against
    the local runtime's storage (for historical stats). Prints the estimate
    table (TEXT) or the JSON estimate (JSON). No LLM call, no job, no charge.
    """
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
        _run_local_estimate(
            bundle, payload, estimate_retrieval=estimate_retrieval, output_format=output_format
        )
    )


async def _run_local_estimate(
    bundle: AgentBundle,
    payload: dict[str, Any],
    *,
    estimate_retrieval: bool,
    output_format: Run,
) -> None:
    from movate.core.run_estimator import estimate_run  # noqa: PLC0415

    # A non-mock runtime so the executor's retrieval seam embeds against
    # real backends when --estimate-retrieval is on. The estimator NEVER
    # calls .execute(), so no completion happens regardless.
    rt = await build_local_runtime(mock=False)
    try:
        est = await estimate_run(
            bundle,
            payload,
            storage=rt.storage,
            tenant_id="local",
            executor=rt.executor,
            estimate_retrieval=estimate_retrieval,
        )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    _render_estimate(est_dict=_estimate_to_dict(est), output_format=output_format)


def _estimate_to_dict(est: Any) -> dict[str, Any]:
    """Flatten a :class:`movate.core.run_estimator.RunEstimate` into the
    same JSON shape the runtime returns (``RunEstimateView``), so the local
    + remote renderers share one code path."""
    return {
        "estimate": True,
        "agent_name": est.agent_name,
        "model": est.model,
        "predicted": {
            "tokens_in": est.predicted.tokens_in,
            "tokens_out_max": est.predicted.tokens_out_max,
            "tokens_out_expected": est.predicted.tokens_out_expected,
            "cost_usd_min": est.predicted.cost_usd_min,
            "cost_usd_expected": est.predicted.cost_usd_expected,
            "cost_usd_max": est.predicted.cost_usd_max,
            "latency_ms_p50": est.predicted.latency_ms_p50,
            "latency_ms_p95": est.predicted.latency_ms_p95,
        },
        "basis": {
            "prompt_tokens_method": est.basis.prompt_tokens_method,
            "out_expected_method": est.basis.out_expected_method,
            "latency_method": est.basis.latency_method,
            "sample_size": est.basis.sample_size,
        },
        "budget_check": {
            "within_per_run_budget": est.budget_check.within_per_run_budget,
            "per_run_budget_usd": est.budget_check.per_run_budget_usd,
        },
        "retrieval_embedded": est.retrieval_embedded,
        "notes": list(est.notes),
    }


def _render_estimate(*, est_dict: dict[str, Any], output_format: Run) -> None:
    """Render a RunEstimateView-shaped dict.

    JSON mode → dump the estimate to stdout (pipe-friendly).
    TEXT mode → a rich table to stdout + a one-line greppable summary to
    stderr. NO run executed — the rendering makes that explicit.
    """
    if output_format != Run.TEXT:
        sys.stdout.write(json.dumps(est_dict, indent=2, default=str) + "\n")
        _emit_estimate_summary(est_dict)
        return

    pred = est_dict.get("predicted", {})
    basis = est_dict.get("basis", {})
    budget = est_dict.get("budget_check", {})

    table = Table(
        title=f"Cost estimate — {est_dict.get('agent_name', '?')} ({est_dict.get('model', '?')})",
        show_header=True,
        title_style="bold",
    )
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    def _ms(v: Any) -> str:
        return f"{v} ms" if v is not None else "[dim]unavailable[/dim]"

    table.add_row("tokens in", str(pred.get("tokens_in")))
    table.add_row("tokens out (expected)", str(pred.get("tokens_out_expected")))
    table.add_row("tokens out (max)", str(pred.get("tokens_out_max")))
    table.add_row("cost min (USD)", f"${pred.get('cost_usd_min')}")
    table.add_row("cost expected (USD)", f"${pred.get('cost_usd_expected')}")
    table.add_row("cost max (USD)", f"${pred.get('cost_usd_max')}")
    table.add_row("latency p50", _ms(pred.get("latency_ms_p50")))
    table.add_row("latency p95", _ms(pred.get("latency_ms_p95")))

    within = budget.get("within_per_run_budget")
    budget_cell = (
        f"[green]within[/green] (≤ ${budget.get('per_run_budget_usd')})"
        if within
        else f"[red]OVER[/red] (> ${budget.get('per_run_budget_usd')})"
    )
    table.add_row("per-run budget", budget_cell)

    # rich.Console(stderr=True) is our `console`; estimate output is the
    # command's primary product, so the table goes to stdout instead.
    Console().print(table)

    # Basis line — how the estimate was derived (stderr; honors --quiet).
    _console.hint(
        f"[dim]basis: prompt={basis.get('prompt_tokens_method')} "
        f"out={basis.get('out_expected_method')} "
        f"latency={basis.get('latency_method')} "
        f"(n={basis.get('sample_size')} historical runs)[/dim]"
    )
    if est_dict.get("retrieval_embedded"):
        _console.hint("[dim]→ retrieval embedded for this estimate (small cost)[/dim]")
    for note in est_dict.get("notes", []):
        _console.hint(f"[dim]note: {note}[/dim]")
    _console.hint("[dim]→ NO run executed — this is an estimate only.[/dim]")
    _emit_estimate_summary(est_dict)


def _emit_estimate_summary(est_dict: dict[str, Any]) -> None:
    """Greppable one-line summary (stderr) mirroring mdk_run_summary so CI
    can scrape estimates the same way it scrapes runs."""
    pred = est_dict.get("predicted", {})
    budget = est_dict.get("budget_check", {})
    sys.stderr.write(
        f"mdk_estimate_summary: agent={est_dict.get('agent_name')} "
        f"model={est_dict.get('model')} "
        f"tokens_in={pred.get('tokens_in')} "
        f"cost_expected={pred.get('cost_usd_expected')} "
        f"cost_max={pred.get('cost_usd_max')} "
        f"within_budget={str(budget.get('within_per_run_budget')).lower()} "
        f"executed=false\n"
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
        return f"[dim]→ run [bold]{run_short}[/bold] · [cyan]mdk explain {run_short}[/cyan][/dim]"
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


def _looks_like_existing_file(raw: str) -> bool:
    """True only if ``raw`` names an existing regular file.

    Guards ``Path.is_file()`` against ``OSError``: when ``raw`` is a long
    JSON string rather than a path (common for ``mdk run <agent> '<json>'``),
    ``os.stat`` raises ``ENAMETOOLONG`` (errno 63) and ``Path.is_file()``
    re-raises it instead of returning ``False``. A string that can't even be
    a valid path is simply not a file.
    """
    try:
        return Path(raw).is_file()
    except OSError:
        return False


_SCHEMA_ERROR_TYPES = frozenset({"schema_error", "output_validation_error", "validation_error"})


def _is_schema_error(error_type: str | None) -> bool:
    """True when an error envelope's ``type`` denotes a schema / output-
    validation failure (the class the MockProvider's generic output can't
    satisfy). Tolerant of the few near-synonyms the executor emits; matches
    case-insensitively and on a ``schema`` substring so a renamed variant
    still triggers the hint."""
    if not error_type:
        return False
    lowered = error_type.lower()
    return lowered in _SCHEMA_ERROR_TYPES or "schema" in lowered


_MOCK_SCHEMA_HINT = (
    "hint: the MockProvider's generic output doesn't satisfy this agent's "
    "output_schema. Run without --mock (real provider) to validate end-to-end, "
    "or test against a lenient-schema agent."
)


def _maybe_mock_schema_hint(*, error_type: str | None, mock: bool) -> None:
    """P4 — when a run errored on a schema/output-validation failure AND it
    used ``--mock``, append the actionable hint. Real-provider schema errors
    keep today's bare message. Always renders (it IS an error hint, so it must
    survive ``--quiet``) but only on this specific combination."""
    if mock and _is_schema_error(error_type):
        console.print(f"[yellow]{_MOCK_SCHEMA_HINT}[/yellow]")


def _maybe_trace_line(
    trace_id: str | None,
    *,
    output_format: Run,
    target: str | None,
    run_id: str | None = None,
) -> None:
    """P1 — surface the run's trace id (+ how to view it) on stderr.

    Skipped cleanly when there's no trace id or under ``--json`` (JSON stdout
    must stay machine-parseable; this human hint never goes to stdout). For a
    deployed ``--target`` run we ALSO print a one-line pointer to look the run
    result back up — ``mdk runs show <run_id> --target <target>`` (the inline
    remote run persists a RunRecord on the runtime, queryable by id). Honors
    ``--quiet`` via ``_console.hint``.

    NOTE: the view pointer references ``mdk runs show`` — NOT ``mdk trace``.
    ``mdk trace`` is a local-only Typer group whose sole subcommand is
    ``replay``; there is no ``mdk trace <id> --target`` command, so pointing a
    remote run at it was a dead end (#125)."""
    if output_format != Run.TEXT:
        return
    tid = (trace_id or "").strip()
    if not tid:
        return
    # ADR 031 D1 — when Langfuse is configured, append a one-click deep link
    # to the run's trace. Omitted (no error) when Langfuse isn't wired.
    from movate.tracing.langfuse_link import langfuse_trace_url  # noqa: PLC0415

    lf_url = langfuse_trace_url(tid)
    lf_suffix = f"  ·  [cyan]{lf_url}[/cyan]" if lf_url else ""
    rid = (run_id or "").strip()
    if target and rid:
        _console.hint(
            f"[dim]trace: [bold]{tid}[/bold]  ·  view: [cyan]mdk runs show {rid} "
            f"--target {target}[/cyan]  (or App Insights → Transaction search "
            f"→ paste the id){lf_suffix}[/dim]"
        )
    else:
        _console.hint(f"[dim]trace: [bold]{tid}[/bold]{lf_suffix}[/dim]")


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

    if _looks_like_existing_file(arg):
        return _ensure_dict(json.loads(Path(arg).read_text()))

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


async def _run_local_agent(  # noqa: PLR0912 — linear pipeline + error branches; extraction hurts readability
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
        try:
            if _spin:
                with console.status(
                    f"Running [bold]{bundle.spec.name}[/bold]…",
                    spinner="dots",
                ):
                    response = await rt.executor.execute(bundle, request, on_token=on_token)
            else:
                response = await rt.executor.execute(bundle, request, on_token=on_token)
        finally:
            if on_token is not None:
                # End the streamed preview with a newline so the JSON output
                # below starts on its own line. Inner try/finally guarantees
                # this fires even if execute() raises (rare: most errors
                # are returned as RunResponse status="error", not exceptions).
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

    # P1 — surface the trace id (local runs have no --target pointer).
    trace_id = getattr(response.metrics, "trace_id", "") or response.trace_id
    _maybe_trace_line(trace_id, output_format=output_format, target=None)

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
        # P4 — friendly hint only on (error + schema_error + --mock).
        err_type = response.error.type if response.error else None
        _maybe_mock_schema_hint(error_type=err_type, mock=mock)
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


def _dispatch_workflow(
    path: Path,
    raw: str | None,
    *,
    mock: bool,
    output_format: Run,
    runtime_override: str | None = None,
) -> None:
    try:
        spec, parent = load_workflow_spec(path)
    except WorkflowSpecLoadError as exc:
        console.print(f"[red]✗ workflow.yaml load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    # A JUDGE node (ADR 056) may sit on a bounded reflection back-edge, which is
    # a cycle — so judge workflows compile on the cycle-tolerant path and skip
    # the linear phase gate (the native runner enforces its own runaway cap, and
    # the JUDGE node enforces its own ``max_iterations`` bound). Every non-judge
    # workflow still goes through the unchanged ``validate_linear`` gate.
    has_judge = any(getattr(n, "type", None) == "judge" for n in spec.nodes)
    try:
        graph = compile_workflow(spec, parent, allow_cycles=has_judge)
        if not has_judge:
            validate_graph(graph)
    except WorkflowCompileError as exc:
        console.print(f"[red]✗ workflow validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if raw is None:
        # Default to empty initial state — convenient when state_schema has
        # no `required` fields.
        initial_state: dict[str, Any] = {}
    else:
        initial_state = _coerce_workflow_input(raw)

    asyncio.run(
        _run_local_workflow(
            graph,
            initial_state,
            output_format=output_format,
            mock=mock,
            runtime_override=runtime_override,
        )
    )


def _coerce_workflow_input(arg: str) -> dict[str, Any]:
    """Workflows take a JSON object for ``initial_state`` — no auto-wrap.

    Accepts ``-`` (stdin), a file path, or a JSON string literal.
    """
    if arg == "-":
        return _ensure_dict(json.loads(sys.stdin.read()))
    if _looks_like_existing_file(arg):
        return _ensure_dict(json.loads(Path(arg).read_text()))
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
    runtime_override: str | None = None,
) -> None:
    # ADR 055 D2/D3 — the single dispatch fork. Resolve the effective runtime
    # (override > workflow.yaml 'runtime:' > native), fail loud on an
    # unavailable backend (D6), then route. The native branch is byte-for-byte
    # today's path (no compile, no temporalio import).
    from movate.runtime.langgraph_backend import (  # noqa: PLC0415
        run_langgraph_workflow,
    )
    from movate.runtime.workflow_backend import (  # noqa: PLC0415
        WorkflowBackendError,
        require_backend_available,
        resolve_effective_runtime,
        run_temporal_workflow,
    )

    try:
        effective = resolve_effective_runtime(graph, runtime_override)
        require_backend_available(effective)
    except WorkflowBackendError as exc:
        console.print(f"[red]✗ runtime unavailable:[/red] {exc}")
        raise typer.Exit(code=2) from None

    rt = await build_local_runtime(mock=mock)
    try:
        if effective == "native":
            runner = WorkflowRunner(executor=rt.executor, storage=rt.storage)
            try:
                result = await runner.run(graph, initial_state=initial_state)
            except WorkflowRunError as exc:
                console.print(f"[red]✗ workflow failed:[/red] {exc}")
                raise typer.Exit(code=2) from None
        elif effective == "langgraph":
            # ADR 030 D1 — LangGraph in-process execution. Builds a StateGraph
            # from the IR and executes via the same Executor the native runner
            # uses (ADR 054 D3 reuse). Requires mdk[langgraph] extra.
            try:
                result = await run_langgraph_workflow(
                    graph,
                    initial_state,
                    executor=rt.executor,
                    tracer=rt.tracer,
                    storage=rt.storage,
                    tenant_id="local",
                    mock=mock,
                )
            except Exception as exc:
                console.print(f"[red]✗ langgraph execution failed:[/red] {exc}")
                raise typer.Exit(code=2) from None
        else:
            # temporal — compile (Track B) + execute on Temporal via Track C
            # activities. Reuses the SAME provider/pricing/tracer/storage the
            # native runner uses (ADR 054 D3, one execution model).
            from movate.providers.pricing import load_pricing  # noqa: PLC0415

            try:
                result = await run_temporal_workflow(
                    graph,
                    initial_state,
                    storage=rt.storage,
                    pricing=load_pricing(),
                    tracer=rt.tracer,
                    provider=rt.provider,
                    tenant_id="local",
                    mock=mock,
                )
            except WorkflowBackendError as exc:
                console.print(f"[red]✗ temporal execution failed:[/red] {exc}")
                raise typer.Exit(code=2) from None
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if output_format == Run.JSON:
        _emit_workflow_json(result)
    else:
        _emit_workflow_text(result)
        # After a durable run, point the user at its timeline — the
        # multi-activity trace view that the trace-context propagation (the
        # interceptor on the client) now renders as one connected trace.
        if effective == "temporal":
            _print_temporal_trace_hints(result)

    if result.status is WorkflowStatus.ERROR:
        raise typer.Exit(code=1)


def _print_temporal_trace_hints(result: WorkflowResult) -> None:
    """Point the user at a durable run's Temporal Web timeline (its per-activity
    trace view).

    The run id IS the Temporal workflow id (ADR 054 D6), so ``mdk workflow web``
    deep-links straight to it. Always print the command (works regardless of
    env); add the resolved URL when a UI base is configured. Best-effort — a
    hint must never break the run, so any resolution failure is swallowed.
    """
    run_id = result.workflow_run_id
    console.print(
        f"[dim]↳ durable timeline + per-activity trace:[/dim] "
        f"[cyan]mdk workflow web {run_id} --open[/cyan]"
    )
    try:
        from movate.cli.workflow_cmd import (  # noqa: PLC0415
            _resolve_temporal_ui_base,
            _temporal_web_url,
        )

        base = _resolve_temporal_ui_base()
        if not base:
            return
        try:
            from movate.runtime.workflow_backend import (  # noqa: PLC0415
                _resolve_temporal_connection,
            )

            namespace = _resolve_temporal_connection().namespace
        except Exception:
            namespace = "default"
        console.print(f"[dim]  {_temporal_web_url(base, namespace, run_id)}[/dim]")
    except Exception:  # pragma: no cover — a hint never breaks a run
        pass


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
    if result.status is WorkflowStatus.PAUSED:
        # ADR 017 D5 (PR 1): paused at a HUMAN gate — resumable, not a failure.
        return "[yellow]PAUSED[/yellow]"
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
_HTTP_ACCEPTED = 202
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
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

    # Echo which target + URL + credential source (masked) we're about
    # to hit, so a subsequent 401/403 is self-diagnosing. Suppressed
    # under --json (machine output) and --quiet; stderr-only so it
    # never corrupts the JSON run view on stdout.
    _console.echo_remote_context(
        target_name, target_cfg, action="run", suppress=output_format != Run.TEXT
    )

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
    # Long-ish timeout — inline mode blocks for the full agent run.
    # Typical LLM call is a few seconds; tool-use loops can stretch.
    # 120s gives us headroom without hanging forever on a stuck pod.
    timeout = httpx.Timeout(120.0, connect=10.0)

    def _post(bearer: str) -> httpx.Response:
        """Send the run request with the given bearer. Factored out so the
        401 shell-shadow path can replay the SAME request once with the saved
        file key (see below) without duplicating the POST."""
        with httpx.Client(timeout=timeout) as client:
            return client.post(
                f"{base_url}/api/v1/agents/{name}/runs",
                params={"wait": "true"},
                json=body,
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                },
            )

    console.print(
        f"[dim]→ running [bold]{name}[/bold] on [bold]{target_name}[/bold] "
        f"({target_cfg.url}) …[/dim]"
    )

    try:
        response = _post(api_key)
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
        # Shell-shadow case: the bearer we sent came from $<key_env>, and
        # the credentials file holds a DIFFERENT (presumably fresher, e.g.
        # just-deployed) value for the same var. Autoload never clobbers a
        # shell export, so the stale shell value is winning. `unset` is the
        # real fix here — `save-runtime-key` writes to the file the shell is
        # already shadowing, so it would loop. Only when the file has no
        # entry (shell is the sole source) do we fall back to save/pull.
        from movate.credentials.store import CredentialsStore  # noqa: PLC0415

        file_value = (CredentialsStore().get(target_cfg.key_env) or "").strip()
        # Auto-retry ONCE with the saved key. We get here only when the bearer
        # we sent was the shell value (api_key == os.environ[key_env]); if the
        # file holds a NON-EMPTY value that DIFFERS, the shell export is almost
        # certainly stale and shadowing a good saved key. Replay the same
        # request once with the file key so the user is spared the
        # `env -u <KEY> mdk run …` / `unset` dance. Guards: shell was the sent
        # bearer, file exists, file != shell. No file entry / file == shell /
        # bearer-from-file → no retry. At most once; never recurse.
        shell_value = os.environ.get(target_cfg.key_env, "").strip()
        if shell_value == api_key and file_value and file_value != api_key:
            try:
                retry_response = _post(file_value)
            except httpx.HTTPError:
                retry_response = None
            if retry_response is not None and retry_response.status_code != _HTTP_UNAUTHORIZED:
                # Saved key worked. Swap in the good response, drop a one-line
                # stderr note (honors --quiet via _console.hint; never touches
                # stdout, so --json stays clean), and fall through to the
                # normal success/error mapping below.
                response = retry_response
                _console.hint(
                    f"[dim]note: your shell ${target_cfg.key_env} 401'd; used your saved "
                    f"key instead — unset ${target_cfg.key_env} (it's shadowing the "
                    f"saved one).[/dim]"
                )
        # Still a 401 — either no retry happened (guards failed) or the saved
        # key 401'd too. Fall back to the existing hint + exit, unchanged.
        if response.status_code == _HTTP_UNAUTHORIZED:
            if file_value and file_value != api_key:
                console.print(
                    f"[red]✗ runtime rejected the bearer token[/red] "
                    f"(value starts with: '{prefix}…').\n"
                    f"  A stale [bold]{target_cfg.key_env}[/bold] exported in your shell is "
                    f"shadowing a different key saved in ~/.movate/credentials.\n"
                    f"  Fix: [bold]unset {target_cfg.key_env}[/bold] (and remove it from your "
                    f"profile) so the saved key is used."
                )
            else:
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
    # P5 — async submission. Even with ?wait=true the runtime MAY return a
    # queued job (202 + RunAccepted: {job_id, status: queued}) rather than an
    # inline result. Surface the poll/cancel follow-ups + exit cleanly instead
    # of mis-reporting a queued job as an HTTP error.
    if response.status_code == _HTTP_ACCEPTED:
        _handle_remote_async_submission(
            response, name=name, target_name=target_name, output_format=output_format
        )
        return
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

    _render_remote_run(run_view, output_format=output_format, target_name=target_name)

    status = (run_view.get("status") or "").lower()
    ok = status == "success"
    metrics = run_view.get("metrics") or {}
    cost = metrics.get("cost_usd")
    latency = metrics.get("latency_ms")
    run_id = run_view.get("run_id")

    # P1 — surface the trace id + a deployed-target view pointer.
    _maybe_trace_line(
        metrics.get("trace_id"),
        output_format=output_format,
        target=target_name,
        run_id=run_id,
    )

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
        # P4 — mock + schema-error hint (only on that combination).
        _maybe_mock_schema_hint(error_type=err.get("type") if err else None, mock=mock)
        raise typer.Exit(code=1)


def _dispatch_remote_agent_estimate(
    *,
    agent_name: str,
    raw: str | None,
    target: str,
    estimate_retrieval: bool,
    output_format: Run,
) -> None:
    """Estimate a deployed agent's run cost + latency WITHOUT executing it.

    Resolves the target the same way :func:`_dispatch_remote_agent` does,
    then POSTs to ``/api/v1/agents/<name>/runs?estimate=true`` and renders
    the returned :class:`movate.runtime.schemas.RunEstimateView`. No run
    executes server-side; the endpoint returns 200 with the estimate.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

    name = Path(agent_name).name or agent_name

    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    _console.echo_remote_context(
        target_name, target_cfg, action="estimate", suppress=output_format != Run.TEXT
    )

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
            "(remote estimates require JSON input; no schema available client-side)."
        )
        raise typer.Exit(code=2)

    payload = _coerce_remote_agent_input(raw, name)
    base_url = target_cfg.url.rstrip("/")
    timeout = httpx.Timeout(60.0, connect=10.0)

    params = {"estimate": "true"}
    if estimate_retrieval:
        params["estimate_retrieval"] = "true"

    console.print(
        f"[dim]→ estimating [bold]{name}[/bold] on [bold]{target_name}[/bold] "
        f"({target_cfg.url}) — no run will execute …[/dim]"
    )
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/api/v1/agents/{name}/runs",
                params=params,
                json={"input": payload},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        console.print(f"[red]✗ network error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if response.status_code != _HTTP_OK:
        try:
            body_json = response.json()
        except ValueError:
            body_json = {"raw": response.text[:300]}
        console.print(
            f"[red]✗ HTTP {response.status_code}[/red] from {target_cfg.url}: {body_json}"
        )
        raise typer.Exit(code=1)

    try:
        est_dict = response.json()
    except ValueError as exc:
        console.print(f"[red]✗ runtime returned non-JSON body:[/red] {exc}")
        raise typer.Exit(code=1) from None

    _render_estimate(est_dict=est_dict, output_format=output_format)


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
    if _looks_like_existing_file(raw):
        return _ensure_dict(json.loads(Path(raw).read_text()))
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


def _render_remote_run(
    run_view: dict[str, Any], *, output_format: Run, target_name: str | None = None
) -> None:
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

    # Surface WHICH agent version served the run. ADR 021 made deploys
    # publish content-addressed versions (e.g. ``0.1.0+9f3a1c0d``) and the
    # runtime resolves "latest" by default, so after a redeploy the operator's
    # first question is "did my edit take effect — which version answered?".
    # The runtime's RunView already carries ``agent_version`` (resolved bundle
    # for a success, ``bundle.spec.version`` on an error), so this is a pure
    # render of a field that's in the response. TEXT mode only + stderr (via
    # _console.hint): in JSON mode the version is already in the dumped RunView
    # on stdout, so injecting nothing keeps --json a pure passthrough.
    if output_format == Run.TEXT:
        served_version = (run_view.get("agent_version") or "").strip()
        served_agent = (run_view.get("agent") or "").strip()
        if served_version:
            who = f"{served_agent} " if served_agent else ""
            _console.hint(f"[dim]→ served by [bold]{who}{served_version}[/bold][/dim]")

    run_id = run_view.get("run_id")
    if run_id:
        short = str(run_id)[:8]
        # An inline (`wait=true`) remote run persists a RunRecord but does
        # NOT enqueue a queryable JobRecord — so `mdk jobs list` won't show
        # it. Point at `mdk runs show <run_id>` so the operator can look the
        # result back up by id later (closes the "how do I check results?"
        # gap). The hint carries the full id (not the short prefix) so it's
        # copy-pasteable.
        if target_name:
            _console.hint(
                f"[dim]→ remote run_id [bold]{short}[/bold] (persisted on the "
                f"runtime, not locally) · inspect: "
                f"[cyan]mdk runs show {run_id} --target {target_name}[/cyan][/dim]"
            )
        else:
            _console.hint(
                f"[dim]→ remote run_id [bold]{short}[/bold] "
                f"(persisted on the runtime, not locally)[/dim]"
            )


def _handle_remote_async_submission(
    response: Any,
    *,
    name: str,
    target_name: str,
    output_format: Run,
) -> None:
    """P5 — render the poll/cancel follow-ups for an async (queued)
    submission and emit a success summary.

    The runtime returns ``202 + {job_id, status: queued}`` when it enqueues a
    job instead of running inline. We print the JSON body to stdout (so it
    stays pipe-parseable) and, in text mode, a one-line hint to stderr with the
    ``mdk jobs wait`` / ``mdk jobs cancel`` follow-ups. Exits 0 — a queued
    submission is a successful handoff, not a failure."""
    try:
        accepted = response.json()
    except ValueError:
        accepted = {"raw": response.text[:300]}

    # Body to stdout in both modes — the RunAccepted shape (job_id + status)
    # is small and already machine-parseable, so it doubles as the text-mode
    # render and the JSON output a caller would pipe to jq.
    sys.stdout.write(json.dumps(accepted, indent=2, default=str) + "\n")

    job_id = accepted.get("job_id") if isinstance(accepted, dict) else None
    if job_id and output_format == Run.TEXT:
        _console.hint(
            f"[dim]job [bold]{job_id}[/bold] queued · "
            f"poll: [cyan]mdk jobs wait {job_id} --target {target_name}[/cyan] · "
            f"cancel: [cyan]mdk jobs cancel {job_id} --target {target_name}[/cyan][/dim]"
        )

    # Greppable summary — the handoff succeeded (job is enqueued); the run's
    # own success/failure is observed later via `mdk jobs wait`.
    _emit_remote_summary(
        agent=name,
        target=target_name,
        run_id=None,
        cost=None,
        latency=None,
        ok=True,
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


# ---------------------------------------------------------------------------
# Remote streaming (--target + --stream) — SSE over /runs/stream (BACKLOG #75)
# ---------------------------------------------------------------------------


def parse_sse_events(
    lines: Iterable[str],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse an SSE line stream into ``(event, data)`` tuples.

    Minimal Server-Sent Events reader matching the runtime's
    ``/runs/stream`` frame shape: an ``event:`` line, a ``data:`` line
    (compact JSON), terminated by a blank line. Lines are expected to be
    already-decoded strings (trailing ``\\r``/``\\n`` tolerated). A blank
    line flushes the buffered frame; ``data`` is JSON-decoded (a non-JSON
    ``data:`` line is wrapped as ``{"raw": "<value>"}`` so a malformed
    frame never crashes the consumer).

    Pulled out as a pure function so it's unit-testable without a live
    HTTP stream.
    """
    event: str | None = None
    data_buf: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line == "":
            if event is not None or data_buf:
                yield (event or "message", _decode_sse_data(data_buf))
            event = None
            data_buf = []
            continue
        if line.startswith(":"):
            # SSE comment / heartbeat — ignore.
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_buf.append(line[len("data:") :].lstrip(" "))


def _decode_sse_data(data_buf: list[str]) -> dict[str, Any]:
    """JSON-decode a frame's accumulated ``data:`` lines into a dict.

    Multi-line ``data:`` fields join with ``\\n`` per the SSE spec.
    Non-JSON / non-object payloads are wrapped as ``{"raw": ...}`` so the
    consumer always gets a dict."""
    payload = "\n".join(data_buf)
    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return {"raw": payload}
    if not isinstance(data, dict):
        return {"raw": data}
    return data


async def _aiter_sse(resp: Any) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Async analog of :func:`parse_sse_events` over an ``httpx``
    streaming response's ``aiter_lines()``. Same framing rules, so the
    wire shape has a single source of truth."""
    event: str | None = None
    data_buf: list[str] = []
    async for raw_line in resp.aiter_lines():
        line = raw_line.rstrip("\r\n")
        if line == "":
            if event is not None or data_buf:
                yield (event or "message", _decode_sse_data(data_buf))
            event = None
            data_buf = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_buf.append(line[len("data:") :].lstrip(" "))


def _dispatch_remote_agent_stream(
    *,
    agent_name: str,
    raw: str | None,
    target: str,
    mock: bool,
    output_format: Run,
) -> None:
    """Run an agent against a deployed runtime, streaming tokens via SSE.

    The streaming sibling of :func:`_dispatch_remote_agent`: resolves the
    target's URL + bearer the same way, but POSTs to
    ``/api/v1/agents/<name>/runs/stream`` with an ``httpx`` streaming
    request, parses the SSE frames, and prints each token delta to stderr
    live (mirroring the local ``--stream`` render — stderr so stdout stays
    clean for the final output). On the terminal ``done`` frame it renders
    the final output to stdout; on an ``error`` frame it surfaces the
    runtime's error envelope.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

    name = Path(agent_name).name or agent_name

    try:
        target_name, target_cfg = resolve_target(target)
    except UserConfigError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

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
        "Accept": "text/event-stream",
    }
    # Read timeout disabled (None) so a slow token stream doesn't trip a
    # per-read deadline; connect timeout stays bounded so a dead pod
    # fails fast rather than hanging.
    timeout = httpx.Timeout(None, connect=10.0)

    console.print(
        f"[dim]→ streaming [bold]{name}[/bold] on [bold]{target_name}[/bold] "
        f"({target_cfg.url}) …[/dim]"
    )

    async def _consume() -> dict[str, Any]:
        """Open the SSE stream, render token deltas live, return the
        terminal frame's data (``done`` or ``error``). Raises
        :class:`typer.Exit` on HTTP errors so the caller's summary line
        still fires."""
        terminal: dict[str, Any] = {}
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(
                "POST",
                f"{base_url}/api/v1/agents/{name}/runs/stream",
                json=body,
                headers=headers,
            ) as resp,
        ):
            if resp.status_code != _HTTP_OK:
                # Drain so the friendly mapper can read the body.
                await resp.aread()
                _handle_remote_stream_http_error(
                    resp, name=name, target_name=target_name, target_cfg=target_cfg
                )
            async for event, data in _aiter_sse(resp):
                if event == "token":
                    text = data.get("text", "")
                    if text:
                        sys.stderr.write(text)
                        sys.stderr.flush()
                elif event in ("done", "error"):
                    terminal = {"event": event, **data}
                    break
        return terminal

    try:
        terminal = asyncio.run(_consume())
    except httpx.HTTPError as exc:
        sys.stderr.write("\n")
        console.print(f"[red]✗ network error:[/red] {exc}")
        _emit_remote_summary(
            agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
        )
        raise typer.Exit(code=2) from None

    # Newline terminates the streamed preview so the JSON/summary below
    # starts cleanly — mirrors the local --stream path.
    sys.stderr.write("\n")
    sys.stderr.flush()

    if terminal.get("event") == "done":
        run_id = terminal.get("run_id")
        status = (terminal.get("status") or "").lower()
        ok = status == "success"
        metrics = terminal.get("metrics") or {}
        output = terminal.get("output")

        if output_format == Run.TEXT:
            sys.stdout.write(json.dumps(output, indent=2, default=str) + "\n")
        else:
            sys.stdout.write(json.dumps(terminal, indent=2, default=str) + "\n")
        if run_id:
            short = str(run_id)[:8]
            _console.hint(
                f"[dim]→ remote run_id [bold]{short}[/bold] "
                f"(persisted on the runtime, not locally)[/dim]"
            )
        # P1 — surface the trace id + a deployed-target view pointer.
        _maybe_trace_line(
            metrics.get("trace_id"),
            output_format=output_format,
            target=target_name,
            run_id=run_id,
        )
        _emit_remote_summary(
            agent=name,
            target=target_name,
            run_id=run_id,
            cost=metrics.get("cost_usd"),
            latency=metrics.get("latency_ms"),
            ok=ok,
        )
        if not ok:
            # P4 — a done frame can still carry an error envelope.
            err = terminal.get("error") or {}
            _maybe_mock_schema_hint(error_type=err.get("type") if err else None, mock=mock)
            raise typer.Exit(code=1)
        return

    # error frame (or no terminal frame at all → treat as error)
    msg = terminal.get("message", "stream ended without a terminal event")
    code = terminal.get("code", "stream_error")
    console.print(f"[red]✗ run errored:[/red] {code}: {msg}")
    # P4 — mock + schema-error hint (the error frame's ``code`` is the type).
    _maybe_mock_schema_hint(error_type=code, mock=mock)
    _emit_remote_summary(
        agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
    )
    raise typer.Exit(code=1)


def _handle_remote_stream_http_error(
    resp: Any,
    *,
    name: str,
    target_name: str,
    target_cfg: Any,
) -> None:
    """Map a non-200 SSE response to the same friendly errors the
    non-streaming remote path uses, then exit. ``resp`` must already be
    drained (``await resp.aread()``)."""
    status_code = resp.status_code
    if status_code == _HTTP_UNAUTHORIZED:
        console.print(
            "[red]✗ runtime rejected the bearer token[/red].\n"
            f"  Check your env: [bold]echo ${target_cfg.key_env}[/bold] — "
            "likely stale.\n"
            f"  Fix: [bold]mdk auth save-runtime-key {target_name} <new-key>[/bold]."
        )
    elif status_code == _HTTP_FORBIDDEN:
        console.print(
            "[red]✗ token lacks the [bold]run[/bold] scope[/red] — "
            "streaming needs a key minted with run permission."
        )
    elif status_code == _HTTP_NOT_FOUND:
        console.print(
            f"[red]✗ agent [bold]{name}[/bold] not found on [bold]{target_name}[/bold].[/red]\n"
            f"  Did you forget to [bold]mdk deploy --target {target_name}[/bold] first?"
        )
    else:
        try:
            detail = resp.json()
        except (ValueError, json.JSONDecodeError):
            detail = {"raw": resp.text[:300]}
        console.print(f"[red]✗ HTTP {status_code}[/red] from {target_cfg.url}: {detail}")
    _emit_remote_summary(
        agent=name, target=target_name, run_id=None, cost=None, latency=None, ok=False
    )
    raise typer.Exit(code=1)
