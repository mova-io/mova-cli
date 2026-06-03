"""Voice-shaped output: the speakify() normalizer + the pipeline text_filter hook."""

from __future__ import annotations

from collections.abc import AsyncIterator

from movate.voice import (
    AudioChunk,
    FakeAgentTurn,
    FakeSTT,
    FakeTTS,
    run_voice_pipeline,
    speakify,
)

# --- speakify (pure) -------------------------------------------------------


def test_speakify_strips_emphasis_and_inline_code() -> None:
    assert speakify("This is **bold** and *italic* and `code`.") == (
        "This is bold and italic and code."
    )


def test_speakify_unwraps_links_and_images() -> None:
    assert speakify("See [the docs](https://x.io) now.") == "See the docs now."
    assert speakify("![a cat](cat.png) is cute") == "a cat is cute"


def test_speakify_removes_headings_bullets_and_quotes() -> None:
    md = "# Title\n\n- first item\n- second item\n\n> a quote\n"
    assert speakify(md) == "Title first item second item a quote"


def test_speakify_drops_fenced_code_blocks() -> None:
    md = "Run this:\n```python\nprint('hi')\n```\nthen done."
    assert speakify(md) == "Run this: then done."


def test_speakify_expands_safe_symbols() -> None:
    assert speakify("Tom & Jerry") == "Tom and Jerry"
    assert speakify("up 50% today") == "up 50 percent today"
    assert speakify("it costs $5 total") == "it costs 5 dollars total"


def test_speakify_collapses_whitespace_and_handles_empty() -> None:
    assert speakify("a\n\n\n   b\t c") == "a b c"
    assert speakify("   ") == ""
    assert speakify("") == ""


def test_speakify_numbered_lists() -> None:
    assert speakify("1. one\n2. two\n3. three") == "one two three"


# --- text_filter in the pipeline -------------------------------------------


async def _audio() -> AsyncIterator[AudioChunk]:
    yield AudioChunk(data=b"x")


async def _spoken(tts: FakeTTS, *, streaming: bool, answer: str) -> list[str]:
    agent = FakeAgentTurn(answer)
    async for _ in run_voice_pipeline(
        audio_in=_audio(),
        stt=FakeSTT("hi"),
        tts=tts,
        agent=agent,
        text_filter=speakify,
        tts_streaming=streaming,
    ):
        pass
    return tts.spoken


async def test_text_filter_applied_sequential() -> None:
    tts = FakeTTS()
    spoken = await _spoken(tts, streaming=False, answer="Here is **bold** text.")
    assert spoken == ["Here is bold text."]


async def test_text_filter_applied_streaming_per_sentence() -> None:
    tts = FakeTTS()
    spoken = await _spoken(tts, streaming=True, answer="**Bold** one. `code` two.")
    # Markdown stripped from each streamed sentence.
    assert spoken == ["Bold one.", "code two."]
