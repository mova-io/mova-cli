"""Voice adapter seams — the two speech Protocols + their chunk types.

ADR 048 (D3) defines voice as **a transport + two adapter seams that wrap
the unchanged text Executor**. This module is those seams, in *exactly* the
shape of the ``AgentTurn``/provider seam (the ``BaseLLMProvider`` seam):

* streaming-friendly **async generators** of audio / text chunks,
* ``api_key=``-style BYOK injection (resolved through the ADR 018 key store
  at the edge, never hard-coded in an adapter),
* **no cost computed in the adapter** — pricing/metering is derived at the
  metering seam (ADR 036), the same way ``BaseLLMProvider`` defers pricing
  to the executor's versioned table.

This module defines **three** seams:

* the pipeline pair (Phase 1) — :class:`SpeechToTextProvider` /
  :class:`TextToSpeechProvider` (D2a): audio → STT → the unchanged text
  Executor → TTS → audio, voice-enabling EVERY existing agent with zero
  changes; and
* the optional full-duplex :class:`RealtimeVoiceProvider` (Phase 2, ADR 048
  D2b / ADR 050 D12): voice↔voice with **no** intermediate text Executor —
  the premium, voice-native path selected via the ``?mode=realtime`` transport
  mode, with its first impls in :mod:`movate.voice.realtime_openai` /
  :mod:`movate.voice.realtime_azure`.

The realtime seam is **deliberately separate** from the pipeline seams: it is
voice-native (it does NOT reuse the text Executor — ADR 048 Boundaries), and
the transport routes to one or the other by mode, never mixing them.

The streaming-generator shape is **mandatory, not optional**: it is what
makes ADR 048's "stream every stage" latency story possible (partial STT →
streaming agent tokens → streaming TTS, so the agent starts speaking before
the full answer exists).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

# Realtime (speech↔speech) control-event kinds — the non-audio side-channel a
# full-duplex provider emits alongside synthesized audio (ADR 048 D2b / Phase 2,
# ADR 050 D12). Kept as a Literal (not an Enum) to mirror the ``StreamChunk``
# wire-type style in ``providers/base.py`` and the pipeline's ``VoiceEventKind``.
#
# * ``transcript`` — a recognized slice (the provider's own STT; the model's
#   input transcription or the assistant's output transcript), surfaced for the
#   UI/governance plane. ``is_final`` on the chunk marks an endpointed slice.
# * ``speech_started`` — the provider detected the *caller* started speaking
#   (server-side VAD): the transport's barge-in signal (stop local playback).
# * ``speech_stopped`` — the caller stopped speaking (end of the user's turn).
# * ``response_done`` — the assistant finished its turn (no more audio coming
#   for this response).
# * ``error`` — a provider/session error; the transport degrades (ADR 048
#   Failure modes: a realtime outage falls back to the pipeline at the edge).
RealtimeEventKind = Literal[
    "transcript",
    "speech_started",
    "speech_stopped",
    "response_done",
    "error",
]

# Codecs the edge transport understands. ADR 048 D8 transcodes codecs *at the
# edge* (the WS / telephony handler), never inside the agent — so an adapter
# only ever sees/emits one of these, and the codec rides on the chunk so the
# transport can convert. ``pcm16`` (web), ``opus`` (web), ``mulaw`` (telephony,
# Phase 3). Kept as a Literal (not an Enum) to mirror ``StreamChunk``-style
# wire types in ``providers/base.py``.
AudioCodec = Literal["pcm16", "opus", "mulaw"]


class TranscriptChunk(BaseModel):
    """One slice of a streaming transcription (STT output).

    ``is_final`` marks an **endpointed** utterance — the STT provider (or an
    edge VAD) decided the speaker finished a turn, and ``text`` is the
    complete utterance to feed the agent. Partial chunks (``is_final=False``)
    stream as the caller speaks so a UI can render a live caption; only the
    final chunk's text is the contract the agent runs on.

    ``confidence`` is an optional provider-supplied score in ``[0, 1]``;
    ``None`` when the provider does not surface one (e.g. OpenAI Whisper's
    non-streaming transcription).
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    is_final: bool
    confidence: float | None = None


class AudioChunk(BaseModel):
    """One slice of synthesized (TTS output) or captured (STT input) audio.

    ``codec`` / ``sample_rate`` describe the ``data`` bytes so the transport
    can transcode at the edge (ADR 048 D8). The agent never sees these — it
    only ever sees text — so an audio-format concern reaching the agent is a
    boundary violation (CLAUDE.md rule 6).
    """

    model_config = ConfigDict(extra="forbid")

    data: bytes
    codec: AudioCodec = "pcm16"
    sample_rate: int = 24_000


@runtime_checkable
class SpeechToTextProvider(Protocol):
    """Audio → text. Whisper / Deepgram / Azure Speech / AssemblyAI.

    STREAMING + endpointing: :meth:`transcribe` consumes an async stream of
    :class:`AudioChunk` and yields :class:`TranscriptChunk` slices —
    partial transcripts as the caller speaks and a final endpointed
    transcript at utterance end (``is_final=True``). A non-streaming
    provider (e.g. OpenAI Whisper, which transcribes a buffered clip) still
    satisfies the contract by yielding a single ``is_final=True`` chunk.

    A new STT backend is a **new file implementing this Protocol** + a
    registry entry — the same extension story as adding a
    ``BaseLLMProvider`` (ADR 007 / CLAUDE.md rule 7).
    """

    name: str
    version: str

    def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
        keyterms: Sequence[str] | None = None,
        endpointing_ms: int | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        """Transcribe a stream of audio into a stream of transcript chunks.

        ``language`` is an optional BCP-47 hint (``"en-US"``); ``None`` lets
        the provider auto-detect. ``api_key`` is the tenant's BYOK key
        (ADR 018), resolved at the edge and passed in — adapters MUST NOT
        read a global env var when a key is supplied.

        ``keyterms`` (ADR 071 D4, additive, optional) is a per-call list of
        domain terms to **boost** at recognition time (names, acronyms, jargon
        a general model mis-hears — ``["VPN", "Okta", "Mova-iO"]``). It lets the
        transport pass an *agent-specific* vocabulary through this seam without a
        per-agent adapter rebuild. Providers that support boosting (Deepgram)
        honor it; providers that do not **MUST accept and ignore it**. ``None``
        (the default) sends no boosting and is byte-for-byte the prior behavior.

        ``endpointing_ms`` (ADR 073 D3, additive, optional) is a per-call
        override of the silence-hold the provider waits before declaring the
        utterance final — the dominant fixed latency of a pipeline turn. It lets
        a *deliberate-speaker* agent hold longer (fewer mid-pause barge-ins) and
        a *snappy* agent cut shorter, without rebuilding the adapter. Streaming
        providers that expose endpointing (Deepgram) honor it for this call;
        others **MUST accept and ignore it**. ``None`` (the default) keeps the
        adapter's configured value and is byte-for-byte the prior behavior.

        Implementations MUST yield at least one chunk and MUST mark the
        terminal utterance with ``is_final=True`` so the transport knows
        when to run the agent.
        """
        ...


@runtime_checkable
class TextToSpeechProvider(Protocol):
    """Text → audio. OpenAI TTS / ElevenLabs / Cartesia / Azure Neural.

    STREAMING synthesis: :meth:`synthesize` consumes an async stream of text
    deltas (the agent's streamed output tokens, ADR 045 D11) and yields
    :class:`AudioChunk` slices as the audio is produced, so playback can
    start before the full answer is synthesized (the latency story, ADR 048
    D7). A provider whose API only accepts whole utterances satisfies the
    contract by buffering the text stream and yielding the synthesized
    audio in one or more chunks.

    A new TTS backend is a **new file implementing this Protocol** + a
    registry entry — same as STT above.
    """

    name: str
    version: str

    def synthesize(
        self,
        text: AsyncIterator[str],
        *,
        voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        """Synthesize a stream of text deltas into a stream of audio chunks.

        ``voice_id`` selects the synthesized voice (provider-specific id /
        name; ``""`` → the provider's default). ``codec`` is the desired
        output codec the transport will play. ``api_key`` is the tenant's
        BYOK key (ADR 018), resolved at the edge.

        Implementations MUST yield each chunk's ``codec`` / ``sample_rate``
        so the transport can transcode at the edge if the negotiated client
        codec differs.
        """
        ...


class RealtimeChunk(BaseModel):
    """One slice of a full-duplex realtime session's output (ADR 048 D2b).

    A realtime (speech↔speech) provider streams **two interleaved kinds** of
    output back over a single session: synthesized **audio** to play, and
    **control events** (the caller started/stopped speaking, a transcript
    slice, the response finished, an error). This one envelope carries both so
    :meth:`RealtimeVoiceProvider.session` is a single async generator (mirroring
    the pipeline's :class:`~movate.voice.pipeline.VoiceEvent`), keeping the
    transport a thin serializer.

    Exactly one payload is meaningful per ``kind``:

    * ``kind="audio"`` → :attr:`audio` (a synthesized :class:`AudioChunk`).
    * ``kind="transcript"`` → :attr:`text` + :attr:`is_final` (a recognized
      slice — the model's input/output transcription, surfaced for the UI and
      the governance plane).
    * ``kind="speech_started"`` / ``"speech_stopped"`` → no payload (a
      server-VAD turn-boundary signal; ``speech_started`` is the barge-in cue).
    * ``kind="response_done"`` → no payload (the assistant's turn is complete).
    * ``kind="error"`` → :attr:`message` + :attr:`code` (a session failure; the
      transport degrades per ADR 048's Failure modes).
    """

    model_config = ConfigDict(extra="forbid")

    # ``"audio"`` carries synthesized audio; the rest mirror the control-event
    # side-channel (:data:`RealtimeEventKind`).
    kind: Literal[
        "audio",
        "transcript",
        "speech_started",
        "speech_stopped",
        "response_done",
        "error",
    ]
    audio: AudioChunk | None = None
    text: str = ""
    is_final: bool = False
    message: str = ""
    code: str = ""


@runtime_checkable
class RealtimeVoiceProvider(Protocol):
    """Full-duplex voice↔voice — OpenAI Realtime / Azure OpenAI Realtime / Gemini Live.

    The **optional, premium** seam of ADR 048 (D2b / Phase 2), selected via the
    realtime transport mode (ADR 050 D12 — the ``?mode=realtime`` query on the
    same ``WS /api/v1/agents/{name}/voice`` URL). Unlike the pipeline seams
    (:class:`SpeechToTextProvider` / :class:`TextToSpeechProvider`), a realtime
    provider is **voice-native**: audio goes in, audio comes out, and there is
    **no intermediate text Executor** (ADR 048 D2b / Boundaries). It trades the
    pipeline's provider-portability + zero-change-to-existing-agents promise for
    the lowest latency floor — the lock-in is contained to a single adapter file.

    The contract is one **bidirectional** call: :meth:`session` consumes an
    async stream of inbound :class:`AudioChunk` (the caller's mic) and yields a
    stream of :class:`RealtimeChunk` (interleaved synthesized audio + control
    events). It is the same async-generator, ``api_key=``-BYOK, no-cost-in-the-
    adapter shape as the pipeline seams and ``BaseLLMProvider`` (ADR 007 /
    CLAUDE.md rule 7).

    A new realtime backend is a **new file implementing this Protocol** + a
    registry/factory entry — the same extension story as adding a
    ``BaseLLMProvider``.
    """

    name: str
    version: str

    def session(
        self,
        audio_in: AsyncIterator[AudioChunk],
        *,
        voice_id: str = "",
        instructions: str = "",
        language: str | None = None,
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[RealtimeChunk]:
        """Run one full-duplex voice↔voice session.

        ``audio_in`` is the caller's live mic stream (the transport feeds raw
        :class:`AudioChunk` frames as they arrive — there is no per-turn
        endpointing at this seam; the provider's own server-side VAD decides
        turns and signals them via ``speech_started`` / ``speech_stopped``
        control chunks). The returned stream interleaves synthesized
        ``audio`` chunks to play with those control events.

        ``voice_id`` selects the synthesized voice (provider-specific id;
        ``""`` → the provider default). ``instructions`` is the system prompt /
        persona for the voice-native model (a realtime agent supplies it
        explicitly — there is no text ``prompt.md`` run through an Executor
        here). ``language`` is an optional BCP-47 hint. ``codec`` is the
        desired audio I/O codec the transport will play/capture. ``api_key`` is
        the tenant's BYOK key (ADR 018), resolved at the edge and passed in —
        adapters MUST NOT read a global env var when a key is supplied.

        Implementations MUST yield audio as ``kind="audio"`` chunks and SHOULD
        surface turn boundaries (``speech_started`` / ``speech_stopped`` /
        ``response_done``) and any session error (``kind="error"``) so the
        transport can drive barge-in + degrade without provider-specific logic.
        """
        ...
