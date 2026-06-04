"""``mdk voice`` — terminal voice conversation and voice provider tooling.

ADR 048 (D1 / D5) / ADR 050 (D11): voice is an I/O modality on the existing
agent; ``mdk voice try`` connects to ``WS /api/v1/agents/{name}/voice``,
captures mic audio, streams frames, prints partial transcripts, and plays TTS
audio replies.  ``mdk voice providers list`` reads the capabilities endpoint
(ADR 050 D4/D5) and renders the available STT/TTS providers.

All commands gate behind the ``[voice]`` extra (ADR 048 D9): running without
it prints a clear install hint and exits rather than crashing with a cryptic
``ImportError``.  Mic capture (sounddevice) is lazy-imported inside the
conversation loop — a runtime check lets us import this module anywhere.

CLAUDE.md rule 5 — flagged new surface:
  * ``mdk voice try <agent>`` — new, opt-in CLI verb (``mdk[voice]`` only);
    connects to the existing WS ``/api/v1/agents/{name}/voice``.
  * ``mdk voice providers list`` — new, opt-in CLI verb; reads the existing
    ``GET /api/v1/capabilities`` endpoint (no new server surface).
  * ``mdk voice say`` / ``mdk voice transcribe`` / ``mdk voice ask`` — new,
    opt-in CLI verbs that drive the REST one-shot ``POST
    /api/v1/agents/{name}/voice`` (ADR 050 D2/D11) via :class:`MovateClient`.
  None of these verbs changes any existing CLI shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING for typing only; the runtime import lives in
    # the verbs that use it. Naming it at module scope is also what the CLI↔API
    # parity gate (tests/test_cli_api_parity.py) reads to classify the voice
    # ``say``/``transcribe``/``ask`` verbs as remote-capable (ADR 050 D11).
    from movate.core.client import MovateClient
    from movate.voice.base import SpeechToTextProvider, TextToSpeechProvider

err = Console(stderr=True)

voice_app = typer.Typer(
    name="voice",
    help=(
        "Terminal voice conversation + voice provider tooling. "
        "Requires [bold]mdk[voice][/bold] (``pip install 'movate-cli[voice]'``).\n\n"
        "[bold]Examples:[/bold]\n"
        "  [dim]$ mdk voice try my-agent                  # live mic → WS → TTS[/dim]\n"
        "  [dim]$ mdk voice try my-agent --mode realtime  # realtime path[/dim]\n"
        "  [dim]$ mdk voice providers list                # show STT/TTS providers[/dim]"
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Sub-group: ``mdk voice providers <subcommand>``
providers_app = typer.Typer(
    name="providers",
    help="Voice provider tooling — list available STT/TTS providers.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
voice_app.add_typer(providers_app, name="providers")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_voice_extra() -> None:
    """Gate commands behind the ``[voice]`` optional extra.

    ADR 048 D9: the voice package's audio/provider SDKs are heavy and opt-in.
    Rather than letting a bare ``ImportError`` surface (which looks like a
    bug), we probe for one of the voice-extra markers and emit a friendly
    install hint before exiting.  A runtime without ``mdk[voice]`` is wholly
    unaffected by this module being imported — nothing imports an audio lib at
    module scope.
    """
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("movate.voice.pipeline") is None:
        err.print(
            "[red]✗[/red] The [bold]mdk\\[voice][/bold] extra is not installed.\n"
            "  Run: [bold]pip install 'movate-cli[voice]'[/bold]\n"
            "  (or: [bold]uv pip install 'movate-cli[voice]'[/bold])"
        )
        raise typer.Exit(code=1)


def _resolve_target_url(target: str | None) -> str:
    """Resolve the runtime URL from ``--target`` or env vars.

    Resolution order:
    1. ``--target`` flag if it looks like a URL (starts with ``http``).
    2. ``MDK_TARGET`` / ``MOVATE_TARGET`` env var (if it looks like a URL).
    3. Fall back to ``http://127.0.0.1:8000`` (local dev default).

    Non-URL target names (config profiles) are not resolved here — use
    ``--target http://...`` to specify a full URL explicitly.
    """
    import os  # noqa: PLC0415

    raw = target or os.environ.get("MDK_TARGET") or os.environ.get("MOVATE_TARGET") or ""
    raw = raw.strip()

    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")

    # Bare host:port (e.g. "localhost:8080") — add the http scheme.
    if raw and ":" in raw and not raw.startswith("http"):
        return f"http://{raw}"

    return "http://127.0.0.1:8000"


def _resolve_api_key(api_key: str | None) -> str | None:
    """Resolve the bearer key from ``--api-key`` or the active env/config."""
    import os  # noqa: PLC0415

    if api_key:
        return api_key
    return (
        os.environ.get("MOVATE_API_KEY")
        or os.environ.get("MDK_API_KEY")
        or os.environ.get("MDK_DEV_KEY")
        or None
    )


# ---------------------------------------------------------------------------
# ``mdk voice try <agent>``
# ---------------------------------------------------------------------------


@voice_app.command("try")
def voice_try(
    agent: Annotated[str, typer.Argument(help="Agent name on the target runtime.")],
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Runtime URL or config target name."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Bearer key for the runtime (falls back to env vars)."),
    ] = None,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="Voice pipeline mode: 'pipeline' (default) or 'realtime'.",
        ),
    ] = "pipeline",
    stt: Annotated[
        str | None,
        typer.Option("--stt", help="STT provider override (e.g. 'deepgram', 'openai', 'azure')."),
    ] = None,
    tts: Annotated[
        str | None,
        typer.Option("--tts", help="TTS provider override (e.g. 'cartesia', 'openai', 'azure')."),
    ] = None,
) -> None:
    """Start a terminal voice conversation with an agent.

    Captures mic audio via sounddevice (part of the [voice] extra), streams
    PCM frames over the WS ``/api/v1/agents/{name}/voice`` endpoint, prints
    partial and final transcripts to the terminal, and plays TTS audio replies
    through the default audio output device.

    Press [bold]Ctrl-C[/bold] to end the call.

    [bold]Examples:[/bold]

      [dim]# Pipeline (STT → unchanged text agent → TTS)[/dim]
      $ mdk voice try my-agent

      [dim]# Against a specific runtime target[/dim]
      $ mdk voice try my-agent --target https://mdk-prod.example.com

      [dim]# Realtime (voice↔voice) path[/dim]
      $ mdk voice try my-agent --mode realtime

      [dim]# Override STT/TTS provider for this session[/dim]
      $ mdk voice try my-agent --stt deepgram --tts cartesia
    """
    import asyncio  # noqa: PLC0415

    _require_voice_extra()

    if mode not in ("pipeline", "realtime"):
        err.print(f"[red]✗[/red] Unknown --mode '{mode}'. Choose 'pipeline' or 'realtime'.")
        raise typer.Exit(code=1)

    base_url = _resolve_target_url(target)
    key = _resolve_api_key(api_key)

    asyncio.run(
        _voice_try_async(
            agent=agent,
            base_url=base_url,
            api_key=key,
            mode=mode,
            stt_override=stt,
            tts_override=tts,
        )
    )


async def _voice_try_async(
    *,
    agent: str,
    base_url: str,
    api_key: str | None,
    mode: str,
    stt_override: str | None,
    tts_override: str | None,
) -> None:
    """Async core of ``mdk voice try``.

    Pipeline:
      1. Open mic → sounddevice InputStream (lazy-imported here; the
         ``[voice]`` gate above ensures it's available).
      2. Connect to WS ``/api/v1/agents/{name}/voice?mode={mode}``.
      3. Send an optional init control frame with STT/TTS override hints.
      4. Concurrently:
         a. Read mic frames → base64-encode → send as ``audio`` WS frames.
         b. Receive WS frames:
            - ``transcript.partial`` → print inline (overwrite line).
            - ``transcript.final``   → print final transcript.
            - ``agent.token``        → accumulate agent answer.
            - ``tts.audio``          → play through sounddevice OutputStream.
            - ``done`` / ``error``   → log + break.
      5. Ctrl-C → send ``{"type":"close"}`` and close the socket gracefully.
    """
    import asyncio  # noqa: PLC0415
    import base64  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import json  # noqa: PLC0415
    import queue  # noqa: PLC0415

    # Lazy-import sounddevice — only needed inside this function; the
    # ``_require_voice_extra`` gate above already confirmed the extra is present.
    try:
        import sounddevice as sd  # noqa: PLC0415  # type: ignore[import-not-found]
    except ImportError as _exc:
        err.print(
            "[red]✗[/red] sounddevice is not installed. It is included in "
            "mdk[voice]; reinstall with:\n"
            "  [bold]pip install 'movate-cli[voice]'[/bold]"
        )
        raise typer.Exit(code=1) from _exc

    # websockets library — the runtime uses FastAPI/Starlette; the CLI-side
    # WS client uses ``websockets`` (permissive MIT, already in the runtime
    # extra; safe to require in voice_cmd since voice_cmd itself requires
    # mdk[voice] which implies mdk[runtime]).
    try:
        import websockets  # noqa: PLC0415
        import websockets.asyncio.client as _ws_client  # noqa: PLC0415
    except ImportError as _exc:
        err.print(
            "[red]✗[/red] websockets is not installed. Install the runtime extra:\n"
            "  [bold]pip install 'movate-cli[runtime]'[/bold]"
        )
        raise typer.Exit(code=1) from _exc

    # Build the WS URL: ws:// or wss:// mirrors the runtime base.
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/api/v1/agents/{agent}/voice?mode={mode}"

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Audio parameters — 16-bit signed PCM, mono, 16 kHz. These match the
    # ``pcm16`` AudioChunk default (raw PCM LE, 16-bit, the most compatible
    # codec for STT providers that accept raw audio).
    sample_rate = 16_000
    channels = 1
    dtype = "int16"
    chunk_frames = 1_600  # 100 ms of audio per frame

    err.print(
        f"[bold green]voice:[/bold green] connecting to [bold]{ws_url}[/bold]  (Ctrl-C to end)"
    )

    # Thread-safe queue for mic frames (the sounddevice callback runs in a
    # background thread; the async WS sender reads from this queue).
    mic_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=50)
    # Thread-safe queue for TTS audio frames to play back.
    play_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=200)

    # --------------- Sounddevice callbacks (run in C audio thread) ----------

    def _mic_callback(
        indata: sd.np.ndarray,
        frames: int,
        time: object,
        status: sd.CallbackFlags,
    ) -> None:
        """Put raw PCM bytes from the mic into mic_queue."""
        if status:
            pass  # drop; the WS sender will notice a stall and close
        with contextlib.suppress(queue.Full):
            mic_queue.put_nowait(bytes(indata))

    def _play_callback(
        outdata: sd.np.ndarray,
        frames: int,
        time: object,
        status: sd.CallbackFlags,
    ) -> None:
        """Fill the output buffer from play_queue."""
        import numpy as np  # noqa: PLC0415

        needed = frames * channels * 2  # 2 bytes per int16 sample
        buf = bytearray(needed)
        pos = 0
        while pos < needed:
            try:
                chunk = play_queue.get_nowait()
                if chunk is None:
                    break
                avail = min(len(chunk), needed - pos)
                buf[pos : pos + avail] = chunk[:avail]
                pos += avail
            except queue.Empty:
                break
        outdata[:] = np.frombuffer(bytes(buf), dtype=np.int16).reshape(-1, channels)

    # --------------- Async WS tasks -----------------------------------------

    async def _send_audio(ws: websockets.asyncio.client.ClientConnection) -> None:
        """Drain mic_queue and send audio frames to the WS."""
        loop = asyncio.get_running_loop()
        while True:
            # Use run_in_executor to avoid blocking the event loop on a
            # blocking queue.get (timeout=0.05 s).
            try:
                chunk = await loop.run_in_executor(
                    None,
                    lambda: mic_queue.get(timeout=0.05),
                )
            except queue.Empty:
                continue
            if chunk is None:
                break
            # Protocol: binary audio frame — raw PCM bytes. The WS route in
            # app.py distinguishes audio (bytes) from JSON control frames.
            try:
                await ws.send(base64.b64encode(chunk).decode("ascii"))
                # Actually the transport expects raw binary frames or a JSON
                # envelope; we send a JSON audio frame per ADR 048 D4:
                # ``{"type": "audio", "data": "<base64>"}``
            except Exception:
                break

    async def _send_audio_json(ws: websockets.asyncio.client.ClientConnection) -> None:
        """Send PCM frames as JSON audio control frames per ADR 048 D4."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                chunk = await loop.run_in_executor(
                    None,
                    lambda: mic_queue.get(timeout=0.05),
                )
            except queue.Empty:
                continue
            if chunk is None:
                break
            frame = json.dumps({"type": "audio", "data": base64.b64encode(chunk).decode("ascii")})
            try:
                await ws.send(frame)
            except Exception:
                break

    async def _receive_loop(ws: websockets.asyncio.client.ClientConnection) -> None:
        """Receive WS frames and dispatch them."""
        agent_answer: list[str] = []
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    # Binary frame: TTS audio — enqueue for playback.
                    with contextlib.suppress(queue.Full):
                        play_queue.put_nowait(raw)
                    continue

                try:
                    frame = json.loads(raw)
                except Exception:
                    continue

                ftype = frame.get("type", "")

                if ftype == "transcript.partial":
                    text = frame.get("text", "")
                    err.print(f"[dim]  transcript (partial): {text}[/dim]", end="\r")

                elif ftype == "transcript.final":
                    text = frame.get("text", "")
                    err.print(f"\n[bold cyan]you:[/bold cyan] {text}")

                elif ftype == "agent.token":
                    token = frame.get("text", "")
                    agent_answer.append(token)
                    err.print(token, end="")

                elif ftype == "tts.audio":
                    # JSON-encoded TTS audio chunk with base64 data.
                    data_b64 = frame.get("data", "")
                    if data_b64:
                        audio_bytes = base64.b64decode(data_b64)
                        with contextlib.suppress(queue.Full):
                            play_queue.put_nowait(audio_bytes)

                elif ftype == "done":
                    if agent_answer:
                        err.print()  # newline after streamed tokens
                    run_id = frame.get("run_id", "")
                    err.print(
                        "[bold green]agent:[/bold green] [dim](turn complete"
                        + (f", run {run_id}" if run_id else "")
                        + ")[/dim]"
                    )
                    agent_answer.clear()

                elif ftype == "error":
                    msg = frame.get("message", "unknown error")
                    stage = frame.get("stage", "")
                    err.print(
                        "\n[red]✗ voice error[/red]" + (f" [{stage}]" if stage else "") + f": {msg}"
                    )

        except Exception:
            pass  # connection closed or network error

    # --------------- Main conversation loop ---------------------------------

    try:
        async with _ws_client.connect(ws_url, additional_headers=headers) as ws:
            err.print(
                "[bold green]✓[/bold green] connected  [dim](speak now — Ctrl-C to end)[/dim]"
            )

            # Send optional init frame with STT/TTS overrides and mode.
            init_frame: dict[str, str] = {"type": "init", "mode": mode}
            if stt_override:
                init_frame["stt"] = stt_override
            if tts_override:
                init_frame["tts"] = tts_override
            await ws.send(json.dumps(init_frame))

            # Open mic stream and playback stream.
            with (
                sd.InputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype=dtype,
                    blocksize=chunk_frames,
                    callback=_mic_callback,
                ),
                sd.OutputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype=dtype,
                    blocksize=chunk_frames,
                    callback=_play_callback,
                ),
            ):
                # Run sender + receiver concurrently; cancel on first to finish.
                sender = asyncio.create_task(_send_audio_json(ws))
                receiver = asyncio.create_task(_receive_loop(ws))
                try:
                    await asyncio.gather(sender, receiver)
                except asyncio.CancelledError:
                    pass
                finally:
                    sender.cancel()
                    receiver.cancel()

    except KeyboardInterrupt:
        err.print("\n[bold yellow]voice call ended (Ctrl-C)[/bold yellow]")
    except Exception as exc:
        err.print(f"[red]✗[/red] Could not connect: {exc}")
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# ``mdk voice providers list``
# ---------------------------------------------------------------------------


@providers_app.command("list")
def providers_list(
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Runtime URL or config target name."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Bearer key for the runtime (falls back to env vars)."),
    ] = None,
) -> None:
    """List available STT and TTS voice providers on the target runtime.

    Reads the ``GET /api/v1/capabilities`` endpoint (ADR 050 D4) and prints the
    ``voice`` section: which modes are available, which STT/TTS providers are
    configured, and whether voice is enabled at all.

    [bold]Examples:[/bold]

      [dim]# Against the local runtime[/dim]
      $ mdk voice providers list

      [dim]# Against a specific target[/dim]
      $ mdk voice providers list --target https://mdk-prod.example.com
    """
    import asyncio  # noqa: PLC0415

    asyncio.run(
        _providers_list_async(
            base_url=_resolve_target_url(target),
            api_key=_resolve_api_key(api_key),
        )
    )


async def _providers_list_async(*, base_url: str, api_key: str | None) -> None:
    """Fetch capabilities and render the voice section."""
    try:
        import httpx  # noqa: PLC0415
    except ImportError as _exc:
        err.print(
            "[red]✗[/red] httpx is not installed (it ships with mdk[runtime]).\n"
            "  Run: [bold]pip install 'movate-cli[runtime]'[/bold]"
        )
        raise typer.Exit(code=1) from _exc

    url = f"{base_url}/api/v1/capabilities"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        err.print(f"[red]✗[/red] Could not reach {url}: {exc}")
        raise typer.Exit(code=1) from exc

    if resp.status_code not in (200, 401, 403):
        err.print(f"[red]✗[/red] {url} returned HTTP {resp.status_code}")
        raise typer.Exit(code=1)

    try:
        data = resp.json()
    except Exception as _exc:
        err.print("[red]✗[/red] Could not parse capabilities response.")
        raise typer.Exit(code=1) from _exc

    voice = data.get("voice")
    version = data.get("mdk_version", "?")

    err.print(f"[bold]runtime:[/bold] {base_url}  (mdk {version})")
    err.print()

    if voice is None:
        # Old runtime without the voice block — read from the flat features dict.
        features = data.get("features") or {}
        voice_enabled = bool(features.get("voice", False))
        voice_realtime = bool(features.get("voice_realtime", False))
        if not voice_enabled:
            err.print(
                "[yellow]voice:[/yellow]  not configured\n"
                "  Set [bold]DEEPGRAM_API_KEY[/bold] + [bold]CARTESIA_API_KEY[/bold] "
                "(or OPENAI_API_KEY) to enable."
            )
        else:
            modes = ["pipeline"]
            if voice_realtime:
                modes.append("realtime")
            err.print(f"[bold green]voice:[/bold green]  enabled  modes={modes}")
        return

    enabled = voice.get("enabled", False)
    if not enabled:
        err.print(
            "[yellow]voice:[/yellow]  not configured\n"
            "  Set [bold]DEEPGRAM_API_KEY[/bold] + [bold]CARTESIA_API_KEY[/bold] "
            "(or OPENAI_API_KEY) to enable."
        )
        return

    modes = voice.get("modes", [])
    stt_providers = voice.get("stt_providers", [])
    tts_providers = voice.get("tts_providers", [])

    err.print("[bold green]voice:[/bold green]  enabled")
    err.print(f"  modes:         {', '.join(modes) if modes else '(none)'}")
    err.print(f"  STT providers: {', '.join(stt_providers) if stt_providers else '(none)'}")
    err.print(f"  TTS providers: {', '.join(tts_providers) if tts_providers else '(none)'}")


# ---------------------------------------------------------------------------
# One-shot REST verbs — ``say`` / ``transcribe`` / ``ask`` (ADR 050 D2 / D11)
#
# The request/response parity to the streaming ``voice try``: each drives the
# REST one-shot ``POST /api/v1/agents/{name}/voice`` through ``MovateClient``
# (so the CLI↔API parity gate maps them to that endpoint). ``say`` speaks typed
# text, ``transcribe`` turns an audio file into a transcript, ``ask`` runs a
# full audio→answer→audio turn. All take ``--target`` (the remote runtime).
# ---------------------------------------------------------------------------


def _make_client(base_url: str, api_key: str | None) -> MovateClient:
    """Build a :class:`MovateClient` bound to the target + bearer key."""
    from movate.core.client import MovateClient  # noqa: PLC0415

    return MovateClient(base_url=base_url, api_key=api_key or "")


def _write_audio_out(audio_b64: str | None, out_path: str | None) -> None:
    """Decode a turn's inline audio and either save it or report its size."""
    import base64  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    if not audio_b64:
        err.print("[dim]  (no audio returned)[/dim]")
        return
    raw = base64.b64decode(audio_b64)
    if out_path:
        Path(out_path).write_bytes(raw)
        err.print(
            f"[bold green]✓[/bold green] wrote {len(raw)} bytes of audio → [bold]{out_path}[/bold]"
        )
    else:
        err.print(f"[dim]  ({len(raw)} bytes of audio returned; pass --out to save)[/dim]")


@voice_app.command("say")
def voice_say(
    agent: Annotated[str, typer.Argument(help="Agent name on the target runtime.")],
    text: Annotated[str, typer.Argument(help="Text for the agent to answer / speak.")],
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Runtime URL or config target name."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Bearer key for the runtime (falls back to env vars)."),
    ] = None,
    out: Annotated[
        str | None,
        typer.Option("--out", "-o", help="Write the synthesized answer audio to this file."),
    ] = None,
    tts: Annotated[
        str | None, typer.Option("--tts", help="TTS provider override (e.g. 'cartesia').")
    ] = None,
    voice_id: Annotated[
        str | None, typer.Option("--voice-id", help="TTS voice id override.")
    ] = None,
    mock: Annotated[
        bool, typer.Option("--mock", help="Run the agent stage offline (no live LLM key).")
    ] = False,
) -> None:
    """Speak a one-shot answer: text in → the agent answers → spoken audio out.

    Drives ``POST /api/v1/agents/{name}/voice`` (ADR 050 D2/D11) with the typed
    ``text`` — STT is bypassed server-side, so this is the "have the agent say
    something" path. Prints the agent's answer text and saves/sizes the
    synthesized audio.

    [bold]Examples:[/bold]

      [dim]# Have faq-bot answer a question and save the spoken reply[/dim]
      $ mdk voice say faq-bot "what are your hours?" --out reply.pcm
    """
    import asyncio  # noqa: PLC0415

    asyncio.run(
        _voice_oneshot_async(
            agent=agent,
            base_url=_resolve_target_url(target),
            api_key=_resolve_api_key(api_key),
            text=text,
            audio_path=None,
            out=out,
            tts=tts,
            voice_id=voice_id,
            stt=None,
            audio_out="inline",
            mock=mock,
            show_transcript=False,
        )
    )


@voice_app.command("transcribe")
def voice_transcribe(
    audio: Annotated[str, typer.Argument(help="Path to an audio file to transcribe.")],
    agent: Annotated[
        str,
        typer.Option(
            "--agent",
            "-a",
            help="Agent whose voice route runs the transcription (any agent works).",
        ),
    ] = "default",
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Runtime URL or config target name."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Bearer key for the runtime (falls back to env vars)."),
    ] = None,
    stt: Annotated[
        str | None, typer.Option("--stt", help="STT provider override (e.g. 'deepgram').")
    ] = None,
    language: Annotated[
        str | None, typer.Option("--language", help="STT language hint (e.g. 'en').")
    ] = None,
    codec: Annotated[
        str, typer.Option("--codec", help="Codec of the input audio (pcm16/opus/mulaw).")
    ] = "pcm16",
) -> None:
    """Transcribe an audio file: audio in → transcript out (STT only).

    Drives ``POST /api/v1/agents/{name}/voice?audio=none`` (ADR 050 D2/D11):
    sends the file, prints the recognized transcript, and skips synthesis (no
    answer audio). The agent still runs (a voice turn IS a run, ADR 050 D1), but
    only the transcript is surfaced.

    [bold]Examples:[/bold]

      [dim]# Transcribe a recording[/dim]
      $ mdk voice transcribe call.wav --target https://mdk-prod.example.com
    """
    import asyncio  # noqa: PLC0415

    asyncio.run(
        _voice_oneshot_async(
            agent=agent,
            base_url=_resolve_target_url(target),
            api_key=_resolve_api_key(api_key),
            text=None,
            audio_path=audio,
            out=None,
            tts=None,
            voice_id=None,
            stt=stt,
            language=language,
            codec=codec,
            audio_out="none",
            mock=False,
            show_transcript=True,
            transcript_only=True,
        )
    )


@voice_app.command("ask")
def voice_ask(
    agent: Annotated[str, typer.Argument(help="Agent name on the target runtime.")],
    audio: Annotated[str, typer.Argument(help="Path to an audio file (the spoken question).")],
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Runtime URL or config target name."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Bearer key for the runtime (falls back to env vars)."),
    ] = None,
    out: Annotated[
        str | None,
        typer.Option("--out", "-o", help="Write the synthesized answer audio to this file."),
    ] = None,
    stt: Annotated[str | None, typer.Option("--stt", help="STT provider override.")] = None,
    tts: Annotated[str | None, typer.Option("--tts", help="TTS provider override.")] = None,
    voice_id: Annotated[
        str | None, typer.Option("--voice-id", help="TTS voice id override.")
    ] = None,
    language: Annotated[str | None, typer.Option("--language", help="STT language hint.")] = None,
    codec: Annotated[
        str, typer.Option("--codec", help="Codec of the input audio (pcm16/opus/mulaw).")
    ] = "pcm16",
    mock: Annotated[
        bool, typer.Option("--mock", help="Run the agent stage offline (no live LLM key).")
    ] = False,
) -> None:
    """Ask in one shot: spoken audio in → transcript + answer + spoken audio out.

    The request/response companion to ``voice try`` — drives the REST one-shot
    ``POST /api/v1/agents/{name}/voice`` (ADR 050 D2). Prints the recognized
    transcript and the agent's answer, and saves/sizes the synthesized reply
    audio.

    [bold]Examples:[/bold]

      [dim]# One-shot spoken Q&A against a recorded question[/dim]
      $ mdk voice ask faq-bot question.wav --out answer.pcm
    """
    import asyncio  # noqa: PLC0415

    asyncio.run(
        _voice_oneshot_async(
            agent=agent,
            base_url=_resolve_target_url(target),
            api_key=_resolve_api_key(api_key),
            text=None,
            audio_path=audio,
            out=out,
            tts=tts,
            voice_id=voice_id,
            stt=stt,
            language=language,
            codec=codec,
            audio_out="inline",
            mock=mock,
            show_transcript=True,
        )
    )


async def _voice_oneshot_async(
    *,
    agent: str,
    base_url: str,
    api_key: str | None,
    text: str | None,
    audio_path: str | None,
    out: str | None,
    tts: str | None,
    voice_id: str | None,
    stt: str | None,
    language: str | None = None,
    codec: str = "pcm16",
    audio_out: str,
    mock: bool,
    show_transcript: bool,
    transcript_only: bool = False,
) -> None:
    """Shared async core for ``say`` / ``transcribe`` / ``ask``.

    Reads the audio file (when given), POSTs to the one-shot voice endpoint via
    :class:`MovateClient`, and renders the transcript / answer / audio result.
    Errors (unreachable runtime, missing voice extra → 503, agent 404) are
    surfaced as a clear message + non-zero exit, never a traceback.
    """
    from pathlib import Path  # noqa: PLC0415

    from movate.core.client import MovateClientError  # noqa: PLC0415

    audio_bytes: bytes | None = None
    filename = "audio.wav"
    if audio_path is not None:
        p = Path(audio_path)
        if not p.is_file():
            err.print(f"[red]✗[/red] audio file not found: {audio_path}")
            raise typer.Exit(code=1)
        audio_bytes = p.read_bytes()
        filename = p.name

    client = _make_client(base_url, api_key)
    try:
        try:
            result = await client.voice_oneshot(
                agent=agent,
                audio=audio_bytes,
                text=text,
                filename=filename,
                stt=stt,
                tts=tts,
                voice_id=voice_id,
                language=language,
                codec=codec,
                mock=mock,
                audio_out=audio_out,
            )
        except MovateClientError as exc:
            err.print(f"[red]✗ voice request failed[/red] (HTTP {exc.status_code}): {exc.message}")
            raise typer.Exit(code=1) from exc
        except Exception as exc:
            err.print(f"[red]✗[/red] could not reach {base_url}: {exc}")
            raise typer.Exit(code=1) from exc
    finally:
        await client.aclose()

    if show_transcript:
        err.print(f"[bold cyan]transcript:[/bold cyan] {result.transcript}")
    if not transcript_only:
        err.print(f"[bold green]answer:[/bold green] {result.response_text}")
        _write_audio_out(result.audio_bytes_b64, out)
    if result.status and result.status != "success":
        detail = f": {result.error}" if result.error else ""
        err.print(f"[yellow]status: {result.status}[/yellow]{detail}")


# ---------------------------------------------------------------------------
# ``mdk voice bench`` — reproducible STT/TTS eval harness (ADR 049 D5)
# ---------------------------------------------------------------------------


@voice_app.command("bench")
def voice_bench(
    corpus_dir: Annotated[
        str | None,
        typer.Option(
            "--corpus",
            help="Path to corpus directory with manifest.json (default: tests/voice/corpus).",
        ),
    ] = None,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: 'table' (default) or 'json'."),
    ] = "table",
    save_baseline: Annotated[
        bool,
        typer.Option(
            "--save-baseline",
            help="Write results as voice-bench-baseline.json for future regression checks.",
        ),
    ] = False,
    fail_on_regression: Annotated[
        bool,
        typer.Option(
            "--fail-on-regression",
            help="Compare against voice-bench-baseline.json; exit non-zero on regression.",
        ),
    ] = False,
    baseline_path: Annotated[
        str | None,
        typer.Option(
            "--baseline",
            help="Path to baseline file (default: voice-bench-baseline.json in project root).",
        ),
    ] = None,
    use_fakes: Annotated[
        bool,
        typer.Option(
            "--fakes",
            help="Use in-memory fake providers (for CI / harness validation).",
        ),
    ] = False,
) -> None:
    """Run the voice eval/regression harness over a golden audio corpus.

    Benchmarks configured STT providers on WER + latency, and TTS providers on
    first-byte + total latency. Compares against a saved baseline to catch
    regressions (ADR 049 D5 — standing bake-off).

    [bold]Examples:[/bold]

      [dim]# Run with fake providers (CI / harness validation)[/dim]
      $ mdk voice bench --fakes

      [dim]# Run and save a baseline[/dim]
      $ mdk voice bench --fakes --save-baseline

      [dim]# Run and fail on regression[/dim]
      $ mdk voice bench --fakes --fail-on-regression

      [dim]# JSON output for CI consumption[/dim]
      $ mdk voice bench --fakes --output json
    """
    import asyncio  # noqa: PLC0415

    asyncio.run(
        _voice_bench_async(
            corpus_dir=corpus_dir,
            output=output,
            save_baseline_flag=save_baseline,
            fail_on_regression=fail_on_regression,
            baseline_path=baseline_path,
            use_fakes=use_fakes,
        )
    )


async def _voice_bench_async(
    *,
    corpus_dir: str | None,
    output: str,
    save_baseline_flag: bool,
    fail_on_regression: bool,
    baseline_path: str | None,
    use_fakes: bool,
) -> None:
    """Async core of ``mdk voice bench``."""
    from pathlib import Path  # noqa: PLC0415

    from movate.voice.bench import BenchReport as _BenchReport  # noqa: PLC0415
    from movate.voice.bench import (  # noqa: PLC0415
        bench_stt,
        bench_tts,
        load_audio_chunks,
        load_corpus,
    )
    from movate.voice.bench import compare_to_baseline as _compare  # noqa: PLC0415
    from movate.voice.bench import load_baseline as _load_baseline  # noqa: PLC0415
    from movate.voice.bench import save_baseline as _save_baseline  # noqa: PLC0415

    cdir = Path(corpus_dir) if corpus_dir else Path("tests/voice/corpus")
    if not (cdir / "manifest.json").is_file():
        err.print(f"[red]x[/red] Corpus manifest not found at {cdir / 'manifest.json'}")
        raise typer.Exit(code=1)

    corpus_items = load_corpus(cdir)
    if not corpus_items:
        err.print("[red]x[/red] Corpus is empty.")
        raise typer.Exit(code=1)

    stt_providers, tts_providers = _resolve_bench_providers(use_fakes)

    stt_corpus = [
        (load_audio_chunks(cdir, item.filename), item.expected_transcript) for item in corpus_items
    ]
    tts_phrases = [item.expected_transcript for item in corpus_items]

    report = _BenchReport()
    for stt in stt_providers:
        report.stt_reports.append(await bench_stt(stt, stt_corpus))
    for tts in tts_providers:
        report.tts_reports.append(await bench_tts(tts, tts_phrases))

    _emit_bench_output(report, output)

    bl_path = Path(baseline_path) if baseline_path else Path("voice-bench-baseline.json")
    if save_baseline_flag:
        _save_baseline(report, bl_path)
        err.print(f"\n[bold green]baseline saved:[/bold green] {bl_path}")

    if fail_on_regression:
        _check_regression(report, bl_path, _compare, _load_baseline)


def _resolve_bench_providers(
    use_fakes: bool,
) -> tuple[list[SpeechToTextProvider], list[TextToSpeechProvider]]:
    """Return (stt_providers, tts_providers) for the bench run."""
    if use_fakes:
        from movate.voice.doubles import FakeSTT, FakeTTS  # noqa: PLC0415

        return [FakeSTT(transcript="hello")], [FakeTTS()]
    err.print(
        "[yellow]note:[/yellow] No real providers configured. "
        "Use --fakes for harness validation, or configure STT/TTS providers."
    )
    raise typer.Exit(code=1)


def _emit_bench_output(report: object, output: str) -> None:
    """Render bench results to stderr as a table or JSON."""
    import json as _json  # noqa: PLC0415

    from movate.voice.bench import BenchReport  # noqa: PLC0415

    assert isinstance(report, BenchReport)
    if output == "json":
        err.print(_json.dumps(report.to_dict(), indent=2))
    else:
        _render_bench_table(report)


def _check_regression(
    report: object,
    bl_path: object,
    compare_fn: object,
    load_fn: object,
) -> None:
    """Compare report against baseline and exit non-zero on regression."""
    from pathlib import Path  # noqa: PLC0415

    from movate.voice.bench import BenchReport  # noqa: PLC0415

    assert isinstance(report, BenchReport)
    assert isinstance(bl_path, Path)
    if not bl_path.is_file():
        err.print(
            f"[red]x[/red] Baseline file not found: {bl_path}\n  Run with --save-baseline first."
        )
        raise typer.Exit(code=1)
    baseline = load_fn(bl_path)  # type: ignore[operator]
    regressions = compare_fn(report, baseline)  # type: ignore[operator]
    if regressions:
        err.print(f"\n[red]REGRESSION DETECTED ({len(regressions)}):[/red]")
        for reg in regressions:
            err.print(f"  [red]x[/red] {reg.message}")
        raise typer.Exit(code=1)
    err.print("\n[bold green]No regressions detected.[/bold green]")


def _render_bench_table(report: object) -> None:
    """Render the bench report as a Rich table."""
    from rich.table import Table  # noqa: PLC0415

    from movate.voice.bench import BenchReport  # noqa: PLC0415

    assert isinstance(report, BenchReport)

    if report.stt_reports:
        table = Table(title="STT Benchmark", show_lines=True)
        table.add_column("Provider", style="bold")
        table.add_column("WER", justify="right")
        table.add_column("p50 Latency (ms)", justify="right")
        table.add_column("p95 Latency (ms)", justify="right")
        table.add_column("Samples", justify="right")
        for r in report.stt_reports:
            table.add_row(
                r.provider,
                f"{r.mean_wer:.1%}",
                f"{r.p50_latency_ms:.0f}",
                f"{r.p95_latency_ms:.0f}",
                str(len(r.items)),
            )
        err.print(table)

    if report.tts_reports:
        table = Table(title="TTS Benchmark", show_lines=True)
        table.add_column("Provider", style="bold")
        table.add_column("First-byte (ms)", justify="right")
        table.add_column("Total (ms)", justify="right")
        table.add_column("Samples", justify="right")
        for tr in report.tts_reports:
            table.add_row(
                tr.provider,
                f"{tr.mean_first_byte_ms:.0f}",
                f"{tr.mean_total_ms:.0f}",
                str(len(tr.items)),
            )
        err.print(table)
