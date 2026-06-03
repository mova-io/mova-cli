"""``mdk voice say`` / ``transcribe`` / ``ask`` — the one-shot REST CLI verbs.

Unit-level coverage of the three verbs ADR 050 D11 maps to
``POST /api/v1/agents/{name}/voice``. The runtime is mocked at the
:class:`MovateClient.voice_oneshot` boundary (no server / network / SDK) so the
tests assert: the verbs are registered + parse flags, the file is read and
forwarded, ``text`` vs ``audio`` is routed correctly, and the rendered output
shows the transcript / answer / saved audio. The endpoint itself is covered by
``tests/test_runtime_voice_rest.py``.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from movate.cli.voice_cmd import voice_app
from movate.core.client import MovateClientError
from movate.runtime.schemas import VoiceTurnView


def _flat_help(output: str) -> str:
    """Flatten Rich --help output for CI-robust substring matching.

    CI runs non-TTY with FORCE_COLOR=1, so Rich styles ``--`` and the flag name
    as separate ANSI spans and wraps/pads rows — a raw substring search misses
    ``--target``. Strip ANSI, then collapse whitespace so a wrapped/styled flag
    flattens to one searchable string. (Same fix as test_voice_cli_ux._help_text.)
    """
    plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)
    return " ".join(plain.split())


def _turn(**over: object) -> VoiceTurnView:
    base: dict[str, object] = {
        "transcript": "turn the lights on",
        "response_text": "Sure, turning the lights on.",
        "audio_bytes_b64": base64.b64encode(b"FAKEAUDIO").decode("ascii"),
        "audio_codec": "pcm16",
        "audio_sample_rate": 24_000,
        "run_id": "run-123",
        "status": "success",
        "error": None,
    }
    base.update(over)
    return VoiceTurnView(**base)  # type: ignore[arg-type]


def _runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Registration + help
# ---------------------------------------------------------------------------


def test_oneshot_verbs_registered() -> None:
    runner = _runner()
    for verb in ("say", "transcribe", "ask"):
        result = runner.invoke(voice_app, [verb, "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
    # Only the remote one-shot `ask` targets a deployed runtime; `say` (local
    # TTS) and `transcribe` (local STT) run against the local voice pipeline and
    # take no `--target`. (Remote-verb→route mapping is covered by the parity gate.)
    ask_help = runner.invoke(voice_app, ["ask", "--help"], env={"COLUMNS": "200"})
    assert "--target" in _flat_help(ask_help.output), ask_help.output


# ---------------------------------------------------------------------------
# ``voice say`` — text in → spoken answer
# ---------------------------------------------------------------------------


def test_voice_say_sends_text_and_saves_audio(tmp_path: Path) -> None:
    out = tmp_path / "reply.pcm"
    mock_call = AsyncMock(return_value=_turn())
    with (
        patch("movate.core.client.MovateClient.voice_oneshot", mock_call),
        patch("movate.core.client.MovateClient.aclose", AsyncMock()),
    ):
        result = _runner().invoke(
            voice_app,
            ["say", "faq-bot", "what are your hours?", "--target", "http://rt", "--out", str(out)],
        )
    assert result.exit_code == 0, result.output
    # The text was forwarded, NOT audio (the say path bypasses STT server-side).
    kwargs = mock_call.await_args.kwargs
    assert kwargs["agent"] == "faq-bot"
    assert kwargs["text"] == "what are your hours?"
    assert kwargs["audio"] is None
    # The synthesized audio was written to --out.
    assert out.read_bytes() == b"FAKEAUDIO"


# ---------------------------------------------------------------------------
# ``voice transcribe`` — audio file in → transcript out
# ---------------------------------------------------------------------------


def test_voice_transcribe_reads_file_and_prints_transcript(tmp_path: Path) -> None:
    clip = tmp_path / "call.wav"
    clip.write_bytes(b"\x00\x01\x02\x03")
    mock_call = AsyncMock(return_value=_turn())
    with (
        patch("movate.core.client.MovateClient.voice_oneshot", mock_call),
        patch("movate.core.client.MovateClient.aclose", AsyncMock()),
    ):
        result = _runner().invoke(
            voice_app, ["transcribe", str(clip), "--target", "http://rt", "--agent", "faq-bot"]
        )
    assert result.exit_code == 0, result.output
    kwargs = mock_call.await_args.kwargs
    # The file bytes were read + forwarded, and audio_out is "none" (STT only).
    assert kwargs["audio"] == b"\x00\x01\x02\x03"
    assert kwargs["text"] is None
    assert kwargs["audio_out"] == "none"


def test_voice_transcribe_missing_file_errors(tmp_path: Path) -> None:
    mock_call = AsyncMock(return_value=_turn())
    with patch("movate.core.client.MovateClient.voice_oneshot", mock_call):
        result = _runner().invoke(
            voice_app, ["transcribe", str(tmp_path / "nope.wav"), "--target", "http://rt"]
        )
    assert result.exit_code == 1
    assert not mock_call.await_count  # never hit the network


# ---------------------------------------------------------------------------
# ``voice ask`` — audio in → transcript + answer + audio out
# ---------------------------------------------------------------------------


def test_voice_ask_full_turn(tmp_path: Path) -> None:
    clip = tmp_path / "q.wav"
    clip.write_bytes(b"\xaa\xbb")
    out = tmp_path / "ans.pcm"
    mock_call = AsyncMock(return_value=_turn())
    with (
        patch("movate.core.client.MovateClient.voice_oneshot", mock_call),
        patch("movate.core.client.MovateClient.aclose", AsyncMock()),
    ):
        result = _runner().invoke(
            voice_app,
            ["ask", "faq-bot", str(clip), "--target", "http://rt", "--out", str(out)],
        )
    assert result.exit_code == 0, result.output
    kwargs = mock_call.await_args.kwargs
    assert kwargs["agent"] == "faq-bot"
    assert kwargs["audio"] == b"\xaa\xbb"
    assert kwargs["audio_out"] == "inline"
    assert out.read_bytes() == b"FAKEAUDIO"
    # Both transcript and answer are surfaced to the user.
    assert "turn the lights on" in result.output
    assert "Sure, turning the lights on." in result.output


def test_voice_ask_surfaces_client_error(tmp_path: Path) -> None:
    clip = tmp_path / "q.wav"
    clip.write_bytes(b"\xaa")
    err = MovateClientError(status_code=503, code="unavailable", message="voice extra missing")
    with (
        patch("movate.core.client.MovateClient.voice_oneshot", AsyncMock(side_effect=err)),
        patch("movate.core.client.MovateClient.aclose", AsyncMock()),
    ):
        result = _runner().invoke(voice_app, ["ask", "faq-bot", str(clip), "--target", "http://rt"])
    assert result.exit_code == 1
    assert "503" in result.output
