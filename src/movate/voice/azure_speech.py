"""Azure Speech adapters — Azure Speech STT + Azure Neural TTS.

The **T1 enterprise / sovereignty** pair behind the ADR 048 D3 seams
(:class:`movate.voice.base.SpeechToTextProvider` /
:class:`~movate.voice.base.TextToSpeechProvider`). ADR 048's provider tiering
puts Azure Speech in the *enterprise* tier: the customer runs it against
**their own Azure subscription** (data residency / sovereignty), and the
key+region are BYOK injected through the ADR 018 key store at the edge — never
hard-coded here. One provider, both directions: STT streams partial→final
transcripts, TTS synthesizes the agent's streamed answer back to audio.

The ``azure.cognitiveservices.speech`` SDK import is **lazy + guarded** exactly
like :mod:`movate.voice.openai_speech` / :mod:`movate.providers.openai_native`:
nothing here imports the SDK at module scope, so a runtime/CLI installed
without ``mdk[voice]`` is wholly unaffected (ADR 048 D9). The SDK objects are
constructed on first use; tests inject fakes via the ``recognizer_factory=`` /
``synthesizer_factory=`` kwargs (the same test seam as OpenAI's ``client=``).

Key + region (ADR 018 BYOK)
---------------------------

Azure Speech authenticates with a subscription **key AND a region** (unlike the
single-key OpenAI/Anthropic providers). Both ride in via the constructor:
``api_key=`` (the subscription key, defaulting to ``$AZURE_SPEECH_KEY``) and
``region=`` (defaulting to ``$AZURE_SPEECH_REGION``). When the edge resolves a
tenant BYOK key it passes ``api_key=`` to :meth:`transcribe` / :meth:`synthesize`
per call (ADR 018) — that wins over the constructor default. A missing key or
region is a clear ``ValueError`` at call time, not an opaque SDK error.

Shape notes (re-confirmed at build time, per ADR 048's caveat that the
provider landscape moves fast):

* **STT** — the Speech SDK is callback/event driven and **synchronous** (no
  native asyncio). The adapter bridges it to the streaming Protocol with a
  ``PushAudioInputStream`` fed from the inbound :class:`AudioChunk` stream and
  an :class:`asyncio.Queue` that the SDK's ``recognizing`` (partial) /
  ``recognized`` (endpointed) / ``canceled`` / ``session_stopped`` callbacks
  push onto from the SDK's worker thread (via ``call_soon_threadsafe``). It
  yields ``is_final=False`` partials as the caller speaks and ``is_final=True``
  at each endpointed utterance — the streaming-native shape ADR 048 wants.
* **TTS** — Azure's ``speak_text_async`` returns the **full** audio for a piece
  of text (buffered, like OpenAI's speech API). The adapter buffers the inbound
  text-delta stream into one utterance, requests **raw 24 kHz / 16-bit mono
  PCM** (so the bytes map straight onto our ``pcm16`` codec with no container to
  parse at the edge, ADR 048 D8), synthesizes once, and yields the audio in
  fixed-size :class:`AudioChunk` slices so playback can start promptly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Protocol

from movate.voice.base import AudioChunk, AudioCodec, TranscriptChunk

# Azure Neural TTS, like OpenAI's, emits 24 kHz PCM when we request the
# ``Raw24Khz16BitMonoPcm`` output format (header-less signed 16-bit LE) — the
# bytes map straight onto our ``pcm16`` ``AudioChunk`` codec, no WAV/RIFF
# container to strip at the edge. Matches the OpenAI adapter's 24 kHz choice so
# the transport's codec handling is identical across providers.
_AZURE_TTS_SAMPLE_RATE = 24_000

# Azure Speech STT's push stream expects raw 16 kHz / 16-bit mono PCM by
# default (the service's native recognition sample rate). The edge captures /
# transcodes to this before handing us ``pcm16`` frames (ADR 048 D8); we just
# pass the bytes through to the push stream.
_AZURE_STT_SAMPLE_RATE = 16_000

# How many bytes of synthesized audio to emit per :class:`AudioChunk`. ~40 ms
# of 24 kHz/16-bit mono audio (24000 * 2 * 0.04 ≈ 1920) — identical framing to
# the OpenAI adapter so a slow consumer gets the same steady playback cadence.
_TTS_CHUNK_BYTES = 1920


def _require_azure_speech() -> Any:
    """Import the Azure Speech SDK lazily, with a clear install hint.

    Mirrors :func:`movate.voice.openai_speech._require_openai` — the import
    lives inside the call so importing this module (e.g. for the Protocol type
    or to satisfy ``isinstance(p, SpeechToTextProvider)``) never requires the
    optional dep. A default install without ``mdk[voice]`` is unaffected.
    """
    try:
        import azure.cognitiveservices.speech as _speechsdk  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the import-guard test
        raise ImportError(
            "the 'azure-cognitiveservices-speech' package is required for the "
            "Azure Speech voice adapters. Install with: uv add 'movate-cli[voice]'"
        ) from exc
    return _speechsdk


def _resolve_key_region(
    *,
    api_key: str | None,
    ctor_key: str | None,
    region: str | None,
) -> tuple[str, str]:
    """Resolve the subscription key + region with ADR 018 precedence.

    Precedence for the key: a per-call ``api_key`` (the edge-resolved tenant
    BYOK key) wins over the constructor default. The region comes from the
    constructor (which defaulted it to ``$AZURE_SPEECH_REGION``). Both must be
    non-empty — Azure Speech needs the pair — or we raise a clear
    :class:`ValueError` rather than letting the SDK fail opaquely deep in a
    worker thread.
    """
    key = (api_key or ctor_key or "").strip()
    reg = (region or "").strip()
    if not key:
        raise ValueError(
            "Azure Speech needs a subscription key. Set AZURE_SPEECH_KEY "
            "(run `mdk auth login azure-speech`) or pass api_key=."
        )
    if not reg:
        raise ValueError(
            "Azure Speech needs a region. Set AZURE_SPEECH_REGION "
            "(run `mdk auth login azure-speech`) or pass region=."
        )
    return key, reg


class _RecognizerFactory(Protocol):
    """Test seam: build a recognizer from a resolved key/region + push stream.

    Production passes ``None`` and the adapter builds a real
    ``SpeechRecognizer`` wired to a ``PushAudioInputStream``. Tests pass a
    callable returning a fake exposing the ``recognizing`` / ``recognized`` /
    ``canceled`` / ``session_stopped`` ``.connect(cb)`` signals and the
    ``start_continuous_recognition`` / ``stop_continuous_recognition`` methods —
    so the streaming bridge is exercised with NO SDK and NO network.
    """

    def __call__(self, *, key: str, region: str, push_stream: Any, language: str | None) -> Any: ...


class _SynthesizerFactory(Protocol):
    """Test seam: build a synthesizer from a resolved key/region + voice.

    Production passes ``None`` and the adapter builds a real
    ``SpeechSynthesizer`` with ``audio_config=None`` (audio returned in the
    result, not sent to a speaker). Tests pass a callable returning a fake
    exposing ``speak_text(text)`` → an object with ``.reason`` + ``.audio_data``.
    """

    def __call__(self, *, key: str, region: str, voice: str) -> Any: ...


class AzureSpeechSTT:
    """Azure Speech :class:`~movate.voice.base.SpeechToTextProvider`.

    Streams partial transcripts (``is_final=False``) as the caller speaks and
    an endpointed final transcript (``is_final=True``) at each utterance end,
    via Azure's continuous-recognition event model bridged onto an async
    generator. Runs against the customer's own Azure subscription (key+region
    BYOK).
    """

    name = "azure_speech_stt"
    version = "0.0.1"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        region: str | None = None,
        recognizer_factory: _RecognizerFactory | None = None,
    ) -> None:
        """``api_key`` / ``region`` default to ``$AZURE_SPEECH_KEY`` /
        ``$AZURE_SPEECH_REGION`` (read here, not at import, so the env is
        resolved at construction). ``recognizer_factory`` is the test seam —
        pass a fake builder; production leaves it ``None`` and a real
        ``SpeechRecognizer`` is constructed on first :meth:`transcribe`."""
        import os  # noqa: PLC0415

        self._ctor_key = api_key if api_key is not None else os.environ.get("AZURE_SPEECH_KEY")
        self._region = region if region is not None else os.environ.get("AZURE_SPEECH_REGION")
        self._recognizer_factory = recognizer_factory

    def _build_recognizer(
        self, *, key: str, region: str, push_stream: Any, language: str | None
    ) -> Any:
        if self._recognizer_factory is not None:
            return self._recognizer_factory(
                key=key, region=region, push_stream=push_stream, language=language
            )
        speechsdk = _require_azure_speech()
        speech_config = speechsdk.SpeechConfig(subscription=key, region=region)
        if language:
            # Azure wants the full BCP-47 form ("en-US"), unlike OpenAI's bare
            # ISO-639-1 — pass it through verbatim. ``None`` lets Azure use its
            # configured default locale.
            speech_config.speech_recognition_language = language
        audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
        return speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        language: str | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[TranscriptChunk]:
        key, region = _resolve_key_region(
            api_key=api_key, ctor_key=self._ctor_key, region=self._region
        )

        push_stream = self._make_push_stream()
        recognizer = self._build_recognizer(
            key=key, region=region, push_stream=push_stream, language=language
        )

        loop = asyncio.get_running_loop()
        # The SDK fires callbacks on its OWN worker thread; hop back to the
        # event loop with ``call_soon_threadsafe`` before touching the queue so
        # the async generator stays single-threaded from asyncio's view.
        queue: asyncio.Queue[TranscriptChunk | None] = asyncio.Queue()

        def _emit(chunk: TranscriptChunk | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, chunk)

        def _on_recognizing(evt: Any) -> None:
            text = getattr(getattr(evt, "result", None), "text", "") or ""
            if text:
                _emit(TranscriptChunk(text=text, is_final=False))

        def _on_recognized(evt: Any) -> None:
            text = getattr(getattr(evt, "result", None), "text", "") or ""
            # An endpointed utterance — the contract the agent runs on. Emit
            # even when empty so a downstream "wait for is_final" loop unblocks.
            _emit(TranscriptChunk(text=text, is_final=True))

        def _on_done(_evt: Any) -> None:
            # ``session_stopped`` / ``canceled`` → sentinel so the consumer
            # loop below terminates.
            _emit(None)

        recognizer.recognizing.connect(_on_recognizing)
        recognizer.recognized.connect(_on_recognized)
        recognizer.session_stopped.connect(_on_done)
        recognizer.canceled.connect(_on_done)

        recognizer.start_continuous_recognition()
        # Feed the push stream from the inbound audio in the background so we
        # can yield partial transcripts WHILE the caller is still speaking.
        feeder = asyncio.ensure_future(self._feed(audio, push_stream))
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            # Best-effort teardown — never let a consumer break leak the SDK's
            # recognition session / worker thread.
            await self._safe_cancel(feeder)
            with contextlib.suppress(Exception):
                recognizer.stop_continuous_recognition()

    def _make_push_stream(self) -> Any:
        if self._recognizer_factory is not None:
            # Test path: a plain in-memory sink the fake recognizer can read.
            return _FakePushStream()
        speechsdk = _require_azure_speech()
        fmt = speechsdk.audio.AudioStreamFormat(samples_per_second=_AZURE_STT_SAMPLE_RATE)
        return speechsdk.audio.PushAudioInputStream(stream_format=fmt)

    @staticmethod
    async def _feed(audio: AsyncIterator[AudioChunk], push_stream: Any) -> None:
        """Drain the inbound audio into the push stream, then close it.

        Closing the push stream is what tells Azure "no more audio" so it emits
        the final ``recognized`` event and then ``session_stopped`` — the
        signal that ends the consumer loop. The ``finally`` guarantees the
        stream is closed even if the inbound iterator errors, so recognition
        always terminates (no hung session)."""
        try:
            async for chunk in audio:
                if chunk.data:
                    push_stream.write(chunk.data)
        finally:
            with contextlib.suppress(Exception):
                push_stream.close()

    @staticmethod
    async def _safe_cancel(task: asyncio.Future[Any]) -> None:
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


class AzureNeuralTTS:
    """Azure Neural :class:`~movate.voice.base.TextToSpeechProvider`.

    Buffers the inbound text-delta stream into one utterance, synthesizes it
    with Azure Neural TTS (raw 24 kHz PCM), and yields the audio in fixed-size
    chunks so the transport can start playback promptly. Runs against the
    customer's own Azure subscription (key+region BYOK).
    """

    name = "azure_neural_tts"
    version = "0.0.1"

    # Azure's neural voices are full BCP-47-tagged ids (e.g. "en-US-JennyNeural").
    # Empty ``voice_id`` → this sensible default; the agent author overrides per
    # call via ``voice_id=``.
    _DEFAULT_VOICE = "en-US-JennyNeural"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        region: str | None = None,
        default_voice: str = "",
        synthesizer_factory: _SynthesizerFactory | None = None,
    ) -> None:
        import os  # noqa: PLC0415

        self._ctor_key = api_key if api_key is not None else os.environ.get("AZURE_SPEECH_KEY")
        self._region = region if region is not None else os.environ.get("AZURE_SPEECH_REGION")
        self._default_voice = default_voice or self._DEFAULT_VOICE
        self._synthesizer_factory = synthesizer_factory

    def _build_synthesizer(self, *, key: str, region: str, voice: str) -> Any:
        if self._synthesizer_factory is not None:
            return self._synthesizer_factory(key=key, region=region, voice=voice)
        speechsdk = _require_azure_speech()
        speech_config = speechsdk.SpeechConfig(subscription=key, region=region)
        speech_config.speech_synthesis_voice_name = voice
        # Raw 24 kHz / 16-bit mono PCM — no RIFF/WAV container, so the bytes map
        # straight onto our ``pcm16`` codec at the edge (ADR 048 D8).
        speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
        )
        # ``audio_config=None`` → audio is returned in the result, not pushed to
        # a speaker device (we're a server, not a desktop app).
        return speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)

    async def synthesize(
        self,
        text: AsyncIterator[str],
        *,
        voice_id: str = "",
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[AudioChunk]:
        # Buffer the agent's streamed tokens into one utterance. Azure's
        # ``speak_text`` takes whole text, not a token stream (a streaming-native
        # TTS would synthesize incrementally here instead).
        parts: list[str] = []
        async for delta in text:
            if delta:
                parts.append(delta)
        utterance = "".join(parts)

        if not utterance.strip():
            return  # nothing to say → no audio frames

        key, region = _resolve_key_region(
            api_key=api_key, ctor_key=self._ctor_key, region=self._region
        )
        synthesizer = self._build_synthesizer(
            key=key, region=region, voice=voice_id or self._default_voice
        )

        # ``speak_text_async`` returns a ResultFuture; ``.get()`` blocks until
        # synthesis completes. Run that blocking wait off the event loop so we
        # don't stall other coroutines (the WS transport, other turns).
        data = await self._synthesize_once(synthesizer, utterance)

        for start in range(0, len(data), _TTS_CHUNK_BYTES):
            yield AudioChunk(
                data=data[start : start + _TTS_CHUNK_BYTES],
                codec=codec,
                sample_rate=_AZURE_TTS_SAMPLE_RATE,
            )

    async def _synthesize_once(self, synthesizer: Any, utterance: str) -> bytes:
        """Synthesize one utterance and return the raw PCM bytes.

        Prefers the async ``speak_text_async().get()`` shape (real SDK) and
        falls back to a synchronous ``speak_text`` (the simplest test fake).
        Either way the blocking call runs in a worker thread so the event loop
        stays responsive. A non-completed result reason raises so the failure
        surfaces at the metering/edge seam rather than emitting silent empty
        audio."""

        def _run() -> Any:
            speak_async = getattr(synthesizer, "speak_text_async", None)
            if speak_async is not None:
                return speak_async(utterance).get()
            return synthesizer.speak_text(utterance)

        result = await asyncio.to_thread(_run)
        return _read_synthesis_bytes(result)


def _read_synthesis_bytes(result: Any) -> bytes:
    """Pull the PCM body off an Azure synthesis result, raising on failure.

    The SDK result exposes ``.reason`` (a ``ResultReason``) and ``.audio_data``
    (bytes). We accept the result when ``reason`` is the completed sentinel OR
    when the fake/older shape doesn't carry a recognizable reason but DOES carry
    audio. A canceled/failed reason with no audio raises a clear ``RuntimeError``
    (with the cancellation detail when present) so the edge can surface it —
    never silently return empty audio that the agent would "speak" as silence.
    """
    audio = getattr(result, "audio_data", None)
    reason = getattr(result, "reason", None)
    reason_name = getattr(reason, "name", str(reason)) if reason is not None else ""

    if reason_name and "Completed" in reason_name:
        return bytes(audio or b"")
    if audio:
        # No recognizable reason but audio is present (a minimal fake) — accept.
        return bytes(audio)
    # Failure path — surface the cancellation detail if the SDK attached one.
    detail = ""
    cancellation = getattr(result, "cancellation_details", None)
    if cancellation is not None:
        detail = getattr(cancellation, "error_details", "") or str(
            getattr(cancellation, "reason", "")
        )
    raise RuntimeError(
        f"Azure Neural TTS synthesis did not complete (reason={reason_name or 'unknown'})"
        + (f": {detail}" if detail else "")
    )


class _FakePushStream:
    """In-memory push-stream stand-in for the recognizer test seam.

    Not a real SDK object — only used on the test path (when a
    ``recognizer_factory`` is injected) so the streaming bridge can be
    exercised with no SDK installed. Records written audio for assertion."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, buffer: bytes) -> None:
        self.written.append(bytes(buffer))

    def close(self) -> None:
        self.closed = True
