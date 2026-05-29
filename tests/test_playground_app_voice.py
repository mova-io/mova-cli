"""``movate.playground.app`` voice-mode wiring — Chainlit-gated.

Voice mode is opt-in (``mdk playground serve --voice`` → ``MDK_PLAYGROUND_VOICE=1``)
and additive. These exercise the Chainlit app's audio-callback wiring, which
binds the pure :mod:`movate.playground.voice` transport to Chainlit's
``@cl.on_audio_start`` / ``@cl.on_audio_chunk`` / ``@cl.on_audio_end``:

1. **Default OFF leaves the text path unchanged** — with the env unset, NONE of
   the audio callbacks are registered on the app module.
2. **Enabled registers them** — with ``MDK_PLAYGROUND_VOICE=1`` the three
   callbacks exist.
3. **on_audio_start wires mic→WS** — it builds a :class:`VoiceWSClient` from the
   session's runtime URL + bearer (the SAME the text path uses), opens it, and
   returns True so Chainlit streams chunks.
4. **on_audio_chunk forwards frames** to the WS client as binary frames.
5. **on_audio_end renders + plays back** — transcript + answer stream into one
   message and the TTS audio is attached as a ``cl.Audio`` element.
6. **Graceful voice-not-enabled** — a :class:`VoiceNotEnabledError` on connect
   shows a friendly message and aborts (returns False), never a stack trace.

``app.py`` imports chainlit at module scope and reads ``MDK_PLAYGROUND_VOICE``
at import, so we set the env then re-import the module fresh — mirroring how
the child ``chainlit run`` process boots.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

import pytest

pytest.importorskip("chainlit")

from movate.playground.voice import VoiceFrame, VoiceNotEnabledError

pytestmark = pytest.mark.unit


def _reload_app(monkeypatch: pytest.MonkeyPatch, *, voice: bool) -> ModuleType:
    """(Re)import ``movate.playground.app`` with voice mode on/off in the env."""
    monkeypatch.setenv("MDK_PLAYGROUND_NO_HISTORY", "1")
    monkeypatch.delenv("MDK_PLAYGROUND_TARGETS", raising=False)
    if voice:
        monkeypatch.setenv("MDK_PLAYGROUND_VOICE", "1")
    else:
        monkeypatch.delenv("MDK_PLAYGROUND_VOICE", raising=False)
    sys.modules.pop("movate.playground.app", None)
    return importlib.import_module("movate.playground.app")


class _FakeMsg:
    """Records content/elements/actions and the send/update lifecycle."""

    def __init__(self, content: str = "", **_: object) -> None:
        self.content = content
        self.elements: list[Any] = []
        self.actions: list[Any] = []
        self.sent = False
        self.updates = 0

    async def send(self) -> None:
        self.sent = True

    async def update(self) -> None:
        self.updates += 1


def _install_session(monkeypatch: pytest.MonkeyPatch, app: ModuleType) -> dict[str, Any]:
    """Back the Chainlit user_session with a plain dict for get/set."""
    session: dict[str, Any] = {}

    def _get(key: str, default: object = None) -> object:
        return session.get(key, default)

    def _set(key: str, value: object) -> None:
        session[key] = value

    monkeypatch.setattr(app.cl.user_session, "get", _get)
    monkeypatch.setattr(app.cl.user_session, "set", _set)
    return session


# ---------------------------------------------------------------------------
# Registration gating — default OFF must not touch the text path
# ---------------------------------------------------------------------------


def test_voice_callbacks_absent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (no --voice) → audio callbacks are NOT registered."""
    app = _reload_app(monkeypatch, voice=False)
    assert app._VOICE_ENABLED is False
    assert not hasattr(app, "on_audio_start")
    assert not hasattr(app, "on_audio_chunk")
    assert not hasattr(app, "on_audio_end")
    # The text-path handlers are still present and unchanged.
    assert hasattr(app, "on_message")
    assert hasattr(app, "start")


def test_voice_callbacks_registered_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """MDK_PLAYGROUND_VOICE=1 → the three audio callbacks are registered."""
    app = _reload_app(monkeypatch, voice=True)
    assert app._VOICE_ENABLED is True
    assert hasattr(app, "on_audio_start")
    assert hasattr(app, "on_audio_chunk")
    assert hasattr(app, "on_audio_end")


# ---------------------------------------------------------------------------
# on_audio_start — mic → WS wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_start_opens_ws_from_session_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_audio_start builds the WS from the session's URL+key and connects."""
    app = _reload_app(monkeypatch, voice=True)
    session = _install_session(monkeypatch, app)
    session[app._K_AGENT] = "echo"
    session[app._K_CLIENT] = app.PlaygroundClient(
        app.PlaygroundClientConfig(runtime_url="https://rt:8000", api_key="sek")
    )

    opened: dict[str, Any] = {}

    class _FakeWS:
        runtime_url = "https://rt:8000"

        async def connect(self) -> None:
            opened["connected"] = True

        async def send_config(self, **kw: object) -> None:
            opened["config"] = kw

    monkeypatch.setattr(app, "_voice_client_for_session", _FakeWS)
    monkeypatch.setattr(app.cl, "Message", _FakeMsg)

    proceed = await app.on_audio_start()
    assert proceed is True
    assert opened.get("connected") is True
    assert session.get(app._K_VOICE_CLIENT) is not None


@pytest.mark.asyncio
async def test_audio_start_no_agent_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No bound agent → friendly nudge, returns False (no recording)."""
    app = _reload_app(monkeypatch, voice=True)
    _install_session(monkeypatch, app)
    sent: list[str] = []

    class _Msg(_FakeMsg):
        async def send(self) -> None:
            sent.append(self.content)

    monkeypatch.setattr(app.cl, "Message", _Msg)
    proceed = await app.on_audio_start()
    assert proceed is False
    assert sent and "pick an agent" in sent[0].lower()


@pytest.mark.asyncio
async def test_audio_start_voice_not_enabled_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connect failure shows 'voice isn't enabled' and aborts, no trace."""
    app = _reload_app(monkeypatch, voice=True)
    _install_session(monkeypatch, app)
    sent: list[str] = []

    class _Msg(_FakeMsg):
        async def send(self) -> None:
            sent.append(self.content)

    class _FailWS:
        runtime_url = "http://old-runtime:8000"

        async def connect(self) -> None:
            raise VoiceNotEnabledError("route absent")

        async def send_config(self, **kw: object) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr(app, "_voice_client_for_session", _FailWS)
    monkeypatch.setattr(app.cl, "Message", _Msg)

    proceed = await app.on_audio_start()
    assert proceed is False
    assert sent
    msg = sent[0].lower()
    assert "voice isn't enabled" in msg
    assert "text" in msg  # tells the operator text still works


def test_voice_client_for_session_reuses_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The WS client carries the session client's URL + key (not a global)."""
    app = _reload_app(monkeypatch, voice=True)
    session = _install_session(monkeypatch, app)
    session[app._K_AGENT] = "echo"
    session[app._K_CLIENT] = app.PlaygroundClient(
        app.PlaygroundClientConfig(runtime_url="https://prod:9000", api_key="tok-9")
    )
    ws = app._voice_client_for_session()
    assert ws is not None
    assert ws.runtime_url == "https://prod:9000"
    assert ws.token == "tok-9"
    assert ws.agent == "echo"
    assert ws.url.startswith("wss://prod:9000/api/v1/agents/echo/voice?token=tok-9")


# ---------------------------------------------------------------------------
# on_audio_chunk — forward frames
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_chunk_forwards_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _reload_app(monkeypatch, voice=True)
    session = _install_session(monkeypatch, app)
    forwarded: list[bytes] = []

    class _WS:
        async def send_audio(self, b: bytes) -> None:
            forwarded.append(b)

    session[app._K_VOICE_CLIENT] = _WS()

    class _Chunk:
        data = b"\x10\x20"

    await app.on_audio_chunk(_Chunk())
    assert forwarded == [b"\x10\x20"]
    assert session.get(app._K_VOICE_CHUNKS) == 1


@pytest.mark.asyncio
async def test_audio_chunk_no_client_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk with no active WS (e.g. start was aborted) does nothing."""
    app = _reload_app(monkeypatch, voice=True)
    _install_session(monkeypatch, app)

    class _Chunk:
        data = b"x"

    await app.on_audio_chunk(_Chunk())  # no raise


# ---------------------------------------------------------------------------
# on_audio_end — run the turn, render transcript, play TTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_end_renders_and_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_audio_end runs the turn: transcript + answer rendered, audio attached."""
    app = _reload_app(monkeypatch, voice=True)
    session = _install_session(monkeypatch, app)
    session[app._K_VOICE_CHUNKS] = 3

    frames = [
        VoiceFrame(type="transcript.final", data={"text": "hello there"}),
        VoiceFrame(type="agent.token", data={"text": "Hi!"}),
        VoiceFrame(type="tts.audio", data={"type": "tts.audio", "codec": "pcm16"}, audio=b"WAVE"),
        VoiceFrame(type="done", data={"run_id": "run-7", "status": "success"}),
    ]

    class _WS:
        async def end_turn(self) -> None:
            pass

        async def iter_turn(self) -> Any:  # async generator (matches the real client)
            for f in frames:
                yield f

        async def aclose(self) -> None:
            pass

    session[app._K_VOICE_CLIENT] = _WS()

    audios: list[Any] = []

    class _Audio:
        def __init__(self, **kw: object) -> None:
            audios.append(kw)

    monkeypatch.setattr(app.cl, "Message", _FakeMsg)
    monkeypatch.setattr(app.cl, "Audio", _Audio)

    await app.on_audio_end()

    # An Audio element was attached with the synthesized bytes + auto-play.
    assert audios, "expected a cl.Audio playback element"
    assert audios[0]["content"] == b"WAVE"
    assert audios[0]["auto_play"] is True
    # The run id was recorded for feedback.
    assert session.get("last_run_id") == "run-7"


@pytest.mark.asyncio
async def test_audio_end_no_audio_captured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recording with zero chunks tells the user nothing was captured."""
    app = _reload_app(monkeypatch, voice=True)
    session = _install_session(monkeypatch, app)
    session[app._K_VOICE_CHUNKS] = 0
    closed = {"n": 0}

    class _WS:
        async def aclose(self) -> None:
            closed["n"] += 1

    session[app._K_VOICE_CLIENT] = _WS()
    sent: list[str] = []

    class _Msg(_FakeMsg):
        async def send(self) -> None:
            sent.append(self.content)

    monkeypatch.setattr(app.cl, "Message", _Msg)
    await app.on_audio_end()
    assert sent and "no audio captured" in sent[0].lower()
    assert closed["n"] == 1
