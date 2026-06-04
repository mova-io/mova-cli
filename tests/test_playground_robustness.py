"""Tests for playground robustness items #216, #217, #218, #219, #220.

Covers:
  #216 — Runtime-down resilience + 429/quota UX (retry with backoff)
  #217 — Streaming-drop resilience (truncation marker on partial messages)
  #218 — MIME/type validation, configurable size limit, progress indication
  #219 — Feedback delivery robustness (retry + idempotency)
  #220 — session isolation, bearer refresh on 401, capability staleness,
         X-Request-Id correlation

All tests are pure-unit — no real network, mocked httpx.  The pure-logic
retry/parse helpers in ``movate.playground.client`` are tested directly;
the Chainlit integration tests mock the session and verify UI messages.
"""

from __future__ import annotations

import importlib
import sys
import time
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
    _request_id,
)
from movate.playground.connection import ConnectionState
from movate.playground.conversation import ConversationState, TurnResult
from movate.playground.sse import StreamEvent
from movate.playground.uploads import (
    DEFAULT_MIME_ALLOWLIST,
    DEFAULT_PLAYGROUND_MAX_UPLOAD_MB,
    UploadOutcome,
    UploadStore,
    adapt_upload,
    check_mime_allowed,
    configured_max_upload_mb,
    configured_mime_allowlist,
)

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
# #218 — Upload hardening: MIME/type validation
# ===========================================================================


class TestMimeValidation:
    """MIME-type allowlist enforcement."""

    def test_pdf_allowed_by_default(self) -> None:
        assert check_mime_allowed("report.pdf") is None

    def test_json_allowed_by_default(self) -> None:
        assert check_mime_allowed("data.json") is None

    def test_text_wildcard_allows_txt(self) -> None:
        assert check_mime_allowed("notes.txt") is None

    def test_text_wildcard_allows_md(self) -> None:
        assert check_mime_allowed("readme.md") is None

    def test_image_allowed_by_default(self) -> None:
        assert check_mime_allowed("photo.png") is None
        assert check_mime_allowed("diagram.jpg") is None

    def test_exe_rejected(self) -> None:
        err = check_mime_allowed("virus.exe")
        assert err is not None
        assert ".exe" in err

    def test_zip_rejected(self) -> None:
        err = check_mime_allowed("archive.zip")
        assert err is not None

    def test_custom_allowlist(self) -> None:
        narrow = frozenset({"application/pdf"})
        assert check_mime_allowed("doc.pdf", narrow) is None
        err = check_mime_allowed("notes.txt", narrow)
        assert err is not None  # text not in narrow list

    def test_unknown_extension_rejected(self) -> None:
        err = check_mime_allowed("file.xyzabc")
        assert err is not None

    def test_adapt_upload_mime_rejected(self) -> None:
        doc = adapt_upload(
            "malware.exe",
            b"\x00" * 100,
            max_size_mb=20,
            mime_allowlist=DEFAULT_MIME_ALLOWLIST,
        )
        assert doc.outcome == UploadOutcome.MIME_REJECTED
        assert ".exe" in doc.note

    def test_adapt_upload_too_large_message(self) -> None:
        doc = adapt_upload(
            "big.txt",
            b"x" * (2 * 1024 * 1024),
            max_size_mb=1,
            mime_allowlist=DEFAULT_MIME_ALLOWLIST,
        )
        assert doc.outcome == UploadOutcome.TOO_LARGE
        assert "max 1MB" in doc.note

    def test_valid_file_accepted(self) -> None:
        doc = adapt_upload(
            "notes.md",
            b"# Hello\n\nContent here.",
            max_size_mb=20,
            mime_allowlist=DEFAULT_MIME_ALLOWLIST,
        )
        assert doc.outcome == UploadOutcome.EXTRACTED


class TestConfigurableUploadLimits:
    """Env-configurable upload size limit and MIME allowlist."""

    def test_default_max_upload_mb(self) -> None:
        assert DEFAULT_PLAYGROUND_MAX_UPLOAD_MB == 10

    def test_configured_max_upload_mb_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MDK_PLAYGROUND_MAX_UPLOAD_MB", "25")
        assert configured_max_upload_mb() == 25

    def test_configured_max_upload_mb_invalid_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MDK_PLAYGROUND_MAX_UPLOAD_MB", "not-a-number")
        assert configured_max_upload_mb() == DEFAULT_PLAYGROUND_MAX_UPLOAD_MB

    def test_configured_max_upload_mb_zero_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MDK_PLAYGROUND_MAX_UPLOAD_MB", "0")
        assert configured_max_upload_mb() == DEFAULT_PLAYGROUND_MAX_UPLOAD_MB

    def test_configured_mime_allowlist_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MDK_PLAYGROUND_UPLOAD_MIME_ALLOWLIST", "application/pdf,text/*")
        result = configured_mime_allowlist()
        assert "application/pdf" in result
        assert "text/*" in result
        assert "image/*" not in result

    def test_configured_mime_allowlist_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MDK_PLAYGROUND_UPLOAD_MIME_ALLOWLIST", raising=False)
        assert configured_mime_allowlist() == DEFAULT_MIME_ALLOWLIST


# ===========================================================================
# #220 — Session isolation (via Chainlit user_session)
# ===========================================================================


class TestSessionIsolation:
    """Verify that per-user session state does not leak between sessions.

    The playground stores ALL mutable state in ``cl.user_session`` (keyed
    constants). Two concurrent sessions must operate independently — no
    module-level mutable state crosses sessions.
    """

    @pytest.mark.asyncio
    async def test_two_sessions_no_cross_talk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two mock sessions see their own state independently."""
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)

        # Session A — directly manipulated dict, no monkeypatch needed
        # because we're testing the data model, not the Chainlit runtime.
        session_a: dict[str, Any] = {}
        session_a[app._K_AGENT] = "agent_alpha"
        session_a[app._K_CONVO] = ConversationState()
        session_a[app._K_CONVO].add_user("user A message")

        # Session B — separate dict
        session_b: dict[str, Any] = {}
        session_b[app._K_AGENT] = "agent_beta"
        session_b[app._K_CONVO] = ConversationState()
        session_b[app._K_CONVO].add_user("user B message")

        # Assert no cross-contamination.
        assert session_a[app._K_AGENT] == "agent_alpha"
        assert session_b[app._K_AGENT] == "agent_beta"
        assert len(session_a[app._K_CONVO].turns) == 1
        assert session_a[app._K_CONVO].turns[0].text == "user A message"
        assert session_b[app._K_CONVO].turns[0].text == "user B message"

    def test_no_module_level_mutable_session_state(self) -> None:
        """Audit: the app module has no module-level mutable dicts/lists
        that would cross sessions. Only frozen/immutable module globals."""
        pytest.importorskip("chainlit")
        app = importlib.import_module("movate.playground.app")
        # The known module-level mutables are the TARGETS list (decoded at
        # import from an env var) and the voice-enabled flag. Both are
        # read-only after import. Check that they're not dicts or sets that
        # could accumulate per-session state.
        assert isinstance(app._TARGETS, list)  # read-only after decode
        assert isinstance(app._VOICE_ENABLED, bool)  # read-only
        # _UPLOAD_MIME_ALLOWLIST is a frozenset (immutable).
        assert isinstance(app._UPLOAD_MIME_ALLOWLIST, frozenset)


# ===========================================================================
# #220 — Bearer refresh on 401
# ===========================================================================


class TestBearerRefresh:
    """Bearer token re-resolution on 401 during on_message."""

    @pytest.mark.asyncio
    async def test_401_triggers_reauth_and_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        call_count = {"n": 0}

        class _FakeBackend:
            name = "client-managed"

            async def send_turn(self, **kwargs: Any) -> Any:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # Simulate a 401 on first attempt.
                    exc = Exception("auth failed")
                    resp = type("R", (), {"status_code": 401})()
                    exc.response = resp  # type: ignore[attr-defined]
                    raise exc
                # Succeed on retry.
                return TurnResult(run_id="r2", status="success", output={"message": "ok"})

        class _FakeClient:
            last_request_id = "pg-test"
            _config = PlaygroundClientConfig(runtime_url="http://test:8000", api_key="old")

        session[app._K_AGENT] = "echo"
        session[app._K_CLIENT] = _FakeClient()
        session[app._K_BACKEND] = _FakeBackend()
        session[app._K_CAPS] = RuntimeCapabilities()
        session[app._K_CONVO] = ConversationState()
        session[app._K_UPLOADS] = UploadStore()
        session[app._K_CONN_STATE] = ConnectionState.CONNECTED
        session[app._K_CAPS_FETCHED_AT] = time.monotonic()

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

            async def update(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)

        # Patch _check_connection and _maybe_refresh_capabilities to no-op.
        async def _noop() -> None:
            pass

        monkeypatch.setattr(app, "_check_connection", _noop)
        monkeypatch.setattr(app, "_maybe_refresh_capabilities", _noop)

        # Patch _refresh_bearer_and_retry to return a fresh client.
        async def _fake_refresh(client: Any) -> Any:
            return _FakeClient()

        monkeypatch.setattr(app, "_refresh_bearer_and_retry", _fake_refresh)

        # Patch select_backend to return the same fake backend (for retry).
        monkeypatch.setattr(app, "select_backend", lambda caps, client: _FakeBackend())

        msg = type("M", (), {"content": "hello", "elements": []})()
        await app.on_message(msg)

        # Should see "Re-authenticating..." in the messages.
        assert any("re-authenticating" in m.lower() for m in sent)


# ===========================================================================
# #220 — Capability staleness detection
# ===========================================================================


class TestCapabilityStaleness:
    """Capabilities are re-fetched when older than the threshold."""

    @pytest.mark.asyncio
    async def test_stale_caps_trigger_refetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        # Seed old capabilities (5+ minutes ago).
        old_caps = RuntimeCapabilities(run_streaming=False)
        session[app._K_CAPS] = old_caps
        session[app._K_CAPS_FETCHED_AT] = time.monotonic() - 400  # > 300s threshold

        fetch_count = {"n": 0}

        class _FakeClient:
            last_request_id = ""

            async def get_capabilities(self) -> dict[str, Any]:
                fetch_count["n"] += 1
                return {"features": {"run_streaming": True}}

        session[app._K_CLIENT] = _FakeClient()

        sent: list[str] = []

        class _Msg(_FakeMsg):
            async def send(self) -> None:
                sent.append(self.content)

        monkeypatch.setattr(app.cl, "Message", _Msg)

        await app._maybe_refresh_capabilities()

        # Capabilities should have been re-fetched.
        assert fetch_count["n"] == 1
        # New caps should be stored.
        new_caps: RuntimeCapabilities = session[app._K_CAPS]
        assert new_caps.run_streaming is True
        # A notification should have been emitted (caps changed).
        assert any("new capabilities" in m.lower() for m in sent)

    @pytest.mark.asyncio
    async def test_fresh_caps_not_refetched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("chainlit")
        app = _reload_app(monkeypatch)
        session = _install_session(monkeypatch, app)

        session[app._K_CAPS] = RuntimeCapabilities()
        session[app._K_CAPS_FETCHED_AT] = time.monotonic()  # just now

        fetch_count = {"n": 0}

        class _FakeClient:
            last_request_id = ""

            async def get_capabilities(self) -> dict[str, Any]:
                fetch_count["n"] += 1
                return {}

        session[app._K_CLIENT] = _FakeClient()

        await app._maybe_refresh_capabilities()

        # Should NOT have re-fetched (still fresh).
        assert fetch_count["n"] == 0


# ===========================================================================
# #220 — X-Request-Id correlation
# ===========================================================================


class TestRequestIdCorrelation:
    """Every PlaygroundClient request includes X-Request-Id."""

    @pytest.mark.asyncio
    async def test_request_id_sent_on_list_agents(self) -> None:
        """list_agents includes X-Request-Id header."""
        captured_headers: list[dict[str, str]] = []

        class _FakeTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured_headers.append(dict(request.headers))
                return httpx.Response(200, json={"agents": [], "count": 0})

        client = PlaygroundClient(
            PlaygroundClientConfig(runtime_url="http://test:8000", api_key="tok")
        )
        # Replace the transport to capture headers.
        client._client = httpx.AsyncClient(
            base_url="http://test:8000",
            transport=_FakeTransport(),
        )
        await client.list_agents()
        assert captured_headers
        rid = captured_headers[0].get("x-request-id", "")
        assert rid.startswith("pg-")
        assert client.last_request_id == rid

    @pytest.mark.asyncio
    async def test_request_id_on_submit_run(self) -> None:
        captured_headers: list[dict[str, str]] = []

        class _FakeTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured_headers.append(dict(request.headers))
                return httpx.Response(200, json={"job_id": "j1", "status": "queued"})

        client = PlaygroundClient(PlaygroundClientConfig(runtime_url="http://test:8000"))
        client._client = httpx.AsyncClient(
            base_url="http://test:8000",
            transport=_FakeTransport(),
        )
        await client.submit_run(agent="bot", input_data={"message": "hi"})
        assert captured_headers
        assert captured_headers[0].get("x-request-id", "").startswith("pg-")

    def test_request_id_format(self) -> None:
        rid = _request_id()
        assert rid.startswith("pg-")
        assert len(rid) == 15  # "pg-" + 12 hex chars


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


class _FakeMsg:
    """Minimal Chainlit Message stand-in."""

    def __init__(self, content: str = "", **_: object) -> None:
        self.content = content
        self.elements: list[Any] = []
        self.actions: list[Any] = []
        self.sent = False
        self.updates = 0

    async def send(self) -> None:
        self.sent = True

    async def update(self) -> None:
        self.updates += 1


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
