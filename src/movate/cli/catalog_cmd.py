"""``mdk catalog`` — browse and contribute to the agent catalog (ADR 041).

Talks to a deployed runtime's ``/api/v1/catalog/...`` surface. Three
namespaces share one read view:

* ``movate``    — the curated public catalog (synced from
  ``catalog.movate.io`` once the production sync flips on; v1 sees
  whatever the bundled floor + manual upserts have written).
* ``private``   — the caller's tenant-private entries; never synced
  upward (data sovereignty — ADR 041 D5).
* ``community`` — schema-ready; v1 returns no rows (deferred — D7).

Subcommands:

* ``mdk catalog list``           — paginate the visible catalog.
* ``mdk catalog show <slug>``    — render one entry's detail (latest
  version + ratings).
* ``mdk catalog search <q>``     — alias for ``list --q``.
* ``mdk catalog submit <slug>``  — create a tenant-private entry from a
  local directory (tarballs ``<dir>``).
* ``mdk catalog publish-version <slug>`` — publish a new version of a
  tenant-private entry.
* ``mdk catalog rate <slug>``    — record a 1-5 rating with an optional
  comment.
* ``mdk catalog sync``           — trigger a manual sync. v1's sync
  handler is a STUB (logs intent + advances the watermark) — the
  production wiring against ``catalog.movate.io`` is a separate
  Movate-side build.

Every subcommand accepts ``--target <env>`` (the deployment target;
defaults to the active target from ``mdk config``). Local-store-only
ops are intentionally absent from this command — local browsing is the
job of ``mdk add --list`` / ``mdk templates`` (the image-bundled floor,
ADR 028).
"""

from __future__ import annotations

import asyncio
import base64
import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from movate.cli._console import error, get_global_target, hint
from movate.cli._output import TableJson
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)

_HTTP_ERROR_FLOOR = 400

stdout = Console()
err = Console(stderr=True)


catalog_app = typer.Typer(
    name="catalog",
    help="Browse, contribute to, and sync the agent catalog (ADR 041).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _resolved_target(target: str | None) -> tuple[str, str, str]:
    """Resolve ``--target`` (or the active target) to ``(name, url, token)``.

    Lifts the boilerplate every subcommand would otherwise repeat.
    Raises ``typer.Exit(2)`` on a config error (the user already saw a
    formatted ``error(...)`` line)."""

    try:
        name, cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None
    return name, cfg.url, token


def _client(base_url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code < _HTTP_ERROR_FLOOR:
        return
    try:
        body = r.json()
        msg = body.get("error", {}).get("message") or body.get("detail") or r.text
    except (ValueError, KeyError):
        msg = r.text
    error(f"runtime returned HTTP {r.status_code}: {msg}")
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# list / search
# ---------------------------------------------------------------------------


async def _list_entries(
    *,
    base_url: str,
    token: str,
    source: str | None,
    tag: str | None,
    shape: str | None,
    q: str | None,
    limit: int,
) -> dict[str, Any]:
    async with _client(base_url, token) as client:
        params: dict[str, str | int] = {"limit": limit}
        if source:
            params["source"] = source
        if tag:
            params["tag"] = tag
        if shape:
            params["shape"] = shape
        if q:
            params["q"] = q
        r = await client.get("/api/v1/catalog/agents", params=params)
        _raise_for_status(r)
        data: dict[str, Any] = r.json()
        return data


@catalog_app.command("list")
def list_cmd(
    source: str | None = typer.Option(
        None,
        "--source",
        help="Filter to one namespace (movate | private | community).",
    ),
    tag: str | None = typer.Option(None, "--tag", help="Filter by single tag membership."),
    shape: str | None = typer.Option(
        None, "--shape", help="Filter by ADR 028 shape (faq / rag_qa / ...)."
    ),
    q: str | None = typer.Option(
        None, "--q", help="Substring match over name / title / description."
    ),
    limit: int = typer.Option(50, "--limit", help="Max entries to return."),
    target: str | None = typer.Option(
        None, "--target", "-t", help="Deployment target. Omit to use the active target."
    ),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """List catalog entries visible to the caller (filtered + paginated).

    [bold]Examples:[/bold]

      [dim]# Browse everything[/dim]
      $ mdk catalog list

      [dim]# Just the Movate-published RAG agents[/dim]
      $ mdk catalog list --source movate --shape rag_qa

      [dim]# Search + limit, machine-readable[/dim]
      $ mdk catalog list --q returns --limit 10 -o json
    """

    _name, base_url, token = _resolved_target(target)
    payload = asyncio.run(
        _list_entries(
            base_url=base_url,
            token=token,
            source=source,
            tag=tag,
            shape=shape,
            q=q,
            limit=limit,
        )
    )

    if output_format == TableJson.JSON:
        stdout.print_json(data=payload)
        return

    entries = payload.get("entries", [])
    if not entries:
        hint("[dim]no entries matched[/dim]")
        return

    table = Table(title="catalog entries")
    table.add_column("slug", style="bold")
    table.add_column("source")
    table.add_column("latest")
    table.add_column("shape")
    table.add_column("title")
    table.add_column("⭐", justify="right")

    for e in entries:
        summary = e.get("ratings_summary") or {}
        avg = summary.get("avg", 0.0)
        count = summary.get("count", 0)
        star = f"{avg:.1f} ({count})" if count else "[dim]—[/dim]"
        table.add_row(
            e["slug"],
            e["source"],
            e["latest_version"],
            e.get("shape") or "[dim]—[/dim]",
            e["title"],
            star,
        )
    stdout.print(table)
    if payload.get("next_after_slug"):
        hint(f"[dim]more results — pass --after-slug {payload['next_after_slug']}[/dim]")


@catalog_app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Substring to search."),
    target: str | None = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Alias for ``mdk catalog list --q <query>``.

    [bold]Examples:[/bold]

      [dim]# Find entries mentioning "returns"[/dim]
      $ mdk catalog search returns
    """

    list_cmd(
        source=None,
        tag=None,
        shape=None,
        q=query,
        limit=50,
        target=target,
        output_format=output_format,
    )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


async def _fetch_entry(*, base_url: str, token: str, slug: str, source: str) -> dict[str, Any]:
    async with _client(base_url, token) as client:
        r = await client.get(f"/api/v1/catalog/agents/{slug}", params={"source": source})
        _raise_for_status(r)
        data: dict[str, Any] = r.json()
        return data


async def _fetch_version(
    *, base_url: str, token: str, slug: str, source: str, version: str
) -> dict[str, Any]:
    async with _client(base_url, token) as client:
        r = await client.get(
            f"/api/v1/catalog/agents/{slug}/versions/{version}",
            params={"source": source},
        )
        _raise_for_status(r)
        data: dict[str, Any] = r.json()
        return data


@catalog_app.command("show")
def show_cmd(
    slug: str = typer.Argument(..., help="Catalog entry slug."),
    version: str | None = typer.Option(
        None, "--version", help="A specific version (defaults to the latest)."
    ),
    source: str = typer.Option(
        "movate", "--source", help="Namespace (movate | private | community)."
    ),
    target: str | None = typer.Option(None, "--target", "-t"),
    output_format: TableJson = typer.Option(
        TableJson.TABLE, "--output", "-o", case_sensitive=False
    ),
) -> None:
    """Show detail for one catalog entry (and optionally one version).

    [bold]Examples:[/bold]

      [dim]# Latest version of an entry[/dim]
      $ mdk catalog show faq-starter

      [dim]# A pinned version[/dim]
      $ mdk catalog show faq-starter --version 0.2.0
    """

    _name, base_url, token = _resolved_target(target)
    entry = asyncio.run(_fetch_entry(base_url=base_url, token=token, slug=slug, source=source))
    ver_payload: dict[str, Any] | None = None
    if version is not None:
        ver_payload = asyncio.run(
            _fetch_version(
                base_url=base_url,
                token=token,
                slug=slug,
                source=source,
                version=version,
            )
        )

    if output_format == TableJson.JSON:
        out: dict[str, Any] = {"entry": entry}
        if ver_payload is not None:
            # Don't print the (potentially very large) bundle to JSON
            # stdout by default; the caller can pipe through `jq` to
            # the b64 field if they need it.
            slim = {k: v for k, v in ver_payload.items() if k != "bundle_tar_b64"}
            out["version"] = slim
        stdout.print_json(data=out)
        return

    table = Table(title=f"catalog entry: {entry['slug']} ({entry['source']})")
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("title", entry["title"])
    table.add_row("name", entry["name"])
    table.add_row("latest_version", entry["latest_version"])
    table.add_row("shape", entry.get("shape") or "[dim]—[/dim]")
    table.add_row("tags", ", ".join(entry.get("tags", []) or []) or "[dim]—[/dim]")
    table.add_row("recommended_for", entry.get("recommended_for") or "[dim]—[/dim]")
    summary = entry.get("ratings_summary") or {}
    table.add_row(
        "ratings",
        f"⭐ {summary.get('avg', 0):.1f} ({summary.get('count', 0)} votes)",
    )
    table.add_row("description", entry["description"])
    stdout.print(table)

    if ver_payload is not None:
        vtable = Table(title=f"version: {ver_payload['version']}")
        vtable.add_column("field", style="bold")
        vtable.add_column("value")
        vtable.add_row("digest", ver_payload["digest"])
        vtable.add_row("published_at", ver_payload["published_at"])
        vtable.add_row("deprecated_at", ver_payload.get("deprecated_at") or "[dim]—[/dim]")
        b64 = ver_payload.get("bundle_tar_b64") or ""
        vtable.add_row("bundle_bytes", str(len(base64.b64decode(b64)) if b64 else 0))
        stdout.print(vtable)


# ---------------------------------------------------------------------------
# submit / publish-version
# ---------------------------------------------------------------------------


def _tar_from_dir(dir_path: Path) -> bytes:
    if not dir_path.is_dir():
        raise typer.BadParameter(f"{dir_path} is not a directory")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for entry in sorted(dir_path.rglob("*")):
            if entry.is_dir():
                continue
            arcname = entry.relative_to(dir_path).as_posix()
            tf.add(entry, arcname=arcname, recursive=False)
    return buf.getvalue()


async def _post(*, base_url: str, token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with _client(base_url, token) as client:
        r = await client.post(path, json=body)
        _raise_for_status(r)
        data: dict[str, Any] = r.json()
        return data


@catalog_app.command("submit")
def submit_cmd(
    slug: str = typer.Argument(..., help="Catalog entry slug (URL-safe id)."),
    from_dir: Path = typer.Option(
        ...,
        "--from-dir",
        help="Local directory; tarballed and uploaded as the entry bundle.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Display name. Defaults to the slug.",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Card title. Defaults to the slug.",
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="Plain-text description.",
    ),
    tag: list[str] = typer.Option([], "--tag", help="Tag to attach (repeatable)."),
    shape: str | None = typer.Option(None, "--shape", help="ADR 028 shape (faq / rag_qa / ...)."),
    recommended_for: str | None = typer.Option(
        None, "--recommended-for", help="One-line use-case statement."
    ),
    version: str = typer.Option("0.1.0", "--version"),
    target: str | None = typer.Option(None, "--target", "-t"),
) -> None:
    """Submit a tenant-private catalog entry from ``<from-dir>``.

    [bold]Examples:[/bold]

      [dim]# Publish a local agent dir as a private catalog entry[/dim]
      $ mdk catalog submit returns-bot --from-dir ./agents/returns-bot \\
          --title "Returns Bot" --shape rag_qa --tag support
    """

    _name, base_url, token = _resolved_target(target)
    bundle = _tar_from_dir(from_dir)
    body = {
        "slug": slug,
        "name": name or slug,
        "title": title or slug,
        "description": description or f"private entry {slug}",
        "tags": list(tag),
        "shape": shape,
        "recommended_for": recommended_for,
        "version": version,
        "bundle_tar_b64": base64.b64encode(bundle).decode("ascii"),
    }
    payload = asyncio.run(
        _post(
            base_url=base_url,
            token=token,
            path="/api/v1/catalog/agents",
            body=body,
        )
    )
    stdout.print_json(data=payload)


@catalog_app.command("publish-version")
def publish_version_cmd(
    slug: str = typer.Argument(...),
    version: str = typer.Option(..., "--version"),
    from_dir: Path = typer.Option(
        ...,
        "--from-dir",
        help="Local directory; tarballed and uploaded as the new version's bundle.",
    ),
    target: str | None = typer.Option(None, "--target", "-t"),
) -> None:
    """Publish a new version of a tenant-private entry.

    [bold]Examples:[/bold]

      [dim]# Ship v0.2.0 of an existing entry[/dim]
      $ mdk catalog publish-version returns-bot --version 0.2.0 \\
          --from-dir ./agents/returns-bot
    """

    _name, base_url, token = _resolved_target(target)
    bundle = _tar_from_dir(from_dir)
    body = {
        "version": version,
        "bundle_tar_b64": base64.b64encode(bundle).decode("ascii"),
    }
    payload = asyncio.run(
        _post(
            base_url=base_url,
            token=token,
            path=f"/api/v1/catalog/agents/{slug}/versions",
            body=body,
        )
    )
    stdout.print_json(data=payload)


# ---------------------------------------------------------------------------
# rate / sync
# ---------------------------------------------------------------------------


@catalog_app.command("rate")
def rate_cmd(
    slug: str = typer.Argument(...),
    rating: int = typer.Option(..., "--rating", min=1, max=5),
    comment: str | None = typer.Option(None, "--comment"),
    source: str = typer.Option("movate", "--source", help="Namespace whose entry you're rating."),
    target: str | None = typer.Option(None, "--target", "-t"),
) -> None:
    """Record a 1-5 rating for one catalog entry.

    [bold]Examples:[/bold]

      [dim]# Rate an entry 5 stars with a note[/dim]
      $ mdk catalog rate faq-starter --rating 5 --comment "great defaults"
    """

    _name, base_url, token = _resolved_target(target)
    body = {"rating": rating, "comment": comment, "source": source}
    payload = asyncio.run(
        _post(
            base_url=base_url,
            token=token,
            path=f"/api/v1/catalog/agents/{slug}/ratings",
            body=body,
        )
    )
    stdout.print_json(data=payload)


@catalog_app.command("sync")
def sync_cmd(
    source: str = typer.Option(
        "movate",
        "--source",
        help="Namespace to sync. Only 'movate' is meaningful in v1.",
    ),
    target: str | None = typer.Option(None, "--target", "-t"),
) -> None:
    """Trigger a manual catalog sync.

    v1's server-side handler is a STUB — it logs the intent and bumps
    the watermark. The production wiring against ``catalog.movate.io``
    is a separate Movate-side build (ADR 041 D4).

    [bold]Examples:[/bold]

      [dim]# Pull the latest Movate-published entries[/dim]
      $ mdk catalog sync --source movate
    """

    _name, base_url, token = _resolved_target(target)
    payload = asyncio.run(
        _post(
            base_url=base_url,
            token=token,
            path="/api/v1/catalog/sync",
            body={"source": source},
        )
    )
    stdout.print_json(data=payload)


__all__ = ["catalog_app"]
