"""Tests for the native OpenAI adapter.

Mirrors test_anthropic_provider.py: a mock client that mimics the
``openai.AsyncOpenAI`` shape we depend on, so tests stay hermetic
and don't burn API credits. Real-API smoke is gated by
``@pytest.mark.smoke`` (nightly only)."""

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
from movate.providers.base import CompletionRequest, Message
from movate.providers.openai_native import (
    OpenAIProvider,
    _stream_chunk_from_openai,
    _tokens_from_usage,
    _translate_exception,
)

# ---------------------------------------------------------------------------
# Fakes that mimic the openai SDK surface
# ---------------------------------------------------------------------------


@dataclass
class _FakePromptDetails:
    cached_tokens: int = 0


@dataclass
class _FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _FakePromptDetails = field(default_factory=_FakePromptDetails)


@dataclass
class _FakeMessage:
    content: str = ""


@dataclass
class _FakeChoice:
    message: _FakeMessage = field(default_factory=_FakeMessage)
    finish_reason: str = "stop"


@dataclass
class _FakeChatCompletion:
    choices: list[_FakeChoice]
    usage: _FakeUsage
    model: str = "gpt-4o-mini-2024-07-18"


@dataclass
class _FakeDelta:
    content: str = ""


@dataclass
class _FakeStreamChoice:
    delta: _FakeDelta | None = None


@dataclass
class _FakeChatChunk:
    choices: list[_FakeStreamChoice] = field(default_factory=list)
    usage: _FakeUsage | None = None


class _FakeAsyncIter:
    def __init__(self, chunks: list[_FakeChatChunk]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _FakeAsyncIter:
        self._cursor = 0
        return self

    async def __anext__(self) -> _FakeChatChunk:
        if self._cursor >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._cursor]
        self._cursor += 1
        return c


@dataclass
class _FakeChatCompletions:
    create_response: _FakeChatCompletion | None = None
    create_exc: Exception | None = None
    stream_chunks: list[_FakeChatChunk] = field(default_factory=list)
    stream_exc: Exception | None = None
    last_create_call: dict[str, Any] = field(default_factory=dict)

    async def create(self, **kwargs: Any) -> _FakeChatCompletion | _FakeAsyncIter:
        self.last_create_call = kwargs
        if kwargs.get("stream"):
            if self.stream_exc is not None:
                raise self.stream_exc
            return _FakeAsyncIter(self.stream_chunks)
        if self.create_exc is not None:
            raise self.create_exc
        assert self.create_response is not None
        return self.create_response


@dataclass
class _FakeChat:
    completions: _FakeChatCompletions = field(default_factory=_FakeChatCompletions)


@dataclass
class _FakeClient:
    chat: _FakeChat = field(default_factory=_FakeChat)


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pricing_key_prepends_openai_prefix() -> None:
    """Native-OpenAI agents declare bare model ids in agent.yaml
    (``gpt-4o-mini-2024-07-18``), but pricing.yaml uses LiteLLM-style
    keys (``openai/gpt-4o-mini-2024-07-18``). The adapter bridges.

    ``azure/...`` prefixes pass through unchanged because Azure-OpenAI
    deployments use the same pricing table entries with the azure prefix."""
    provider = OpenAIProvider(client=_FakeClient())  # type: ignore[arg-type]
    assert provider.pricing_key("gpt-4o-mini-2024-07-18") == "openai/gpt-4o-mini-2024-07-18"
    assert provider.pricing_key("openai/gpt-4o") == "openai/gpt-4o"
    # Azure deployments use the azure/ prefix in pricing.yaml.
    assert provider.pricing_key("azure/gpt-4.1") == "azure/gpt-4.1"


@pytest.mark.unit
async def test_complete_happy_path() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="hi there"))],
        usage=_FakeUsage(prompt_tokens=11, completion_tokens=3),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    resp = await provider.complete(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    )
    assert resp.text == "hi there"
    assert resp.tokens.input == 11
    assert resp.tokens.output == 3


@pytest.mark.unit
async def test_complete_passes_messages_and_params() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice()], usage=_FakeUsage()
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini",
            messages=[
                Message(role="system", content="be concise"),
                Message(role="user", content="hi"),
            ],
            params={"temperature": 0.3, "max_tokens": 256},
        )
    )
    # OpenAI takes system as a message (unlike Anthropic) — both go in.
    assert fake.chat.completions.last_create_call["messages"] == [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hi"},
    ]
    assert fake.chat.completions.last_create_call["temperature"] == 0.3


@pytest.mark.unit
async def test_complete_extracts_cached_tokens() -> None:
    """``usage.prompt_tokens_details.cached_tokens`` maps to
    ``TokenUsage.cached_input`` — used for prompt-caching cost math."""
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="ok"))],
        usage=_FakeUsage(
            prompt_tokens=200,
            completion_tokens=10,
            prompt_tokens_details=_FakePromptDetails(cached_tokens=150),
        ),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    resp = await provider.complete(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    )
    assert resp.tokens.cached_input == 150


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------


class _StubError(Exception):
    pass


class AuthenticationError(_StubError):
    pass


class RateLimitError(_StubError):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        if retry_after is not None:
            from types import SimpleNamespace  # noqa: PLC0415

            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


class APITimeoutError(_StubError):
    pass


class BadRequestError(_StubError):
    pass


class APIConnectionError(_StubError):
    pass


@pytest.mark.unit
async def test_exception_auth() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = AuthenticationError("bad key")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(AuthError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_rate_limit_carries_retry_after() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = RateLimitError("slow", retry_after=7.0)
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(MovateRateLimitError) as exc_info:
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )
    assert exc_info.value.retry_after == 7.0


@pytest.mark.unit
async def test_exception_timeout() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = APITimeoutError("timed out")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(MovateTimeoutError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_context_length() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = BadRequestError(
        "message is too long for the model's context window"
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContextLengthError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_content_filter() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = BadRequestError("blocked by content policy")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ContentFilterError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_bad_request_falls_through_to_schema_error() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = BadRequestError("invalid params")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


@pytest.mark.unit
async def test_exception_connection_is_model_unavailable() -> None:
    fake = _FakeClient()
    fake.chat.completions.create_exc = APIConnectionError("network error")
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]
    with pytest.raises(ModelUnavailableError):
        await provider.complete(
            CompletionRequest(provider="gpt-4o", messages=[Message(role="user", content="hi")])
        )


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_stream_yields_text_chunks_and_final_usage() -> None:
    fake = _FakeClient()
    fake.chat.completions.stream_chunks = [
        _FakeChatChunk(choices=[_FakeStreamChoice(delta=_FakeDelta(content="hello "))]),
        _FakeChatChunk(choices=[_FakeStreamChoice(delta=_FakeDelta(content="world"))]),
        # Final chunk has no text but populated usage.
        _FakeChatChunk(usage=_FakeUsage(prompt_tokens=5, completion_tokens=2)),
    ]
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    chunks = []
    async for chunk in provider.stream(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    ):
        chunks.append(chunk)
    # Two text chunks + one usage-only final.
    assert len(chunks) == 3
    assert chunks[0].text == "hello "
    assert chunks[1].text == "world"
    assert chunks[2].text == ""
    assert chunks[2].tokens is not None
    assert chunks[2].tokens.input == 5


@pytest.mark.unit
async def test_stream_forces_include_usage_option() -> None:
    """The adapter MUST set ``stream_options={'include_usage': True}``
    even if the user didn't — otherwise cost accounting downstream
    reads zero."""
    fake = _FakeClient()
    fake.chat.completions.stream_chunks = []
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    async for _ in provider.stream(
        CompletionRequest(provider="gpt-4o-mini", messages=[Message(role="user", content="hi")])
    ):
        pass

    call = fake.chat.completions.last_create_call
    assert call["stream"] is True
    assert call["stream_options"]["include_usage"] is True


# ---------------------------------------------------------------------------
# Tool-use (PR 6b): tools passthrough + tool_calls parsing
# ---------------------------------------------------------------------------


@dataclass
class _FakeFunctionCall:
    """Mirrors the SDK's ``Function`` shape inside a ``tool_calls`` entry."""

    name: str
    arguments: str  # JSON-encoded


@dataclass
class _FakeToolCall:
    """Mirrors the SDK's ``ChatCompletionMessageToolCall`` shape."""

    id: str
    function: _FakeFunctionCall
    type: str = "function"


@dataclass
class _FakeMessageWithTools:
    """Variant of _FakeMessage that also carries ``tool_calls`` — used when
    the model emits a tool call instead of a final answer."""

    content: str | None = None
    tool_calls: list[_FakeToolCall] = field(default_factory=list)


@pytest.mark.unit
async def test_complete_passes_tools_through_to_sdk() -> None:
    """When ``request.tools`` is set, it's forwarded to ``chat.completions.create``
    as the ``tools`` kwarg — unchanged (the default to_tool_spec already
    produces the OpenAI shape the SDK accepts)."""
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="ok"))],
        usage=_FakeUsage(),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    tool_specs = [
        {
            "type": "function",
            "function": {
                "name": "calc",
                "description": "Adds",
                "parameters": {"type": "object"},
            },
        }
    ]
    await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini-2024-07-18",
            messages=[Message(role="user", content="hi")],
            tools=tool_specs,
        )
    )
    assert fake.chat.completions.last_create_call.get("tools") == tool_specs


@pytest.mark.unit
async def test_complete_omits_tools_kwarg_when_none() -> None:
    """Single-shot agents (no skills) don't get a tools= kwarg. The SDK
    handles ``tools=None`` cleanly but explicit-only keeps wire payload
    minimal — important under upstream Azure-OpenAI proxies that have
    historically choked on empty tool arrays."""
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="ok"))],
        usage=_FakeUsage(),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini-2024-07-18",
            messages=[Message(role="user", content="hi")],
        )
    )
    assert "tools" not in fake.chat.completions.last_create_call


@pytest.mark.unit
async def test_complete_surfaces_tool_call_response() -> None:
    """A response with ``message.tool_calls`` → ``CompletionResponse(kind="tool_use", ...)``."""
    fake = _FakeClient()
    msg = _FakeMessageWithTools(
        content=None,
        tool_calls=[
            _FakeToolCall(
                id="call_abc",
                function=_FakeFunctionCall(name="calc", arguments='{"a": 2, "b": 3}'),
            )
        ],
    )
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=msg, finish_reason="tool_calls")],
        usage=_FakeUsage(prompt_tokens=10, completion_tokens=4),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini-2024-07-18",
            messages=[Message(role="user", content="2+3?")],
        )
    )
    assert resp.kind == "tool_use"
    assert resp.tool_name == "calc"
    assert resp.tool_id == "call_abc"
    assert resp.tool_input == {"a": 2, "b": 3}
    # Tokens still surfaced for cost accounting.
    assert resp.tokens.input == 10
    assert resp.tokens.output == 4


@pytest.mark.unit
async def test_complete_populates_parallel_tool_calls_for_multiple_calls() -> None:
    """All tool calls from one turn land in ``parallel_tool_calls``.
    The singular fields (tool_name/tool_id/tool_input) still mirror the
    first call for backward compatibility with callers that predate parallel
    tool-use support."""
    from movate.providers.base import ToolCallSpec  # noqa: PLC0415

    fake = _FakeClient()
    msg = _FakeMessageWithTools(
        tool_calls=[
            _FakeToolCall(
                id="call_1",
                function=_FakeFunctionCall(name="calc", arguments='{"a": 1, "b": 2}'),
            ),
            _FakeToolCall(
                id="call_2",
                function=_FakeFunctionCall(name="other", arguments='{"x": 99}'),
            ),
        ],
    )
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=msg, finish_reason="tool_calls")],
        usage=_FakeUsage(),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini-2024-07-18",
            messages=[Message(role="user", content="?")],
        )
    )
    # Singular fields still mirror the first call (backward compat).
    assert resp.kind == "tool_use"
    assert resp.tool_id == "call_1"
    assert resp.tool_name == "calc"
    assert resp.tool_input == {"a": 1, "b": 2}
    # parallel_tool_calls carries all calls.
    assert len(resp.parallel_tool_calls) == 2
    first: ToolCallSpec = resp.parallel_tool_calls[0]
    second: ToolCallSpec = resp.parallel_tool_calls[1]
    assert first.name == "calc" and first.call_id == "call_1" and first.input == {"a": 1, "b": 2}
    assert second.name == "other" and second.call_id == "call_2" and second.input == {"x": 99}


@pytest.mark.unit
async def test_complete_handles_malformed_tool_call_arguments() -> None:
    """Upstream-bug case: ``arguments`` isn't valid JSON. The adapter
    falls through with empty input rather than crashing — the executor's
    input-schema validator will surface a readable error on dispatch."""
    fake = _FakeClient()
    msg = _FakeMessageWithTools(
        tool_calls=[
            _FakeToolCall(
                id="call_x",
                function=_FakeFunctionCall(name="calc", arguments="{not valid json"),
            )
        ],
    )
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=msg, finish_reason="tool_calls")],
        usage=_FakeUsage(),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini-2024-07-18",
            messages=[Message(role="user", content="?")],
        )
    )
    assert resp.kind == "tool_use"
    assert resp.tool_input == {}


@pytest.mark.unit
async def test_complete_handles_dict_shape_tool_calls() -> None:
    """Some SDK versions surface tool_calls as dicts rather than Pydantic
    models. The adapter accepts both shapes via ``_func_field``."""
    fake = _FakeClient()

    # Use a plain dict instead of the dataclass — exercise the dict path.
    tool_call_as_dict = {
        "id": "call_dict",
        "type": "function",
        "function": {"name": "calc", "arguments": '{"a": 1}'},
    }
    msg = _FakeMessageWithTools(tool_calls=[tool_call_as_dict])  # type: ignore[list-item]
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=msg, finish_reason="tool_calls")],
        usage=_FakeUsage(),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    resp = await provider.complete(
        CompletionRequest(
            provider="gpt-4o-mini-2024-07-18",
            messages=[Message(role="user", content="?")],
        )
    )
    assert resp.tool_id == "call_dict"
    assert resp.tool_name == "calc"
    assert resp.tool_input == {"a": 1}


@pytest.mark.unit
async def test_complete_passes_through_openai_style_tool_history() -> None:
    """Continuing-loop history: the executor's OpenAI-style assistant
    turn (with ``tool_calls``) + ``role="tool"`` result pass straight
    through to the SDK. No translation needed — OpenAI IS the wire
    format the executor builds in."""
    fake = _FakeClient()
    fake.chat.completions.create_response = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="5"))],
        usage=_FakeUsage(),
    )
    provider = OpenAIProvider(client=fake)  # type: ignore[arg-type]

    history = [
        Message(role="user", content="2+3?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_42",
                    "type": "function",
                    "function": {"name": "calc", "arguments": '{"a": 2, "b": 3}'},
                }
            ],
        ),
        Message(role="tool", content='{"sum": 5}', tool_call_id="call_42"),
    ]
    await provider.complete(CompletionRequest(provider="gpt-4o-mini-2024-07-18", messages=history))
    sent = fake.chat.completions.last_create_call["messages"]
    # Same three messages on the wire, with tool_calls / tool_call_id
    # preserved (none stripped by ``model_dump(exclude_none=True)``).
    assert len(sent) == 3
    assert sent[1]["role"] == "assistant"
    assert sent[1]["tool_calls"][0]["id"] == "call_42"
    assert sent[2]["role"] == "tool"
    assert sent[2]["tool_call_id"] == "call_42"


# ---------------------------------------------------------------------------
# _translate_exception — direct unit tests (including unmapped paths)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_translate_exception_direct_auth() -> None:
    """AuthenticationError → AuthError (direct call, not via complete)."""
    AuthenticationError = type("AuthenticationError", (Exception,), {})
    with pytest.raises(AuthError):
        _translate_exception(AuthenticationError("bad key"))


@pytest.mark.unit
def test_translate_exception_direct_permission_denied_is_auth_error() -> None:
    """PermissionDeniedError → AuthError."""
    PermissionDeniedError = type("PermissionDeniedError", (Exception,), {})
    with pytest.raises(AuthError):
        _translate_exception(PermissionDeniedError("denied"))


@pytest.mark.unit
def test_translate_exception_direct_internal_server_is_model_unavailable() -> None:
    """InternalServerError → ModelUnavailableError."""
    InternalServerError = type("InternalServerError", (Exception,), {})
    with pytest.raises(ModelUnavailableError):
        _translate_exception(InternalServerError("500"))


@pytest.mark.unit
def test_translate_exception_direct_not_found_is_model_unavailable() -> None:
    """NotFoundError → ModelUnavailableError."""
    NotFoundError = type("NotFoundError", (Exception,), {})
    with pytest.raises(ModelUnavailableError):
        _translate_exception(NotFoundError("not found"))


@pytest.mark.unit
def test_translate_exception_unknown_class_is_model_unavailable_with_prefix() -> None:
    """Unknown exception class → ModelUnavailableError with 'unmapped openai.' prefix."""
    SomeWeirdError = type("SomeWeirdError", (Exception,), {})
    with pytest.raises(ModelUnavailableError, match="unmapped openai.SomeWeirdError"):
        _translate_exception(SomeWeirdError("oops"))


@pytest.mark.unit
def test_translate_exception_bad_request_context_window_variant() -> None:
    """BadRequestError with 'context window' text → ContextLengthError."""
    BadRequestError = type("BadRequestError", (Exception,), {})
    with pytest.raises(ContextLengthError):
        _translate_exception(BadRequestError("context window exceeded"))


# ---------------------------------------------------------------------------
# _stream_chunk_from_openai — direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_chunk_from_openai_mid_stream_text() -> None:
    """Mid-stream chunk with text → StreamChunk(text=..., tokens=None)."""
    chunk = _stream_chunk_from_openai(
        _FakeChatChunk(choices=[_FakeStreamChoice(delta=_FakeDelta(content="hello"))])
    )
    assert chunk is not None
    assert chunk.text == "hello"
    assert chunk.tokens is None


@pytest.mark.unit
def test_stream_chunk_from_openai_final_usage_chunk() -> None:
    """Final chunk with usage and no text → StreamChunk(text='', tokens=...)."""
    chunk = _stream_chunk_from_openai(
        _FakeChatChunk(usage=_FakeUsage(prompt_tokens=10, completion_tokens=4))
    )
    assert chunk is not None
    assert chunk.text == ""
    assert chunk.tokens is not None
    assert chunk.tokens.input == 10
    assert chunk.tokens.output == 4


@pytest.mark.unit
def test_stream_chunk_from_openai_empty_chunk_returns_none() -> None:
    """Chunk with no text and no usage → None (filtered out by iterator)."""
    chunk = _stream_chunk_from_openai(_FakeChatChunk())
    assert chunk is None


@pytest.mark.unit
def test_stream_chunk_from_openai_empty_delta_content_no_usage_returns_none() -> None:
    """Chunk where delta.content is '' and no usage → None."""
    chunk = _stream_chunk_from_openai(
        _FakeChatChunk(choices=[_FakeStreamChoice(delta=_FakeDelta(content=""))])
    )
    assert chunk is None


# ---------------------------------------------------------------------------
# _tokens_from_usage — direct unit tests (OpenAI)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tokens_from_usage_none_returns_empty() -> None:
    """None usage → empty TokenUsage."""
    tokens = _tokens_from_usage(None)
    assert tokens.input == 0
    assert tokens.output == 0
    assert tokens.cached_input == 0


@pytest.mark.unit
def test_tokens_from_usage_maps_all_fields() -> None:
    """prompt_tokens, completion_tokens, and cached_tokens all map correctly."""
    tokens = _tokens_from_usage(
        _FakeUsage(
            prompt_tokens=100,
            completion_tokens=25,
            prompt_tokens_details=_FakePromptDetails(cached_tokens=60),
        )
    )
    assert tokens.input == 100
    assert tokens.output == 25
    assert tokens.cached_input == 60


@pytest.mark.unit
def test_tokens_from_usage_no_cached_tokens_details() -> None:
    """Usage with no prompt_tokens_details → cached_input=0."""
    from types import SimpleNamespace  # noqa: PLC0415

    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=2, prompt_tokens_details=None)
    tokens = _tokens_from_usage(usage)
    assert tokens.cached_input == 0


# ---------------------------------------------------------------------------
# Optional-dep gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_without_client_raises_import_error_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the openai package isn't installed AND no client is injected,
    construction raises ImportError with the install hint."""
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openai":
            raise ImportError("no module named openai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"movate-cli\[openai\]"):
        OpenAIProvider()
