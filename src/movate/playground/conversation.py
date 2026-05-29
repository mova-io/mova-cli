"""Conversation backends + context assembly (pure logic).

This is the heart of the capability-aware chat. A multi-turn
conversation can be carried two ways:

* :class:`SessionBackend` — **server-managed**. The runtime owns
  conversation memory (ADR 045 D10): open a session
  (``POST /api/v1/sessions``), then post each turn to
  ``/sessions/{id}/messages``. The server threads prior turns into the
  model context, so the client sends only the *new* message. Used when
  the capabilities endpoint advertises ``sessions``.

* :class:`ClientManagedBackend` — **client-managed** (the common case
  today). The runtime's run endpoint is stateless, so the *playground*
  re-sends the prior turns (plus any uploaded-document context) as part
  of each request. Memory lives in the browser session.

Both satisfy the :class:`ConversationBackend` Protocol, so the Chainlit
app drives them identically. :func:`select_backend` picks one from the
runtime's :class:`~movate.playground.capabilities.RuntimeCapabilities` —
the *single* place the sessions-vs-stateless decision is made. When the
Sessions API lands, the playground auto-upgrades: same code, the flag
flips, ``SessionBackend`` is selected, memory becomes server-managed.

The pure helpers here — :func:`assemble_conversation_context`,
:func:`build_run_input`, :func:`feedback_route` — have no Chainlit and no
network at import time, so they unit-test in isolation. The backends
take a client object with the needed async methods (duck-typed via a
narrow Protocol) so tests inject a fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from movate.playground.capabilities import RuntimeCapabilities
from movate.playground.uploads import UploadedDocument

# The conversation-input key the playground writes the running transcript
# under when client-managed. Kept distinct from the raw user-input keys
# so an agent's own input schema is never silently clobbered.
CONVERSATION_KEY = "conversation"
# Where uploaded-document text rides into a client-managed run input.
DOCUMENTS_KEY = "documents"
# The free-text key a chat-style agent most commonly reads. We mirror the
# new user message here so a plain ``{"message": "..."}``-shaped agent
# works out of the box without the operator hand-crafting JSON.
MESSAGE_KEY = "message"

# Cap how much upload text we splice into a single client-managed request
# so a huge PDF doesn't blow the model's context window. Server-managed
# sessions don't hit this (the runtime owns context budgeting).
_MAX_DOC_CHARS_PER_REQUEST = 12_000


class Role(StrEnum):
    """Turn author. Mirrors the conventional chat roles."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Turn:
    """One conversation turn — a user message or an assistant reply.

    ``text`` is the human-readable content (what renders in the chat and
    what the client-managed backend re-sends). ``run_id`` is stamped on
    assistant turns so feedback can attach to the right run.
    """

    role: Role
    text: str
    run_id: str | None = None


@dataclass
class ConversationState:
    """The running conversation held in ``cl.user_session``.

    Persisted-and-restored on thread resume (the data layer stores the
    Chainlit messages; this is the playground's structured mirror used
    for client-managed re-send + feedback attachment).
    """

    turns: list[Turn] = field(default_factory=list)
    session_id: str | None = None
    """Server session id when :class:`SessionBackend` is active; ``None``
    for client-managed."""

    def add_user(self, text: str) -> None:
        self.turns.append(Turn(role=Role.USER, text=text))

    def add_assistant(self, text: str, run_id: str | None = None) -> None:
        self.turns.append(Turn(role=Role.ASSISTANT, text=text, run_id=run_id))

    def last_run_id(self) -> str | None:
        for turn in reversed(self.turns):
            if turn.role is Role.ASSISTANT and turn.run_id:
                return turn.run_id
        return None


def assemble_conversation_context(
    turns: list[Turn],
    *,
    documents: list[UploadedDocument] | None = None,
    max_doc_chars: int = _MAX_DOC_CHARS_PER_REQUEST,
) -> dict[str, Any]:
    """Build the client-managed conversation context block.

    Returns a dict the :class:`ClientManagedBackend` merges into the run
    input so a stateless runtime still sees prior turns + uploaded docs.
    Shape::

        {
          "conversation": [{"role": "user", "content": "..."}, ...],
          "documents": [{"filename": "x.pdf", "content": "..."}],  # if any
        }

    Document text is truncated to ``max_doc_chars`` *in total* (oldest
    docs first) so a large upload can't blow the context window. Only
    *prior* turns belong here — the caller adds the new user message
    separately via :func:`build_run_input`.
    """
    context: dict[str, Any] = {
        CONVERSATION_KEY: [{"role": t.role.value, "content": t.text} for t in turns],
    }
    docs = documents or []
    if docs:
        budget = max_doc_chars
        packed: list[dict[str, str]] = []
        for doc in docs:
            if budget <= 0:
                break
            snippet = doc.text[:budget]
            budget -= len(snippet)
            packed.append({"filename": doc.filename, "content": snippet})
        if packed:
            context[DOCUMENTS_KEY] = packed
    return context


def build_run_input(
    *,
    user_message: str,
    base_input: dict[str, Any] | None,
    turns: list[Turn],
    documents: list[UploadedDocument] | None = None,
) -> dict[str, Any]:
    """Assemble the full run input for a **client-managed** turn.

    Merges, in precedence order:

    1. ``base_input`` — any structured fields the operator supplied
       (e.g. an agent whose schema needs more than free text). When the
       operator typed plain prose, this is ``{}``.
    2. the new ``user_message`` under :data:`MESSAGE_KEY` (so a simple
       ``{"message": "..."}`` agent works without hand-built JSON) —
       only when ``base_input`` didn't already set it.
    3. the conversation + document context block (prior turns only).

    The result is what :class:`ClientManagedBackend` posts to the
    stateless run endpoint.
    """
    run_input: dict[str, Any] = dict(base_input or {})
    if MESSAGE_KEY not in run_input and user_message:
        run_input[MESSAGE_KEY] = user_message
    context = assemble_conversation_context(turns, documents=documents)
    # Don't overwrite an explicit operator-supplied conversation/documents.
    for key, value in context.items():
        run_input.setdefault(key, value)
    return run_input


@runtime_checkable
class _RunClient(Protocol):
    """Narrow client surface the backends need.

    Duck-typed so :class:`~movate.playground.client.PlaygroundClient`
    satisfies it without an explicit base class, and tests can inject a
    minimal fake. Methods mirror the client's existing async API.
    """

    async def submit_run(self, *, agent: str, input_data: dict[str, Any]) -> dict[str, Any]: ...
    async def wait_for_run(self, job_id: str) -> dict[str, Any]: ...


@runtime_checkable
class _SessionClient(_RunClient, Protocol):
    """Adds the (future) Sessions API surface to :class:`_RunClient`."""

    async def create_session(self, *, agent: str) -> dict[str, Any]: ...
    async def submit_session_message(
        self, *, session_id: str, input_data: dict[str, Any]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TurnResult:
    """Outcome of one conversation turn, backend-agnostic."""

    run_id: str | None
    status: str
    output: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def output_text(self) -> str:
        """Best-effort human-readable text from the run output."""
        return extract_output_text(self.output)


class ConversationBackend(Protocol):
    """How a turn is sent + memory is carried. See module docstring.

    The Chainlit app holds one instance per chat session (selected by
    :func:`select_backend`) and calls :meth:`send_turn` for each user
    message, identically regardless of which impl is active.
    """

    name: str

    async def send_turn(
        self,
        *,
        agent: str,
        user_message: str,
        base_input: dict[str, Any] | None,
        state: ConversationState,
        documents: list[UploadedDocument] | None = None,
    ) -> TurnResult: ...


@dataclass
class ClientManagedBackend:
    """Stateless runtime → playground re-sends prior turns as context.

    Today's common case. Each turn POSTs the new message *plus* the
    running transcript + uploaded-doc context (:func:`build_run_input`)
    to the ordinary run endpoint, then polls to completion. Memory lives
    in :class:`ConversationState`.
    """

    client: _RunClient
    name: str = "client-managed"

    async def send_turn(
        self,
        *,
        agent: str,
        user_message: str,
        base_input: dict[str, Any] | None,
        state: ConversationState,
        documents: list[UploadedDocument] | None = None,
    ) -> TurnResult:
        run_input = build_run_input(
            user_message=user_message,
            base_input=base_input,
            # Prior turns only — the new message is added separately above.
            turns=state.turns,
            documents=documents,
        )
        submission = await self.client.submit_run(agent=agent, input_data=run_input)
        job_id = submission.get("job_id")
        if not job_id:
            return TurnResult(run_id=None, status="error", output={"error": submission})
        run = await self.client.wait_for_run(job_id)
        return _turn_result_from_run(run)


@dataclass
class SessionBackend:
    """Server-managed memory via the runtime's Sessions API (ADR 045 D10).

    Opens a session lazily on the first turn (stored on
    :class:`ConversationState`), then posts only the *new* message to
    ``/sessions/{id}/messages`` — the runtime threads prior turns into
    the model context, so the playground does NOT re-send the transcript.
    Selected automatically once the runtime advertises ``sessions``.
    """

    client: _SessionClient
    name: str = "server-sessions"

    async def send_turn(
        self,
        *,
        agent: str,
        user_message: str,
        base_input: dict[str, Any] | None,
        state: ConversationState,
        documents: list[UploadedDocument] | None = None,
    ) -> TurnResult:
        if state.session_id is None:
            session = await self.client.create_session(agent=agent)
            state.session_id = session.get("session_id") or session.get("id")
            if not state.session_id:
                return TurnResult(run_id=None, status="error", output={"error": session})
        # Server owns conversation memory — send only the new message
        # (+ structured base_input + any fresh document context). We do
        # NOT include prior turns; the runtime threads them.
        run_input: dict[str, Any] = dict(base_input or {})
        run_input.setdefault(MESSAGE_KEY, user_message)
        docs = documents or []
        if docs:
            context = assemble_conversation_context([], documents=docs)
            if DOCUMENTS_KEY in context:
                run_input.setdefault(DOCUMENTS_KEY, context[DOCUMENTS_KEY])
        submission = await self.client.submit_session_message(
            session_id=state.session_id, input_data=run_input
        )
        job_id = submission.get("job_id")
        if not job_id:
            # Some session impls may return the run inline (no polling).
            if submission.get("run_id") or submission.get("output") is not None:
                return _turn_result_from_run(submission)
            return TurnResult(run_id=None, status="error", output={"error": submission})
        run = await self.client.wait_for_run(job_id)
        return _turn_result_from_run(run)


def select_backend(
    caps: RuntimeCapabilities,
    client: _RunClient,
) -> ConversationBackend:
    """Pick the conversation backend from the runtime's capabilities.

    The ONE place the sessions-vs-stateless decision is made:

    * ``caps.sessions`` true → :class:`SessionBackend` (server memory).
    * else → :class:`ClientManagedBackend` (re-send transcript).

    When ADR 045 D10 (Sessions API) ships and the runtime starts
    advertising ``sessions``, this flips automatically — no playground
    change. ``client`` must satisfy :class:`_SessionClient` for the
    session path; the :class:`~movate.playground.client.PlaygroundClient`
    does once its session methods land, and already satisfies the
    client-managed path today.
    """
    if caps.sessions:
        return SessionBackend(client=client)  # type: ignore[arg-type]
    return ClientManagedBackend(client=client)


# ---------------------------------------------------------------------------
# Feedback routing
# ---------------------------------------------------------------------------


class FeedbackRoute(StrEnum):
    """Where a 👍/👎 should be persisted."""

    FEEDBACK_API = "feedback_api"
    """First-class ``POST /runs/{id}/feedback`` (ADR 045 D14)."""

    LEGACY = "legacy"
    """The runtime's existing feedback persistence path (today)."""


def feedback_route(caps: RuntimeCapabilities) -> FeedbackRoute:
    """Decide where feedback goes, given the runtime's capabilities.

    Routes to :attr:`FeedbackRoute.FEEDBACK_API` when the runtime
    advertises the feedback API, else falls back to the existing
    persistence path — never regressing today's behavior.

    (Today both routes hit ``POST /runs/{id}/feedback`` on the client
    side; the distinction is forward-looking so when the runtime gates
    the endpoint behind the capability flag the playground still does
    the right thing without guessing.)
    """
    return FeedbackRoute.FEEDBACK_API if caps.feedback_api else FeedbackRoute.LEGACY


# ---------------------------------------------------------------------------
# Run-output text extraction (shared by both backends)
# ---------------------------------------------------------------------------

# Output keys, in priority order, that commonly hold the human-readable
# assistant text across agent shapes. First non-empty string wins.
_OUTPUT_TEXT_KEYS: tuple[str, ...] = (
    "message",
    "response",
    "text",
    "answer",
    "content",
    "output",
    "result",
    "reply",
)


def extract_output_text(output: dict[str, Any] | None) -> str:
    """Best-effort: pull human-readable text from a run's output dict.

    Agents vary in output shape (``{"answer": ...}``,
    ``{"message": ...}``, free JSON). We try the common text keys in
    priority order; if none match, fall back to a compact JSON dump so
    the operator always sees *something*. Pure + total — never raises.
    """
    if not output:
        return ""
    for key in _OUTPUT_TEXT_KEYS:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value
    # No conventional text field — show the raw structure compactly.
    import json  # noqa: PLC0415 - keep import cost off the hot import path

    try:
        return json.dumps(output, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


def _turn_result_from_run(run: dict[str, Any]) -> TurnResult:
    """Normalise a run/job record (either backend) into a TurnResult."""
    output = run.get("output") or run.get("data") or {}
    if not isinstance(output, dict):
        output = {"output": output}
    return TurnResult(
        run_id=run.get("run_id") or run.get("job_id"),
        status=run.get("status", "unknown"),
        output=output,
        metrics=run.get("metrics") or {},
    )
