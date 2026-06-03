"""In-memory / fake voice adapters for tests.

Mirror the ``testing/doubles.py`` philosophy: these satisfy the ADR 048 D3
Protocols (:class:`movate.voice.base.SpeechToTextProvider` /
:class:`~movate.voice.base.TextToSpeechProvider`) closely enough to type-check
against them, and capture what they were fed in plain lists so a test can
assert directly (``assert stt.received == [...]``). No network, no SDK, no
audio libs — usable from a default install with no ``mdk[voice]`` extra.

These live in ``mdk_voice`` (not ``tests/``) so an agent author writing
voice tests for their own deployment can import them too, same as
an in-memory storage double.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from movate.voice.agent_turn import AgentTurnError, AgentTurnResult
from movate.voice.base import AudioChunk, AudioCodec, RealtimeChunk, TranscriptChunk


class FakeAgentTurn:
    """Scripted :class:`~movate.voice.agent_turn.AgentTurn` for pipeline tests.

    Replaces a real agent framework (the mdk ``Executor``, a Lyzr agent) in the
    voice pipeline so the package is testable with no framework present. It
    records every transcript it was asked to run in :attr:`prompts`, streams its
    configured ``answer`` through ``on_token`` (one token per whitespace word,
    so a test sees ``agent.token`` events), and returns an
    :class:`~movate.voice.agent_turn.AgentTurnResult`.

    * ``answer`` — the human-readable answer the pipeline speaks via TTS.
    * ``stream`` — when ``True`` (default) the answer is emitted word-by-word via
      ``on_token``; set ``False`` to model a non-streaming agent (e.g. Lyzr's
      buffered ``agent.run``) that returns the whole answer at once.
    * ``error`` — when set, the turn returns a failed result (no answer), which
      the pipeline surfaces as a ``stage="agent"`` error.
    * ``answer_in_result`` — when ``False``, the result's ``answer_text`` is left
      empty so the pipeline's fall-back-to-streamed-tokens path is exercised.
    """

    name = "fake_agent"
    version = "0.0.1"

    def __init__(
        self,
        answer: str = "spoken answer",
        *,
        stream: bool = True,
        error: AgentTurnError | None = None,
        answer_in_result: bool = True,
        run_id: str = "run-fake",
    ) -> None:
        self._answer = answer
        self._stream = stream
        self._error = error
        self._answer_in_result = answer_in_result
        self._run_id = run_id
        self.prompts: list[str] = []
        self.session_ids: list[str | None] = []
        self.languages: list[str | None] = []

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurnResult:
        self.prompts.append(text)
        self.session_ids.append(session_id)
        self.languages.append(language)
        if self._error is not None:
            return AgentTurnResult(run_id=self._run_id, status="error", error=self._error)
        if self._stream and on_token is not None and self._answer:
            words = self._answer.split(" ")
            for i, word in enumerate(words):
                on_token(word if i == 0 else " " + word)
        answer_text = self._answer if self._answer_in_result else ""
        return AgentTurnResult(answer_text=answer_text, run_id=self._run_id, status="ok")


class FakeSTT:
    """Scripted :class:`~movate.voice.base.SpeechToTextProvider`.

    Drains the inbound audio (recording every chunk's bytes in
    :attr:`received` for assertion) and yields a configured transcript. By
    default it returns one ``is_final=True`` chunk with :attr:`transcript`;
    pass ``partials=`` to also emit non-final partial chunks first, exercising
    the streaming-endpointing path.
    """

    name = "fake_stt"
    version = "0.0.1"

    def __init__(self, transcript: str = "hello", *, partials: list[str] | None = None) -> None:
        self.transcript = transcript
        self._partials = partials or []
        self.received: list[bytes] = []
        self.languages: list[str | None] = []
        self.api_keys: list[str | None] = []

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        self.languages.append(language)
        self.api_keys.append(api_key)
        async for chunk in audio:
            self.received.append(chunk.data)
        for partial in self._partials:
            yield TranscriptChunk(text=partial, is_final=False)
        yield TranscriptChunk(text=self.transcript, is_final=True, confidence=1.0)


class FakeTTS:
    """Scripted :class:`~movate.voice.base.TextToSpeechProvider`.

    Buffers the inbound text deltas (recording the joined utterance in
    :attr:`spoken` for assertion) and yields the utterance encoded as audio
    bytes — by default ``utf-8`` of the text wrapped in a single
    :class:`~movate.voice.base.AudioChunk`, so a test can decode the audio
    back to text and assert the agent's answer round-tripped through TTS.
    Set ``frames`` to split the bytes into that many chunks to exercise the
    transport's multi-frame audio-out path.
    """

    name = "fake_tts"
    version = "0.0.1"

    def __init__(self, *, frames: int = 1) -> None:
        self._frames = max(1, frames)
        self.spoken: list[str] = []
        self.voice_ids: list[str] = []
        self.api_keys: list[str | None] = []

    async def synthesize(
        self,
        text: AsyncIterator[str],
        *,
        voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        self.voice_ids.append(voice_id)
        self.api_keys.append(api_key)
        parts: list[str] = []
        async for delta in text:
            if delta:
                parts.append(delta)
        utterance = "".join(parts)
        self.spoken.append(utterance)
        if not utterance:
            return
        data = utterance.encode("utf-8")
        # Split into ``frames`` roughly-equal slices.
        step = max(1, len(data) // self._frames)
        for start in range(0, len(data), step):
            yield AudioChunk(data=data[start : start + step], codec=codec, sample_rate=24_000)


class FakeRealtime:
    """Scripted :class:`~movate.voice.base.RealtimeVoiceProvider` (Phase 2).

    Drains the inbound audio (recording every chunk's bytes in
    :attr:`received` + the call kwargs for assertion) and replays a fixed,
    realistic event sequence: a ``speech_started`` cue, an input
    ``transcript`` slice, one or more synthesized ``audio`` chunks carrying
    ``answer`` (so a test can decode the audio back to text and assert the
    voice↔voice round-trip), then ``response_done``. No network, no SDK, no
    audio libs — usable from a default install with no ``mdk[voice]`` extra.

    Set ``frames`` to split the answer audio into that many chunks to exercise
    the transport's multi-frame audio-out path.
    """

    name = "fake_realtime"
    version = "0.0.1"

    def __init__(
        self,
        *,
        transcript: str = "hello",
        answer: str = "hi there",
        frames: int = 1,
    ) -> None:
        self._transcript = transcript
        self._answer = answer
        self._frames = max(1, frames)
        self.received: list[bytes] = []
        self.voice_ids: list[str] = []
        self.instructions: list[str] = []
        self.languages: list[str | None] = []
        self.api_keys: list[str | None] = []

    async def session(
        self,
        audio_in: AsyncIterator[AudioChunk],
        *,
        voice_id: str = "",
        instructions: str = "",
        language: str | None = None,
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[RealtimeChunk]:
        self.voice_ids.append(voice_id)
        self.instructions.append(instructions)
        self.languages.append(language)
        self.api_keys.append(api_key)
        async for chunk in audio_in:
            self.received.append(chunk.data)

        yield RealtimeChunk(kind="speech_started")
        yield RealtimeChunk(kind="transcript", text=self._transcript, is_final=True)
        data = self._answer.encode("utf-8")
        if data:
            step = max(1, len(data) // self._frames)
            for start in range(0, len(data), step):
                yield RealtimeChunk(
                    kind="audio",
                    audio=AudioChunk(data=data[start : start + step], codec=codec),
                )
        yield RealtimeChunk(kind="response_done")
