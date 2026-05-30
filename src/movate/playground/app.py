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
import logging
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
from movate.playground.connection import (
    ConnectionMonitor,
    ConnectionState,
    reconnected_banner,
    slow_banner,
    unreachable_banner,
)
from movate.playground.conversation import (
    ConversationState,
    FeedbackRoute,
    extract_output_text,
    feedback_route,
    select_backend,
)
from movate.playground.state import resolve_data_layer_config
from movate.playground.targets import (
    TARGETS_ENV_VAR,
    PlaygroundTarget,
    decode_targets,
)
from movate.playground.uploads import UploadOutcome, UploadStore, adapt_upload
from movate.playground.voice import (
    VoiceNotEnabledError,
    VoiceWSClient,
    collect_audio,
)

logger = logging.getLogger(__name__)

# Session keys (kept as constants so set/get can't typo-drift).
_K_CLIENT = "client"
_K_CAPS = "capabilities"
_K_AGENT = "agent_name"
_K_AGENT_DETAIL = "agent_detail"
_K_BACKEND = "backend"
_K_CONVO = "conversation_state"
_K_UPLOADS = "upload_store"
_K_TARGET = "target_name"
# Connection-status monitor (Item 1). One per session; tracks reachability.
_K_CONN_MONITOR = "conn_monitor"
_K_CONN_STATE = "conn_state"
# Feedback idempotency guard (Item 4). Records run_ids already submitted.
_K_FEEDBACK_SUBMITTED = "feedback_submitted"
# Voice mode (opt-in; default OFF). The voice WS client is session-scoped; the
# per-turn chunk counter lets on_audio_end skip a no-audio recording.
_K_VOICE_CLIENT = "voice_client"
_K_VOICE_CHUNKS = "voice_chunks"

# Configured targets for multi-target mode, decoded ONCE at import from the
# env var the CLI launcher sets (:data:`TARGETS_ENV_VAR`). Empty list →
# single-runtime mode (the original behavior): no chat-profile picker, the
# client is built from MDK_PLAYGROUND_RUNTIME_URL / _API_KEY as before.
_TARGETS: list[PlaygroundTarget] = decode_targets(os.environ.get(TARGETS_ENV_VAR))


def _targets_by_name() -> dict[str, PlaygroundTarget]:
    """Index the configured targets by name (for chat-profile lookup)."""
    return {t.name: t for t in _TARGETS}


def _client_from_target(target: PlaygroundTarget) -> PlaygroundClient:
    """Build a runtime client pinned to one configured target.

    The target carries its OWN resolved bearer token (read from its
    ``key_env`` by the launcher), so each profile talks to its runtime
    with its own credentials — no global key assumption.
    """
    return PlaygroundClient(PlaygroundClientConfig(runtime_url=target.url, api_key=target.api_key))


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


def _voice_enabled() -> bool:
    """Whether voice mode is enabled for this playground (``--voice``).

    OFF by default — the launcher exports ``MDK_PLAYGROUND_VOICE=1`` only when
    the operator passes ``--voice``. With it OFF the audio callbacks below are
    never registered, so the text playground is byte-for-byte unchanged.
    Read at import time so registration matches the launch flag.
    """
    return os.environ.get("MDK_PLAYGROUND_VOICE", "") in {"1", "true", "True"}


# Resolved ONCE at import (the child ``chainlit run`` process reads the env the
# launcher set). Gates whether the audio callbacks are registered at all.
_VOICE_ENABLED: bool = _voice_enabled()


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

        Returning ``None`` is how Chainlit expresses "no data layer" —
        ``chainlit.data.get_data_layer()`` hands the function's return value
        straight back and every caller guards with ``if get_data_layer():``,
        so a missing dependency drops persistence instead of breaking the UI.
        """
        # SQLAlchemyDataLayer (and its async engine) needs ``sqlalchemy`` +
        # ``greenlet`` — both declared in the [playground] extra. If the
        # operator's environment is missing them (partial install, packaging
        # drift), degrade to no persistence rather than crashing the whole
        # playground on startup / first request.
        try:
            from chainlit.data.sql_alchemy import SQLAlchemyDataLayer  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "Playground history disabled: the conversation-history data "
                "layer needs 'sqlalchemy' (and 'greenlet'), which aren't "
                "importable. Reinstall the playground extra "
                "(`uv pip install 'movate-cli[playground]'`) to enable the "
                "past-conversations sidebar. Continuing without persistence.",
                exc_info=True,
            )
            return None

        if _DATA_LAYER_CFG.postgres_url:
            conninfo = _DATA_LAYER_CFG.postgres_url
        else:
            db_path = _DATA_LAYER_CFG.sqlite_path
            assert db_path is not None  # invariant: sqlite path set when no PG url
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conninfo = f"sqlite+aiosqlite:///{db_path}"
        return SQLAlchemyDataLayer(conninfo=conninfo)


# ---------------------------------------------------------------------------
# Chat profiles (multi-target mode). Registered at import time when the CLI
# launcher handed us configured targets — one profile per target. Selecting a
# profile pins the session to THAT target's runtime + key (see _init_session).
# Absent in single-runtime mode, so the original no-picker flow is unchanged.
# ---------------------------------------------------------------------------

if _TARGETS:

    @cl.set_chat_profiles  # pragma: no cover - requires chainlit at runtime
    async def _chat_profiles(_user: Any = None) -> list[Any]:
        """One chat profile per configured target (name + URL label).

        A target whose key is missing still appears (so the operator sees
        every registered runtime) but its description flags the absent key;
        :func:`start` then shows a friendly "no key" message instead of
        letting a 401 surface as a stack trace.
        """
        return [
            cl.ChatProfile(
                name=t.name,
                markdown_description=t.profile_description(),
                default=(idx == 0),
            )
            for idx, t in enumerate(_TARGETS)
        ]


# ---------------------------------------------------------------------------
# Session setup helpers
# ---------------------------------------------------------------------------


def _selected_target() -> PlaygroundTarget | None:
    """Return the target for this session's selected chat profile, if any.

    In multi-target mode Chainlit stores the chosen profile's name under
    ``chat_profile``; we map it back to the configured
    :class:`PlaygroundTarget`. Returns ``None`` in single-runtime mode (no
    targets configured) or when the selection doesn't match a known target
    (e.g. resumed thread from a different config) — callers fall back to
    the env-based single-runtime client.
    """
    if not _TARGETS:
        return None
    selected = cl.user_session.get("chat_profile")
    if not selected:
        return None
    return _targets_by_name().get(str(selected))


async def _init_session() -> tuple[PlaygroundClient, RuntimeCapabilities]:
    """Create the client + detect capabilities, store both in the session.

    The client is pinned to the selected chat profile's target in
    multi-target mode, else built from the single-runtime env vars
    (unchanged original behavior). Capability detection runs ONCE per
    chat. A runtime that predates the capabilities endpoint (404) degrades
    to the all-off default — client-managed memory, buffered responses,
    legacy feedback — i.e. today's behavior. Never raises on a missing
    endpoint.
    """
    target = _selected_target()
    if target is not None:
        client = _client_from_target(target)
        cl.user_session.set(_K_TARGET, target.name)
    else:
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
    # Item 1: connection monitor — probe the runtime on each turn.
    # ``_client`` is the inner httpx.AsyncClient; absent on stub clients (tests).
    http_client = getattr(client, "_client", None)
    monitor = ConnectionMonitor(client=http_client) if http_client is not None else None
    cl.user_session.set(_K_CONN_MONITOR, monitor)
    cl.user_session.set(_K_CONN_STATE, ConnectionState.CONNECTED)
    # Item 4: track which run_ids have already received feedback (idempotency).
    cl.user_session.set(_K_FEEDBACK_SUBMITTED, set())
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
    voice = ""
    if _VOICE_ENABLED:
        # Voice mode is on for this launch. ``caps.voice`` is only a hint (the
        # capabilities probe can't see the WS route), so we phrase it as
        # available + note the graceful-degrade rather than a hard promise.
        advertised = " (advertised)" if caps.voice else ""
        voice = f" · 🎙 **voice mode on**{advertised} — use the mic to talk"
    return (
        f"_Memory: **{memory}** · responses: **{stream}** · "
        f"uploads up to {caps.max_upload_mb} MB, {caps.max_upload_count} files{voice}_"
    )


_AGENT_LABEL_MAX_DESC = 60  # max description chars in the picker label
_AGENT_LABEL_MAX_TAGS = 3  # max tags shown in the label


def _agent_picker_label(agent: dict[str, Any]) -> str:
    """Build a rich label for the agent picker (Item 2).

    Format: ``name — description [tag1, tag2]`` with graceful truncation.
    The name + version are always shown; the description and tags are added
    when present so the operator can pick the right agent without guessing.
    ``v?`` is suppressed when the runtime doesn't include a version field.
    """
    name = agent.get("name") or "?"
    version = agent.get("version")
    version_part = f" · v{version}" if version else ""

    desc = (agent.get("description") or "").strip()
    if len(desc) > _AGENT_LABEL_MAX_DESC:
        desc = desc[: _AGENT_LABEL_MAX_DESC - 1] + "…"

    tags: list[str] = agent.get("tags") or []
    tag_part = ""
    if tags:
        shown = tags[:_AGENT_LABEL_MAX_TAGS]
        overflow = ", …" if len(tags) > _AGENT_LABEL_MAX_TAGS else ""
        tag_part = " [" + ", ".join(shown) + overflow + "]"

    if desc:
        return f"{name}{version_part} — {desc}{tag_part}"
    return f"{name}{version_part}{tag_part}"


def _agent_picker_tooltip(agent: dict[str, Any]) -> str:
    """Full description + all tags for the picker button tooltip."""
    parts: list[str] = []
    desc = (agent.get("description") or "").strip()
    if desc:
        parts.append(desc)
    tags: list[str] = agent.get("tags") or []
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    return "  ".join(parts)[:200] if parts else agent.get("name", "")


def _is_auth_error(exc: Exception) -> bool:
    """Heuristic: does ``exc`` look like a 401/403 from the runtime?

    The client raises ``httpx.HTTPStatusError`` whose ``.response`` carries
    the status code. We read it structurally (no httpx import here) so an
    auth failure can be surfaced as a friendly "no key" message rather than
    a raw stack trace. Anything we can't classify returns False (handled by
    the generic error path).
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status in {401, 403}


# ---------------------------------------------------------------------------
# Chat lifecycle
# ---------------------------------------------------------------------------


@cl.on_chat_start
async def start() -> None:
    """Detect capabilities, then show the agent picker on session start.

    In multi-target mode the session is already pinned (via the selected
    chat profile) to one target's runtime + key. A target with no
    resolvable key short-circuits to a friendly hint — we never fire a
    request that's guaranteed to 401.
    """
    target = _selected_target()
    if target is not None and not target.key_available:
        await cl.Message(
            content=(
                f"🔒 No key for target **{target.name}** "
                f"(`{target.url}`). Set the `{target.key_env}` env var to "
                "this target's bearer token, then reload — "
                "e.g. `mdk auth login` or `export "
                f"{target.key_env}=...`. Pick a different target from the "
                "profile selector to continue meanwhile."
            )
        ).send()
        return

    client, caps = await _init_session()

    try:
        agents = await client.list_agents()
    except Exception as exc:
        if _is_auth_error(exc) and target is not None:
            await cl.Message(
                content=(
                    f"🔒 Authentication failed for target **{target.name}** "
                    f"(`{target.url}`). The key in `{target.key_env}` is "
                    "missing, wrong, or expired — set it to a valid bearer "
                    "token and reload."
                )
            ).send()
            return
        if _is_auth_error(exc):
            await cl.Message(
                content=(
                    "🔒 Authentication failed (401/403). Set a valid bearer "
                    "token via `MOVATE_API_KEY` / `--api-key` and reload."
                )
            ).send()
            return
        hint = (
            "``MDK_PLAYGROUND_RUNTIME_URL`` points at it."
            if target is None
            else f"target **{target.name}** (`{target.url}`) is reachable."
        )
        await cl.Message(
            content=(
                f"❌ Could not reach the runtime: {type(exc).__name__}: {exc}\n\n"
                f"Check that the runtime is running and that {hint}"
            )
        ).send()
        return

    if not agents:
        scope = f" on target **{target.name}**" if target is not None else ""
        await cl.Message(
            content=(
                f"⚠ The runtime has no agents registered{scope}. "
                "Use ``mdk add <role>`` to scaffold one, then "
                "``mdk deploy --target <env>`` to publish it."
            )
        ).send()
        return

    actions = [
        cl.Action(
            name="pick_agent",
            payload={"value": a.get("name", "")},
            label=_agent_picker_label(a),
            tooltip=_agent_picker_tooltip(a),
        )
        for a in agents
    ]
    where = f" on target **{target.name}** (`{target.url}`)" if target is not None else ""
    await cl.Message(
        content=(
            f"**MDK playground** — {len(agents)} agent(s) available{where}. "
            "Pick one, then just start chatting.\n\n"
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


async def _check_connection() -> None:
    """Item 1 — probe the runtime and surface status changes in the chat.

    Compares the new state against the stored previous state and emits a
    Chainlit step/message only on transitions (CONNECTED→DISCONNECTED,
    DISCONNECTED→CONNECTED, etc.) so the UI isn't cluttered by repeated
    green banners on every turn.
    """
    monitor: ConnectionMonitor | None = cl.user_session.get(_K_CONN_MONITOR)
    if monitor is None:
        return
    prev: ConnectionState = cl.user_session.get(_K_CONN_STATE, ConnectionState.CONNECTED)
    new_state = await monitor.check()
    cl.user_session.set(_K_CONN_STATE, new_state)
    if new_state == prev:
        return
    # State changed — emit an appropriate banner.
    if new_state == ConnectionState.DISCONNECTED:
        await cl.Message(content=unreachable_banner()).send()
    elif prev == ConnectionState.DISCONNECTED and new_state in {
        ConnectionState.CONNECTED,
        ConnectionState.SLOW,
    }:
        await cl.Message(content=reconnected_banner()).send()
    elif new_state == ConnectionState.SLOW and monitor.last_duration_s is not None:
        await cl.Message(content=slow_banner(monitor.last_duration_s)).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Handle one operator turn: uploads → run → render → feedback.

    The conversation runs through the selected :class:`ConversationBackend`
    so memory is carried server-side (sessions) or client-side
    (re-sent transcript) transparently. Streaming renders tokens live when
    the runtime advertises it.
    """
    # Item 1: probe the runtime before each turn; surface status changes.
    await _check_connection()

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

    # Item 1: if the runtime was found unreachable, show a friendly retry
    # hint rather than letting the run attempt fail with a raw exception.
    conn_state: ConnectionState = cl.user_session.get(_K_CONN_STATE, ConnectionState.CONNECTED)
    if conn_state == ConnectionState.DISCONNECTED:
        await cl.Message(
            content=(
                "⚠️ Runtime unreachable — retrying...\n\n"
                "The last reachability check found the runtime unreachable. "
                "Your message will be sent once the runtime is back — "
                "or try again in a moment."
            )
        ).send()
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
# Voice mode (opt-in; default OFF — registered only when --voice was passed)
#
# Mic audio → WS /api/v1/agents/{name}/voice → STT → the unchanged agent →
# TTS → audio back. Chainlit fires three callbacks per recording:
#   on_audio_start  — open the voice WS to the bound agent on the SELECTED
#                     target (reusing the same base URL + bearer the text path
#                     uses) and send the per-turn ``config`` frame.
#   on_audio_chunk  — forward each mic frame to the runtime as a binary frame.
#   on_audio_end    — send ``end``, then consume the turn: render partial /
#                     final transcripts + the agent's streamed answer into one
#                     live bubble, and play the returned TTS audio via cl.Audio.
# A runtime without the voice route degrades to a friendly "voice not enabled"
# message (VoiceNotEnabledError) rather than crashing the UI.
# ---------------------------------------------------------------------------


def _voice_client_for_session() -> VoiceWSClient | None:
    """Build a :class:`VoiceWSClient` for the bound agent on this session's target.

    Reuses the SAME runtime URL + bearer token the text path resolved (the
    selected chat-profile target in multi-target mode, else the single-runtime
    env vars) by reading them off the session's :class:`PlaygroundClient`
    config — so voice talks to exactly the runtime the operator picked, with
    that runtime's credentials. Returns ``None`` when no agent is bound yet.
    """
    agent_name = cl.user_session.get(_K_AGENT)
    client: PlaygroundClient | None = cl.user_session.get(_K_CLIENT)
    if not agent_name or client is None:
        return None
    cfg = client._config  # the playground's own config dataclass (URL + key)
    return VoiceWSClient(runtime_url=cfg.runtime_url, agent=agent_name, token=cfg.api_key)


async def _render_voice_turn(ws: VoiceWSClient, msg: cl.Message) -> None:
    """Consume one voice turn off ``ws``, streaming text into ``msg`` + playing TTS.

    Item 3 — partial-transcript display:
    Each ``transcript.partial`` frame updates the live message content so the
    operator sees captions appearing in real-time (like live subtitles), even
    on slow connections. When ``is_final`` arrives the caption is replaced with
    the confirmed utterance. Agent tokens are streamed via ``stream_token`` for
    a responsive feel. On completion, synthesized audio is attached as a
    ``cl.Audio`` element (auto-played) plus the 👍/👎 feedback actions. An
    ``error`` frame is surfaced inline; any text already streamed is preserved
    so a TTS-stage failure still leaves the reply readable (ADR 048 D8).
    """
    transcript = ""
    answer_parts: list[str] = []
    audio_frames: list[Any] = []
    run_id: str | None = None
    # Item 3: track the last partial text so we can replace it cleanly
    # when a new partial or the final transcript arrives.
    _last_caption = ""

    def _compose(caption: str) -> str:
        head = f"🎙 _{caption}_" if caption else ""
        body = "".join(answer_parts)
        return f"{head}\n\n{body}" if head and body else head or body

    async for frame in ws.iter_turn():
        if frame.is_partial:
            # Item 3: stream each partial into the live message so the operator
            # sees STT progress in real-time (caption-style, updated in-place).
            _last_caption = f"(listening) {frame.text}"
            msg.content = _compose(_last_caption)
            await msg.update()
        elif frame.is_final_transcript:
            # Final transcript replaces the last partial caption.
            transcript = frame.text
            _last_caption = f"you said: “{transcript}”"
            msg.content = _compose(_last_caption)
            await msg.update()
        elif frame.is_agent_token:
            # Item 3: use stream_token for agent tokens so the answer feels live.
            answer_parts.append(frame.text)
            # Rebuild full content to keep the transcript header visible.
            caption = _last_caption if _last_caption else ""
            msg.content = _compose(caption)
            await msg.update()
        elif frame.is_audio:
            audio_frames.append(frame)
        elif frame.is_error:
            stage = frame.data.get("stage", "?")
            message = frame.data.get("message", "voice error")
            answer_parts.append(f"\n\n❌ voice {stage} error: {message}")
            msg.content = _compose(_last_caption if _last_caption else "")
            await msg.update()
        elif frame.is_done:
            run_id = frame.data.get("run_id") or None

    # Play the synthesized audio back (one element, auto-played). Falls through
    # silently when TTS produced nothing (e.g. a degraded text-only turn).
    audio_bytes = collect_audio(audio_frames)
    elements: list[Any] = []
    if audio_bytes:
        mime = _voice_mime(audio_frames)
        elements.append(cl.Audio(content=audio_bytes, name="reply", mime=mime, auto_play=True))
    msg.elements = elements
    msg.actions = _feedback_actions(run_id)
    if run_id:
        cl.user_session.set("last_run_id", run_id)
    await msg.update()


def _voice_mime(audio_frames: list[Any]) -> str:
    """Best-effort MIME for the synthesized audio from the first frame's codec.

    The runtime tags each ``tts.audio`` header with ``codec`` (``pcm16`` /
    ``opus`` / ``mulaw``). Browsers play raw PCM poorly, but ``cl.Audio``
    wraps a player around the bytes; we map the codec to a sensible container
    hint and default to ``audio/wav`` (the OpenAI TTS reference adapter emits
    WAV-framed PCM) so the common path plays.
    """
    codec = ""
    if audio_frames:
        codec = str(audio_frames[0].data.get("codec", ""))
    return {"opus": "audio/ogg", "mulaw": "audio/basic"}.get(codec, "audio/wav")


if _VOICE_ENABLED:

    @cl.on_audio_start  # pragma: no cover - requires chainlit audio at runtime
    async def on_audio_start() -> bool:
        """Open the voice WS for the bound agent at the start of a recording.

        Returns ``True`` to let Chainlit proceed streaming mic chunks; ``False``
        aborts the recording (no agent bound, or the runtime can't do voice) so
        we never buffer audio we can't deliver. A connect failure shows the
        friendly "voice not enabled" hint.
        """
        ws = _voice_client_for_session()
        if ws is None:
            await cl.Message(
                content="🎙 Pick an agent first, then use the mic to talk to it."
            ).send()
            return False
        try:
            await ws.connect()
            await ws.send_config(mock=os.environ.get("MDK_PLAYGROUND_VOICE_MOCK", "") == "1")
        except VoiceNotEnabledError as exc:
            await cl.Message(
                content=(
                    "🔇 Voice isn't enabled on this runtime "
                    f"(`{ws.runtime_url}`). The agent still works in text — "
                    "just type. \n\n_Details: "
                    f"{type(exc).__name__}: {exc}_"
                )
            ).send()
            return False
        cl.user_session.set(_K_VOICE_CLIENT, ws)
        cl.user_session.set(_K_VOICE_CHUNKS, 0)
        return True

    @cl.on_audio_chunk  # pragma: no cover - requires chainlit audio at runtime
    async def on_audio_chunk(chunk: Any) -> None:
        """Forward one mic audio chunk to the runtime as a binary WS frame."""
        ws: VoiceWSClient | None = cl.user_session.get(_K_VOICE_CLIENT)
        if ws is None:
            return
        data = getattr(chunk, "data", None)
        if not data:
            return
        try:
            await ws.send_audio(bytes(data))
            cl.user_session.set(_K_VOICE_CHUNKS, cl.user_session.get(_K_VOICE_CHUNKS, 0) + 1)
        except Exception:
            # A mid-stream socket drop — stop forwarding; on_audio_end reports it.
            cl.user_session.set(_K_VOICE_CLIENT, None)

    @cl.on_audio_end  # pragma: no cover - requires chainlit audio at runtime
    async def on_audio_end() -> None:
        """End the utterance, run the turn, render the transcript + play TTS."""
        ws: VoiceWSClient | None = cl.user_session.get(_K_VOICE_CLIENT)
        cl.user_session.set(_K_VOICE_CLIENT, None)
        if ws is None:
            await cl.Message(
                content="🔇 The voice connection dropped before the turn ran. Try again."
            ).send()
            return
        if not cl.user_session.get(_K_VOICE_CHUNKS, 0):
            # No audio captured (e.g. an instant stop) — nothing to transcribe.
            await ws.aclose()
            await cl.Message(content="🎙 No audio captured — hold the mic and speak.").send()
            return
        msg = cl.Message(content="🎙 _(processing)_")
        await msg.send()
        try:
            await ws.end_turn()
            await _render_voice_turn(ws, msg)
        except Exception as exc:
            msg.content = f"❌ Voice turn failed: {type(exc).__name__}: {exc}"
            await msg.update()
        finally:
            await ws.aclose()


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

    Item 4 changes:
    * Idempotent-safe: same run_id + value submitted twice is a no-op
      (tracked in the session-scoped ``_K_FEEDBACK_SUBMITTED`` set so the
      buttons can't be double-submitted).
    * On success: emits "✓ Thanks!" confirmation.
    * On failure: emits a toast-style note and leaves buttons active.
    """
    client: PlaygroundClient = cl.user_session.get(_K_CLIENT)
    caps: RuntimeCapabilities = cl.user_session.get(_K_CAPS)
    run_id = action.payload.get("run_id") or cl.user_session.get("last_run_id")
    if not run_id or not client:
        await cl.Message(content="No run to attach feedback to. Send a message first.").send()
        return

    # Item 4: idempotency guard — same run + value combination is a no-op.
    feedback_key = f"{run_id}:{action.payload.get('value', '')}"
    submitted: set[str] = cl.user_session.get(_K_FEEDBACK_SUBMITTED) or set()
    if feedback_key in submitted:
        await cl.Message(content="✓ Feedback already recorded for this turn.").send()
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
        # Item 4: on failure, surface a toast-style message and leave buttons active.
        await cl.Message(
            content=f"⚠️ Feedback couldn't be saved — try again. ({type(exc).__name__}: {exc})"
        ).send()
        return

    # Item 4: mark this run+value as submitted (idempotency) + confirm visually.
    submitted.add(feedback_key)
    cl.user_session.set(_K_FEEDBACK_SUBMITTED, submitted)

    suffix = " + comment" if comment_text else ""
    via = "feedback API" if route is FeedbackRoute.FEEDBACK_API else "runtime persistence"
    await cl.Message(
        content=(f"✓ Thanks! Feedback saved ({'👍' if score == 1 else '👎'}{suffix}) via {via}.")
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
