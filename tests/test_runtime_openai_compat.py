"""OpenAI-compatible API (ADR 085): /v1/models + /v1/chat/completions.

Verifies the protocol-adapter shim that lets OpenAI clients (OpenWebUI, the
openai SDK, LangChain) drive a deployed mdk agent. Hermetic: a fake LLM
provider (monkeypatched in place of LiteLLM) so there is no network/keys —
the runtime resolves the agent + Executor exactly as in production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.providers.base import BaseLLMProvider, CompletionRequest, CompletionResponse
from movate.runtime.app import build_app
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage
from movate.voice.base import TranscriptChunk


class _FakeProvider(BaseLLMProvider):
    """Deterministic offline provider standing in for LiteLLM (no keys/network)."""

    name = "fake"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(text='{"answer": "Hello from mdk!"}')

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def _write_echo_agent(root: Path) -> None:
    d = root / "echo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "echo",
                "version": "0.1.0",
                "description": "Echo agent for the OpenAI-compat test.",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {
                    "input": {"message": "string"},
                    "output": {"answer": "string"},
                },
            }
        )
    )
    (d / "prompt.md").write_text(
        'Reply to the user. Return JSON like {"answer": "..."}.\n\n{{ input.message }}\n'
    )


@pytest.fixture
def _provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # The handler does `from movate.providers.litellm import LiteLLMProvider`
    # at call time, so patching the attribute makes it pick up the fake.
    monkeypatch.setattr("movate.providers.litellm.LiteLLMProvider", _FakeProvider)


async def _client(storage: InMemoryStorage, agents_root: Path) -> tuple[TestClient, dict[str, str]]:
    await storage.init()
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="oai-test", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    bundles = scan_agents(agents_root)
    client = TestClient(build_app(storage, agents=bundles))
    return client, {"Authorization": f"Bearer {minted.full_key}"}


@pytest.mark.unit
async def test_v1_models_lists_agents(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    r = client.get("/v1/models", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "echo" in ids
    assert body["data"][0]["object"] == "model"
    assert body["data"][0]["owned_by"] == "movate"


@pytest.mark.unit
async def test_v1_models_requires_auth(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, _ = await _client(InMemoryStorage(), tmp_path)
    assert client.get("/v1/models").status_code == 401


@pytest.mark.unit
async def test_v1_chat_completions_non_streaming(tmp_path: Path, _provider: None) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    r = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "echo", "messages": [{"role": "user", "content": "hi there"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "echo"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert isinstance(choice["message"]["content"], str)
    assert "Hello from mdk!" in choice["message"]["content"]
    assert choice["finish_reason"] == "stop"
    assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}


@pytest.mark.unit
async def test_v1_chat_completions_streaming(tmp_path: Path, _provider: None) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    r = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "echo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert frames[-1] == "data: [DONE]"
    # The content delta carries the assistant text.
    payloads = [json.loads(ln[len("data: ") :]) for ln in frames if ln != "data: [DONE]"]
    contents = "".join(
        p["choices"][0]["delta"].get("content", "") for p in payloads if p["choices"][0]["delta"]
    )
    assert "Hello from mdk!" in contents
    assert all(p["object"] == "chat.completion.chunk" for p in payloads)


@pytest.mark.unit
async def test_v1_chat_completions_unknown_model_404(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    r = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 404


@pytest.mark.unit
async def test_v1_chat_completions_empty_messages_400(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    r = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "echo", "messages": []},
    )
    assert r.status_code == 400


# --- /v1/audio/transcriptions (ADR 085 follow-on: OpenWebUI mic → STT) --------


class _FakeSTT:
    """Offline STT standing in for Deepgram/Whisper — no network/keys.

    Drains the inbound audio stream and endpoints with a fixed transcript,
    satisfying the SpeechToTextProvider contract (one ``is_final=True`` chunk).
    """

    name = "fake_stt"
    version = "0.0.1"

    def __init__(self, transcript: str = "hello from the microphone") -> None:
        self._transcript = transcript

    async def transcribe(self, audio: Any, **_kw: Any) -> Any:
        async for _ in audio:
            pass
        yield TranscriptChunk(text=self._transcript, is_final=True, confidence=1.0)


def _with_fake_stt(client: TestClient, transcript: str = "hello from the microphone") -> None:
    """Swap the runtime's STT factory for a hermetic fake (no Deepgram/keys)."""
    client.app.state.voice_stt_factory = lambda: _FakeSTT(transcript)


@pytest.mark.unit
async def test_v1_audio_transcriptions(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    _with_fake_stt(client, "talk to my agent")
    r = client.post(
        "/v1/audio/transcriptions",
        headers=headers,
        data={"model": "whisper-1"},
        files={"file": ("clip.wav", b"\x00\x01\x02fake-audio-bytes", "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "talk to my agent"}


@pytest.mark.unit
async def test_v1_audio_transcriptions_requires_auth(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, _ = await _client(InMemoryStorage(), tmp_path)
    _with_fake_stt(client)
    r = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"fake-audio-bytes", "audio/wav")},
    )
    assert r.status_code == 401


@pytest.mark.unit
async def test_v1_audio_transcriptions_empty_file_400(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    _with_fake_stt(client)
    r = client.post(
        "/v1/audio/transcriptions",
        headers=headers,
        files={"file": ("clip.wav", b"", "audio/wav")},
    )
    assert r.status_code == 400


@pytest.mark.unit
async def test_v1_audio_transcriptions_missing_file_422(tmp_path: Path) -> None:
    _write_echo_agent(tmp_path)
    client, headers = await _client(InMemoryStorage(), tmp_path)
    _with_fake_stt(client)
    # No multipart ``file`` part → FastAPI request validation rejects it.
    r = client.post("/v1/audio/transcriptions", headers=headers, data={"model": "whisper-1"})
    assert r.status_code == 422
