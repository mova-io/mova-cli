"""Pure-logic tests for the playground enhancement — NO Chainlit import.

Covers the capability-aware decision logic that the Chainlit app binds to
the UI, but which is unit-testable in isolation:

* capability parsing + the SessionBackend-vs-ClientManagedBackend selection,
* conversation-context assembly (client-managed re-send),
* the upload→context adapter (reusing the shared KB parser),
* feedback routing,
* SSE frame parsing,
* data-layer path resolution.

These modules import WITHOUT ``chainlit`` installed — that's the whole
point: the verify gate runs green on a no-extras install. We assert that
invariant explicitly so a future import leak (someone adds
``import chainlit`` to a pure module) fails loudly here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from movate.playground.capabilities import (
    DEFAULT_MAX_UPLOAD_COUNT,
    DEFAULT_MAX_UPLOAD_MB,
    RuntimeCapabilities,
    default_capabilities,
    parse_capabilities,
)
from movate.playground.conversation import (
    ClientManagedBackend,
    ConversationState,
    FeedbackRoute,
    Role,
    SessionBackend,
    assemble_conversation_context,
    build_run_input,
    extract_output_text,
    feedback_route,
    select_backend,
)
from movate.playground.sse import StreamEvent, parse_sse_lines
from movate.playground.state import DataLayerConfig, resolve_data_layer_config, threads_db_path
from movate.playground.uploads import (
    UploadedDocument,
    UploadOutcome,
    UploadStore,
    adapt_upload,
    is_image,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Import hygiene — the pure modules must NOT require chainlit.
# ---------------------------------------------------------------------------


def test_pure_modules_import_without_chainlit() -> None:
    """The capability/conversation/upload/sse/state modules import clean
    on a no-extras install (no transitive chainlit import)."""
    # If any of these had ``import chainlit`` at module scope, the imports
    # at the top of THIS file would already have failed when chainlit is
    # absent. Assert the modules are loaded as a belt-and-suspenders check.
    for mod in (
        "movate.playground.capabilities",
        "movate.playground.conversation",
        "movate.playground.uploads",
        "movate.playground.sse",
        "movate.playground.state",
    ):
        assert importlib.util.find_spec(mod) is not None


# ---------------------------------------------------------------------------
# Capability parsing
# ---------------------------------------------------------------------------


def test_default_capabilities_is_all_off() -> None:
    caps = default_capabilities()
    assert caps.sessions is False
    assert caps.run_streaming is False
    assert caps.feedback_api is False
    assert caps.max_upload_mb == DEFAULT_MAX_UPLOAD_MB
    assert caps.max_upload_count == DEFAULT_MAX_UPLOAD_COUNT
    assert caps.raw is None


def test_parse_none_payload_degrades_to_default() -> None:
    """A missing capabilities endpoint (None) → today's behavior."""
    assert parse_capabilities(None) == default_capabilities()


def test_parse_malformed_payload_never_raises() -> None:
    # A dict payload with garbage `features` parses to all-off flags +
    # default limits (only `raw` carries the original payload).
    caps = parse_capabilities({"features": "not-a-dict-or-list"})
    assert (caps.sessions, caps.run_streaming, caps.feedback_api) == (False, False, False)
    assert caps.max_upload_mb == DEFAULT_MAX_UPLOAD_MB
    # A non-dict top-level payload degrades fully to the default.
    assert parse_capabilities([]) == default_capabilities()  # type: ignore[arg-type]


def test_parse_features_dict() -> None:
    caps = parse_capabilities(
        {
            "features": {"sessions": True, "run_streaming": True, "feedback_api": False},
            "limits": {"max_kb_upload_mb": 50, "max_kb_upload_count": 25},
        }
    )
    assert caps.sessions is True
    assert caps.run_streaming is True
    assert caps.feedback_api is False
    assert caps.max_upload_mb == 50
    assert caps.max_upload_count == 25


def test_parse_features_list_of_slugs() -> None:
    """Capabilities may advertise enabled features as a slug list."""
    caps = parse_capabilities({"features": ["sessions", "streaming"]})
    assert caps.sessions is True
    assert caps.run_streaming is True  # "streaming" alias
    assert caps.feedback_api is False


def test_parse_feature_aliases() -> None:
    assert parse_capabilities({"features": ["stateful_sessions"]}).sessions is True
    assert parse_capabilities({"features": ["sse"]}).run_streaming is True
    assert parse_capabilities({"features": ["feedback"]}).feedback_api is True


def test_parse_string_truthiness() -> None:
    caps = parse_capabilities({"features": {"sessions": "true", "run_streaming": "0"}})
    assert caps.sessions is True
    assert caps.run_streaming is False


def test_parse_nonpositive_limit_falls_back_to_default() -> None:
    """A zero/negative advertised limit is treated as unset, not 'no uploads'."""
    caps = parse_capabilities({"limits": {"max_kb_upload_mb": 0, "max_kb_upload_count": -1}})
    assert caps.max_upload_mb == DEFAULT_MAX_UPLOAD_MB
    assert caps.max_upload_count == DEFAULT_MAX_UPLOAD_COUNT


def test_parse_bool_limit_is_ignored() -> None:
    """bool is an int subclass — must NOT be read as a numeric limit."""
    caps = parse_capabilities({"limits": {"max_kb_upload_mb": True}})
    assert caps.max_upload_mb == DEFAULT_MAX_UPLOAD_MB


# ---------------------------------------------------------------------------
# Backend selection — the core capability gate
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal client satisfying both _RunClient and _SessionClient."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_run: dict = {
            "run_id": "r1",
            "status": "success",
            "output": {"message": "hi"},
            "metrics": {"cost_usd": 0.01},
        }

    async def submit_run(self, *, agent: str, input_data: dict) -> dict:
        self.calls.append(("submit_run", {"agent": agent, "input": input_data}))
        return {"job_id": "j1", "status": "queued"}

    async def wait_for_run(self, job_id: str) -> dict:
        self.calls.append(("wait_for_run", {"job_id": job_id}))
        return self.next_run

    async def create_session(self, *, agent: str) -> dict:
        self.calls.append(("create_session", {"agent": agent}))
        return {"session_id": "s1"}

    async def submit_session_message(self, *, session_id: str, input_data: dict) -> dict:
        self.calls.append(
            ("submit_session_message", {"session_id": session_id, "input": input_data})
        )
        return {"job_id": "j2", "status": "queued"}


def test_select_backend_client_managed_when_no_sessions() -> None:
    backend = select_backend(default_capabilities(), _FakeClient())
    assert isinstance(backend, ClientManagedBackend)
    assert backend.name == "client-managed"


def test_select_backend_sessions_when_advertised() -> None:
    caps = RuntimeCapabilities(sessions=True)
    backend = select_backend(caps, _FakeClient())
    assert isinstance(backend, SessionBackend)
    assert backend.name == "server-sessions"


async def test_client_managed_backend_resends_transcript() -> None:
    """Client-managed re-sends prior turns + docs in the run input."""
    client = _FakeClient()
    backend = ClientManagedBackend(client=client)
    state = ConversationState()
    state.add_user("first question")
    state.add_assistant("first answer", run_id="r0")
    state.add_user("second question")  # the current turn

    result = await backend.send_turn(
        agent="bot",
        user_message="second question",
        base_input=None,
        state=state,
        documents=None,
    )

    assert result.run_id == "r1"
    assert result.status == "success"
    assert result.output_text == "hi"
    # The submitted input carried the running transcript.
    submit = next(c for name, c in client.calls if name == "submit_run")
    convo = submit["input"]["conversation"]
    assert [t["role"] for t in convo] == ["user", "assistant", "user"]
    assert submit["input"]["message"] == "second question"


async def test_session_backend_opens_session_then_sends_only_new_message() -> None:
    """Server-managed sends only the new message — NOT the transcript."""
    client = _FakeClient()
    backend = SessionBackend(client=client)
    state = ConversationState()
    state.add_user("prior")
    state.add_assistant("prior reply", run_id="r0")

    await backend.send_turn(
        agent="bot",
        user_message="new turn",
        base_input=None,
        state=state,
        documents=None,
    )

    assert state.session_id == "s1"
    names = [name for name, _ in client.calls]
    assert "create_session" in names
    submit = next(c for name, c in client.calls if name == "submit_session_message")
    # Server owns memory — no transcript re-sent.
    assert "conversation" not in submit["input"]
    assert submit["input"]["message"] == "new turn"


async def test_session_backend_reuses_existing_session() -> None:
    client = _FakeClient()
    backend = SessionBackend(client=client)
    state = ConversationState(session_id="existing")
    await backend.send_turn(
        agent="bot", user_message="hi", base_input=None, state=state, documents=None
    )
    assert state.session_id == "existing"
    assert "create_session" not in [name for name, _ in client.calls]


# ---------------------------------------------------------------------------
# Conversation-context assembly
# ---------------------------------------------------------------------------


def test_assemble_context_turns_only() -> None:
    state = ConversationState()
    state.add_user("u1")
    state.add_assistant("a1")
    ctx = assemble_conversation_context(state.turns)
    assert ctx["conversation"] == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    assert "documents" not in ctx


def test_assemble_context_includes_documents() -> None:
    doc = UploadedDocument(filename="x.pdf", outcome=UploadOutcome.EXTRACTED, text="doc text")
    ctx = assemble_conversation_context([], documents=[doc])
    assert ctx["documents"] == [{"filename": "x.pdf", "content": "doc text"}]


def test_assemble_context_truncates_documents_to_budget() -> None:
    big = UploadedDocument(filename="big.txt", outcome=UploadOutcome.EXTRACTED, text="z" * 100)
    ctx = assemble_conversation_context([], documents=[big], max_doc_chars=10)
    assert len(ctx["documents"][0]["content"]) == 10


def test_build_run_input_merges_message_and_context() -> None:
    state = ConversationState()
    state.add_user("earlier")
    run_input = build_run_input(
        user_message="hello",
        base_input=None,
        turns=state.turns,
        documents=None,
    )
    assert run_input["message"] == "hello"
    assert run_input["conversation"] == [{"role": "user", "content": "earlier"}]


def test_build_run_input_respects_explicit_base_input() -> None:
    """Operator-supplied structured fields win over the auto message key."""
    run_input = build_run_input(
        user_message="ignored",
        base_input={"message": "explicit", "question": "q"},
        turns=[],
        documents=None,
    )
    assert run_input["message"] == "explicit"
    assert run_input["question"] == "q"


def test_role_enum_serializes_to_str() -> None:
    assert Role.USER == "user"
    assert Role.ASSISTANT == "assistant"


# ---------------------------------------------------------------------------
# Feedback routing
# ---------------------------------------------------------------------------


def test_feedback_route_legacy_by_default() -> None:
    assert feedback_route(default_capabilities()) is FeedbackRoute.LEGACY


def test_feedback_route_api_when_advertised() -> None:
    assert feedback_route(RuntimeCapabilities(feedback_api=True)) is FeedbackRoute.FEEDBACK_API


# ---------------------------------------------------------------------------
# Output text extraction
# ---------------------------------------------------------------------------


def test_extract_output_text_prefers_known_keys() -> None:
    assert extract_output_text({"answer": "the answer"}) == "the answer"
    assert extract_output_text({"message": "msg", "answer": "ans"}) == "msg"


def test_extract_output_text_falls_back_to_json() -> None:
    out = extract_output_text({"unknown_field": [1, 2, 3]})
    assert "unknown_field" in out


def test_extract_output_text_empty() -> None:
    assert extract_output_text(None) == ""
    assert extract_output_text({}) == ""


# ---------------------------------------------------------------------------
# Upload → context adapter (reuses the shared KB parser)
# ---------------------------------------------------------------------------


def test_adapt_upload_extracts_text_via_shared_parser() -> None:
    doc = adapt_upload("notes.md", b"# Title\n\nBody text.\n", max_size_mb=20)
    assert doc.outcome == UploadOutcome.EXTRACTED
    assert "Body text." in doc.text


def test_adapt_upload_image_is_deferred() -> None:
    doc = adapt_upload("photo.png", b"\x89PNG\r\n", max_size_mb=20)
    assert doc.outcome == UploadOutcome.IMAGE_DEFERRED
    assert doc.text == ""
    assert is_image("photo.PNG")


def test_adapt_upload_unsupported_extension() -> None:
    doc = adapt_upload("data.xyz", b"whatever", max_size_mb=20)
    assert doc.outcome == UploadOutcome.UNSUPPORTED


def test_adapt_upload_too_large() -> None:
    doc = adapt_upload("big.txt", b"x" * (2 * 1024 * 1024), max_size_mb=1)
    assert doc.outcome == UploadOutcome.TOO_LARGE
    assert doc.text == ""


def test_adapt_upload_empty_text() -> None:
    doc = adapt_upload("blank.txt", b"   \n  ", max_size_mb=20)
    assert doc.outcome == UploadOutcome.EMPTY


def test_upload_store_only_extracted_contribute_context() -> None:
    store = UploadStore()
    store.add(adapt_upload("a.md", b"real text", max_size_mb=20))
    store.add(adapt_upload("b.png", b"\x89PNG", max_size_mb=20))  # image, deferred
    store.add(adapt_upload("c.xyz", b"nope", max_size_mb=20))  # unsupported
    ctx_docs = store.context_documents()
    assert [d.filename for d in ctx_docs] == ["a.md"]
    assert store.has_context() is True


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


def test_parse_sse_token_done_frames() -> None:
    lines = [
        "event: token",
        'data: {"text": "Hel"}',
        "",
        "event: token",
        'data: {"text": "lo"}',
        "",
        "event: done",
        'data: {"run_id": "r9", "status": "success", "output": {"message": "Hello"}}',
        "",
    ]
    events = parse_sse_lines(lines)
    tokens = [e.text for e in events if e.is_token]
    assert "".join(tokens) == "Hello"
    done = next(e for e in events if e.is_done)
    assert done.data["run_id"] == "r9"


def test_parse_sse_error_frame() -> None:
    events = parse_sse_lines(["event: error", 'data: {"message": "boom", "code": "X"}', ""])
    assert events[0].is_error
    assert events[0].data["message"] == "boom"


def test_parse_sse_ignores_comments_and_keepalives() -> None:
    events = parse_sse_lines([": keep-alive", "event: token", 'data: {"text": "a"}', ""])
    assert len(events) == 1
    assert events[0].text == "a"


def test_parse_sse_trailing_frame_without_blank_line() -> None:
    events = parse_sse_lines(["event: done", 'data: {"status": "success"}'])
    assert len(events) == 1
    assert events[0].is_done


def test_parse_sse_non_json_data_kept_raw() -> None:
    events = parse_sse_lines(["event: token", "data: plain text", ""])
    assert events[0].data == {}
    assert events[0].raw_data == "plain text"


def test_stream_event_text_only_for_string() -> None:
    assert StreamEvent(event="token", data={"text": 5}).text == ""


# ---------------------------------------------------------------------------
# Data-layer path resolution
# ---------------------------------------------------------------------------


def test_threads_db_under_home_mdk_playground(tmp_path: Path) -> None:
    path = threads_db_path(home=tmp_path)
    assert path == tmp_path / ".mdk" / "playground" / "threads.db"


def test_resolve_data_layer_disabled() -> None:
    cfg = resolve_data_layer_config(enabled=False)
    assert cfg == DataLayerConfig(enabled=False)
    assert cfg.backend == "disabled"


def test_resolve_data_layer_sqlite_default(tmp_path: Path) -> None:
    cfg = resolve_data_layer_config(enabled=True, home=tmp_path, env={})
    assert cfg.backend == "sqlite"
    assert cfg.sqlite_path == tmp_path / ".mdk" / "playground" / "threads.db"
    assert cfg.postgres_url is None


def test_resolve_data_layer_postgres_when_url_passed(tmp_path: Path) -> None:
    cfg = resolve_data_layer_config(
        enabled=True, postgres_url="postgresql://x/y", home=tmp_path, env={}
    )
    assert cfg.backend == "postgres"
    assert cfg.postgres_url == "postgresql://x/y"
    assert cfg.sqlite_path is None


def test_resolve_data_layer_postgres_from_env(tmp_path: Path) -> None:
    cfg = resolve_data_layer_config(
        enabled=True, home=tmp_path, env={"MDK_PLAYGROUND_THREADS_URL": "postgresql://e/v"}
    )
    assert cfg.backend == "postgres"
    assert cfg.postgres_url == "postgresql://e/v"
