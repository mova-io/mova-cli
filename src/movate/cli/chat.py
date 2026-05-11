"""``movate chat <agent>`` — interactive REPL bound to one agent.

The use case: you're iterating on a prompt and want to quickly send
ten different messages without typing ``movate run agent "..."`` ten
times. Tab-completion of the agent path (via :mod:`movate.cli._completion`)
makes the launch one keystroke; then each REPL turn is a single line
of user input.

Implementation notes
--------------------

* Each turn re-uses the executor as if it were a fresh ``movate run``
  — the agent's JSON contract still applies, persistence still
  happens, schema validation still runs. There's no conversation
  memory in v0.5; that's tracked as Tier-2 #10 (conversation_id +
  history persistence). When it lands, this REPL gets a one-line
  upgrade: pass ``conversation_id=<session>`` to ``execute()``.

* Streaming is on by default — that's the whole UX win. Pass
  ``--no-stream`` to disable.

* Auto-wrap: the user types free-form text; we wrap it as
  ``{<field>: text}`` where ``<field>`` is the agent's single
  required string field. Agents with multi-field input schemas
  can't be ``chat``ed yet — surface that with a clear error so
  the operator knows to either use ``movate run`` or simplify the
  schema.

* Exit: Ctrl-C, EOF (Ctrl-D), or any of ``:q`` / ``exit`` / ``quit``.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.prompt import Prompt

from movate.cli._completion import complete_agent_path
from movate.cli._console import error, hint
from movate.cli._runtime import build_local_runtime, shutdown_runtime
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import RunRequest, RunResponse
from movate.providers.base import Message

stdout = Console()
err = Console(stderr=True)

_EXIT_TOKENS = {":q", "exit", "quit", ":quit"}


def chat(
    path: Path = typer.Argument(
        ...,
        help="Path to an agent directory.",
        shell_complete=complete_agent_path,
    ),
    no_stream: bool = typer.Option(
        False,
        "--no-stream",
        help="Disable streaming (wait for the full response, then render).",
    ),
    no_memory: bool = typer.Option(
        False,
        "--no-memory",
        help="Disable conversation memory — each turn runs as a fresh one-shot.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Use the deterministic MockProvider (smoke / offline).",
    ),
) -> None:
    """Interactive REPL bound to one agent.

    [bold]Example:[/bold]

      [dim]# Iterate on a prompt without retyping `movate run` each turn[/dim]
      $ movate chat ./agents/faq-agent
      you> what is movate?
      ✦ movate is a declarative platform for AI agents and workflows...
      you> what's the alternative?
      ✦ Compared to writing agents in raw LangChain, movate gives you...
      you> :q

    Conversation memory is ON by default — each turn sees the prior
    user/assistant exchange so follow-ups like "what's the alternative?"
    resolve correctly. Pass ``--no-memory`` to make every turn
    independent (useful when iterating on a prompt and you DON'T want
    context bleeding between attempts).

    Each turn runs through the full executor (input validation,
    schema check, persistence) — exactly like ``movate run`` — so
    every message persists to local sqlite for ``movate trace replay``.

    [dim]Memory is in-process only — it lives for the duration of one
    chat session and is dropped on exit. Persisted conversations across
    sessions are a follow-up.[/dim]
    """
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        error(str(exc), context="load")
        raise typer.Exit(code=2) from None

    input_field = _resolve_single_string_field(bundle)
    if input_field is None:
        error(
            f"agent {bundle.spec.name!r} doesn't have a single required string "
            f"input field — chat can't auto-wrap messages. Use `movate run` with "
            f"explicit JSON, or simplify the input schema."
        )
        raise typer.Exit(code=2)

    asyncio.run(
        _chat_loop(
            bundle=bundle,
            input_field=input_field,
            stream=not no_stream,
            mock=mock,
            memory=not no_memory,
        )
    )


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _chat_loop(
    *,
    bundle: AgentBundle,
    input_field: str,
    stream: bool,
    mock: bool,
    memory: bool,
) -> None:
    rt = await build_local_runtime(mock=mock)
    # Conversation history: a flat list of Messages alternating user/assistant.
    # Built up across turns and passed to executor.execute(history=...) so the
    # model sees prior context. Empty when --no-memory; we still track it for
    # the assistant's reply but never pass it.
    history: list[Message] = []
    try:
        mem_status = "memory on" if memory else "memory off"
        hint(
            f"[dim]chat: {bundle.spec.name} v{bundle.spec.version} "
            f"({'mock' if mock else bundle.spec.model.provider}, {mem_status})  "
            f"— Ctrl-C, Ctrl-D, or :q to exit[/dim]"
        )
        while True:
            try:
                user_message = Prompt.ask("[bold cyan]you[/bold cyan]", console=err)
            except (KeyboardInterrupt, EOFError):
                err.print()  # newline after ^C / ^D
                hint("[dim]chat ended[/dim]")
                return

            stripped = user_message.strip()
            if not stripped:
                continue
            if stripped.lower() in _EXIT_TOKENS:
                hint("[dim]chat ended[/dim]")
                return

            request = RunRequest(agent=bundle.spec.name, input={input_field: stripped})
            on_token = _streaming_callback() if stream and not mock else None

            try:
                response = await rt.executor.execute(
                    bundle,
                    request,
                    on_token=on_token,
                    history=history if memory else None,
                )
            except Exception as exc:
                # REPL keeps going on any error — broad catch is
                # deliberate so a bad turn doesn't kill the session.
                if on_token is not None:
                    sys.stderr.write("\n")
                    sys.stderr.flush()
                error(str(exc), context="turn")
                continue

            _render_turn(response, streamed=on_token is not None)
            # Append this turn's user message + assistant response to
            # history so the next turn sees context. Only when memory
            # is on — if disabled, history stays empty.
            if memory and response.status == "success":
                history.append(Message(role="user", content=stripped))
                history.append(
                    Message(
                        role="assistant",
                        content=_assistant_content(response),
                    )
                )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_single_string_field(bundle: AgentBundle) -> str | None:
    """Pick the agent's single required string input field, or ``None``
    if the schema is more complex than auto-wrap can handle.

    Same rule as ``_coerce_agent_input`` in ``run.py``: chat is the
    auto-wrap-friendly subset of agent inputs."""
    schema = bundle.input_schema
    required: list[str] = list(schema.get("required", []))
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    string_required: list[str] = [
        name for name in required if properties.get(name, {}).get("type") == "string"
    ]
    if len(string_required) == 1 and len(required) == 1:
        return string_required[0]
    return None


def _streaming_callback() -> Callable[[str], None]:
    """Same stderr-flushing callback as ``movate run --stream``.

    Inlined rather than imported from run.py to avoid a CLI-module
    dependency cycle when the import order matters at startup."""

    def _emit(text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    return _emit


def _render_turn(response: RunResponse, *, streamed: bool) -> None:
    """Print the assistant's reply.

    When streamed, tokens already hit stderr live — we just emit a
    final newline + the structured response on a new line. When not
    streamed, we render the response in one shot with the ``✦`` marker
    so the user can scan the transcript."""
    # Use stderr for the marker + response so the conversation log
    # stays on one stream — stdout is reserved for the structured
    # response payload (if the operator wants to grep it via `> log`).
    if streamed:
        err.print()  # finish the streamed line
    else:
        # Non-streaming: emit human-readable on stderr with a marker.
        err.print(f"[bold magenta]✦[/bold magenta] {response.human_readable}")

    # Always emit the structured JSON to stdout so a pipe captures it.
    # Soft-wrap + no highlight keeps it pipe-friendly when redirected.
    stdout.print(_compact_json(response), soft_wrap=True, highlight=False)


def _compact_json(response: RunResponse) -> str:
    """One-line JSON of the response data — easier to read in a
    transcript than the executor's full RunResponse model dump."""
    import json  # noqa: PLC0415

    payload: dict[str, Any] = {"data": response.data}
    if response.error is not None:
        payload["error"] = {"type": response.error.type, "message": response.error.message}
    return json.dumps(payload)


def _assistant_content(response: RunResponse) -> str:
    """Best-effort assistant content to thread into conversation history.

    First choice: ``response.human_readable`` if the executor was able
    to extract one (agents whose output schema has ``message`` /
    ``summary`` / ``human_readable`` keys land here). Otherwise fall
    back to the JSON-encoded ``response.data`` so the model still sees
    something coherent in the next turn — better than an empty
    assistant message that breaks conversation continuity."""
    if response.human_readable:
        return response.human_readable
    import json  # noqa: PLC0415

    return json.dumps(response.data or {})


__all__ = ["chat"]
