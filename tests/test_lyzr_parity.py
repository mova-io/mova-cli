"""Tests for :mod:`movate.voice.lyzr_parity` — the Lyzr-discovery parity check.

Fixtures here mirror the *real* shape Lyzr returns (verified against the live
endpoints once and frozen): ``providerId`` / ``displayName`` / ``models[].id``
for pipeline-options, ``providers[]`` for realtime-options. The whole module is
exercised without touching the network — :func:`fetch_lyzr_voice_options` is
stitched together via monkeypatch.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from movate.voice import lyzr_parity
from movate.voice.lyzr_parity import (
    LyzrProvider,
    check_lyzr_parity,
    check_parity,
    fetch_lyzr_voice_options,
    format_parity_report,
)

# A trimmed snapshot of the live Lyzr menu — only the fields the parser reads.
PIPELINE_FIXTURE: dict[str, Any] = {
    "stt": [
        {
            "providerId": "deepgram",
            "displayName": "Deepgram",
            "models": [{"id": "deepgram/nova-3:en"}, {"id": "deepgram/flux-general:en"}],
        },
        {
            "providerId": "assemblyai",
            "displayName": "AssemblyAI",
            "models": [{"id": "assemblyai/u3-rt-pro:en"}],
        },
        {
            "providerId": "sarvam",
            "displayName": "Sarvam",
            "models": [{"id": "sarvam/saarika:v2.5"}],
        },
    ],
    "llm": [
        {"providerId": "openai", "displayName": "OpenAI", "models": [{"id": "openai/gpt-4o-mini"}]},
        {
            "providerId": "google",
            "displayName": "Google Gemini",
            "models": [{"id": "google/gemini-2.5-flash"}],
        },
    ],
    "tts": [
        {
            "providerId": "cartesia",
            "displayName": "Cartesia",
            "models": [{"id": "cartesia/sonic-3"}],
        },
        {
            "providerId": "elevenlabs",
            "displayName": "ElevenLabs",
            "models": [{"id": "elevenlabs/eleven_flash_v2"}],
        },
        {"providerId": "rime", "displayName": "Rime", "models": [{"id": "rime/arcana"}]},
    ],
}

REALTIME_FIXTURE: dict[str, Any] = {
    "providers": [
        {
            "providerId": "openai",
            "displayName": "OpenAI",
            "models": [{"id": "gpt-realtime"}, {"id": "gpt-realtime-2"}],
        },
        {"providerId": "ultravox", "displayName": "Ultravox", "models": [{"id": "ultravox/v0_3"}]},
    ],
}


def test_pipeline_parser_normalizes_three_kinds() -> None:
    """``stt`` / ``llm`` / ``tts`` arrays all flatten into one ``LyzrProvider`` list."""
    providers = lyzr_parity._parse_pipeline(PIPELINE_FIXTURE)
    kinds = {p.kind for p in providers}
    assert kinds == {"stt", "llm", "tts"}
    # Field round-trip on a representative entry.
    dg = next(p for p in providers if p.provider_id == "deepgram")
    assert dg == LyzrProvider("stt", "deepgram", "Deepgram", 2)


def test_realtime_parser_handles_top_level_providers_key() -> None:
    """Realtime endpoint nests under ``providers`` (different shape from pipeline)."""
    providers = lyzr_parity._parse_realtime(REALTIME_FIXTURE)
    assert {p.provider_id for p in providers} == {"openai", "ultravox"}
    assert all(p.kind == "realtime" for p in providers)


def test_check_parity_separates_covered_from_gaps() -> None:
    """Adapters we ship → ``covered``; everything else → ``gaps``."""
    providers = lyzr_parity._parse_pipeline(PIPELINE_FIXTURE) + lyzr_parity._parse_realtime(
        REALTIME_FIXTURE
    )
    report = check_parity(providers)
    covered_ids = {(p.kind, p.provider_id) for p in report.covered}
    gap_ids = {(p.kind, p.provider_id) for p in report.gaps}
    # Should-be-covered (have adapters or OpenAI-compat-via-lyzr-v4 route).
    assert ("stt", "deepgram") in covered_ids
    assert ("tts", "cartesia") in covered_ids
    assert ("tts", "elevenlabs") in covered_ids
    assert ("llm", "openai") in covered_ids  # via OpenAIChatAgent
    assert ("llm", "google") in covered_ids  # via Lyzr /v4 OpenAI-compat
    assert ("realtime", "openai") in covered_ids
    # Known gaps in the default mapping today.
    assert ("stt", "assemblyai") in gap_ids
    assert ("stt", "sarvam") in gap_ids
    assert ("tts", "rime") in gap_ids
    assert ("realtime", "ultravox") in gap_ids


def test_coverage_pct_and_is_parity() -> None:
    """``coverage_pct`` is a float 0-100; ``is_parity`` iff no gaps."""
    report = check_parity(
        [
            LyzrProvider("stt", "deepgram", "Deepgram", 1),
            LyzrProvider("stt", "assemblyai", "AssemblyAI", 1),
        ]
    )
    assert report.coverage_pct == 50.0
    assert not report.is_parity
    # Empty inputs → vacuously parity, 100% coverage (no providers to fail on).
    empty = check_parity([])
    assert empty.is_parity
    assert empty.coverage_pct == 100.0


def test_check_parity_accepts_custom_mapping_for_what_if() -> None:
    """A custom mapping lets you simulate "if we added an X adapter, would we be at parity?"."""
    providers = [LyzrProvider("stt", "assemblyai", "AssemblyAI", 1)]
    # With the default mapping, AssemblyAI is a gap.
    assert check_parity(providers).gaps == (providers[0],)
    # Stub in an adapter and it flips to covered.
    custom = {"stt": {"assemblyai": "assemblyai_stt_hypothetical"}}
    report = check_parity(providers, mapping=custom)  # type: ignore[arg-type]
    assert report.is_parity
    assert report.covered == (providers[0],)


def test_unknown_kind_in_mapping_falls_through_to_gap() -> None:
    """A provider whose ``kind`` isn't in the mapping at all is treated as a gap, not crash."""
    custom: dict = {}  # no entries at all
    report = check_parity([LyzrProvider("stt", "deepgram", "Deepgram", 1)], mapping=custom)
    assert report.gaps and not report.covered


def test_format_parity_report_renders_both_sections() -> None:
    """Human-readable output names both buckets and includes the coverage header."""
    report = check_parity(
        [
            LyzrProvider("stt", "deepgram", "Deepgram", 1),
            LyzrProvider("stt", "assemblyai", "AssemblyAI", 1),
        ]
    )
    text = format_parity_report(report)
    assert "1/2" in text and "50%" in text
    assert "Covered" in text and "deepgram" in text
    assert "Gaps" in text and "assemblyai" in text


def test_format_parity_report_omits_empty_sections() -> None:
    """When there are no gaps, the report doesn't print an empty ``Gaps:`` heading."""
    text = format_parity_report(check_parity([LyzrProvider("stt", "deepgram", "Deepgram", 1)]))
    assert "Gaps" not in text
    assert "Covered" in text


def test_fetch_lyzr_voice_options_monkeypatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end fetch path with ``_http_get_json`` stubbed — no network involved."""

    def fake_get(url: str, *, api_key: str, timeout: float) -> dict[str, Any]:
        assert api_key == "fake-key"
        assert timeout == 5.0
        if url.endswith("/pipeline-options"):
            return PIPELINE_FIXTURE
        if url.endswith("/realtime-options"):
            return REALTIME_FIXTURE
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(lyzr_parity, "_http_get_json", fake_get)
    providers = asyncio.run(fetch_lyzr_voice_options(api_key="fake-key"))
    # Pipeline (3 STT + 2 LLM + 3 TTS = 8) plus realtime (2) = 10.
    assert len(providers) == 10
    assert any(p.kind == "realtime" and p.provider_id == "openai" for p in providers)


def test_check_lyzr_parity_chains_fetch_plus_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one-call convenience returns a :class:`ParityReport` end-to-end."""
    monkeypatch.setattr(
        lyzr_parity,
        "_http_get_json",
        lambda url, *, api_key, timeout: (
            PIPELINE_FIXTURE if url.endswith("/pipeline-options") else REALTIME_FIXTURE
        ),
    )
    report = asyncio.run(check_lyzr_parity(api_key="fake-key"))
    assert not report.is_parity  # default mapping has known gaps
    assert any(p.provider_id == "deepgram" for p in report.covered)
    assert any(p.provider_id == "rime" for p in report.gaps)


def test_fixtures_are_valid_json() -> None:
    """Catches a fixture typo that would silently make every other test pass."""
    json.dumps(PIPELINE_FIXTURE)
    json.dumps(REALTIME_FIXTURE)
