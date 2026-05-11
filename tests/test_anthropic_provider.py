"""Tests for the native Anthropic adapter.

These tests use a mock client (``FakeAnthropicClient`` below) that
mimics the ``anthropic.AsyncAnthropic`` shape we depend on — that
way we don't burn real Anthropic credits and the suite stays hermetic.

Real-API smoke is gated by ``@pytest.mark.smoke`` (nightly only,
see test_smoke_litellm for the precedent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from movate.core.failures import (
    AuthError,
    ContentFilterError,
    ContextLengthError,
    ModelUnavailableError,
    MovateTimeoutError,
    SchemaError,
)
from movate.core.failures import RateLimitError as MovateRateLimitError
from movate.providers.anthropic import AnthropicProvider
from movate.providers.base import CompletionRequest, Message

# ---------------------------------------------------------------------------
# Fakes that mimic the anthropic SDK surface we depend on
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeMessage:
    content: list[_FakeTextBlock]
    usage: _FakeUsage
    model: str = "claude-haiku-4-5-20251001"
    stop_reason: str = "end_turn"


@dataclass
class _FakeContentBlockDelta:
    text: str = ""
    type: str = "text_delta"


@dataclass
class _FakeStreamEvent:
    type: str
    delta: _FakeContentBlockDelta | None = None


class _FakeStreamContext:
    """Mimics the async context manager returned by ``messages.stream``."""

    def __init__(self, events: list[_FakeStreamEvent], final_message: _FakeMessage) -> None:
        self._events = events
        self._final_message = final_message

    async def __aenter__(self) -> _FakeStreamContext:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> _FakeStreamContext:
        self._cursor = 0
        return self

    async def __anext__(self) -> _FakeStreamEvent:
        if self._cursor >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._cursor]
        self._cursor += 1
        return ev

    async def get_final_message(self) -> _FakeMessage:
        return self._final_message


@dataclass
class _FakeMessages:
    create_response: _FakeMessage | None = None
    create_exc: Exception | None = None
    stream_events: list[_FakeStreamEvent] = field(default_factory=list)
    stream_final: _FakeMessage | None = None
    stream_exc: Exception | None = None

    last_create_call: dict[str, Any] = field(default_factory=dict)
    last_stream_call: dict[str, Any] = field(default_factory=dict)

    async def create(self, **kwargs: Any) -> _FakeMessage:
        self.last_create_call = kwargs
        if self.create_exc is not None:
            raise self.create_exc
        assert self.create_response is not None
        return self.create_response

    def stream(self, **kwargs: Any) -> _FakeStreamContext:
        self.last_stream_call = kwargs
        if self.stream_exc is not None:
            raise self.stream_exc
        assert self.stream_final is not None
        return _FakeStreamContext(self.stream_events, self.stream_final)


@dataclass
class _FakeClient:
    messages: _FakeMessages = field(default_factory=_FakeMessages)


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_complete_happy_path_extracts_text_and_tokens() -> None:
    """Text from the first text content block + tokens from usage."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="hello world")],
        usage=_FakeUsage(input_tokens=42, output_tokens=7),
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert resp.text == "hello world"
    assert resp.tokens.input == 42
    assert resp.tokens.output == 7


@pytest.mark.unit
async def test_complete_splits_system_message_to_kwarg() -> None:
    """Anthropic separates system from messages; the adapter should
    extract role=system entries and pass them via the ``system=`` kwarg."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[
                Message(role="system", content="you are concise"),
                Message(role="user", content="explain quicksort"),
            ],
        )
    )
    # System extracted; messages array contains only the user turn.
    assert fake.messages.last_create_call["system"] == "you are concise"
    assert fake.messages.last_create_call["messages"] == [
        {"role": "user", "content": "explain quicksort"}
    ]


@pytest.mark.unit
async def test_complete_defaults_max_tokens() -> None:
    """Anthropic requires ``max_tokens``. If the user didn't set it,
    the adapter picks a sane default — without this the SDK errors
    immediately."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert fake.messages.last_create_call["max_tokens"] == 4096


@pytest.mark.unit
async def test_complete_passes_through_user_params() -> None:
    """User-set params (temperature, top_p, etc.) reach the SDK call."""
    fake = _FakeClient()
    fake.messages.create_response = _FakeMessage(
        content=[_FakeTextBlock(text="ok")], usage=_FakeUsage()
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
            params={"temperature": 0.7, "max_tokens": 100},
        )
    )
    assert fake.messages.last_create_call["temperature"] == 0.7
    # User-set max_tokens wins over the default.
    assert fake.messages.last_create_call["max_tokens"] == 100


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------


class _StubError(Exception):
    """Base class for our exception fakes — only the class NAME matters
    because the adapter does string-based dispatch (so it doesn't have
    to import anthropic at module scope)."""


class AuthenticationError(_StubError):
    pass


class RateLimitError(_StubError):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        if retry_after is not None:
            # Build a one-off response object that mirrors the
            # ``response.headers`` shape the anthropic SDK exposes —
            # instance-attr, not class-attr, to keep ruff happy.
            from types import SimpleNamespace  # noqa: PLC0415

            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


class APITimeoutError(_StubError):
    pass


class BadRequestError(_StubError):
    pass


class APIConnectionError(_StubError):
    pass


@pytest.mark.unit
async def test_exception_translation_auth() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = AuthenticationError("bad key")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(AuthError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_rate_limit_extracts_retry_after() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = RateLimitError("slow down", retry_after=12.5)
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    # The adapter raises movate's RateLimitError (different class from
    # the SDK's), carrying the retry_after extracted from response headers.
    with pytest.raises(MovateRateLimitError) as exc_info:
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )
    assert exc_info.value.retry_after == 12.5


@pytest.mark.unit
async def test_exception_translation_timeout() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = APITimeoutError("timed out")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(MovateTimeoutError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_context_length() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = BadRequestError(
        "input is too long: exceeds context window of 200000 tokens"
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContextLengthError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_content_filter() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = BadRequestError("content policy violation: harmful content detected")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContentFilterError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_generic_bad_request_is_schema_error() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = BadRequestError("invalid request body")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_translation_connection_is_model_unavailable() -> None:
    fake = _FakeClient()
    fake.messages.create_exc = APIConnectionError("can't reach api.anthropic.com")
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ModelUnavailableError):
        await provider.complete(
            CompletionRequest(provider="claude", messages=[Message(role="user", content="hi")])
        )


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_stream_yields_text_chunks_and_final_usage() -> None:
    """Stream events project to chunks; final chunk carries usage."""
    fake = _FakeClient()
    fake.messages.stream_events = [
        _FakeStreamEvent(
            type="content_block_delta",
            delta=_FakeContentBlockDelta(text="hello "),
        ),
        _FakeStreamEvent(
            type="content_block_delta",
            delta=_FakeContentBlockDelta(text="world"),
        ),
        # Non-text event — should be filtered out.
        _FakeStreamEvent(type="message_stop"),
    ]
    fake.messages.stream_final = _FakeMessage(
        content=[_FakeTextBlock(text="hello world")],
        usage=_FakeUsage(input_tokens=5, output_tokens=2),
    )
    provider = AnthropicProvider(client=fake)  # type: ignore[arg-type]

    chunks = []
    async for chunk in provider.stream(
        CompletionRequest(
            provider="claude-haiku-4-5",
            messages=[Message(role="user", content="hi")],
        )
    ):
        chunks.append(chunk)

    # Two text deltas + one usage-only final chunk.
    assert len(chunks) == 3
    assert chunks[0].text == "hello "
    assert chunks[1].text == "world"
    assert chunks[2].text == ""
    assert chunks[2].tokens is not None
    assert chunks[2].tokens.input == 5
    assert chunks[2].tokens.output == 2


# ---------------------------------------------------------------------------
# Optional-dep gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_without_client_raises_clear_error_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the anthropic package isn't installed AND no client is
    injected, construction must raise ImportError with the
    ``movate-cli[anthropic]`` install hint."""
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"movate-cli\[anthropic\]"):
        AnthropicProvider()
