"""``mdk project`` — tenant-scoped Project CRUD + membership (ADR 040).

Front door to the ``/api/v1/projects*`` runtime surface. Sibling to
``mdk agent`` (which manages agent versions) and ``mdk tenants`` (which
manages tenant budgets); a project is the tenant-scoped container that
attaches agents/workflows/KBs together with a member/role model layered
on top of ADR 013 tenant scopes.

All subcommands talk to a deployed runtime via :class:`MovateClient`
(same pattern as ``mdk submit`` / ``mdk jobs *``). The ``--target``
flag resolves the runtime URL + bearer token; falls through to the
process-wide default set by ``mdk -t <name>`` / ``MDK_TARGET``.

Subcommands::

    mdk project create <name> [--description ...] [--owner ...] -t <env>
    mdk project list [--include-archived] [--json] -t <env>
    mdk project show <project_id> [--json] -t <env>
    mdk project update <project_id> [--name ...] [--description ...] -t <env>
    mdk project archive <project_id> [--yes] -t <env>
    mdk project members list <project_id> [--json] -t <env>
    mdk project members add <project_id> --principal <id> --role <role> -t <env>
    mdk project members remove <project_id> --principal <id> [--yes] -t <env>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import (
    confirm_destructive,
    echo_remote_context,
    error,
    get_global_target,
    hint,
    success,
)
from movate.cli._output import TableJson
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import ProjectMemberRole
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.runtime.schemas import (
    ProjectListResponse,
    ProjectMemberListView,
    ProjectMemberView,
    ProjectView,
)

stdout = Console()
err = Console(stderr=True)
# Alias used by the ``add-agent`` (unified agent-creation) command + helpers,
# which were authored against a ``console`` name. Same stdout Console.
console = stdout

project_app = typer.Typer(
    name="project",
    help="Manage tenant-scoped projects + members (ADR 040).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

members_app = typer.Typer(
    name="members",
    help="Manage a project's member roster (viewer / editor / owner).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
project_app.add_typer(members_app, name="members")


# ---------------------------------------------------------------------------
# Client builder — identical pattern to mdk jobs / mdk submit.
# ---------------------------------------------------------------------------


def _build_client(target: str | None, *, suppress: bool = False) -> MovateClient:
    """Resolve a target name → MovateClient (mirrors ``jobs._build_client``).

    Per-command ``--target`` wins; otherwise falls through to the
    process-wide default (``mdk -t`` / ``MDK_TARGET``), then to the
    active config target. Echoes the resolved target on stderr so a
    401/403 from the runtime is self-diagnosing; ``suppress=True``
    (passed by ``--json`` callers) silences the echo for machine-clean
    stdout.
    """
    try:
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    echo_remote_context(target_name, target_cfg, suppress=suppress)
    return MovateClient(base_url=target_cfg.url, api_key=token)


# ---------------------------------------------------------------------------
# Project commands
# ---------------------------------------------------------------------------


@project_app.command("create")
def create_project(
    name: str = typer.Argument(..., help="Project name (unique within the tenant)."),
    description: str | None = typer.Option(
        None, "--description", "-d", help="Human-readable description."
    ),
    owner: str | None = typer.Option(
        None,
        "--owner",
        help=(
            "Explicit ``owner_principal_id``. Omit to default to the caller's "
            "principal (``api_key:<key_id>`` for opaque-key auth)."
        ),
    ),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help="Deployment target name (from `mdk config list-targets`).",
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Create a new project in the active tenant."""
    client = _build_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(_create(client, name=name, description=description, owner=owner, fmt=output_format))


@project_app.command("list")
def list_projects(
    include_archived: bool = typer.Option(
        False, "--include-archived", help="Include soft-deleted projects."
    ),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List this tenant's projects, newest-first."""
    client = _build_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(_list(client, include_archived=include_archived, fmt=output_format))


@project_app.command("show")
def show_project(
    project_id: str = typer.Argument(..., help="Project id (``prj_...``)."),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Show one project's detail."""
    client = _build_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(_show(client, project_id=project_id, fmt=output_format))


@project_app.command("update")
def update_project(
    project_id: str = typer.Argument(..., help="Project id."),
    name: str | None = typer.Option(None, "--name", help="New name (unique within tenant)."),
    description: str | None = typer.Option(None, "--description", "-d", help="New description."),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Rename / re-describe a project."""
    if name is None and description is None:
        error("nothing to update — pass --name and/or --description")
        raise typer.Exit(code=2)
    client = _build_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(
        _update(
            client,
            project_id=project_id,
            name=name,
            description=description,
            fmt=output_format,
        )
    )


@project_app.command("archive")
def archive_project(
    project_id: str = typer.Argument(..., help="Project id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt."),
    target: str = typer.Option(None, "--target", "-t"),
) -> None:
    """Soft-delete (archive) a project.

    The per-tenant ``default`` project cannot be archived (the runtime
    returns 422)."""
    confirm_destructive(
        f"Archive project {project_id}? Soft-delete only — re-list with --include-archived.",
        yes=yes,
    )
    client = _build_client(target)
    asyncio.run(_archive(client, project_id=project_id))


# ---------------------------------------------------------------------------
# Member subcommands
# ---------------------------------------------------------------------------


@members_app.command("list")
def list_members(
    project_id: str = typer.Argument(..., help="Project id."),
    target: str = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List a project's members (creation order)."""
    client = _build_client(target, suppress=output_format == TableJson.JSON)
    asyncio.run(_list_members(client, project_id=project_id, fmt=output_format))


@members_app.command("add")
def add_member(
    project_id: str = typer.Argument(..., help="Project id."),
    principal: str = typer.Option(
        ..., "--principal", help="Principal id to add (e.g. ``api_key:KEYID``)."
    ),
    role: ProjectMemberRole = typer.Option(
        ProjectMemberRole.VIEWER,
        "--role",
        case_sensitive=False,
        help="viewer | editor | owner",
    ),
    target: str = typer.Option(None, "--target", "-t"),
) -> None:
    """Invite a principal to the project with a role."""
    client = _build_client(target)
    asyncio.run(_add_member(client, project_id=project_id, principal=principal, role=role))


@members_app.command("remove")
def remove_member(
    project_id: str = typer.Argument(..., help="Project id."),
    principal: str = typer.Option(..., "--principal", help="Principal id to remove."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirm prompt."),
    target: str = typer.Option(None, "--target", "-t"),
) -> None:
    """Remove a member from the project.

    The runtime rejects removing the last ``owner`` (422 — promote
    someone else first)."""
    confirm_destructive(
        f"Remove {principal} from project {project_id}?",
        yes=yes,
    )
    client = _build_client(target)
    asyncio.run(_remove_member(client, project_id=project_id, principal=principal))


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _create(
    client: MovateClient,
    *,
    name: str,
    description: str | None,
    owner: str | None,
    fmt: TableJson,
) -> None:
    async with client:
        try:
            view = await client.create_project(
                name=name, description=description, owner_principal_id=owner
            )
        except MovateClientError as exc:
            error(str(exc), context="project.create")
            raise typer.Exit(code=1) from None
    _emit_project(view, fmt=fmt)
    if fmt == TableJson.TABLE:
        hint(f"[dim]project {view.project_id} created[/dim]")


async def _list(client: MovateClient, *, include_archived: bool, fmt: TableJson) -> None:
    async with client:
        try:
            listing = await client.list_projects(include_archived=include_archived)
        except MovateClientError as exc:
            error(str(exc), context="project.list")
            raise typer.Exit(code=1) from None
    _emit_project_list(listing, fmt=fmt)


async def _show(client: MovateClient, *, project_id: str, fmt: TableJson) -> None:
    async with client:
        try:
            view = await client.get_project(project_id)
        except MovateClientError as exc:
            error(str(exc), context="project.show")
            raise typer.Exit(code=1) from None
    _emit_project(view, fmt=fmt)


async def _update(
    client: MovateClient,
    *,
    project_id: str,
    name: str | None,
    description: str | None,
    fmt: TableJson,
) -> None:
    async with client:
        try:
            view = await client.update_project(project_id, name=name, description=description)
        except MovateClientError as exc:
            error(str(exc), context="project.update")
            raise typer.Exit(code=1) from None
    _emit_project(view, fmt=fmt)


async def _archive(client: MovateClient, *, project_id: str) -> None:
    async with client:
        try:
            view = await client.archive_project(project_id)
        except MovateClientError as exc:
            error(str(exc), context="project.archive")
            raise typer.Exit(code=1) from None
    success(f"archived project {view.project_id} (name={view.name!r}) at {view.archived_at}")


async def _list_members(client: MovateClient, *, project_id: str, fmt: TableJson) -> None:
    async with client:
        try:
            listing = await client.list_project_members(project_id)
        except MovateClientError as exc:
            error(str(exc), context="project.members.list")
            raise typer.Exit(code=1) from None
    _emit_member_list(listing, fmt=fmt)


async def _add_member(
    client: MovateClient,
    *,
    project_id: str,
    principal: str,
    role: ProjectMemberRole,
) -> None:
    async with client:
        try:
            view = await client.add_project_member(project_id, principal_id=principal, role=role)
        except MovateClientError as exc:
            error(str(exc), context="project.members.add")
            raise typer.Exit(code=1) from None
    success(f"added {view.principal_id} to project {view.project_id} as {view.role.value}")


async def _remove_member(client: MovateClient, *, project_id: str, principal: str) -> None:
    async with client:
        try:
            await client.remove_project_member(project_id, principal)
        except MovateClientError as exc:
            error(str(exc), context="project.members.remove")
            raise typer.Exit(code=1) from None
    success(f"removed {principal} from project {project_id}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _emit_project(view: ProjectView, *, fmt: TableJson) -> None:
    if fmt == TableJson.JSON:
        stdout.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    table = Table(title=f"project {view.name}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("project_id", view.project_id)
    table.add_row("name", view.name)
    table.add_row("description", view.description or "")
    table.add_row("owner_principal_id", view.owner_principal_id)
    table.add_row("created_at", view.created_at.isoformat())
    table.add_row("updated_at", view.updated_at.isoformat())
    if view.archived_at is not None:
        table.add_row("archived_at", view.archived_at.isoformat())
    table.add_row("etag", view.etag)
    stdout.print(table)


def _emit_project_list(listing: ProjectListResponse, *, fmt: TableJson) -> None:
    if fmt == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if listing.count == 0:
        hint("[dim]no projects yet[/dim]")
        return
    table = Table(title=f"projects ({listing.count})")
    table.add_column("project_id", style="bold")
    table.add_column("name")
    table.add_column("description")
    table.add_column("archived")
    for p in listing.projects:
        table.add_row(
            p.project_id,
            p.name,
            (p.description or "")[:40],
            "yes" if p.archived_at is not None else "",
        )
    stdout.print(table)


def _emit_member_list(listing: ProjectMemberListView, *, fmt: TableJson) -> None:
    if fmt == TableJson.JSON:
        stdout.print(listing.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    if listing.count == 0:
        hint("[dim]no members[/dim]")
        return
    table = Table(title=f"members ({listing.count})")
    table.add_column("principal_id", style="bold")
    table.add_column("role")
    table.add_column("added_by")
    table.add_column("added_at", style="dim")
    for m in listing.members:
        table.add_row(m.principal_id, m.role.value, m.added_by, m.added_at.isoformat())
    stdout.print(table)


def _emit_member(view: ProjectMemberView, *, fmt: TableJson) -> None:
    if fmt == TableJson.JSON:
        stdout.print(view.model_dump_json(indent=2), soft_wrap=True, highlight=False)
        return
    table = Table(title=f"member {view.principal_id}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("project_id", view.project_id)
    table.add_row("principal_id", view.principal_id)
    table.add_row("role", view.role.value)
    table.add_row("added_by", view.added_by)
    table.add_row("added_at", view.added_at.isoformat())
    stdout.print(table)


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


__all__ = ["project_app"]
