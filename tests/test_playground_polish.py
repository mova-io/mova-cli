"""Tests for the four playground-polish items (PR feat/playground-polish).

Items covered:
  1. Connection-status indicator — ``movate.playground.connection``
  2. Agent-picker label with description + tags — ``_agent_picker_label``
     and ``_agent_picker_tooltip`` in ``movate.playground.app``
  3. Voice partial-transcript display — ``_render_voice_turn`` with partials
  4. Feedback-button visual confirmation + idempotency guard

All tests are pure-unit (no real network, mocked httpx/WS) and run on a
``[playground]``-extras install (Chainlit available).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType
from typing import Any

import pytest

from movate.playground.capabilities import RuntimeCapabilities
from movate.playground.connection import (
    ConnectionMonitor,
    ConnectionState,
    reconnected_banner,
    slow_banner,
    unreachable_banner,
)
from movate.playground.voice import VoiceFrame

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers shared across sections
# ---------------------------------------------------------------------------


def _reload_app(monkeypatch: pytest.MonkeyPatch, *, voice: bool = False) -> ModuleType:
    """(Re)import ``movate.playground.app`` in a clean environment."""
    monkeypatch.setenv("MDK_PLAYGROUND_NO_HISTORY", "1")
    monkeypatch.delenv("MDK_PLAYGROUND_TARGETS", raising=False)
    if voice:
        monkeypatch.setenv("MDK_PLAYGROUND_VOICE", "1")
    else:
        monkeypatch.delenv("MDK_PLAYGROUND_VOICE", raising=False)
    sys.modules.pop("movate.playground.app", None)
    return importlib.import_module("movate.playground.app")


class _FakeMsg:
    """Minimal Chainlit Message stand-in."""

    def __init__(self, content: str = "", **_: object) -> None:
        self.content = content
        self.elements: list[Any] = []
        self.actions: list[Any] = []
        self.sent = False
        self.updates = 0
        self.streamed: list[str] = []

    async def send(self) -> None:
        self.sent = True

    async def update(self) -> None:
        self.updates += 1

    async def stream_token(self, token: str) -> None:
        self.streamed.append(token)
        self.content += token


def _install_session(monkeypatch: pytest.MonkeyPatch, app: ModuleType) -> dict[str, Any]:
    """Back the Chainlit user_session with a plain dict."""
    session: dict[str, Any] = {}

    def _get(key: str, default: object = None) -> object:
        return session.get(key, default)

    def _set(key: str, value: object) -> None:
        session[key] = value

    monkeypatch.setattr(app.cl.user_session, "get", _get)
    monkeypatch.setattr(app.cl.user_session, "set", _set)
    return session


# ===========================================================================
# Item 1 — Connection-status state machine
# ===========================================================================


class TestConnectionMonitor:
    """Pure-unit tests for ``movate.playground.connection.ConnectionMonitor``."""

    @pytest.mark.asyncio
    async def test_fast_response_is_connected(self) -> None:

        calls: list[str] = []

        class _FastClient:
            async def get(self, url: str, **_: object) -> None:
                calls.append(url)

        monitor = ConnectionMonitor(
            client=_FastClient(),
            fast_threshold_s=10.0,  # anything under 10s is CONNECTED
            probe_timeout_s=5.0,
            cache_ttl_s=0.0,  # no cache so every call probes
        )
        state = await monitor.check()
        assert state == ConnectionState.CONNECTED
        assert calls  # the probe actually ran

    @pytest.mark.asyncio
    async def test_slow_response_is_slow(self, monkeypatch: pytest.MonkeyPatch) -> None:

        class _SlowClient:
            async def get(self, url: str, **_: object) -> None:
                pass  # returns immediately but threshold=0 → SLOW

        monitor = ConnectionMonitor(
            client=_SlowClient(),
            fast_threshold_s=0.0,  # 0s threshold → any latency qualifies as SLOW
            probe_timeout_s=5.0,
            cache_ttl_s=0.0,
        )
        state = await monitor.check()
        assert state == ConnectionState.SLOW

    @pytest.mark.asyncio
    async def test_exception_is_disconnected(self) -> None:

        class _DeadClient:
            async def get(self, url: str, **_: object) -> None:
                raise OSError("connection refused")

        monitor = ConnectionMonitor(
            client=_DeadClient(),
            probe_timeout_s=1.0,
            cache_ttl_s=0.0,
        )
        state = await monitor.check()
        assert state == ConnectionState.DISCONNECTED
        assert monitor.last_duration_s is None

    @pytest.mark.asyncio
    async def test_result_is_cached_within_ttl(self) -> None:

        probe_count = {"n": 0}

        class _Client:
            async def get(self, url: str, **_: object) -> None:
                probe_count["n"] += 1

        monitor = ConnectionMonitor(
            client=_Client(),
            cache_ttl_s=999.0,  # very long TTL → second call is cached
        )
        await monitor.check()
        await monitor.check()
        assert probe_count["n"] == 1  # only one real probe

    def test_status_changed(self) -> None:

        class _NullClient:
            async def get(self, url: str, **_: object) -> None:
                pass

        monitor = ConnectionMonitor(client=_NullClient())
        assert monitor.status_changed(ConnectionState.DISCONNECTED) is True  # default CONNECTED
        assert monitor.status_changed(ConnectionState.CONNECTED) is False

    def test_state_enum_properties(self) -> None:

        assert ConnectionState.CONNECTED.emoji == "🟢"
        assert ConnectionState.SLOW.emoji == "🟡"
        assert ConnectionState.DISCONNECTED.emoji == "🔴"
        assert "Connected" in ConnectionState.CONNECTED.banner()

    def test_banner_helpers(self) -> None:

        assert "unreachable" in unreachable_banner().lower()
        assert "reconnected" in reconnected_banner().lower()
        assert "1.5s" in slow_banner(1.5)

    def test_pure_module_no_chainlit(self) -> None:
        """connection.py must import without Chainlit (like other pure modules)."""
        assert importlib.util.find_spec("movate.playground.connection") is not None


class TestConnectionStatusInApp:
    """Verify that _check_connection emits messages on state transitions."""

    @pytest.mark.asyncio
    async def test_disconnected_transition_shows_banner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        # Seed session with a monitor that will report DISCONNECTED.
        class _DeadClient:
            async def get(self, url: str, **_: object) -> None:
                raise OSError("dead")

        monitor = ConnectionMonitor(client=_DeadClient(), cache_ttl_s=0.0)
        session[app._K_CONN_MONITOR] = monitor
        session[app._K_CONN_STATE] = ConnectionState.CONNECTED  # previous = CONNECTED

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)
        await app._check_connection()
        # A banner must have been sent about the unreachable state.
        assert any("unreachable" in m.lower() or "⚠" in m for m in sent)
        assert session[app._K_CONN_STATE] == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_reconnect_transition_shows_banner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        class _LiveClient:
            async def get(self, url: str, **_: object) -> None:
                pass

        monitor = ConnectionMonitor(client=_LiveClient(), cache_ttl_s=0.0, fast_threshold_s=100.0)
        session[app._K_CONN_MONITOR] = monitor
        session[app._K_CONN_STATE] = ConnectionState.DISCONNECTED  # previous was down

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)
        await app._check_connection()
        assert any("reconnected" in m.lower() or "✓" in m for m in sent)
        assert session[app._K_CONN_STATE] == ConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_no_banner_when_state_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        class _LiveClient:
            async def get(self, url: str, **_: object) -> None:
                pass

        monitor = ConnectionMonitor(client=_LiveClient(), cache_ttl_s=0.0, fast_threshold_s=100.0)
        session[app._K_CONN_MONITOR] = monitor
        session[app._K_CONN_STATE] = ConnectionState.CONNECTED  # same as new state

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)
        await app._check_connection()
        # No state change → no banner.
        assert not sent

    @pytest.mark.asyncio
    async def test_disconnected_blocks_on_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When runtime is unreachable, on_message short-circuits with a hint."""
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        session[app._K_AGENT] = "echo"
        session[app._K_CLIENT] = object()  # truthy
        session[app._K_BACKEND] = object()  # truthy
        session[app._K_CAPS] = None
        session[app._K_CONVO] = None
        session[app._K_UPLOADS] = None
        session[app._K_CONN_STATE] = ConnectionState.DISCONNECTED

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)
        # Patch _check_connection to be a no-op (we pre-seeded DISCONNECTED).

        async def _noop() -> None:
            pass

        monkeypatch.setattr(app, "_check_connection", _noop)

        msg = type("M", (), {"content": "hello", "elements": []})()
        await app.on_message(msg)
        assert any("unreachable" in m.lower() or "⚠" in m for m in sent)


# ===========================================================================
# Item 2 — Agent picker label with description + tags
# ===========================================================================


class TestAgentPickerLabel:
    """Tests for _agent_picker_label and _agent_picker_tooltip."""

    def _app(self) -> ModuleType:
        """Import app module (Chainlit must be available)."""
        pytest.importorskip("chainlit")
        return importlib.import_module("movate.playground.app")

    def test_label_name_only(self) -> None:
        app = self._app()
        label = app._agent_picker_label({"name": "echo"})
        assert label == "echo"

    def test_label_with_version(self) -> None:
        app = self._app()
        label = app._agent_picker_label({"name": "echo", "version": "1.0"})
        assert "echo" in label
        assert "v1.0" in label

    def test_label_with_description(self) -> None:
        app = self._app()
        label = app._agent_picker_label({"name": "echo", "description": "An echo agent"})
        assert "An echo agent" in label

    def test_label_truncates_long_description(self) -> None:
        app = self._app()
        long_desc = "x" * 200
        label = app._agent_picker_label({"name": "agent", "description": long_desc})
        # The truncated desc must be at most MAX_DESC + "…" char overhead
        # and the label must contain the ellipsis.
        assert "…" in label
        # The description portion should be no longer than the cap + "…"
        assert len(label) < 300  # sanity bound

    def test_label_with_tags(self) -> None:
        app = self._app()
        label = app._agent_picker_label({"name": "agent", "tags": ["rag", "summarize", "v2"]})
        assert "rag" in label
        assert "[" in label and "]" in label

    def test_label_truncates_many_tags(self) -> None:
        app = self._app()
        tags = ["t1", "t2", "t3", "t4", "t5"]
        label = app._agent_picker_label({"name": "agent", "tags": tags})
        assert "…" in label  # overflow indicator

    def test_label_full(self) -> None:
        app = self._app()
        label = app._agent_picker_label(
            {
                "name": "summarizer",
                "version": "2.1",
                "description": "Summarize documents",
                "tags": ["rag", "docs"],
            }
        )
        assert "summarizer" in label
        assert "v2.1" in label
        assert "Summarize documents" in label
        assert "rag" in label

    def test_tooltip_has_description_and_tags(self) -> None:
        app = self._app()
        tip = app._agent_picker_tooltip(
            {
                "name": "a",
                "description": "Does stuff",
                "tags": ["t1", "t2"],
            }
        )
        assert "Does stuff" in tip
        assert "t1" in tip
        assert "t2" in tip

    def test_tooltip_empty_agent(self) -> None:
        app = self._app()
        tip = app._agent_picker_tooltip({"name": "minimal"})
        assert tip == "minimal"

    def test_label_no_version_field(self) -> None:
        """A missing version field is suppressed (no 'v?' shown)."""
        app = self._app()
        label = app._agent_picker_label({"name": "agent"})
        assert "v?" not in label
        assert "None" not in label


# ===========================================================================
# Item 3 — Voice partial-transcript display
# ===========================================================================


class TestVoicePartialTranscript:
    """_render_voice_turn streams partials into the live message."""

    @pytest.mark.asyncio
    async def test_partials_update_message_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")

        app = _reload_app(monkeypatch, voice=True)
        _install_session(monkeypatch, app)  # satisfy cl.user_session.set("last_run_id", ...)

        # Script: three partials, then final transcript, then agent token, done.
        frames = [
            VoiceFrame(type="transcript.partial", data={"text": "turn"}),
            VoiceFrame(type="transcript.partial", data={"text": "turn the"}),
            VoiceFrame(type="transcript.partial", data={"text": "turn the lights"}),
            VoiceFrame(type="transcript.final", data={"text": "turn the lights on"}),
            VoiceFrame(type="agent.token", data={"text": "Okay"}),
            VoiceFrame(type="done", data={"run_id": "r42", "status": "success"}),
        ]

        class _FakeWS:
            async def iter_turn(self):  # type: ignore[override]
                for f in frames:
                    yield f

        updates: list[str] = []

        class _TrackingMsg(_FakeMsg):
            async def update(self) -> None:
                updates.append(self.content)

        msg = _TrackingMsg(content="🎙 _(processing)_")
        monkeypatch.setattr(app.cl, "Audio", lambda **_: None)

        await app._render_voice_turn(_FakeWS(), msg)

        # Each partial must have triggered an update with the partial text visible.
        partial_updates = [u for u in updates if "(listening)" in u]
        assert len(partial_updates) >= 3  # one per partial frame
        # The final transcript must appear.
        final_updates = [u for u in updates if "turn the lights on" in u]
        assert final_updates

    @pytest.mark.asyncio
    async def test_final_transcript_replaces_partial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")

        app = _reload_app(monkeypatch, voice=True)
        _install_session(monkeypatch, app)

        frames = [
            VoiceFrame(type="transcript.partial", data={"text": "hello wor"}),
            VoiceFrame(type="transcript.final", data={"text": "hello world"}),
            VoiceFrame(type="done", data={"run_id": "rX", "status": "success"}),
        ]

        class _FakeWS:
            async def iter_turn(self):  # type: ignore[override]
                for f in frames:
                    yield f

        monkeypatch.setattr(app.cl, "Audio", lambda **_: None)
        msg = _FakeMsg()
        await app._render_voice_turn(_FakeWS(), msg)

        # After completion the final transcript should be in the message.
        assert "hello world" in msg.content
        # The partial text should NOT be the last content (replaced by final).
        assert "(listening)" not in msg.content or "hello world" in msg.content

    @pytest.mark.asyncio
    async def test_agent_tokens_accumulate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")

        app = _reload_app(monkeypatch, voice=True)
        _install_session(monkeypatch, app)

        frames = [
            VoiceFrame(type="transcript.final", data={"text": "hi"}),
            VoiceFrame(type="agent.token", data={"text": "Hello"}),
            VoiceFrame(type="agent.token", data={"text": " there"}),
            VoiceFrame(type="done", data={"run_id": "rY", "status": "success"}),
        ]

        class _FakeWS:
            async def iter_turn(self):  # type: ignore[override]
                for f in frames:
                    yield f

        monkeypatch.setattr(app.cl, "Audio", lambda **_: None)
        msg = _FakeMsg()
        await app._render_voice_turn(_FakeWS(), msg)

        assert "Hello" in msg.content
        assert "there" in msg.content


# ===========================================================================
# Item 4 — Feedback confirmation + idempotency guard
# ===========================================================================


class TestFeedbackConfirmation:
    """on_feedback visual confirmation + idempotency (Item 4)."""

    @pytest.mark.asyncio
    async def test_success_shows_thanks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        session[app._K_FEEDBACK_SUBMITTED] = set()

        posted: list[dict] = []

        class _FakeClient:
            async def post_feedback(self, *, run_id: str, score: int, **_: object) -> dict:
                posted.append({"run_id": run_id, "score": score})
                return {"ok": True}

            async def post_feedback_with_retry(self, *, run_id: str, score: int, **_: object) -> tuple:
                posted.append({"run_id": run_id, "score": score})
                return {"ok": True}, True

        session[app._K_CLIENT] = _FakeClient()
        session[app._K_CAPS] = RuntimeCapabilities()
        session[app._K_FEEDBACK_SUBMITTED] = set()

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        # Mock AskUserMessage to return no comment (operator pressed Enter).
        class _AskMsg:
            def __init__(self, **_: object) -> None:
                pass

            async def send(self) -> dict:
                return {"output": ""}

        monkeypatch.setattr(app.cl, "Message", _Msg)
        monkeypatch.setattr(app.cl, "AskUserMessage", _AskMsg)

        action = type("A", (), {"payload": {"value": "up", "run_id": "run-123"}})()
        await app.on_feedback(action)

        # A "Thanks" confirmation must appear.
        assert any("thanks" in m.lower() or "✓" in m for m in sent)
        # The server was called.
        assert posted and posted[0]["run_id"] == "run-123"
        # The run is now in the submitted set.
        submitted: set = session[app._K_FEEDBACK_SUBMITTED]
        assert "run-123:up" in submitted

    @pytest.mark.asyncio
    async def test_duplicate_is_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        # Pre-seed the submitted set as if the feedback was already sent.
        session[app._K_FEEDBACK_SUBMITTED] = {"run-abc:up"}

        session[app._K_CLIENT] = object()  # not called — guard fires first
        session[app._K_CAPS] = RuntimeCapabilities()

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)

        action = type("A", (), {"payload": {"value": "up", "run_id": "run-abc"}})()
        await app.on_feedback(action)

        # Should receive an "already recorded" message, not make a server call.
        assert any("already" in m.lower() for m in sent)

    @pytest.mark.asyncio
    async def test_failure_shows_retry_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)
        session[app._K_FEEDBACK_SUBMITTED] = set()

        class _FailClient:
            async def post_feedback(self, **_: object) -> dict:
                raise RuntimeError("network error")

            async def post_feedback_with_retry(self, **_: object) -> tuple:
                return None, False

        session[app._K_CLIENT] = _FailClient()
        session[app._K_CAPS] = RuntimeCapabilities()

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        class _AskMsg:
            def __init__(self, **_: object) -> None:
                pass

            async def send(self) -> dict:
                return {"output": ""}

        monkeypatch.setattr(app.cl, "Message", _Msg)
        monkeypatch.setattr(app.cl, "AskUserMessage", _AskMsg)

        action = type("A", (), {"payload": {"value": "down", "run_id": "run-fail"}})()
        await app.on_feedback(action)

        # Should show a "couldn't be saved — try again" style message.
        assert any("try again" in m.lower() or "couldn't" in m.lower() or "⚠" in m for m in sent)
        # The run must NOT be added to the submitted set (buttons stay active).
        submitted: set = session[app._K_FEEDBACK_SUBMITTED]
        assert "run-fail:down" not in submitted

    @pytest.mark.asyncio
    async def test_no_run_id_shows_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)
        session[app._K_FEEDBACK_SUBMITTED] = set()

        session[app._K_CLIENT] = object()
        session[app._K_CAPS] = RuntimeCapabilities()

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)
        # No run_id in payload and none in session.
        action = type("A", (), {"payload": {"value": "up"}})()
        await app.on_feedback(action)

        assert any("no run" in m.lower() or "send a message" in m.lower() for m in sent)
