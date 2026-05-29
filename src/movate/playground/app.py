"""Chainlit app for the MDK playground — ChatGPT-like agent testing.

Run via::

    chainlit run -m movate.playground.app

…or — easier — via the CLI wrapper::

    mdk playground serve --runtime-url http://127.0.0.1:8000

The CLI wrapper handles configuration via env vars + spawns Chainlit
with the right module path; this file is the actual app.

What the UX gives an operator:

1. **Agent picker** (``@cl.on_chat_start``) — fetch the agent catalog +
   the runtime's *capabilities* (``GET /api/v1/capabilities``); show a
   picker (Chainlit Action buttons). Capability detection decides, once
   per chat, how memory / streaming / feedback are carried (see below).
2. **Multi-turn chat** — after picking an agent, the operator just
   *talks*. Each turn goes through a :class:`ConversationBackend`:
   - **server sessions** when the runtime advertises ``sessions``
     (memory server-managed, ADR 045 D10), OR
   - **client-managed** (the common case today) — the playground
     re-sends the running transcript + uploaded-doc context to the
     stateless run endpoint.
   The operator can also paste a JSON object to drive an agent whose
   input schema needs structured fields (back-compat with the old
   single-shot flow).
3. **File uploads** — drop a file on the composer; its text is extracted
   via the **shared KB parser** and held as conversation context so the
   agent can reference "the uploaded document". An action lets the
   operator persist the file to the agent's KB for RAG testing.
4. **Streaming** — when the runtime advertises ``run_streaming``, tokens
   render live into the message; else the response is buffered.
5. **History sidebar** — Chainlit's data layer persists threads (local
   SQLite by default, Postgres if configured), so past conversations
   appear in the sidebar and resume restores the transcript.
6. **Feedback** — 👍/👎/comment, routed to the feedback API when
   advertised, else the existing persistence path.

Everything capability-gated is feature-*detected*, so this one
playground build auto-upgrades when the Sessions / streaming / feedback
APIs land — no playground release needed.

Module layout:

* :mod:`movate.playground.client` — async HTTP client to the runtime.
* :mod:`movate.playground.capabilities` — capability discovery (pure).
* :mod:`movate.playground.conversation` — backends + context assembly
  (pure).
* :mod:`movate.playground.uploads` — upload→context adapter (pure).
* :mod:`movate.playground.sse` — SSE frame parsing (pure).
* :mod:`movate.playground.state` — data-layer path resolution (pure).
* :mod:`movate.playground.app` — this file: the Chainlit decorators that
  bind the pure logic to the UI. The one ``chainlit run`` loads.

Chainlit is an optional dependency under the ``[playground]`` extra — the
rest of MDK works without it. The CLI command
(:mod:`movate.cli.playground`) prints a friendly error when the extra
isn't installed. The pure-logic modules above import WITHOUT Chainlit so
they're unit-testable on a no-extras install.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Chainlit is an optional dependency (``[playground]`` extra). Import
# lazily inside the module so that ``import movate.playground.app`` from a
# no-extras install raises a clear error instead of a cryptic
# ModuleNotFoundError mid-decorator. The pure-logic modules below import
# fine without Chainlit; only THIS module needs it.
try:
    import chainlit as cl
except ImportError as exc:  # pragma: no cover - covered by CLI hint
    raise ImportError(
        "movate.playground requires the [playground] extra. "
        "Install with: uv pip install 'movate-cli[playground]'"
    ) from exc

from movate.playground.capabilities import RuntimeCapabilities, parse_capabilities
from movate.playground.client import PlaygroundClient, PlaygroundClientConfig
from movate.playground.conversation import (
    ConversationState,
    FeedbackRoute,
    extract_output_text,
    feedback_route,
    select_backend,
)
from movate.playground.state import resolve_data_layer_config
from movate.playground.uploads import UploadOutcome, UploadStore, adapt_upload

# Session keys (kept as constants so set/get can't typo-drift).
_K_CLIENT = "client"
_K_CAPS = "capabilities"
_K_AGENT = "agent_name"
_K_AGENT_DETAIL = "agent_detail"
_K_BACKEND = "backend"
_K_CONVO = "conversation_state"
_K_UPLOADS = "upload_store"


def _client_from_env() -> PlaygroundClient:
    """Build the runtime client from env vars set by the CLI wrapper.

    Chainlit's process model has no direct way to pass typed args into the
    app module; the CLI exports env vars before ``chainlit run`` and we
    read them here. The bearer token rides in the client's default
    Authorization header — server-side only, never sent to browser JS.
    """
    runtime_url = os.environ.get("MDK_PLAYGROUND_RUNTIME_URL", "http://127.0.0.1:8000")
    api_key = os.environ.get("MDK_PLAYGROUND_API_KEY")
    return PlaygroundClient(PlaygroundClientConfig(runtime_url=runtime_url, api_key=api_key))


def _history_enabled() -> bool:
    """Whether thread persistence (the history sidebar) is on.

    Off only when the CLI passed ``--no-history`` (exported as
    ``MDK_PLAYGROUND_NO_HISTORY=1``).
    """
    return os.environ.get("MDK_PLAYGROUND_NO_HISTORY", "") not in {"1", "true", "True"}


def _auto_persist_uploads() -> bool:
    """Whether uploads auto-ingest into the agent's KB (``--persist-uploads``).

    Off by default — uploads stay session-scoped context and the operator
    opts in per file via the action button. On flips the default to
    always-ingest.
    """
    return os.environ.get("MDK_PLAYGROUND_PERSIST_UPLOADS", "") in {"1", "true", "True"}


# ---------------------------------------------------------------------------
# Data layer (history sidebar + resume). Registered at import time so
# Chainlit picks it up before the first request. Local SQLite by default;
# Postgres when configured. Skipped entirely when --no-history is set.
# ---------------------------------------------------------------------------

_DATA_LAYER_CFG = resolve_data_layer_config(enabled=_history_enabled())


if _DATA_LAYER_CFG.enabled:

    @cl.data_layer  # pragma: no cover - requires chainlit + a DB at runtime
    def _build_data_layer() -> Any:
        """Wire Chainlit's SQLAlchemy data layer for thread persistence.

        SQLite (local, zero-config) by default; Postgres when a URL is
        configured. Enables the past-conversations sidebar + resume. The
        directory is created here (path resolution lives in the pure
        :mod:`movate.playground.state`). Best-effort: if the data-layer
        deps aren't importable, we degrade to no persistence rather than
        crashing the whole UI.
        """
        from chainlit.data.sql_alchemy import SQLAlchemyDataLayer  # noqa: PLC0415

        if _DATA_LAYER_CFG.postgres_url:
            conninfo = _DATA_LAYER_CFG.postgres_url
        else:
            db_path = _DATA_LAYER_CFG.sqlite_path
            assert db_path is not None  # invariant: sqlite path set when no PG url
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conninfo = f"sqlite+aiosqlite:///{db_path}"
        return SQLAlchemyDataLayer(conninfo=conninfo)


# ---------------------------------------------------------------------------
# Session setup helpers
# ---------------------------------------------------------------------------


async def _init_session() -> tuple[PlaygroundClient, RuntimeCapabilities]:
    """Create the client + detect capabilities, store both in the session.

    Capability detection runs ONCE per chat. A runtime that predates the
    capabilities endpoint (404) degrades to the all-off default —
    client-managed memory, buffered responses, legacy feedback — i.e.
    today's behavior. Never raises on a missing endpoint.
    """
    client = _client_from_env()
    cl.user_session.set(_K_CLIENT, client)
    try:
        raw_caps = await client.get_capabilities()
    except Exception:
        raw_caps = None
    caps = parse_capabilities(raw_caps)
    cl.user_session.set(_K_CAPS, caps)
    cl.user_session.set(_K_CONVO, ConversationState())
    cl.user_session.set(_K_UPLOADS, UploadStore())
    return client, caps


def _bind_agent(agent_name: str, caps: RuntimeCapabilities, client: PlaygroundClient) -> None:
    """Bind the picked agent + select its conversation backend.

    The single place :func:`select_backend` runs — server-sessions vs
    client-managed, chosen from capabilities. Resets the conversation +
    uploads so each agent pick starts a clean chat.
    """
    cl.user_session.set(_K_AGENT, agent_name)
    cl.user_session.set(_K_CONVO, ConversationState())
    cl.user_session.set(_K_UPLOADS, UploadStore())
    backend = select_backend(caps, client)
    cl.user_session.set(_K_BACKEND, backend)


def _capability_banner(caps: RuntimeCapabilities) -> str:
    """One-line summary of how this chat will behave, for the operator."""
    memory = "server sessions" if caps.sessions else "client-managed history"
    stream = "streaming" if caps.run_streaming else "buffered"
    return (
        f"_Memory: **{memory}** · responses: **{stream}** · "
        f"uploads up to {caps.max_upload_mb} MB, {caps.max_upload_count} files_"
    )


# ---------------------------------------------------------------------------
# Chat lifecycle
# ---------------------------------------------------------------------------


@cl.on_chat_start
async def start() -> None:
    """Detect capabilities, then show the agent picker on session start."""
    client, caps = await _init_session()

    try:
        agents = await client.list_agents()
    except Exception as exc:
        await cl.Message(
            content=(
                f"❌ Could not reach the runtime: {type(exc).__name__}: {exc}\n\n"
                "Check that the runtime is running and that "
                "``MDK_PLAYGROUND_RUNTIME_URL`` points at it."
            )
        ).send()
        return

    if not agents:
        await cl.Message(
            content=(
                "⚠ The runtime has no agents registered. "
                "Use ``mdk add <role>`` to scaffold one, then "
                "``mdk deploy --target <env>`` to publish it."
            )
        ).send()
        return

    actions = [
        cl.Action(
            name="pick_agent",
            payload={"value": a.get("name", "")},
            label=f"{a.get('name', '?')} · v{a.get('version', '?')}",
            tooltip=a.get("description", "")[:120],
        )
        for a in agents
    ]
    await cl.Message(
        content=(
            f"**MDK playground** — {len(agents)} agent(s) available on this "
            "runtime. Pick one, then just start chatting.\n\n"
            f"{_capability_banner(caps)}"
        ),
        actions=actions,
    ).send()


@cl.action_callback("pick_agent")
async def on_pick_agent(action: cl.Action) -> None:
    """An agent was picked — enter multi-turn chat mode.

    Fetches the agent's detail (for the input schema, surfaced as a hint
    so power users can still send structured JSON), selects the
    conversation backend, and invites the operator to start talking.
    """
    client: PlaygroundClient = cl.user_session.get(_K_CLIENT)
    caps: RuntimeCapabilities = cl.user_session.get(_K_CAPS)
    agent_name = action.payload.get("value")
    if not agent_name or not client:
        await cl.Message(content="Pick an agent first from the buttons above.").send()
        return

    _bind_agent(agent_name, caps, client)

    try:
        detail = await client.get_agent_detail(agent_name)
    except Exception:
        detail = {}
    cl.user_session.set(_K_AGENT_DETAIL, detail)

    schema = detail.get("input_schema") or detail.get("schema", {}).get("input") or {}
    schema_hint = ""
    if schema:
        schema_json = json.dumps(schema, indent=2)
        schema_hint = (
            "\n\n<details><summary>Advanced: input schema (paste JSON to "
            f"set structured fields)</summary>\n\n```json\n{schema_json}\n```\n"
            "</details>"
        )

    await cl.Message(
        content=(
            f"**{agent_name}** selected — send a message to start the "
            "conversation. Attach files with the paperclip to give the "
            "agent context; I'll extract their text.\n\n"
            f"{_capability_banner(caps)}"
            f"{schema_hint}"
        )
    ).send()


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


async def _handle_uploads(message: cl.Message) -> None:
    """Extract text from any files attached to ``message`` into context.

    Reuses the shared KB parser (via :func:`adapt_upload`). Extracted text
    is held in the session's :class:`UploadStore` so subsequent turns can
    reference the document. Images are held but noted as a deferred
    (text-only v1) capability. Each file gets an "Add to agent's KB
    permanently" action so RAG testing can persist it.
    """
    elements = [e for e in (message.elements or []) if getattr(e, "path", None)]
    if not elements:
        return
    caps: RuntimeCapabilities = cl.user_session.get(_K_CAPS)
    store: UploadStore = cl.user_session.get(_K_UPLOADS)

    if len(elements) > caps.max_upload_count:
        await cl.Message(
            content=(
                f"⚠ {len(elements)} files attached — only the first "
                f"{caps.max_upload_count} will be processed (runtime cap)."
            )
        ).send()
        elements = elements[: caps.max_upload_count]

    for el in elements:
        # ``path`` is guaranteed non-None by the filter above; bind it to a
        # str local so the open()/basename calls are type-clean.
        path = str(el.path)
        name = getattr(el, "name", None) or os.path.basename(path)
        try:
            with open(path, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            await cl.Message(content=f"❌ Could not read {name!r}: {exc}").send()
            continue

        doc = adapt_upload(name, content, max_size_mb=caps.max_upload_mb)
        store.add(doc)

        if doc.outcome == UploadOutcome.EXTRACTED:
            msg = (
                f"📎 **{name}** — extracted {len(doc.text)} chars; the agent can now reference it."
            )
        elif doc.outcome == UploadOutcome.IMAGE_DEFERRED:
            msg = (
                f"🖼 **{name}** held, but image/vision input is a future "
                "capability — this playground is text-only in v1."
            )
        elif doc.outcome == UploadOutcome.EMPTY:
            msg = f"📎 **{name}** parsed but contained no text."
        elif doc.outcome == UploadOutcome.TOO_LARGE:
            msg = f"⚠ **{name}** — {doc.note}."
        elif doc.outcome == UploadOutcome.UNSUPPORTED:
            msg = f"⚠ **{name}** — unsupported file type; skipped."
        else:  # PARSE_FAILED
            msg = f"❌ **{name}** — {doc.note}."

        ingestable = doc.outcome not in {UploadOutcome.TOO_LARGE, UploadOutcome.UNSUPPORTED}
        # Stash the bytes so the persist action can forward them without a
        # re-read (the temp file may be gone by the time it's clicked).
        cl.user_session.set(f"upload_bytes::{name}", content)

        # --persist-uploads: auto-ingest into the agent's KB. Otherwise
        # offer the opt-in action button (text docs + images via OCR —
        # the runtime's OCR may differ from the local parser).
        if ingestable and _auto_persist_uploads():
            await cl.Message(content=msg).send()
            await _ingest_to_kb(name, content)
            continue
        actions = []
        if ingestable:
            actions.append(
                cl.Action(
                    name="persist_kb",
                    payload={"filename": name},
                    label="📥 Add to agent's KB permanently",
                    tooltip="Ingest this file into the agent's knowledge base for RAG",
                )
            )
        await cl.Message(content=msg, actions=actions).send()


async def _ingest_to_kb(filename: str, content: bytes) -> None:
    """Ingest one file into the bound agent's KB via the existing endpoint.

    Shared by the ``--persist-uploads`` auto path and the opt-in
    ``persist_kb`` action so both behave identically.
    """
    client: PlaygroundClient = cl.user_session.get(_K_CLIENT)
    agent_name = cl.user_session.get(_K_AGENT)
    if not agent_name or not client:
        await cl.Message(content="Pick an agent first, then upload a file.").send()
        return
    progress = cl.Message(content=f"⏳ Ingesting **{filename}** into **{agent_name}**'s KB...")
    await progress.send()
    try:
        result = await client.upload_kb_files(agent=agent_name, files=[(filename, content)])
    except Exception as exc:
        await cl.Message(content=f"❌ KB ingest failed: {type(exc).__name__}: {exc}").send()
        return
    total = result.get("total_chunks_saved", 0)
    await cl.Message(
        content=(
            f"✅ **{filename}** ingested — {total} chunk(s) saved to "
            f"**{agent_name}**'s KB. Ask a question that needs it to test retrieval."
        )
    ).send()


@cl.action_callback("persist_kb")
async def on_persist_kb(action: cl.Action) -> None:
    """Ingest a previously-uploaded file into the agent's KB.

    Forwards the held bytes to ``POST /api/v1/agents/{name}/kb`` (the
    existing multipart ingest path) so RAG testing persists the document.
    """
    filename = action.payload.get("filename")
    if not filename:
        await cl.Message(content="No file to ingest — upload one first.").send()
        return
    content = cl.user_session.get(f"upload_bytes::{filename}")
    if content is None:
        await cl.Message(
            content=f"❌ The bytes for {filename!r} are no longer available — re-upload it."
        ).send()
        return
    await _ingest_to_kb(filename, content)


# ---------------------------------------------------------------------------
# Multi-turn message handling
# ---------------------------------------------------------------------------


def _parse_structured_input(raw: str) -> dict[str, Any] | None:
    """Parse a JSON-object message into structured input, else ``None``.

    Power users can paste a JSON object to drive an agent whose schema
    needs more than free text (back-compat with the old single-shot
    flow). Tolerates ```json fences. Plain prose returns ``None`` →
    treated as a chat message.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Handle one operator turn: uploads → run → render → feedback.

    The conversation runs through the selected :class:`ConversationBackend`
    so memory is carried server-side (sessions) or client-side
    (re-sent transcript) transparently. Streaming renders tokens live when
    the runtime advertises it.
    """
    # Process any attached files first so this turn's context includes them.
    await _handle_uploads(message)

    agent_name = cl.user_session.get(_K_AGENT)
    client: PlaygroundClient = cl.user_session.get(_K_CLIENT)
    backend = cl.user_session.get(_K_BACKEND)
    caps: RuntimeCapabilities = cl.user_session.get(_K_CAPS)
    convo: ConversationState = cl.user_session.get(_K_CONVO)
    uploads: UploadStore = cl.user_session.get(_K_UPLOADS)

    if not agent_name or not client or backend is None:
        # An upload-only message (no text) before picking an agent is fine
        # — only nudge when there's actual text but no agent.
        if message.content.strip():
            await cl.Message(content="Pick an agent first from the buttons above.").send()
        return

    user_text = message.content.strip()
    if not user_text:
        return  # upload-only turn already handled above

    base_input = _parse_structured_input(user_text)
    # The human-readable message: the JSON itself when structured, else prose.
    display_text = user_text
    docs = uploads.context_documents()

    # Contract: the backend receives ``state`` carrying PRIOR turns only +
    # the new message separately (``user_message``). The current user turn
    # is appended AFTER the call so it isn't double-counted in the
    # re-sent transcript. The assistant turn is appended on completion.

    # Streaming path — render tokens live as they arrive.
    if caps.run_streaming:
        await _run_streaming(
            client=client,
            agent_name=agent_name,
            user_text=display_text,
            base_input=base_input,
            convo=convo,
            docs=docs,
        )
        return

    # Buffered path (today's behavior) — through the backend.
    thinking = cl.Message(content="")
    await thinking.send()
    try:
        result = await backend.send_turn(
            agent=agent_name,
            user_message=display_text,
            base_input=base_input,
            state=convo,
            documents=docs,
        )
    except TimeoutError as exc:
        thinking.content = f"⏱ Timed out: {exc}"
        await thinking.update()
        return
    except Exception as exc:
        thinking.content = f"❌ Run failed: {type(exc).__name__}: {exc}"
        await thinking.update()
        return

    text = result.output_text or "_(no output)_"
    if result.status not in {"success", "unknown"}:
        text = f"⚠ status `{result.status}`\n\n{text}"
    # Record the completed exchange (user + assistant) for the next turn's
    # context + feedback attachment.
    convo.add_user(display_text)
    convo.add_assistant(result.output_text, run_id=result.run_id)
    cl.user_session.set("last_run_id", result.run_id)

    thinking.content = text
    thinking.actions = _feedback_actions(result.run_id)
    await thinking.update()


async def _run_streaming(
    *,
    client: PlaygroundClient,
    agent_name: str,
    user_text: str,
    base_input: dict[str, Any] | None,
    convo: ConversationState,
    docs: list[Any],
) -> None:
    """Stream a turn's tokens into a live ``cl.Message``.

    Uses the SSE run endpoint. Streaming is purely additive observation —
    the run persists exactly as a buffered run, so feedback still attaches
    to the resulting ``run_id`` from the terminal ``done`` frame.

    Streaming is inherently client-managed context-wise (we POST the
    transcript + docs to the stateless stream endpoint), so we assemble
    the same input the client-managed backend would.
    """
    from movate.playground.conversation import build_run_input  # noqa: PLC0415

    run_input = build_run_input(
        user_message=user_text,
        base_input=base_input,
        turns=convo.turns,  # prior turns only (current turn appended after)
        documents=docs,
    )
    msg = cl.Message(content="")
    await msg.send()
    collected: list[str] = []
    run_id: str | None = None
    final_output: dict[str, Any] = {}
    status = "success"
    try:
        async for event in client.stream_run(agent=agent_name, input_data=run_input):
            if event.is_token:
                collected.append(event.text)
                await msg.stream_token(event.text)
            elif event.is_done:
                run_id = event.data.get("run_id")
                status = event.data.get("status", "success")
                final_output = event.data.get("output") or {}
            elif event.is_error:
                status = "error"
                err_text = event.data.get("message", "stream error")
                await msg.stream_token(f"\n\n❌ {err_text}")
    except Exception as exc:
        await msg.stream_token(f"\n\n❌ Stream failed: {type(exc).__name__}: {exc}")
        await msg.update()
        return

    # Reconstruct the assistant text — prefer the terminal output's text
    # field; fall back to the concatenated token deltas. Record the
    # completed exchange (user + assistant) now that the turn is done.
    assistant_text = extract_output_text(final_output) or "".join(collected)
    convo.add_user(user_text)
    convo.add_assistant(assistant_text, run_id=run_id)
    cl.user_session.set("last_run_id", run_id)
    if status not in {"success", "unknown"} and not collected:
        msg.content = f"⚠ status `{status}`"
    msg.actions = _feedback_actions(run_id)
    await msg.update()


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def _feedback_actions(run_id: str | None) -> list[cl.Action]:
    """Build the 👍/👎 actions for an assistant turn (none without a run_id)."""
    if not run_id:
        return []
    return [
        cl.Action(name="feedback", payload={"value": "up", "run_id": run_id}, label="👍 Helpful"),
        cl.Action(
            name="feedback", payload={"value": "down", "run_id": run_id}, label="👎 Not helpful"
        ),
    ]


@cl.action_callback("feedback")
async def on_feedback(action: cl.Action) -> None:
    """Persist 👍/👎 (+ optional comment) for a run.

    Routes to the feedback API when the runtime advertises it (ADR 045
    D14), else the existing persistence path — never regressing today's
    behavior. Both currently POST ``/runs/{id}/feedback`` client-side.
    """
    client: PlaygroundClient = cl.user_session.get(_K_CLIENT)
    caps: RuntimeCapabilities = cl.user_session.get(_K_CAPS)
    run_id = action.payload.get("run_id") or cl.user_session.get("last_run_id")
    if not run_id or not client:
        await cl.Message(content="No run to attach feedback to. Send a message first.").send()
        return

    score = 1 if action.payload.get("value") == "up" else -1
    route = feedback_route(caps)

    comment_msg = await cl.AskUserMessage(
        content=(
            f"Saving {'👍' if score == 1 else '👎'} for run `{run_id}`. "
            "Add a comment (or press Enter to skip)?"
        ),
        timeout=120,
    ).send()
    comment_text: str | None = None
    if comment_msg and isinstance(comment_msg, dict):
        text = comment_msg.get("output", "").strip()
        if text:
            comment_text = text

    try:
        user = cl.user_session.get("user")
        user_id = (
            getattr(user, "identifier", None)
            if user is not None
            else os.environ.get("MDK_PLAYGROUND_USER_ID", "playground-anonymous")
        )
        await client.post_feedback(
            run_id=run_id,
            score=score,
            comment=comment_text,
            user_id=user_id,
        )
    except Exception as exc:
        await cl.Message(content=f"❌ Could not save feedback: {type(exc).__name__}: {exc}").send()
        return

    suffix = " + comment" if comment_text else ""
    via = "feedback API" if route is FeedbackRoute.FEEDBACK_API else "runtime persistence"
    await cl.Message(
        content=(
            f"✅ Feedback saved ({'👍' if score == 1 else '👎'}{suffix}) via {via}. "
            "It's in Postgres now and (if Langfuse is configured on the runtime) "
            "also pushed as a score on the trace."
        )
    ).send()


# ---------------------------------------------------------------------------
# Thread resume (history sidebar → restore conversation)
# ---------------------------------------------------------------------------


@cl.on_chat_resume  # pragma: no cover - requires chainlit + data layer at runtime
async def on_chat_resume(thread: Any) -> None:
    """Restore a resumed thread's conversation into the session.

    Chainlit's data layer persists the message transcript; on resume we
    rebuild the playground's structured :class:`ConversationState` from
    those stored messages so the client-managed backend can keep
    re-sending the right history (and feedback can attach to the right
    run). Re-detects capabilities + re-binds the agent for the new
    process.
    """
    client, caps = await _init_session()

    convo = ConversationState()
    steps = thread.get("steps") if isinstance(thread, dict) else None
    agent_name = None
    metadata = thread.get("metadata") if isinstance(thread, dict) else None
    if isinstance(metadata, dict):
        agent_name = metadata.get("agent_name")
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        output = step.get("output") or ""
        if step_type == "user_message":
            convo.add_user(output)
        elif step_type in {"assistant_message", "run", "llm"}:
            convo.add_assistant(output)
    cl.user_session.set(_K_CONVO, convo)

    if agent_name:
        _bind_agent(agent_name, caps, client)
        # _bind_agent reset the convo — restore the rebuilt one.
        cl.user_session.set(_K_CONVO, convo)
        await cl.Message(
            content=(
                f"📜 Resumed conversation with **{agent_name}** "
                f"({len(convo.turns)} prior turn(s)). Continue chatting."
            )
        ).send()
    else:
        await cl.Message(
            content=(
                "📜 Conversation resumed. Pick the agent again above to "
                "continue (the agent binding isn't recorded in the thread)."
            )
        ).send()
