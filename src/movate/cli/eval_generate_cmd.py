"""``mdk eval-generate <agent>`` + ``mdk eval-generate-commit`` — CLI
parity for the runtime ``POST /api/v1/agents/{name}/evals/generate`` API.

Two commands (sibling pattern, like ``mdk eval-gen`` / ``mdk
eval-scorecard``) — they hit the configured runtime over HTTP:

* ``mdk eval-generate <agent> --from "<description>"`` — submits a
  generation request, streams the SSE progress bar, and waits for the
  completed result. Prints a summary table the operator can review
  before committing.
* ``mdk eval-generate-commit <agent> <job_id> --cases c1,c3,c5`` —
  POSTs the commit step (selective acceptance + optional judge).

Why a separate file rather than extending :mod:`movate.cli.eval_gen_cmd`:
that module is the *local* (in-process) dataset generator; this one is
the *remote* runtime client (different surface, different auth path,
different lifecycle). They share the dataset format (``generated:
true`` JSONL) but no code paths.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

log = logging.getLogger(__name__)
console = Console()
err_console = Console(stderr=True)


def _resolve_target_url_and_key(target: str | None) -> tuple[str, str]:
    """Return ``(base_url, api_key)`` for the configured / requested target.

    Mirrors the resolution path used by ``mdk auth whoami --target``:
    explicit ``--target`` looks up the named entry in user-config;
    omitted uses the ``MDK_RUNTIME_URL`` + ``MDK_API_KEY`` env vars.
    Exits 2 with a typed message on any resolution failure.
    """
    if target is not None:
        from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

        try:
            _, cfg = resolve_target(target)
        except UserConfigError as exc:
            err_console.print(f"[red]ERROR[/red] {exc}")
            raise typer.Exit(code=2) from None
        api_key = os.environ.get(cfg.key_env, "").strip()
        if not api_key:
            err_console.print(
                f"[red]ERROR[/red] env var ${cfg.key_env} is empty. "
                f"Run [bold]mdk auth refresh-runtime-key {target}[/bold]."
            )
            raise typer.Exit(code=2)
        return cfg.url.rstrip("/"), api_key

    api_key = os.environ.get("MDK_API_KEY", os.environ.get("MOVATE_API_KEY", "")).strip()
    base_url = os.environ.get("MDK_RUNTIME_URL", "").rstrip("/")
    if not api_key:
        err_console.print("[red]ERROR[/red] no API key found. Pass --target or set MDK_API_KEY.")
        raise typer.Exit(code=2)
    if not base_url:
        err_console.print(
            "[red]ERROR[/red] no runtime URL found. Pass --target or set MDK_RUNTIME_URL."
        )
        raise typer.Exit(code=2)
    return base_url, api_key


def _stream_sse_events(
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    json_out: bool,
) -> dict[str, Any] | None:
    """Open the SSE stream and tail its events.

    Returns the parsed ``completed`` / ``error`` event body when the
    stream closes, or ``None`` on a transport failure. In ``--json``
    mode each event is written as a JSON line to stdout so scripts can
    parse them; otherwise progress is rendered to the terminal.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
    terminal: dict[str, Any] | None = None
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(300.0, read=300.0)) as client,
            client.stream(
                "GET", f"{base_url}/api/v1/jobs/{job_id}/stream", headers=headers
            ) as resp,
        ):
            if resp.status_code == httpx.codes.NOT_FOUND:
                err_console.print(f"[red]ERROR[/red] job {job_id!r} not found")
                return None
            if resp.status_code != httpx.codes.OK:
                err_console.print(f"[red]ERROR[/red] HTTP {resp.status_code} from /stream")
                return None
            current_event = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    raw = line.split(":", 1)[1].strip()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if json_out:
                        sys.stdout.write(json.dumps({"event": current_event, **data}) + "\n")
                        sys.stdout.flush()
                    else:
                        _render_event(current_event, data)
                    if current_event in ("completed", "error"):
                        terminal = {"event": current_event, **data}
    except httpx.HTTPError as exc:
        err_console.print(f"[red]ERROR[/red] SSE stream failed: {exc}")
    return terminal


def _render_event(event: str, data: dict[str, Any]) -> None:
    """Pretty-print one SSE event to the terminal."""
    if event == "category_complete":
        console.print(
            f"  [green]+[/green] [bold]{data.get('category')}[/bold] "
            f"({data.get('cases_so_far')} cases so far)"
        )
    elif event == "judge_drafted":
        console.print("  [green]+[/green] judge.yaml drafted")
    elif event == "preview_eval":
        console.print(
            f"  [green]+[/green] preview eval: "
            f"mock pass rate = [bold]{data.get('mock_pass_rate'):.0%}[/bold]"
        )
    elif event == "completed":
        console.print(
            f"  [green]✓[/green] completed: {data.get('case_count')} cases, "
            f"${data.get('cost_usd', 0):.4f}"
        )
    elif event == "error":
        console.print(f"  [red]✗[/red] {data.get('message')}")


def _render_summary_table(result: dict[str, Any]) -> None:
    """Print a one-row-per-case summary so the operator can review
    before committing."""
    cases = result.get("cases") or []
    if not cases:
        console.print("[yellow]No cases generated.[/yellow]")
        return
    t = Table(title=f"Generated cases ({len(cases)})", show_lines=False)
    t.add_column("id", style="cyan", no_wrap=True)
    t.add_column("category", style="magenta")
    t.add_column("rationale")
    for case in cases:
        t.add_row(
            case.get("id", "?"),
            case.get("category", "?"),
            (case.get("rationale", "") or "")[:80],
        )
    console.print(t)
    if result.get("judge_yaml"):
        console.print("[dim]judge.yaml drafted — pass --commit-judge to write it.[/dim]")
    score = result.get("preview_score")
    if score:
        console.print(f"[dim]Preview mock pass rate: {score.get('mock_pass_rate'):.0%}[/dim]")


def eval_generate(
    agent: str = typer.Argument(..., help="Agent name (matches agent.yaml's `name`)."),
    description: str = typer.Option(
        ...,
        "--from",
        help=("Plain-English agent description Claude will use to author the cases."),
    ),
    count: int = typer.Option(20, "--count", "-n", min=1, max=100, help="How many cases."),
    categories: str = typer.Option(
        "happy,edge,adversarial",
        "--categories",
        help="Comma-separated categories. Default: all three.",
    ),
    include_judge: bool = typer.Option(
        False, "--include-judge", help="Also draft a judge.yaml rubric."
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Optional LiteLLM model string. Default: the agent's declared model.",
    ),
    budget_usd: float | None = typer.Option(
        None, "--budget-usd", help="Hard server-side cost ceiling."
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help="Named target from your user-config. Default: MDK_RUNTIME_URL env var.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit one JSON line per SSE event + the final summary as JSON.",
    ),
) -> None:
    """Author an eval dataset from a plain-English agent description.

    [bold]Review-then-commit:[/bold] this command DOES NOT modify the
    agent's dataset on disk. It prints a summary so you can review,
    then quotes the [bold]mdk eval-generate-commit[/bold] line to run
    next.

    [bold]Examples:[/bold]

      $ mdk eval-generate triage --from "triages tickets" --count 30

      $ mdk eval-generate triage --from "..." --include-judge \\
            --budget-usd 0.50 --target dev
    """
    cats = [c.strip() for c in categories.split(",") if c.strip()]
    base_url, api_key = _resolve_target_url_and_key(target)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "description": description,
        "count": count,
        "categories": cats,
        "include_judge": include_judge,
    }
    if model:
        body["model"] = model
    if budget_usd is not None:
        body["budget_usd"] = budget_usd

    try:
        with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
            r = client.post(
                f"{base_url}/api/v1/agents/{agent}/evals/generate",
                json=body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        err_console.print(f"[red]ERROR[/red] could not reach {base_url}: {exc}")
        raise typer.Exit(code=2) from None
    if r.status_code == httpx.codes.UNAUTHORIZED:
        err_console.print("[red]ERROR[/red] 401 Unauthorized — key invalid or expired.")
        raise typer.Exit(code=2)
    if r.status_code == httpx.codes.NOT_FOUND:
        err_console.print(f"[red]ERROR[/red] agent {agent!r} not found on target")
        raise typer.Exit(code=2)
    if r.status_code != httpx.codes.ACCEPTED:
        err_console.print(f"[red]ERROR[/red] HTTP {r.status_code}: {r.text[:200]!r}")
        raise typer.Exit(code=2)
    accepted = r.json()
    job_id = accepted["job_id"]

    if json_output:
        sys.stdout.write(json.dumps({"event": "accepted", **accepted}) + "\n")
        sys.stdout.flush()
    else:
        console.print(
            f"[green]Accepted.[/green] job_id=[bold]{job_id}[/bold] "
            f"(~{accepted.get('estimated_seconds', '?')}s)"
        )
        console.print("Streaming progress…")

    terminal = _stream_sse_events(
        base_url=base_url, api_key=api_key, job_id=job_id, json_out=json_output
    )

    # After SSE terminates, fetch the full job for the summary table —
    # the SSE frames don't carry the case-by-case rationale list, only
    # the per-category counts.
    try:
        r = httpx.get(
            f"{base_url}/api/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        err_console.print(f"[red]ERROR[/red] could not fetch job: {exc}")
        raise typer.Exit(code=2) from None
    if r.status_code != httpx.codes.OK:
        err_console.print(f"[red]ERROR[/red] job fetch HTTP {r.status_code}")
        raise typer.Exit(code=2)
    job_view = r.json()
    if job_view.get("status") == "failed":
        err_console.print(f"[red]✗[/red] job failed: {job_view.get('error')}")
        raise typer.Exit(code=1)
    result = job_view.get("result") or {}

    if json_output:
        sys.stdout.write(json.dumps({"event": "summary", **job_view}) + "\n")
        sys.stdout.flush()
        return

    console.print()
    _render_summary_table(result)
    console.print()
    console.print(
        f"[dim]To commit:[/dim] [bold]mdk eval-generate-commit "
        f"{agent} {job_id}[/bold]" + (" --commit-judge" if result.get("judge_yaml") else "")
    )
    # Reference the terminal event so the function signature stays honest
    # about what _stream_sse_events returned (and the linter doesn't
    # warn on the unused assignment).
    log.debug("eval-generate terminal event: %s", terminal)


def eval_generate_commit(
    agent: str = typer.Argument(..., help="Agent name (matches the generate-step argument)."),
    job_id: str = typer.Argument(..., help="The ``evgen_*`` job id from the generate step."),
    cases: str | None = typer.Option(
        None,
        "--cases",
        help=(
            "Comma-separated case ids to commit (e.g. c1,c3,c5). "
            "Default: commit every case in the job."
        ),
    ),
    commit_judge: bool = typer.Option(
        False, "--commit-judge", help="Also write the drafted judge.yaml."
    ),
    target: str | None = typer.Option(None, "--target", help="Named target."),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Commit selected generated cases to the agent's dataset.

    The only mutation step in the generator flow — appends accepted
    cases to ``<agents_path>/<agent>/evals/dataset.jsonl`` on the
    target runtime.

    [bold]Examples:[/bold]

      $ mdk eval-generate-commit triage evgen_abc123    # commit all

      $ mdk eval-generate-commit triage evgen_abc123 --cases c1,c3,c5
    """
    base_url, api_key = _resolve_target_url_and_key(target)
    case_ids: list[str] | None = None
    if cases:
        case_ids = [c.strip() for c in cases.split(",") if c.strip()]
    body: dict[str, Any] = {"commit_judge": commit_judge}
    if case_ids is not None:
        body["case_ids"] = case_ids
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
            r = client.post(f"{base_url}/api/v1/jobs/{job_id}/commit", json=body, headers=headers)
    except httpx.HTTPError as exc:
        err_console.print(f"[red]ERROR[/red] could not reach {base_url}: {exc}")
        raise typer.Exit(code=2) from None
    if r.status_code == httpx.codes.UNAUTHORIZED:
        err_console.print("[red]ERROR[/red] 401 Unauthorized.")
        raise typer.Exit(code=2)
    if r.status_code == httpx.codes.FORBIDDEN:
        err_console.print("[red]ERROR[/red] 403 Forbidden — key lacks the 'admin' scope.")
        raise typer.Exit(code=2)
    if r.status_code == httpx.codes.NOT_FOUND:
        err_console.print(f"[red]ERROR[/red] job or agent not found: {agent}/{job_id}")
        raise typer.Exit(code=2)
    if r.status_code == httpx.codes.CONFLICT:
        err_console.print(f"[red]ERROR[/red] {r.json().get('detail')}")
        raise typer.Exit(code=2)
    if r.status_code != httpx.codes.OK:
        err_console.print(f"[red]ERROR[/red] HTTP {r.status_code}: {r.text[:200]!r}")
        raise typer.Exit(code=2)
    result = r.json()
    if json_output:
        sys.stdout.write(json.dumps(result) + "\n")
        return
    console.print(
        f"[green]✓[/green] Committed [bold]{result['cases_added']}[/bold] cases "
        f"to [bold]{result['dataset_path']}[/bold]"
        + (" + judge.yaml" if result.get("judge_yaml_updated") else "")
    )
    # Acknowledge the agent argument so the CLI rejects mismatches —
    # the result already carries the agent_name, so this is a sanity
    # check rather than a security boundary (the runtime enforces tenant
    # isolation on its side).
    if result.get("agent_name") != agent:
        err_console.print(
            f"[yellow]warning[/yellow] response agent {result.get('agent_name')!r} "
            f"differs from requested {agent!r}"
        )
