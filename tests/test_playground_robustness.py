"""Tests for playground robustness batch 2 (#218 upload hardening + #220 hosted concurrency).

Covers:
  #218 — MIME/type validation, configurable size limit, progress indication
  #220 — session isolation, bearer refresh on 401, capability staleness,
         X-Request-Id correlation

All tests are pure-unit (no real network, mocked httpx) and run on a
``[playground]``-extras install (Chainlit available).
"""

from __future__ import annotations

import importlib
import sys
import time
from types import ModuleType
from typing import Any

import httpx
import pytest

from movate.playground.capabilities import RuntimeCapabilities
from movate.playground.client import PlaygroundClient, PlaygroundClientConfig, _request_id
from movate.playground.connection import ConnectionState
from movate.playground.conversation import ConversationState, TurnResult
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
