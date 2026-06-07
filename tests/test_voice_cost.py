"""Tests for the voice cost computation (ADR 050 D7 — three-stage cost)."""

from __future__ import annotations

from movate.voice.cost import VoiceTurnCost, compute_voice_turn_cost


def test_compute_voice_turn_cost_defaults() -> None:
    """With default provider rates, cost is audio_duration * stt_rate + chars * tts_rate + llm."""
    cost = compute_voice_turn_cost(
        audio_duration_s=5.0,
        answer_chars=200,
        llm_cost_usd=0.001,
    )
    assert isinstance(cost, VoiceTurnCost)
    assert cost.stt_cost_usd > 0.0
    assert cost.tts_cost_usd > 0.0
    assert cost.llm_cost_usd == 0.001
    assert cost.total_cost_usd == cost.stt_cost_usd + cost.tts_cost_usd + cost.llm_cost_usd


def test_compute_voice_turn_cost_zero_audio() -> None:
    """Zero-duration audio → zero STT cost."""
    cost = compute_voice_turn_cost(
        audio_duration_s=0.0,
        answer_chars=100,
        llm_cost_usd=0.0,
    )
    assert cost.stt_cost_usd == 0.0
    assert cost.tts_cost_usd > 0.0


def test_compute_voice_turn_cost_zero_chars() -> None:
    """Zero answer chars → zero TTS cost."""
    cost = compute_voice_turn_cost(
        audio_duration_s=3.0,
        answer_chars=0,
        llm_cost_usd=0.0,
    )
    assert cost.stt_cost_usd > 0.0
    assert cost.tts_cost_usd == 0.0


def test_compute_voice_turn_cost_env_override(monkeypatch) -> None:
    """Operator-configured rates override defaults."""
    monkeypatch.setenv("VOICE_COST_PER_STT_SECOND", "0.01")
    monkeypatch.setenv("VOICE_COST_PER_TTS_CHAR", "0.0001")
    cost = compute_voice_turn_cost(
        audio_duration_s=10.0,
        answer_chars=100,
        llm_cost_usd=0.0,
    )
    assert cost.stt_cost_usd == 0.1  # 10 * 0.01
    assert cost.tts_cost_usd == 0.01  # 100 * 0.0001
    assert cost.total_cost_usd == 0.11


def test_total_cost_property() -> None:
    """The total_cost_usd property sums all three stages."""
    cost = VoiceTurnCost(stt_cost_usd=0.03, tts_cost_usd=0.003, llm_cost_usd=0.001)
    assert cost.total_cost_usd == 0.034
