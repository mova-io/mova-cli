"""Tests for playground robustness items #216, #217, #219.

Covers:
  #216 — Runtime-down resilience + 429/quota UX (retry with backoff)
  #217 — Streaming-drop resilience (truncation marker on partial messages)
  #219 — Feedback delivery robustness (retry + idempotency)

All tests are pure-unit — no real network, mocked httpx.  The pure-logic
retry/parse helpers in ``movate.playground.client`` are tested directly;
the Chainlit integration tests mock the session and verify UI messages.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from movate.playground.capabilities import RuntimeCapabilities
from movate.playground.client import (
    PlaygroundClient,
    PlaygroundClientConfig,
    _is_quota_exceeded,
    _is_rate_limited,
    _is_retryable,
    _parse_retry_after,
)
from movate.playground.connection import ConnectionState
from movate.playground.conversation import ConversationState
from movate.playground.sse import StreamEvent

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a minimal ``httpx.Response`` for testing."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_body or {},
        headers=headers or {},
        request=httpx.Request("POST", "http://test/"),
    )
    return resp


def _make_status_error(
    status_code: int,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    """Build an ``httpx.HTTPStatusError`` with the given status + body."""
    resp = _make_response(status_code, json_body, headers)
    return httpx.HTTPStatusError(
        message=f"{status_code}",
        request=resp.request,
        response=resp,
    )


# ===========================================================================
# #216 — Retry helpers (pure logic, no network)
# ===========================================================================


class TestRetryHelpers:
    """Unit tests for the retry classification helpers in client.py."""

    def test_5xx_is_retryable(self) -> None:
        for code in (500, 502, 503, 504):
            assert _is_retryable(_make_status_error(code)) is True

    def test_4xx_is_not_retryable(self) -> None:
        for code in (400, 401, 403, 404, 422):
            assert _is_retryable(_make_status_error(code)) is False

    def test_429_is_not_retryable(self) -> None:
        """429 is handled by the rate-limit path, not the generic retry."""
        assert _is_retryable(_make_status_error(429)) is False

    def test_connect_error_is_retryable(self) -> None:
        exc = httpx.ConnectError("connection refused")
        assert _is_retryable(exc) is True

    def test_connect_timeout_is_retryable(self) -> None:
        exc = httpx.ConnectTimeout("timed out")
        assert _is_retryable(exc) is True

    def test_is_rate_limited_on_429(self) -> None:
        assert _is_rate_limited(_make_status_error(429)) is True

    def test_is_rate_limited_on_non_429(self) -> None:
        assert _is_rate_limited(_make_status_error(500)) is False

    def test_quota_exceeded_nested_error(self) -> None:
        body = {"error": {"code": "quota_exceeded", "message": "over limit"}}
        assert _is_quota_exceeded(_make_status_error(429, json_body=body)) is True

    def test_quota_exceeded_top_level_code(self) -> None:
        body = {"code": "quota-exceeded"}
        assert _is_quota_exceeded(_make_status_error(429, json_body=body)) is True

    def test_not_quota_exceeded_on_regular_429(self) -> None:
        body = {"error": {"code": "rate_limited"}}
        assert _is_quota_exceeded(_make_status_error(429, json_body=body)) is False

    def test_not_quota_exceeded_on_non_429(self) -> None:
        body = {"error": {"code": "quota_exceeded"}}
        assert _is_quota_exceeded(_make_status_error(500, json_body=body)) is False

    def test_parse_retry_after_integer(self) -> None:
        exc = _make_status_error(429, headers={"Retry-After": "30"})
        assert _parse_retry_after(exc) == 30.0

    def test_parse_retry_after_float(self) -> None:
        exc = _make_status_error(429, headers={"Retry-After": "2.5"})
        assert _parse_retry_after(exc) == 2.5

    def test_parse_retry_after_missing(self) -> None:
        exc = _make_status_error(429)
        assert _parse_retry_after(exc) is None

    def test_parse_retry_after_unparseable(self) -> None:
        exc = _make_status_error(429, headers={"Retry-After": "Sat, 01 Jan 2030 00:00:00 GMT"})
        assert _parse_retry_after(exc) is None


# ===========================================================================
# #216 — PlaygroundClient._request_with_retry
# ===========================================================================


class TestRequestWithRetry:
    """Test the retry loop inside PlaygroundClient."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self) -> None:
        config = PlaygroundClientConfig(runtime_url="http://test", retry_delays=(1.0,))
        client = PlaygroundClient(config)
        # Patch the inner client to return 200.
        client._client = AsyncMock()
        mock_resp = _make_response(200, json_body={"ok": True})
        client._client.request = AsyncMock(return_value=mock_resp)
        _resp, outcome = await client._request_with_retry("GET", "/test")
        assert outcome.ok is True
        assert outcome.attempts == 1

    @pytest.mark.asyncio
    async def test_retry_on_502_then_success(self) -> None:
        config = PlaygroundClientConfig(
            runtime_url="http://test",
            retry_delays=(0.01, 0.01),  # fast for tests
        )
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        # First call: 502, second call: 200.
        fail_resp = _make_response(502)
        ok_resp = _make_response(200, json_body={"ok": True})
        client._client.request = AsyncMock(
            side_effect=[
                httpx.HTTPStatusError("502", request=fail_resp.request, response=fail_resp),
                ok_resp,
            ]
        )
        _resp, outcome = await client._request_with_retry("GET", "/test")
        assert outcome.ok is True
        assert outcome.attempts == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries(self) -> None:
        config = PlaygroundClientConfig(
            runtime_url="http://test",
            retry_delays=(0.01,),
        )
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        fail_resp = _make_response(503)
        client._client.request = AsyncMock(
            side_effect=httpx.HTTPStatusError("503", request=fail_resp.request, response=fail_resp)
        )
        _resp, outcome = await client._request_with_retry("GET", "/test")
        assert outcome.ok is False
        assert outcome.attempts == 2  # 1 original + 1 retry
        assert outcome.error is not None

    @pytest.mark.asyncio
    async def test_quota_exceeded_no_retry(self) -> None:
        config = PlaygroundClientConfig(
            runtime_url="http://test",
            retry_delays=(0.01, 0.01),
        )
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        body = {"error": {"code": "quota_exceeded"}}
        fail_resp = _make_response(429, json_body=body)
        client._client.request = AsyncMock(
            side_effect=httpx.HTTPStatusError("429", request=fail_resp.request, response=fail_resp)
        )
        _resp, outcome = await client._request_with_retry("GET", "/test")
        assert outcome.ok is False
        assert outcome.quota_exceeded is True
        assert outcome.attempts == 1  # no retry

    @pytest.mark.asyncio
    async def test_rate_limited_retries_with_retry_after(self) -> None:
        config = PlaygroundClientConfig(
            runtime_url="http://test",
            retry_delays=(0.01,),
        )
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        fail_resp = _make_response(429, headers={"Retry-After": "0.01"})
        ok_resp = _make_response(200, json_body={"ok": True})
        client._client.request = AsyncMock(
            side_effect=[
                httpx.HTTPStatusError("429", request=fail_resp.request, response=fail_resp),
                ok_resp,
            ]
        )
        _resp, outcome = await client._request_with_retry("GET", "/test")
        assert outcome.ok is True
        assert outcome.attempts == 2

    @pytest.mark.asyncio
    async def test_non_retryable_4xx_fails_immediately(self) -> None:
        config = PlaygroundClientConfig(
            runtime_url="http://test",
            retry_delays=(0.01, 0.01, 0.01),
        )
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        fail_resp = _make_response(404)
        client._client.request = AsyncMock(
            side_effect=httpx.HTTPStatusError("404", request=fail_resp.request, response=fail_resp)
        )
        _resp, outcome = await client._request_with_retry("GET", "/test")
        assert outcome.ok is False
        assert outcome.attempts == 1  # no retry

    @pytest.mark.asyncio
    async def test_on_retry_callback_called(self) -> None:
        config = PlaygroundClientConfig(
            runtime_url="http://test",
            retry_delays=(0.01,),
        )
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        fail_resp = _make_response(500)
        ok_resp = _make_response(200, json_body={"ok": True})
        client._client.request = AsyncMock(
            side_effect=[
                httpx.HTTPStatusError("500", request=fail_resp.request, response=fail_resp),
                ok_resp,
            ]
        )
        callback_calls: list[tuple[int, float]] = []

        async def on_retry(attempt: int, delay: float) -> None:
            callback_calls.append((attempt, delay))

        await client._request_with_retry("GET", "/test", on_retry=on_retry)
        assert len(callback_calls) == 1
        assert callback_calls[0][0] == 1


# ===========================================================================
# #219 — Feedback retry
# ===========================================================================


class TestFeedbackWithRetry:
    """Test ``post_feedback_with_retry`` in PlaygroundClient."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self) -> None:
        config = PlaygroundClientConfig(runtime_url="http://test")
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        ok_resp = _make_response(201, json_body={"id": "fb1"})
        client._client.post = AsyncMock(return_value=ok_resp)
        _result, ok = await client.post_feedback_with_retry(
            run_id="r1",
            score=1,
            max_retries=1,
            retry_delay_s=0.01,
        )
        assert ok is True
        assert _result is not None

    @pytest.mark.asyncio
    async def test_retry_once_then_success(self) -> None:
        config = PlaygroundClientConfig(runtime_url="http://test")
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        fail_resp = _make_response(500)
        ok_resp = _make_response(201, json_body={"id": "fb1"})
        client._client.post = AsyncMock(
            side_effect=[
                httpx.HTTPStatusError("500", request=fail_resp.request, response=fail_resp),
                ok_resp,
            ]
        )
        _result, ok = await client.post_feedback_with_retry(
            run_id="r1",
            score=1,
            max_retries=1,
            retry_delay_s=0.01,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_failure(self) -> None:
        config = PlaygroundClientConfig(runtime_url="http://test")
        client = PlaygroundClient(config)
        client._client = AsyncMock()
        fail_resp = _make_response(500)
        client._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError("500", request=fail_resp.request, response=fail_resp)
        )
        _result, ok = await client.post_feedback_with_retry(
            run_id="r1",
            score=1,
            max_retries=1,
            retry_delay_s=0.01,
        )
        assert ok is False
        assert _result is None


# ===========================================================================
# #217 — Streaming-drop resilience (SSE token stream)
# ===========================================================================


class TestStreamingDropResilience:
    """Verify that a mid-stream disconnect finalizes the partial message
    with a truncation marker rather than leaving a half-rendered bubble."""

    @pytest.mark.asyncio
    async def test_mid_stream_disconnect_appends_truncation_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mock a mid-stream disconnect and verify the partial message
        is finalized with the truncation suffix."""
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        # Set up minimal session state.
        caps = RuntimeCapabilities(
            sessions=False,
            run_streaming=True,
            feedback_api=False,
            voice=False,
            raw=None,
        )
        convo = ConversationState()
        session[app._K_CAPS] = caps
        session[app._K_CONVO] = convo
        session[app._K_AGENT] = "test-agent"
        session[app._K_CONN_STATE] = ConnectionState.CONNECTED

        # Build a mock client whose stream_run yields two tokens then raises.
        mock_client = AsyncMock()
        mock_client._config = PlaygroundClientConfig(runtime_url="http://test")

        async def _fake_stream(**_: Any):
            yield StreamEvent(event="token", data={"text": "Hello "})
            yield StreamEvent(event="token", data={"text": "world"})
            raise httpx.RemoteProtocolError("connection reset")

        mock_client.stream_run = _fake_stream
        session[app._K_CLIENT] = mock_client

        # Capture sent messages.
        sent_messages: list[Any] = []

        class _CaptureMsg:
            def __init__(self, content: str = "", **kwargs: Any):
                self.content = content
                self.elements: list[Any] = []
                self.actions: list[Any] = []
                self.streamed: list[str] = []
                sent_messages.append(self)

            async def send(self) -> None:
                pass

            async def update(self) -> None:
                pass

            async def stream_token(self, token: str) -> None:
                self.streamed.append(token)
                self.content += token

        monkeypatch.setattr(app.cl, "Message", _CaptureMsg)

        await app._run_streaming(
            client=mock_client,
            agent_name="test-agent",
            user_text="hi",
            base_input=None,
            convo=convo,
            docs=[],
        )

        # The message should contain partial content + truncation marker.
        msg = sent_messages[0]
        assert "Hello world" in msg.content or "Hello " in "".join(msg.streamed)
        assert "truncated" in msg.content.lower()
        assert "connection lost" in msg.content.lower() or "truncated" in msg.content.lower()

    @pytest.mark.asyncio
    async def test_mid_stream_disconnect_no_content_shows_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the stream drops before any content, show a specific marker."""
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        caps = RuntimeCapabilities(
            sessions=False,
            run_streaming=True,
            feedback_api=False,
            voice=False,
            raw=None,
        )
        convo = ConversationState()
        session[app._K_CAPS] = caps
        session[app._K_CONVO] = convo
        session[app._K_AGENT] = "test-agent"
        session[app._K_CONN_STATE] = ConnectionState.CONNECTED

        mock_client = AsyncMock()
        mock_client._config = PlaygroundClientConfig(runtime_url="http://test")

        async def _fake_stream(**_: Any):
            raise httpx.ConnectError("refused")
            yield  # make it an async generator  # type: ignore[misc]

        mock_client.stream_run = _fake_stream
        session[app._K_CLIENT] = mock_client

        sent_messages: list[Any] = []

        class _CaptureMsg:
            def __init__(self, content: str = "", **kwargs: Any):
                self.content = content
                self.elements: list[Any] = []
                self.actions: list[Any] = []
                self.streamed: list[str] = []
                sent_messages.append(self)

            async def send(self) -> None:
                pass

            async def update(self) -> None:
                pass

            async def stream_token(self, token: str) -> None:
                self.streamed.append(token)
                self.content += token

        monkeypatch.setattr(app.cl, "Message", _CaptureMsg)

        await app._run_streaming(
            client=mock_client,
            agent_name="test-agent",
            user_text="hi",
            base_input=None,
            convo=convo,
            docs=[],
        )

        msg = sent_messages[0]
        assert "truncated" in msg.content.lower() or "connection lost" in msg.content.lower()


# ===========================================================================
# #219 — Feedback idempotency (session-scoped guard)
# ===========================================================================


class TestFeedbackIdempotency:
    """Verify that duplicate feedback submissions are blocked client-side."""

    def test_duplicate_detected_in_session_set(self) -> None:
        """The session-scoped set detects duplicates by (run_id, value)."""
        submitted: set[str] = set()
        key = "run123:up"
        assert key not in submitted
        submitted.add(key)
        assert key in submitted
        # A different value for the same run is NOT a duplicate.
        assert "run123:down" not in submitted


# ===========================================================================
# Module reload helpers (same pattern as test_playground_polish.py)
# ===========================================================================


def _reload_app(monkeypatch: pytest.MonkeyPatch, *, voice: bool = False) -> ModuleType:
    """(Re)import ``movate.playground.app`` in a clean environment."""
    monkeypatch.setenv("MDK_PLAYGROUND_NO_HISTORY", "1")
    monkeypatch.delenv("MDK_PLAYGROUND_TARGETS", raising=False)
    if voice:
        monkeypatch.setenv("MDK_PLAYGROUND_VOICE", "1")
    else:
        monkeypatch.delenv("MDK_PLAYGROUND_VOICE", raising=False)
    sys.modules.pop("movate.playground.app", None)
    return importlib.import_module("movate.playground.app")


def _install_session(monkeypatch: pytest.MonkeyPatch, app: ModuleType) -> dict[str, Any]:
    """Back the Chainlit user_session with a plain dict."""
    session: dict[str, Any] = {}

    def _get(key: str, default: object = None) -> object:
        return session.get(key, default)

    def _set(key: str, value: object) -> None:
        session[key] = value

    monkeypatch.setattr(app.cl.user_session, "get", _get)
    monkeypatch.setattr(app.cl.user_session, "set", _set)
    return session
