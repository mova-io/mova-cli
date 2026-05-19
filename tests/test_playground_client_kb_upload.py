"""Tests for ``PlaygroundClient.upload_kb_files`` — multipart wire shape.

The runtime endpoint is covered by ``test_runtime_kb_upload``; this
test fixture exists to ensure the Chainlit-side client builds the
multipart payload the runtime expects (repeating ``files`` field,
correct filenames, raw bytes per file). Catches future regressions
when somebody refactors the client method.
"""

from __future__ import annotations

import json

import httpx
import pytest

from movate.playground.client import PlaygroundClient, PlaygroundClientConfig


@pytest.mark.unit
async def test_upload_kb_files_posts_repeating_files_field() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "agent_name": "demo",
                "total_chunks_saved": 2,
                "files": [
                    {
                        "source": "a.md",
                        "status": "ingested",
                        "chunks_total": 1,
                        "chunks_saved": 1,
                        "embedding_model": "openai/text-embedding-3-small",
                    },
                    {
                        "source": "b.md",
                        "status": "ingested",
                        "chunks_total": 1,
                        "chunks_saved": 1,
                        "embedding_model": "openai/text-embedding-3-small",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = PlaygroundClient(
        PlaygroundClientConfig(runtime_url="http://runtime.example", api_key="t0k3n")
    )
    # Swap in the mock transport.
    await client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://runtime.example",
        transport=transport,
        headers={"Authorization": "Bearer t0k3n"},
    )

    result = await client.upload_kb_files(
        agent="demo",
        files=[
            ("a.md", b"# A\n\nFirst document.\n"),
            ("b.md", b"# B\n\nSecond document.\n"),
        ],
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://runtime.example/api/v1/agents/demo/kb"
    assert isinstance(captured["content_type"], str)
    assert captured["content_type"].startswith("multipart/form-data;")
    # Both filenames should appear in the multipart body somewhere.
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'filename="a.md"' in body
    assert b'filename="b.md"' in body
    assert b"First document" in body
    assert b"Second document" in body

    # Response is the parsed runtime payload.
    assert result["total_chunks_saved"] == 2
    assert len(result["files"]) == 2
    assert json.dumps(result)  # serialisable round-trip


@pytest.mark.unit
async def test_upload_kb_files_raises_on_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"detail": "agent not found"})

    transport = httpx.MockTransport(handler)
    client = PlaygroundClient(
        PlaygroundClientConfig(runtime_url="http://runtime.example", api_key=None)
    )
    await client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://runtime.example",
        transport=transport,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_kb_files(
            agent="missing",
            files=[("x.md", b"# X")],
        )
