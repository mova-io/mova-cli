"""Voice adapter seams — the two speech Protocols + their chunk types.

ADR 048 (D3) defines voice as **a transport + two adapter seams that wrap
the unchanged text Executor**. This module is those seams, in *exactly* the
shape of :mod:`movate.providers.base` (the ``BaseLLMProvider`` seam):

* streaming-friendly **async generators** of audio / text chunks,
* ``api_key=``-style BYOK injection (resolved through the ADR 018 key store
  at the edge, never hard-coded in an adapter),
* **no cost computed in the adapter** — pricing/metering is derived at the
  metering seam (ADR 036), the same way ``BaseLLMProvider`` defers pricing
  to the executor's versioned table.

Phase 1 ships **pipeline mode only**: the two Protocols below
(:class:`SpeechToTextProvider` / :class:`TextToSpeechProvider`) plus their
first reference implementations (OpenAI Whisper STT + OpenAI TTS). The
optional full-duplex :class:`RealtimeVoiceProvider` (ADR 048 D2b / Phase 2)
is **deliberately not defined here** — it lands behind its own seam in a
later phase so this file stays the minimal pipeline contract.

The streaming-generator shape is **mandatory, not optional**: it is what
makes ADR 048's "stream every stage" latency story possible (partial STT →
streaming agent tokens → streaming TTS, so the agent starts speaking before
the full answer exists).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

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
    ) -> AsyncIterator[TranscriptChunk]:
        """Transcribe a stream of audio into a stream of transcript chunks.

        ``language`` is an optional BCP-47 hint (``"en-US"``); ``None`` lets
        the provider auto-detect. ``api_key`` is the tenant's BYOK key
        (ADR 018), resolved at the edge and passed in — adapters MUST NOT
        read a global env var when a key is supplied.

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
