"""Tests for the Lyzr HTTP adapter.

Mocks ``httpx.AsyncClient`` so no real network calls happen. The
adapter is purely a transport — we assert it makes the right request
shape, handles the documented error codes, and returns a clean
``CompletionResponse``.
"""

from __future__ import annotations

import httpx
import pytest

import movate.providers.lyzr as lyzr_mod
from movate.core.failures import (
    AuthError,
    ModelUnavailableError,
    MovateTimeoutError,
    RateLimitError,
    SchemaError,
)
from movate.core.models import TokenUsage
from movate.providers.base import CompletionRequest, Message
from movate.providers.lyzr import _DEFAULT_BASE, _INFERENCE_PATH, LyzrProvider

# ---------------------------------------------------------------------------
# Fake httpx.AsyncClient used as a drop-in via monkeypatch
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeAsyncClient:
    """Captures posted URL / headers / body and returns a stubbed response."""

    def __init__(self, *, response: _FakeResponse, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.captured: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
        if self._raise is not None:
            raise self._raise
        self.captured["url"] = url
        self.captured["headers"] = headers
        self.captured["json"] = json
        return self._response


def _patch_httpx(monkeypatch, client: _FakeAsyncClient) -> None:
    """Replace httpx.AsyncClient with a factory that returns our fake."""

    def _factory(*_args, **_kwargs):
        return client

    monkeypatch.setattr(lyzr_mod.httpx, "AsyncClient", _factory)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_happy_path(monkeypatch) -> None:
    """A 200 from Lyzr → text + raw payload populated; tokens stay 0."""
    client = _FakeAsyncClient(
        response=_FakeResponse(
            status_code=200,
            json_data={"response": "Hello from Tesla support."},
        )
    )
    _patch_httpx(monkeypatch, client)

    provider = LyzrProvider(api_key="sk-default-test")
    req = CompletionRequest(
        provider="lyzr/69fe0d9890de3014e9f1cf92",
        messages=[Message(role="user", content="What about Model Y?")],
    )

    resp = await provider.complete(req)

    assert resp.text == "Hello from Tesla support."
    assert resp.tokens == TokenUsage(input=0, output=0)
    assert resp.raw["lyzr_agent_id"] == "69fe0d9890de3014e9f1cf92"
    assert "lyzr_session_id" in resp.raw

    # Request shape: correct URL, auth header, and JSON body.
    assert client.captured["url"] == f"{_DEFAULT_BASE}{_INFERENCE_PATH}"
    assert client.captured["headers"]["x-api-key"] == "sk-default-test"
    body = client.captured["json"]
    assert body["agent_id"] == "69fe0d9890de3014e9f1cf92"
    assert body["message"] == "What about Model Y?"
    assert body["session_id"].startswith("69fe0d9890de3014e9f1cf92-")
    assert body["user_id"] == "mdk-runtime"


@pytest.mark.asyncio
async def test_complete_caller_supplies_session_id(monkeypatch) -> None:
    """When ``params.session_id`` is set, the adapter uses it verbatim
    so multi-turn conversation history can be preserved across calls."""
    client = _FakeAsyncClient(
        response=_FakeResponse(status_code=200, json_data={"response": "ok"})
    )
    _patch_httpx(monkeypatch, client)

    provider = LyzrProvider(api_key="sk-default-test")
    req = CompletionRequest(
        provider="lyzr/abc123",
        messages=[Message(role="user", content="hi")],
        params={"session_id": "user-123-conversation-456", "user_id": "alice@movate.com"},
    )

    await provider.complete(req)

    assert client.captured["json"]["session_id"] == "user-123-conversation-456"
    assert client.captured["json"]["user_id"] == "alice@movate.com"


@pytest.mark.asyncio
async def test_missing_api_key_raises_auth_error(monkeypatch) -> None:
    """No LYZR_API_KEY at construction OR env → clean AuthError, no HTTP."""
    # Make sure env doesn't supply one for this test
    monkeypatch.delenv("LYZR_API_KEY", raising=False)
    provider = LyzrProvider(api_key=None)
    with pytest.raises(AuthError, match="LYZR_API_KEY"):
        await provider.complete(
            CompletionRequest(
                provider="lyzr/abc",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.asyncio
async def test_401_raises_auth_error(monkeypatch) -> None:
    client = _FakeAsyncClient(
        response=_FakeResponse(status_code=401, text="unauthorized")
    )
    _patch_httpx(monkeypatch, client)
    provider = LyzrProvider(api_key="sk-default-bogus")
    with pytest.raises(AuthError):
        await provider.complete(
            CompletionRequest(
                provider="lyzr/abc",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.asyncio
async def test_429_raises_rate_limit_error_with_retry_after(monkeypatch) -> None:
    response = _FakeResponse(status_code=429, text="slow down")
    response.headers = {"retry-after": "30"}
    client = _FakeAsyncClient(response=response)
    _patch_httpx(monkeypatch, client)
    provider = LyzrProvider(api_key="sk-default-test")
    with pytest.raises(RateLimitError) as exc_info:
        await provider.complete(
            CompletionRequest(
                provider="lyzr/abc",
                messages=[Message(role="user", content="hi")],
            )
        )
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_500_raises_model_unavailable(monkeypatch) -> None:
    client = _FakeAsyncClient(
        response=_FakeResponse(status_code=503, text="lyzr down")
    )
    _patch_httpx(monkeypatch, client)
    provider = LyzrProvider(api_key="sk-default-test")
    with pytest.raises(ModelUnavailableError):
        await provider.complete(
            CompletionRequest(
                provider="lyzr/abc",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.asyncio
async def test_400_raises_schema_error(monkeypatch) -> None:
    client = _FakeAsyncClient(
        response=_FakeResponse(status_code=400, text="bad request")
    )
    _patch_httpx(monkeypatch, client)
    provider = LyzrProvider(api_key="sk-default-test")
    with pytest.raises(SchemaError):
        await provider.complete(
            CompletionRequest(
                provider="lyzr/abc",
                messages=[Message(role="user", content="hi")],
            )
        )


@pytest.mark.asyncio
async def test_timeout_raises_movate_timeout(monkeypatch) -> None:
    client = _FakeAsyncClient(
        response=_FakeResponse(status_code=200, json_data={"response": "x"}),
        raise_exc=httpx.TimeoutException("slow"),
    )
    _patch_httpx(monkeypatch, client)
    provider = LyzrProvider(api_key="sk-default-test")
    with pytest.raises(MovateTimeoutError):
        await provider.complete(
            CompletionRequest(
                provider="lyzr/abc",
                messages=[Message(role="user", content="hi")],
            )
        )


def test_parse_agent_id_rejects_bad_format() -> None:
    """Provider strings must be 'lyzr/<id>'. Bare ids raise SchemaError."""
    with pytest.raises(SchemaError, match="lyzr/<agent_id>"):
        LyzrProvider._parse_agent_id("just-an-id")


def test_parse_agent_id_rejects_empty_suffix() -> None:
    with pytest.raises(SchemaError, match="missing the agent id"):
        LyzrProvider._parse_agent_id("lyzr/")


@pytest.mark.asyncio
async def test_extract_user_message_picks_last_user_role(monkeypatch) -> None:
    """When multiple messages are passed, adapter uses the LAST user message
    (Lyzr's chat endpoint takes a single string; system + assistant messages
    are encoded server-side via agent_instructions and session state)."""
    client = _FakeAsyncClient(
        response=_FakeResponse(status_code=200, json_data={"response": "ok"})
    )
    _patch_httpx(monkeypatch, client)
    provider = LyzrProvider(api_key="sk-default-test")
    await provider.complete(
        CompletionRequest(
            provider="lyzr/abc",
            messages=[
                Message(role="system", content="(ignored)"),
                Message(role="user", content="first user msg"),
                Message(role="assistant", content="(ignored)"),
                Message(role="user", content="second user msg"),
            ],
        )
    )
    assert client.captured["json"]["message"] == "second user msg"
