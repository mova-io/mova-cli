"""``mdk init <name> "<description>" --target <env>`` — cloud-side bundle.

The combined "wow" CLI demo (item 3 in the project + catalog polish):
when ``--target`` is set on ``mdk init``, the flow switches from the
local-only scaffold to the runtime's unified ``POST /api/v1/agents``
endpoint with ``source: "llm"``, streams the SSE progress events
through a Rich live spinner (same shape as ``mdk deploy --verbose``'s
:func:`live_step` UX), and writes the final bundle to ``./<name>/`` so
the operator can iterate locally afterwards.

Backward compat (CLAUDE.md rule 5):

* ``mdk init <name> "<desc>"`` WITHOUT ``--target`` is UNCHANGED — the
  existing local LLM scaffold path keeps running.
* When ``--target`` IS set we route through this module instead. The
  legacy path is preserved on disk; this is purely additive.

SSE → terminal rendering approach (key UX call):

We REUSE :func:`movate.cli._progress.live_step` for the spinner +
ticking-elapsed-timer shell, then drive its ``.update(message)`` from
each SSE event's ``stage`` field. The default progress
display (transient + spinner + elapsed) is exactly what ``mdk deploy
--verbose`` uses for ``az acr build``, so the visual idiom matches.
Each event's ``message`` is logged ABOVE the live region via
:meth:`LiveStep.log` so streamed sub-step output scrolls naturally
while the spinner stays pinned to the bottom — same pattern
:func:`movate.cli.deploy._stream_az` uses for the build log.

The terminal-event flushes the live region BEFORE writing the bundle
so the file-write trace lands on the operator's screen below the
final ``✓ done`` line, not above it.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from movate.cli._console import echo_remote_context, error, hint, success
from movate.cli._progress import live_step
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)

# SSE wire format: each event is `event: <name>\ndata: <json>\n\n`. The
# unified agent-create endpoint emits a stream of named events; we map
# their `stage` field onto the spinner message and log the `message`
# field above the live region. Final `event: bundle` carries the
# base64-encoded tarball + the canonical agent layout.
_SSE_DEFAULT_TIMEOUT = 120.0


def _ensure_target_dir(name: str, *, force: bool) -> Path:
    """Pick where the bundle lands and reject a collision unless --force.

    Mirrors the local ``_init_agent`` path so an operator who runs the
    two modes back-to-back finds the same bundle at the same place.
    The bundle goes to ``./<name>/`` (the project the operator just
    described).
    """
    target_dir = (Path.cwd() / name).resolve()
    if target_dir.exists() and not force:
        error(f"{target_dir} already exists (use [bold]--force[/bold] to overwrite)")
        # Caller raises typer.Exit; we just signal via the error print.
        raise FileExistsError(target_dir)
    return target_dir


def _write_bundle(*, bundle_files: dict[str, str], target_dir: Path) -> None:
    """Materialize the {relative_path: contents} dict to disk.

    Each value is either a UTF-8 string (regular text file) or a
    base64-encoded blob prefixed with ``b64:`` for binary content.
    Mirrors how :mod:`movate.scaffold` writes a generated agent so the
    bundle is byte-identical to the local-mode output.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for relpath, content in bundle_files.items():
        # Defensive: refuse paths that try to escape via .. — a malicious
        # runtime response must not write outside the target dir.
        full = (target_dir / relpath).resolve()
        try:
            full.relative_to(target_dir)
        except ValueError:
            error(f"refused unsafe path in bundle: {relpath}")
            raise FileExistsError(relpath) from None
        full.parent.mkdir(parents=True, exist_ok=True)
        if content.startswith("b64:"):
            full.write_bytes(base64.b64decode(content[4:]))
        else:
            full.write_text(content)


def _parse_sse_chunk(chunk: bytes, buffer: bytearray) -> list[tuple[str, dict[str, Any]]]:
    """Append `chunk` to `buffer`, drain whole SSE events.

    Returns a list of ``(event_name, parsed_data_dict)`` tuples for
    every complete ``event:\\ndata:\\n\\n`` block consumed. Partial
    trailing data stays in ``buffer`` until the next call. Mirrors the
    minimal SSE parser :mod:`movate.runtime.streaming` ships — we keep
    a CLI-side copy here to avoid pulling in the runtime layer (which
    would violate CLAUDE.md rule 6: control plane ⊥ execution plane).
    """
    buffer.extend(chunk)
    events: list[tuple[str, dict[str, Any]]] = []
    while b"\n\n" in buffer:
        block_bytes, rest = buffer.split(b"\n\n", 1)
        buffer.clear()
        buffer.extend(rest)
        event_name = ""
        data_str = ""
        for raw_line in block_bytes.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_str += line[len("data:") :].strip()
        if not event_name:
            continue
        try:
            data = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            data = {"raw": data_str}
        if not isinstance(data, dict):
            data = {"value": data}
        events.append((event_name, data))
    return events


async def _stream_agent_create(
    *,
    base_url: str,
    token: str,
    payload: dict[str, Any],
    target_dir: Path,
    target_name: str,
) -> dict[str, Any]:
    """POST the agent-create request, drive the live spinner, write the bundle.

    Single source of truth for the SSE-to-terminal rendering described
    in the module docstring. The visual idiom mirrors ``mdk deploy
    --verbose``'s :func:`live_step` — spinner + ticking elapsed timer +
    streaming log lines above. On a non-TTY (CI / piped stdout) the
    live region degrades to a no-op (see :class:`_NullLiveStep`) so
    captured logs stay clean.

    Returns the parsed `bundle` event payload so the caller can render
    the next-step hint with metadata from it.
    """
    import httpx  # noqa: PLC0415

    url = f"{base_url.rstrip('/')}/api/v1/agents"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }

    buffer = bytearray()
    final_payload: dict[str, Any] = {}
    seen_error: dict[str, Any] | None = None

    with live_step(f"creating agent on {target_name}…") as step:
        async with httpx.AsyncClient(timeout=_SSE_DEFAULT_TIMEOUT) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:  # noqa: PLR2004 — HTTP range
                    body = await response.aread()
                    msg = body.decode("utf-8", errors="replace") or f"HTTP {response.status_code}"
                    error(msg, context="agent create")
                    return {}
                async for raw in response.aiter_bytes():
                    for event_name, data in _parse_sse_chunk(raw, buffer):
                        stage = str(data.get("stage", event_name))
                        message = str(data.get("message", ""))
                        if event_name == "progress":
                            step.update(f"{stage}…")
                            if message:
                                step.log(f"  · {message}")
                        elif event_name == "error":
                            seen_error = data
                            step.log(f"  [red]✗[/red] {message or stage}")
                        elif event_name == "bundle":
                            final_payload = data
                            step.update("writing bundle locally…")
                        elif event_name == "done":
                            # Server-side terminator; the bundle event has
                            # already populated final_payload.
                            step.update("done")
                        else:
                            # Unknown event names are logged but don't break
                            # the stream. Lets the runtime evolve the
                            # vocabulary additively.
                            step.log(f"  · [{event_name}] {message}")

    if seen_error is not None:
        error(
            str(seen_error.get("message", "agent create failed")),
            context="agent create",
        )
        return {}

    bundle_files = final_payload.get("files") if isinstance(final_payload, dict) else None
    if not isinstance(bundle_files, dict):
        error(
            "runtime returned no bundle (missing or empty 'files' map). "
            "Try again, or fall back to local mode by dropping --target.",
            context="agent create",
        )
        return {}

    _write_bundle(bundle_files=bundle_files, target_dir=target_dir)
    return final_payload


def init_with_target(
    *,
    name: str,
    description: str,
    target: str,
    force: bool,
) -> int:
    """Entrypoint for ``mdk init <name> "<desc>" --target <env>``.

    Returns the exit code; the caller (``mdk init``) translates it
    into a :class:`typer.Exit`. Why an int rather than raising: keeps
    this module pure I/O so tests can patch the SSE source and assert
    the return value without catching ``SystemExit``.
    """
    try:
        target_name, target_cfg = resolve_target(target)
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        return 2

    try:
        target_dir = _ensure_target_dir(name, force=force)
    except FileExistsError:
        return 2

    echo_remote_context(target_name, target_cfg, action="init")

    payload = {"name": name, "source": "llm", "description": description}
    try:
        result = asyncio.run(
            _stream_agent_create(
                base_url=target_cfg.url,
                token=token,
                payload=payload,
                target_dir=target_dir,
                target_name=target_name,
            )
        )
    except (FileExistsError, OSError) as exc:
        error(f"failed to write bundle: {exc}", context="init")
        return 1

    if not result:
        # An error was already printed inside _stream_agent_create.
        return 1

    success(
        f"created [bold]{name}[/bold] on [bold]{target_name}[/bold] and "
        f"wrote bundle to [bold]{target_dir}[/bold]"
    )
    hint("\n[bold]Next steps:[/bold]")
    hint(f"  [dim]$[/dim] [bold]cd {name}[/bold]")
    hint(
        f"  [dim]$[/dim] [bold]mdk run {name} --target {target_name} '{{...}}'[/bold]"
        "   [dim]# try it live on the deployed runtime[/dim]"
    )
    hint(
        "  [dim]$[/dim] [bold]mdk validate .[/bold]"
        "   [dim]# zero-cost structural check on the local bundle[/dim]"
    )
    return 0


__all__ = ["init_with_target"]
