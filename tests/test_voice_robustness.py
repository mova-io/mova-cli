"""Voice robustness batch 1 — WS reconnect/resume, per-stage timeouts, abuse guards.

Covers #209, #210, #212 from the robustness backlog.
"""

from __future__ import annotations

import asyncio

import pytest

from movate.voice.abuse_guard import (
    WS_CLOSE_IDLE,
    WS_CLOSE_MAX_DURATION,
    WS_CLOSE_TOO_MANY_SESSIONS,
    ConcurrentSessionTracker,
    IdleTimeoutGuard,
    SessionDurationGuard,
    SessionGuardConfig,
)
from movate.voice.stage_timeout import (
    LLM_TIMEOUT_MSG,
    STT_TIMEOUT_MSG,
    TTS_TIMEOUT_MSG,
    Heartbeat,
    StageTimeouts,
    degradation_for_stage,
)
from movate.voice.ws_resilience import AudioRingBuffer, VoiceSessionResumeState

# ── #209: WS reconnect / resume + audio ring buffer ─────────────────────


class TestAudioRingBuffer:
    """#209 — bounded ring buffer for WS reconnect/resume."""

    def test_push_and_drain(self) -> None:
        buf = AudioRingBuffer(max_seconds=5.0, max_bytes=1024)
        buf.push(b"\x00" * 100)
        buf.push(b"\x01" * 200)
        assert buf.frame_count == 2
        assert buf.total_bytes == 300
        frames = buf.drain()
        assert len(frames) == 2
        assert frames[0] == b"\x00" * 100
        assert frames[1] == b"\x01" * 200
        assert buf.frame_count == 0

    def test_evicts_oldest_when_byte_cap_exceeded(self) -> None:
        buf = AudioRingBuffer(max_seconds=100.0, max_bytes=500)
        buf.push(b"\x00" * 300)
        buf.push(b"\x01" * 300)
        # 300 + 300 = 600 > 500 -> the first frame should be evicted.
        assert buf.total_bytes <= 500
        frames = buf.drain()
        assert len(frames) == 1
        assert frames[0] == b"\x01" * 300

    def test_evicts_oldest_when_duration_cap_exceeded(self) -> None:
        # 16kHz PCM-16: 32000 bytes/sec.  5s = 160000 bytes.
        # Push 3s + 3s -> should evict the first to stay under 5s.
        bytes_per_sec = 16_000 * 2  # 32000
        frame_3s = b"\x00" * (bytes_per_sec * 3)
        buf = AudioRingBuffer(max_seconds=5.0, max_bytes=1_000_000)
        buf.push(frame_3s)
        buf.push(frame_3s)
        # 6s > 5s -> oldest evicted.
        assert buf.frame_count == 1
        assert buf.estimated_seconds <= 5.0

    def test_max_seconds_clamped_to_10(self) -> None:
        buf = AudioRingBuffer(max_seconds=99.0)
        assert buf.max_seconds == 10.0

    def test_flush_clears_everything(self) -> None:
        buf = AudioRingBuffer()
        buf.push(b"\x00" * 100)
        buf.flush()
        assert buf.frame_count == 0
        assert buf.total_bytes == 0
        assert buf.flushed is True

    @pytest.mark.asyncio
    async def test_reconnect_within_window(self) -> None:
        buf = AudioRingBuffer(reconnect_timeout_s=2.0)
        buf.push(b"\x00" * 100)
        buf.mark_disconnected()
        assert buf.is_disconnected is True

        # Simulate reconnect after a short delay.
        async def reconnect_soon() -> None:
            await asyncio.sleep(0.05)
            buf.mark_reconnected()

        task = asyncio.create_task(reconnect_soon())
        ok = await buf.wait_for_reconnect()
        assert ok is True
        # Buffer preserved.
        assert buf.frame_count == 1
        await task

    @pytest.mark.asyncio
    async def test_reconnect_timeout_flushes(self) -> None:
        buf = AudioRingBuffer(reconnect_timeout_s=0.1)
        buf.push(b"\x00" * 100)
        buf.mark_disconnected()
        ok = await buf.wait_for_reconnect()
        assert ok is False
        assert buf.flushed is True
        assert buf.frame_count == 0


class TestVoiceSessionResumeState:
    def test_default_construction(self) -> None:
        state = VoiceSessionResumeState(session_id="s1")
        assert state.session_id == "s1"
        assert state.buffer.frame_count == 0
        assert state.turn_in_progress is False


# ── #210: Per-stage timeouts + partial-failure degradation ───────────────


class TestStageTimeouts:
    """#210 — per-stage timeout configuration."""

    def test_defaults(self) -> None:
        t = StageTimeouts()
        assert t.stt == 30.0
        assert t.llm == 60.0
        assert t.tts == 30.0
        assert t.heartbeat_interval == 5.0

    def test_effective_returns_none_when_disabled(self) -> None:
        t = StageTimeouts(stt=0, llm=-1, tts=0)
        assert t.effective_stt() is None
        assert t.effective_llm() is None
        assert t.effective_tts() is None

    def test_effective_returns_value_when_positive(self) -> None:
        t = StageTimeouts(stt=10, llm=20, tts=15)
        assert t.effective_stt() == 10.0
        assert t.effective_llm() == 20.0
        assert t.effective_tts() == 15.0

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOICE_STT_TIMEOUT_S", "5")
        monkeypatch.setenv("VOICE_LLM_TIMEOUT_S", "10")
        monkeypatch.setenv("VOICE_TTS_TIMEOUT_S", "7")
        monkeypatch.setenv("VOICE_HEARTBEAT_INTERVAL_S", "2")
        t = StageTimeouts.from_env()
        assert t.stt == 5.0
        assert t.llm == 10.0
        assert t.tts == 7.0
        assert t.heartbeat_interval == 2.0


class TestDegradation:
    """#210 — every failure path produces a user-visible message."""

    def test_stt_timeout_message(self) -> None:
        assert degradation_for_stage("stt") == STT_TIMEOUT_MSG

    def test_llm_timeout_message(self) -> None:
        assert degradation_for_stage("llm") == LLM_TIMEOUT_MSG

    def test_tts_timeout_message(self) -> None:
        assert degradation_for_stage("tts") == TTS_TIMEOUT_MSG

    def test_unknown_stage_has_message(self) -> None:
        msg = degradation_for_stage("unknown")
        assert "timed out" in msg


class TestHeartbeat:
    """#210 — "still working" heartbeat cue after silence."""

    @pytest.mark.asyncio
    async def test_heartbeat_fires_after_interval(self) -> None:
        received: list[str] = []

        async def send(stage: str) -> None:
            received.append(stage)

        hb = Heartbeat(send_fn=send, interval_s=0.05)
        hb.start("stt")
        await asyncio.sleep(0.18)
        hb.stop()
        # Should have fired 2-3 times in 0.18s with 0.05s interval.
        assert len(received) >= 2
        assert all(s == "stt" for s in received)

    @pytest.mark.asyncio
    async def test_heartbeat_stop_is_idempotent(self) -> None:
        async def noop(s: str) -> None:
            return

        hb = Heartbeat(send_fn=noop, interval_s=1.0)
        hb.stop()  # never started -- should not raise
        hb.start("llm")
        hb.stop()
        hb.stop()  # double-stop -- should not raise


class TestStageTimeoutSimulation:
    """#210 — mock a slow STT and assert timeout fires + graceful message."""

    @pytest.mark.asyncio
    async def test_slow_stt_timeout_fires(self) -> None:
        """Simulate an STT call that takes too long; verify timeout + message."""
        timeouts = StageTimeouts(stt=0.1)

        async def slow_stt() -> str:
            await asyncio.sleep(10)
            return "should never reach this"

        timed_out = False
        message = ""
        try:
            await asyncio.wait_for(slow_stt(), timeouts.effective_stt())
        except TimeoutError:
            timed_out = True
            message = degradation_for_stage("stt")

        assert timed_out is True
        assert message == STT_TIMEOUT_MSG
        assert "timed out" in message.lower()


# ── #212: Voice WS abuse / cost guards ──────────────────────────────────


class TestSessionGuardConfig:
    """#212 — guard configuration."""

    def test_defaults(self) -> None:
        cfg = SessionGuardConfig()
        assert cfg.max_duration_s == 1800.0
        assert cfg.idle_timeout_s == 120.0
        assert cfg.max_concurrent_sessions == 3

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOICE_MAX_SESSION_DURATION_S", "600")
        monkeypatch.setenv("VOICE_IDLE_TIMEOUT_S", "30")
        monkeypatch.setenv("VOICE_MAX_CONCURRENT_SESSIONS", "5")
        cfg = SessionGuardConfig.from_env()
        assert cfg.max_duration_s == 600.0
        assert cfg.idle_timeout_s == 30.0
        assert cfg.max_concurrent_sessions == 5


class TestSessionDurationGuard:
    """#212 — max session duration -> clean close."""

    @pytest.mark.asyncio
    async def test_expires_after_max_duration(self) -> None:
        closed_with: list[tuple[int, str]] = []

        async def close_fn(code: int, reason: str) -> None:
            closed_with.append((code, reason))

        guard = SessionDurationGuard(close_fn=close_fn, max_duration_s=0.1)
        guard.start()
        await asyncio.sleep(0.25)
        guard.stop()

        assert guard.expired is True
        assert len(closed_with) == 1
        assert closed_with[0][0] == WS_CLOSE_MAX_DURATION
        assert "maximum duration" in closed_with[0][1].lower()

    @pytest.mark.asyncio
    async def test_stop_before_expiry(self) -> None:
        closed_with: list[tuple[int, str]] = []

        async def close_fn(code: int, reason: str) -> None:
            closed_with.append((code, reason))

        guard = SessionDurationGuard(close_fn=close_fn, max_duration_s=10.0)
        guard.start()
        guard.stop()
        await asyncio.sleep(0.05)
        assert guard.expired is False
        assert len(closed_with) == 0


class TestIdleTimeoutGuard:
    """#212 — idle timeout -> clean close."""

    @pytest.mark.asyncio
    async def test_expires_when_idle(self) -> None:
        closed_with: list[tuple[int, str]] = []

        async def close_fn(code: int, reason: str) -> None:
            closed_with.append((code, reason))

        guard = IdleTimeoutGuard(close_fn=close_fn, idle_timeout_s=0.1)
        guard.start()
        await asyncio.sleep(0.3)
        guard.stop()

        assert guard.expired is True
        assert len(closed_with) == 1
        assert closed_with[0][0] == WS_CLOSE_IDLE
        assert "inactivity" in closed_with[0][1].lower()

    @pytest.mark.asyncio
    async def test_activity_resets_timer(self) -> None:
        closed_with: list[tuple[int, str]] = []

        async def close_fn(code: int, reason: str) -> None:
            closed_with.append((code, reason))

        guard = IdleTimeoutGuard(close_fn=close_fn, idle_timeout_s=0.15)
        guard.start()
        # Keep poking it before the timeout.
        for _ in range(5):
            await asyncio.sleep(0.05)
            guard.note_activity()
        guard.stop()
        assert guard.expired is False
        assert len(closed_with) == 0


class TestConcurrentSessionTracker:
    """#212 — per-key concurrent session limit."""

    def test_allows_up_to_max(self) -> None:
        tracker = ConcurrentSessionTracker(max_concurrent=3)
        assert tracker.try_acquire("key1") is True
        assert tracker.try_acquire("key1") is True
        assert tracker.try_acquire("key1") is True
        assert tracker.active_count("key1") == 3

    def test_rejects_over_max(self) -> None:
        tracker = ConcurrentSessionTracker(max_concurrent=3)
        for _ in range(3):
            tracker.try_acquire("key1")
        # 4th should be rejected.
        assert tracker.try_acquire("key1") is False
        assert tracker.active_count("key1") == 3

    def test_release_frees_slot(self) -> None:
        tracker = ConcurrentSessionTracker(max_concurrent=2)
        tracker.try_acquire("key1")
        tracker.try_acquire("key1")
        assert tracker.try_acquire("key1") is False
        tracker.release("key1")
        assert tracker.try_acquire("key1") is True

    def test_keys_are_independent(self) -> None:
        tracker = ConcurrentSessionTracker(max_concurrent=1)
        assert tracker.try_acquire("key1") is True
        assert tracker.try_acquire("key2") is True
        assert tracker.try_acquire("key1") is False
        assert tracker.try_acquire("key2") is False

    def test_release_nonexistent_is_safe(self) -> None:
        tracker = ConcurrentSessionTracker(max_concurrent=3)
        tracker.release("nonexistent")  # should not raise

    def test_ws_close_codes_are_in_app_range(self) -> None:
        """RFC 6455 7.4.2: application codes are 4000-4999."""
        assert 4000 <= WS_CLOSE_TOO_MANY_SESSIONS <= 4999
        assert 4000 <= WS_CLOSE_MAX_DURATION <= 4999
        assert 4000 <= WS_CLOSE_IDLE <= 4999
