"""Chainlit app for the MDK playground.

Run via::

    chainlit run -m movate.playground.app

…or — easier — via the CLI wrapper::

    mdk playground serve --runtime-url http://127.0.0.1:8000

The CLI wrapper handles configuration via env vars + spawns Chainlit
with the right module path; this file is the actual app.

How the UX flows:

1. **Chat start** (``@cl.on_chat_start``) — fetch the agent catalog
   from the runtime; show a picker (Chainlit Action buttons).
2. **Agent picked** — fetch the agent's detail (input schema), show
   a form built from the schema (Chainlit AskUserMessage with input
   field for each required property).
3. **Input submitted** — submit the run, poll until terminal,
   render the output. Show 👍/👎 buttons + a comment field.
4. **Feedback** — POST to ``/runs/{id}/feedback``; the runtime
   persists to Postgres + (best-effort) mirrors to Langfuse.

This is the MVP — one agent per chat session. A future iteration
could add: side-by-side variant comparison, threading multiple runs
in one conversation, file upload for input schemas with binary
fields, and SSO via Azure AD.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Chainlit is an optional dependency (``[playground]`` extra). Import
# lazily inside the module so that ``import movate.playground.app``
# from a no-extras install raises a clear error instead of a cryptic
# ModuleNotFoundError mid-decorator.
try:
    import chainlit as cl  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover - covered by CLI hint
    raise ImportError(
        "movate.playground requires the [playground] extra. "
        "Install with: uv pip install 'movate-cli[playground]'"
    ) from exc

from movate.playground.client import PlaygroundClient, PlaygroundClientConfig

# Thread-resume UI tuning (PR-P). Rendering the full thread history
# can flood the chat — show the most recent N turns inline, truncate
# each input/output preview to keep rows scannable.
_RECENT_TURNS_TO_RENDER = 5
_TURN_PREVIEW_CHARS = 120
# Thread-picker label cap. Chainlit Action labels render in a tight
# row; > 30 chars wrap awkwardly.
_THREAD_LABEL_MAX = 30


def _client_from_env() -> PlaygroundClient:
    """Build the runtime client from env vars set by the CLI wrapper.

    Chainlit's process model has no direct way to pass typed args
    into the app module; the CLI exports env vars before
    ``chainlit run`` and we read them here. Same pattern Chainlit's
    own docs recommend.
    """
    runtime_url = os.environ.get("MDK_PLAYGROUND_RUNTIME_URL", "http://127.0.0.1:8000")
    api_key = os.environ.get("MDK_PLAYGROUND_API_KEY")
    return PlaygroundClient(
        PlaygroundClientConfig(
            runtime_url=runtime_url,
            api_key=api_key,
        )
    )


@cl.on_chat_start
async def start() -> None:
    """Show the agent picker on session start.

    Reads ``GET /api/v1/agents`` and renders one Chainlit Action
    button per agent. The action callback below stores the picked
    agent in the user-session and prompts for input.
    """
    client = _client_from_env()
    cl.user_session.set("client", client)

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
            value=a.get("name", ""),
            label=f"{a.get('name', '?')} · v{a.get('version', '?')}",
            description=a.get("description", "")[:120],
        )
        for a in agents
    ]
    await cl.Message(
        content=(
            f"**MDK playground** — {len(agents)} agent(s) available "
            "on this runtime. Pick one to test:"
        ),
        actions=actions,
    ).send()


@cl.action_callback("pick_agent")
async def on_pick_agent(action: cl.Action) -> None:
    """An agent was picked. Fetch its input schema + ask for input.

    Also lists any existing threads for this agent so the operator
    can resume a prior multi-turn conversation (Tier 10.5 / PR-P).
    Single-shot mode (no thread) stays the default — pre-PR-P
    behavior is byte-for-byte unchanged for operators who don't pick
    a thread.
    """
    client: PlaygroundClient = cl.user_session.get("client")
    agent_name = action.value
    cl.user_session.set("agent_name", agent_name)
    # Clear any thread from a prior agent's session — a fresh pick
    # starts in single-shot mode until the operator explicitly picks
    # or creates a thread.
    cl.user_session.set("thread_id", None)

    try:
        detail = await client.get_agent_detail(agent_name)
    except Exception as exc:
        await cl.Message(
            content=(
                f"❌ Could not fetch agent detail for {agent_name!r}: {type(exc).__name__}: {exc}"
            )
        ).send()
        return

    cl.user_session.set("agent_detail", detail)
    # Render the input schema as a code block so the operator can copy
    # the shape, then ask for JSON. A future iteration could
    # auto-generate one Chainlit input field per property — for now,
    # JSON-text-area is the simplest path that handles all schemas
    # (including nested arrays / objects).
    input_schema = detail.get("input_schema") or detail.get("schema", {}).get("input") or {}
    sample = _example_payload_for_schema(input_schema)
    schema_json = json.dumps(input_schema, indent=2)
    sample_json = json.dumps(sample, indent=2)
    await cl.Message(
        content=(
            f"**{agent_name}** selected. Input schema:\n\n"
            f"```json\n{schema_json}\n```\n\n"
            "Paste your input as JSON in the chat (or edit the sample below)."
        )
    ).send()
    await cl.Message(content=f"**Sample input:**\n```json\n{sample_json}\n```").send()
    # KB upload action — gives the dev-team a fast loop for "did
    # adding this document fix the failing case?" testing without
    # leaving the chat or running CLI commands.
    await cl.Message(
        content=(
            "**Knowledge base:** drag-drop .md / .txt files into the "
            "chat (use the paperclip), or click below to upload a "
            "file via dialog."
        ),
        actions=[
            cl.Action(
                name="upload_kb",
                value=agent_name,
                label="📎 Upload KB file",
                description="Add a document to this agent's knowledge base",
            ),
        ],
    ).send()

    # Thread picker (Tier 10.5 / PR-P). List recent threads for this
    # agent so the operator can resume a multi-turn conversation;
    # offer a "new thread" action that opens a fresh one. Falls back
    # silently when the runtime endpoint isn't available (pre-PR-O
    # runtimes 404 on /api/v1/threads) — operator keeps single-shot
    # mode without seeing an error.
    try:
        threads = await client.list_threads(agent=agent_name, limit=5)
    except Exception:
        threads = []
    actions: list = [
        cl.Action(
            name="new_thread",
            value=agent_name,
            label="+ New thread",
            description="Start a fresh multi-turn conversation",
        ),
    ]
    if threads:
        for t in threads:
            label = t.get("title") or "(untitled)"
            if len(label) > _THREAD_LABEL_MAX:
                label = label[: _THREAD_LABEL_MAX - 3] + "..."
            actions.append(
                cl.Action(
                    name="resume_thread",
                    value=t["thread_id"],
                    label=f"📜 {label}",
                    description=f"Resume thread {t['thread_id'][:8]}…",
                )
            )
    await cl.Message(
        content=(
            "**Conversation mode:** by default each message is a "
            "single-shot run. Pick a thread to keep multi-turn context "
            "(the agent's prior turns will appear in the thread history "
            "even if its prompt template doesn't auto-render them yet)."
        ),
        actions=actions,
    ).send()


@cl.action_callback("new_thread")
async def on_new_thread(action: cl.Action) -> None:
    """Operator clicked "New thread" — open one + tell them subsequent
    messages will go via /api/v1/threads/{id}/messages."""
    client: PlaygroundClient = cl.user_session.get("client")
    agent_name = action.value or cl.user_session.get("agent_name")
    if not agent_name or not client:
        await cl.Message(content="Pick an agent first from the buttons above.").send()
        return
    try:
        thread = await client.create_thread(agent=agent_name)
    except Exception as exc:
        await cl.Message(content=f"❌ Could not open thread: {type(exc).__name__}: {exc}").send()
        return
    thread_id = thread["thread_id"]
    cl.user_session.set("thread_id", thread_id)
    await cl.Message(
        content=(
            f"✅ Opened new thread `{thread_id[:8]}…` for **{agent_name}**.\n\n"
            "Subsequent messages stay in this thread until you pick a different "
            "one or refresh."
        )
    ).send()


@cl.action_callback("resume_thread")
async def on_resume_thread(action: cl.Action) -> None:
    """Operator picked an existing thread — bind it + show prior turns
    so they have visual continuity before sending the next message."""
    client: PlaygroundClient = cl.user_session.get("client")
    thread_id = action.value
    if not thread_id or not client:
        await cl.Message(content="Couldn't bind thread — pick an agent first.").send()
        return
    try:
        thread = await client.get_thread(thread_id, include_runs=True)
    except Exception as exc:
        await cl.Message(content=f"❌ Could not fetch thread: {type(exc).__name__}: {exc}").send()
        return
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("agent_name", thread["agent"])

    runs = thread.get("runs") or []
    lines = [
        f"📜 Resumed thread `{thread_id[:8]}…` (**{thread['agent']}**)",
        f"_{len(runs)} prior turn(s)_",
    ]
    if runs:
        lines.append("")
        # Show the last RECENT_TURNS_TO_RENDER turns inline — older
        # turns are still in the thread but rendering the full history
        # can flood the chat. Per-turn text is truncated to keep each
        # row scannable.
        recent = runs[-_RECENT_TURNS_TO_RENDER:]
        for i, run in enumerate(recent, start=max(1, len(runs) - len(recent) + 1)):
            inp = run.get("input") or {}
            out = run.get("output") or {}
            inp_str = json.dumps(inp, indent=None)
            out_str = json.dumps(out, indent=None)
            if len(inp_str) > _TURN_PREVIEW_CHARS:
                inp_str = inp_str[: _TURN_PREVIEW_CHARS - 3] + "..."
            if len(out_str) > _TURN_PREVIEW_CHARS:
                out_str = out_str[: _TURN_PREVIEW_CHARS - 3] + "..."
            lines.append(f"**Turn {i}** — in: `{inp_str}` → out: `{out_str}`")
    lines.append("\nSend your next message in JSON and it'll continue this thread.")
    await cl.Message(content="\n".join(lines)).send()


@cl.action_callback("upload_kb")
async def on_upload_kb(action: cl.Action) -> None:
    """Operator clicked "Upload KB file" — prompt for a file picker,
    then POST it to the runtime's KB ingest endpoint."""
    client: PlaygroundClient = cl.user_session.get("client")
    agent_name = action.value or cl.user_session.get("agent_name")
    if not agent_name or not client:
        await cl.Message(content="Pick an agent first from the buttons above.").send()
        return

    ask = cl.AskFileMessage(
        content=(
            f"Select one or more **.md / .markdown / .txt** files to add "
            f"to **{agent_name}**'s knowledge base. They'll be chunked, "
            "embedded, and indexed."
        ),
        accept=["text/markdown", "text/plain", ".md", ".markdown", ".txt"],
        max_size_mb=20,
        max_files=10,
        timeout=180,
    )
    files = await ask.send()
    if not files:
        await cl.Message(content="No files received — upload cancelled.").send()
        return

    # ``cl.AskFileMessage`` returns a list of AskFileResponse objects,
    # each carrying ``name`` + ``path`` (a temp file on disk Chainlit
    # already wrote). We read the bytes and forward to the runtime.
    payload: list[tuple[str, bytes]] = []
    for f in files:
        try:
            with open(f.path, "rb") as fh:
                payload.append((f.name, fh.read()))
        except OSError as exc:
            await cl.Message(
                content=f"❌ Could not read uploaded file {f.name!r}: {exc}",
            ).send()
            return

    progress = cl.Message(content=f"⏳ Ingesting {len(payload)} file(s) into **{agent_name}**...")
    await progress.send()
    try:
        result = await client.upload_kb_files(agent=agent_name, files=payload)
    except Exception as exc:
        await cl.Message(
            content=f"❌ KB upload failed: {type(exc).__name__}: {exc}",
        ).send()
        return

    total_saved = result.get("total_chunks_saved", 0)
    per_file = result.get("files") or []
    lines = [f"✅ Ingested **{total_saved}** chunk(s) total."]
    for entry in per_file:
        status = entry.get("status", "?")
        src = entry.get("source", "?")
        saved = entry.get("chunks_saved", 0)
        if status == "ingested":
            lines.append(f"- `{src}` — {saved} chunk(s) saved ✓")
        elif status == "empty":
            lines.append(f"- `{src}` — empty / unchunkable, skipped")
        else:
            lines.append(f"- `{src}` — {status}, skipped")
    lines.append(
        "\nNow try a query that needs this content — the agent's "
        "KB skill (if wired) will retrieve from the new chunks."
    )
    await cl.Message(content="\n".join(lines)).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Operator typed input. Parse as JSON, submit the run, render
    the output + feedback buttons.

    Tolerates code fences (operators often paste JSON wrapped in
    ```json ... ``` blocks); strips them before parsing.
    """
    agent_name = cl.user_session.get("agent_name")
    client: PlaygroundClient = cl.user_session.get("client")
    if not agent_name or not client:
        await cl.Message(content="Pick an agent first from the buttons above.").send()
        return

    raw = message.content.strip()
    if raw.startswith("```"):
        # Strip code fences (```json + closing ```) commonly added
        # when pasting from an editor. Tolerant: ``` and ```json both work.
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        input_data = json.loads(raw)
    except json.JSONDecodeError as exc:
        await cl.Message(
            content=(
                f"❌ Couldn't parse that as JSON: {exc}\n\n"
                "Send a JSON object matching the agent's input schema."
            )
        ).send()
        return

    # Submit + poll. Stream a progress message so the operator sees
    # something happen during the typical 2-10s eval window.
    #
    # Routing: when a thread is bound to the session (PR-P), the
    # message goes via POST /api/v1/threads/{id}/messages so the
    # worker stamps thread_id on the spawned run. Otherwise the
    # default single-shot /run path runs (pre-PR-P behavior, byte-
    # for-byte unchanged for operators who haven't picked a thread).
    thread_id = cl.user_session.get("thread_id")
    mode_label = f"in thread `{thread_id[:8]}…`" if thread_id else "single-shot"
    progress = cl.Message(content=f"⏳ Running **{agent_name}** ({mode_label})...")
    await progress.send()
    try:
        if thread_id:
            submission = await client.submit_thread_message(
                thread_id=thread_id,
                input_data=input_data,
            )
        else:
            submission = await client.submit_run(agent=agent_name, input_data=input_data)
        job_id = submission.get("job_id")
        if not job_id:
            await cl.Message(content=f"❌ Runtime didn't return a job_id: {submission}").send()
            return
        run = await client.wait_for_run(job_id)
    except TimeoutError as exc:
        await cl.Message(content=f"⏱ Timed out: {exc}").send()
        return
    except Exception as exc:
        await cl.Message(content=f"❌ Run failed: {type(exc).__name__}: {exc}").send()
        return

    status = run.get("status", "unknown")
    run_id = run.get("run_id") or run.get("job_id")
    output = run.get("output") or run.get("data") or {}
    metrics = run.get("metrics") or {}
    cost = metrics.get("cost_usd", 0.0)
    latency = metrics.get("latency_ms", 0.0)

    body_lines = [
        f"**Status:** `{status}`",
        f"**Run ID:** `{run_id}`",
        f"**Cost:** ${cost:.4f}" if cost else "",
        f"**Latency:** {latency:.0f}ms" if latency else "",
        "",
        "**Output:**",
        f"```json\n{json.dumps(output, indent=2)}\n```",
    ]
    body = "\n".join(line for line in body_lines if line is not None)
    cl.user_session.set("last_run_id", run_id)
    await cl.Message(
        content=body,
        actions=[
            cl.Action(name="feedback", value="up", label="👍 Helpful"),
            cl.Action(name="feedback", value="down", label="👎 Not helpful"),
        ],
    ).send()


@cl.action_callback("feedback")
async def on_feedback(action: cl.Action) -> None:
    """Persist 👍 / 👎 to the runtime. Optionally ask for a comment
    on the way out so qualitative signal isn't lost."""
    client: PlaygroundClient = cl.user_session.get("client")
    run_id = cl.user_session.get("last_run_id")
    if not run_id:
        await cl.Message(content="No run to attach feedback to. Run something first.").send()
        return

    score = 1 if action.value == "up" else -1

    # Ask for an optional comment before persisting. Cancel-friendly:
    # if the operator declines, we still save the thumbs.
    comment_msg = await cl.AskUserMessage(
        content=(
            f"Saving {'👍' if score == 1 else '👎'} for run `{run_id}`. "
            "Add a comment (or press Enter to skip)?"
        ),
        timeout=120,
    ).send()
    # Chainlit returns a dict {"output": "...", ...} for AskUserMessage.
    comment_text: str | None = None
    if comment_msg and isinstance(comment_msg, dict):
        text = comment_msg.get("output", "").strip()
        if text:
            comment_text = text

    try:
        # User identity: take it from the chat session if available
        # (Chainlit's auth hook sets ``cl.user_session.get("user")``);
        # otherwise fall back to a generic "playground-user" identifier.
        # In prod with Azure AD enabled, ``user.identifier`` would be
        # the AAD object id.
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
    await cl.Message(
        content=(
            f"✅ Feedback saved ({'👍' if score == 1 else '👎'}{suffix}). "
            "It's in Postgres now and (if Langfuse is configured on the "
            "runtime) also pushed as a score on the trace."
        )
    ).send()


def _example_payload_for_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Generate a JSON-Schema-compliant example dict.

    Walks ``properties`` and produces sensible defaults per type so
    operators have a starting shape they can edit. Doesn't aim for
    schema-validation correctness — just a useful template.
    """
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}
    out: dict[str, Any] = {}
    for prop, spec in props.items():
        if not isinstance(spec, dict):
            out[prop] = None
            continue
        # Use ``example`` if the schema provides one (common in our
        # canonical schema YAML files).
        if "example" in spec:
            out[prop] = spec["example"]
            continue
        t = spec.get("type")
        if t == "string":
            out[prop] = spec.get("default", "")
        elif t == "integer":
            out[prop] = spec.get("default", 0)
        elif t == "number":
            out[prop] = spec.get("default", 0.0)
        elif t == "boolean":
            out[prop] = spec.get("default", False)
        elif t == "array":
            out[prop] = []
        elif t == "object":
            out[prop] = _example_payload_for_schema(spec)
        else:
            out[prop] = None
    return out
