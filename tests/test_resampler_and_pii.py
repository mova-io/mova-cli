"""Anti-aliased downsampling + PII redaction on the observability surface."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    FakeAgentTurn,
    FakeSTT,
    FakeTTS,
    redact_pii,
    resample_pcm16,
    run_voice_pipeline,
)


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    s = struct.unpack(f"<{n}h", pcm)
    return (sum(x * x for x in s) / n) ** 0.5


# --- anti-aliased resampling ----------------------------------------------


def test_downsample_attenuates_high_frequency() -> None:
    # ±8000 alternating @ 24 kHz = a 12 kHz tone, far above the 8 kHz Nyquist
    # (4 kHz). A naive linear resample would alias it into the band; the low-pass
    # must crush it.
    hi = struct.pack("<240h", *([8000, -8000] * 120))
    out = resample_pcm16(hi, 24_000, 8_000)
    assert _rms(out) < 2000  # strongly attenuated from ~8000


def test_downsample_preserves_low_frequency() -> None:
    dc = struct.pack("<240h", *([5000] * 240))
    out = resample_pcm16(dc, 24_000, 8_000)
    samples = struct.unpack(f"<{len(out) // 2}h", out)
    assert abs(samples[-1] - 5000) < 200  # DC passes the low-pass


def test_upsample_length_and_noop() -> None:
    pcm = struct.pack("<10h", *range(10))
    assert len(resample_pcm16(pcm, 8_000, 16_000)) // 2 == 20
    assert resample_pcm16(pcm, 8_000, 8_000) == pcm


# --- PII redaction (pure) --------------------------------------------------


def test_redact_pii_patterns() -> None:
    assert "[redacted]" in redact_pii("reach me at jane.doe@acme.io")
    assert redact_pii("card 4111 1111 1111 1111 ok") == "card [redacted] ok"
    assert redact_pii("ssn 123-45-6789") == "ssn [redacted]"
    assert "[redacted]" in redact_pii("call +1 415 555 1234")
    assert redact_pii("account 9001234") == "account [redacted]"
    assert redact_pii("nothing to see here") == "nothing to see here"


def test_redact_pii_irregular_card_groupings() -> None:
    # The literal string the LIVE demo produced: Whisper invented uneven groups
    # and the old 4-4-4-4 regex missed it. Now the whole digit-with-separators
    # span is masked.
    out = redact_pii("My card number is 4111-222-2333-334-444.")
    assert "4111" not in out and "[redacted]" in out
    # Other irregular groupings.
    assert "[redacted]" in redact_pii("4111.1111.1111.1111")
    assert "[redacted]" in redact_pii("411 122 22333 334 4445")


def test_redact_pii_doesnt_eat_short_lists() -> None:
    # A list of small numbers ("step 1 of 3" etc.) must not be redacted.
    assert redact_pii("step 1 of 3") == "step 1 of 3"
    assert redact_pii("turn 1 ended") == "turn 1 ended"


# --- pipeline hook: emit redacted, agent gets raw --------------------------


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"x")


async def test_pipeline_redacts_emitted_transcript_but_agent_gets_raw() -> None:
    raw = "my card is 4111 1111 1111 1111"
    agent = FakeAgentTurn("ok")
    events = [
        e
        async for e in run_voice_pipeline(
            audio_in=_audio(),
            stt=FakeSTT(raw),
            agent=agent,
            tts=FakeTTS(),
            pii_redactor=redact_pii,
        )
    ]
    final = next(e for e in events if e.kind == "transcript.final")
    assert "[redacted]" in final.text  # what gets logged/shown is masked
    assert "4111" not in final.text
    assert agent.prompts == [raw]  # the agent still received the REAL transcript
