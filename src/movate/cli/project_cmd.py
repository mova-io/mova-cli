"""``mdk project`` — manage projects + attach agents.

The first subcommand is ``mdk project add-agent``, which mirrors the
five sources of the unified runtime endpoint
(``POST /api/v1/projects/{project_id}/agents``):

* ``--bundle <path>`` → source: "bundle" (multipart upload).
* ``--from-llm "description"`` → source: "llm" (NL → agent, with
  live SSE in the terminal).
* ``--from-catalog <slug>[@version] [--rename <name>]`` →
  source: "catalog" (clone + decouple).
* Future: ``--from-spec``, ``--from-wizard`` will land alongside the
  Mova iO wizard tooling. The runtime already accepts them; the CLI
  parity is in scope for the same backlog item.

The runtime URL + API key are resolved exactly like ``mdk auth me``
(env vars ``MDK_RUNTIME_URL`` + ``MDK_API_KEY`` or the
``mdk auth --target`` credential).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console

from movate.cli._console import error

console = Console()
err_console = Console(stderr=True)


project_app = typer.Typer(
    name="project",
    help="Manage projects + attach agents to them.",
    no_args_is_help=True,
)


# Status codes we branch on — named constants keep the linter happy and
# document the intent inline (HTTP_ACCEPTED is the trigger for the SSE
# stream path; HTTP_BAD_REQUEST is the start of the error band).
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400


def _resolve_runtime() -> tuple[str, str]:
    """Pull (base_url, api_key) from the env, exiting cleanly on miss.

    Mirrors the resolution path used by ``mdk auth me`` — keeps the
    CLI's "how do I find my runtime" story consistent across commands.
    """
    api_key = os.environ.get("MDK_API_KEY", os.environ.get("MOVATE_API_KEY", "")).strip()
    base_url = os.environ.get(
        "MDK_RUNTIME_URL",
        os.environ.get("MOVATE_RUNTIME_URL", ""),
    ).rstrip("/")
    if not api_key:
        error("no API key found. Set MDK_API_KEY or run `mdk auth pull-runtime-key`.")
        raise typer.Exit(code=2)
    if not base_url:
        error("no runtime URL found. Set MDK_RUNTIME_URL.")
        raise typer.Exit(code=2)
    return base_url, api_key


@project_app.command("add-agent")
def add_agent(
    project_id: str = typer.Argument(..., help="Project id to attach the agent to."),
    bundle: Path | None = typer.Option(
        None,
        "--bundle",
        help="Path to a bundle .tar.gz / .zip — uploaded as source=bundle.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    from_llm: str | None = typer.Option(
        None,
        "--from-llm",
        help=(
            "Natural-language description — runs the LLM authoring pipeline. "
            "Live SSE progress streams to the terminal."
        ),
    ),
    from_catalog: str | None = typer.Option(
        None,
        "--from-catalog",
        help=(
            "Catalog slug, optionally suffixed with @<version> "
            "(e.g. `support-ticket-triage@2.1.0`)."
        ),
    ),
    rename: str | None = typer.Option(
        None,
        "--rename",
        help="When cloning from catalog or generating via LLM, rename the agent.",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Deploy target nickname (resolved via `mdk auth`). Currently a "
            "no-op placeholder — env-var resolution is the only path."
        ),
    ),
    auto_seed_kb: bool = typer.Option(
        False, "--auto-seed-kb", help="LLM: seed a starter KB context."
    ),
    include_evals: bool = typer.Option(False, "--include-evals", help="LLM: generate an eval set."),
    include_judge: bool = typer.Option(
        False, "--include-judge", help="LLM: generate a judge agent."
    ),
) -> None:
    """Attach a new agent to a project.

    Pass exactly one of ``--bundle``, ``--from-llm``, or
    ``--from-catalog`` — they pick the source. Other modes
    (``--from-spec``, ``--from-wizard``) ship alongside the Mova iO
    wizard work; the runtime already accepts those bodies.
    """
    _ = target  # reserved for the post-credential-store CLI

    if sum(bool(s) for s in (bundle, from_llm, from_catalog)) != 1:
        error("exactly one of --bundle / --from-llm / --from-catalog must be set")
        raise typer.Exit(code=2)

    base_url, api_key = _resolve_runtime()
    url = f"{base_url}/api/v1/projects/{project_id}/agents"
    headers = {"Authorization": f"Bearer {api_key}"}

    if bundle is not None:
        _post_bundle(url, headers, bundle)
    elif from_catalog is not None:
        _post_catalog(url, headers, from_catalog, rename)
    else:
        assert from_llm is not None
        _post_llm(
            url,
            headers,
            description=from_llm,
            rename=rename,
            auto_seed_kb=auto_seed_kb,
            include_evals=include_evals,
            include_judge=include_judge,
        )


def _post_bundle(url: str, headers: dict[str, str], bundle: Path) -> None:
    with bundle.open("rb") as fh:
        files = {"bundle": (bundle.name, fh, "application/octet-stream")}
        try:
            resp = httpx.post(url, headers=headers, files=files, timeout=120.0)
        except httpx.HTTPError as exc:
            error(f"upload failed: {exc}")
            raise typer.Exit(code=2) from None
    _print_sync_response(resp)


def _post_catalog(url: str, headers: dict[str, str], from_catalog: str, rename: str | None) -> None:
    slug, _, version = from_catalog.partition("@")
    body: dict[str, object] = {"source": "catalog", "slug": slug}
    if version:
        body["version"] = version
    if rename:
        body["rename_to"] = rename
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=60.0)
    except httpx.HTTPError as exc:
        error(f"catalog clone failed: {exc}")
        raise typer.Exit(code=2) from None
    _print_sync_response(resp)


def _post_llm(
    url: str,
    headers: dict[str, str],
    *,
    description: str,
    rename: str | None,
    auto_seed_kb: bool,
    include_evals: bool,
    include_judge: bool,
) -> None:
    body: dict[str, object] = {
        "source": "llm",
        "description": description,
        "auto_seed_kb": auto_seed_kb,
        "include_evals": include_evals,
        "include_judge": include_judge,
    }
    if rename:
        body["rename_to"] = rename
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=60.0)
    except httpx.HTTPError as exc:
        error(f"LLM authoring submit failed: {exc}")
        raise typer.Exit(code=2) from None

    if resp.status_code != HTTP_ACCEPTED:
        _print_sync_response(resp)
        return

    accepted = resp.json()
    stream_url = accepted.get("stream_url")
    if not stream_url:
        error("runtime returned 202 without a stream_url")
        raise typer.Exit(code=2)
    console.print(f"[dim]job_id:[/dim] {accepted.get('job_id', '')}")
    console.print(f"[dim]subscribing to:[/dim] {stream_url}\n")
    _consume_sse(stream_url, headers)


def _consume_sse(stream_url: str, headers: dict[str, str]) -> None:
    """Consume an SSE stream from the runtime, printing each event
    to the console as it arrives. Each line is either an ``event:`` or
    ``data:`` marker per the SSE wire format."""
    try:
        with httpx.stream("GET", stream_url, headers=headers, timeout=300.0) as stream:
            stream.raise_for_status()
            for line in stream.iter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                    console.print(f"[bold cyan]> {event_name}[/bold cyan]")
                elif line.startswith("data:"):
                    raw = line[len("data:") :].strip()
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = raw
                    console.print(f"  {payload}")
    except httpx.HTTPError as exc:
        error(f"SSE stream broke: {exc}")
        raise typer.Exit(code=2) from None


def _print_sync_response(resp: httpx.Response) -> None:
    """Pretty-print a sync 200/4xx/5xx response from the unified endpoint."""
    if resp.status_code >= HTTP_BAD_REQUEST:
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            payload = {"raw": resp.text}
        error(f"runtime returned {resp.status_code}: {payload}")
        sys.exit(2)
    try:
        body = resp.json()
    except json.JSONDecodeError:
        console.print(resp.text)
        return
    console.print_json(data=body)
