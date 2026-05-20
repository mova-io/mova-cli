"""Tests for ``PlaygroundClient`` thread methods (PR-P).

The Chainlit app calls these to drive thread-aware mode:

* ``create_thread`` — POST /api/v1/threads
* ``list_threads`` — GET /api/v1/threads
* ``get_thread`` — GET /api/v1/threads/{id}
* ``submit_thread_message`` — POST /api/v1/threads/{id}/messages

Each test verifies the right HTTP shape (path, method, payload)
via httpx.MockTransport; the runtime endpoints themselves are
covered by PR-O / PR-Q integration tests.
"""

from __future__ import annotations

import httpx
import pytest

from movate.playground.client import PlaygroundClient, PlaygroundClientConfig


def _mock_client(handler) -> PlaygroundClient:
    """Build a PlaygroundClient wired to a MockTransport."""
    transport = httpx.MockTransport(handler)
    client = PlaygroundClient(
        PlaygroundClientConfig(runtime_url="http://runtime.example", api_key="t0k3n")
    )
    # Swap the inner httpx client for one with the mock transport.
    # We close the original first since it'd hold sockets otherwise.
    import asyncio  # noqa: PLC0415

    asyncio.new_event_loop().run_until_complete(client.aclose())
    client._client = httpx.AsyncClient(
        base_url="http://runtime.example",
        transport=transport,
        headers={"Authorization": "Bearer t0k3n"},
    )
    return client


# ---------------------------------------------------------------------------
# create_thread
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_create_thread_posts_correct_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(
            201,
            json={
                "thread_id": "t_abc123",
                "tenant_id": "tenant_x",
                "agent": "rag-qa",
                "title": "First thread",
                "created_at": "2026-05-20T10:00:00+00:00",
                "updated_at": "2026-05-20T10:00:00+00:00",
                "runs": None,
            },
        )

    client = _mock_client(handler)
    result = await client.create_thread(agent="rag-qa", title="First thread")
    assert captured["method"] == "POST"
    assert captured["url"] == "http://runtime.example/api/v1/threads"
    body_bytes = captured["body"]
    assert isinstance(body_bytes, bytes)
    assert b"rag-qa" in body_bytes
    assert b"First thread" in body_bytes
    assert result["thread_id"] == "t_abc123"


@pytest.mark.unit
async def test_create_thread_omits_empty_title() -> None:
    """Empty title isn't sent over the wire — keeps the request slim
    + lets the runtime apply its own default."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            201,
            json={
                "thread_id": "t_xyz",
                "tenant_id": "tenant_x",
                "agent": "faq",
                "title": "",
                "created_at": "2026-05-20T10:00:00+00:00",
                "updated_at": "2026-05-20T10:00:00+00:00",
                "runs": None,
            },
        )

    client = _mock_client(handler)
    await client.create_thread(agent="faq")
    body_bytes = captured["body"]
    assert isinstance(body_bytes, bytes)
    assert b'"agent":"faq"' in body_bytes
    # No title key.
    assert b"title" not in body_bytes


# ---------------------------------------------------------------------------
# list_threads
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_threads_passes_filter_params() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"threads": [], "count": 0})

    client = _mock_client(handler)
    result = await client.list_threads(agent="rag-qa", limit=20)
    url = captured["url"]
    assert isinstance(url, str)
    assert "agent=rag-qa" in url
    assert "limit=20" in url
    assert result == []


@pytest.mark.unit
async def test_list_threads_omits_agent_when_none() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "threads": [
                    {
                        "thread_id": "t_a",
                        "tenant_id": "x",
                        "agent": "rag-qa",
                        "title": "",
                        "created_at": "2026-05-20T10:00:00+00:00",
                        "updated_at": "2026-05-20T10:00:00+00:00",
                        "runs": None,
                    }
                ],
                "count": 1,
            },
        )

    client = _mock_client(handler)
    result = await client.list_threads()
    url = captured["url"]
    assert isinstance(url, str)
    assert "agent=" not in url
    assert len(result) == 1
    assert result[0]["thread_id"] == "t_a"


# ---------------------------------------------------------------------------
# get_thread
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_get_thread_includes_runs_by_default() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "thread_id": "t_abc",
                "tenant_id": "x",
                "agent": "rag-qa",
                "title": "",
                "created_at": "2026-05-20T10:00:00+00:00",
                "updated_at": "2026-05-20T10:00:00+00:00",
                "runs": [],
            },
        )

    client = _mock_client(handler)
    await client.get_thread("t_abc")
    url = captured["url"]
    assert isinstance(url, str)
    assert "include_runs=true" in url


@pytest.mark.unit
async def test_get_thread_can_skip_runs_for_fast_path() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "thread_id": "t_abc",
                "tenant_id": "x",
                "agent": "rag-qa",
                "title": "",
                "created_at": "2026-05-20T10:00:00+00:00",
                "updated_at": "2026-05-20T10:00:00+00:00",
                "runs": None,
            },
        )

    client = _mock_client(handler)
    await client.get_thread("t_abc", include_runs=False)
    url = captured["url"]
    assert isinstance(url, str)
    assert "include_runs=false" in url


# ---------------------------------------------------------------------------
# submit_thread_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_submit_thread_message_posts_to_messages_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(202, json={"job_id": "j_x", "status": "queued"})

    client = _mock_client(handler)
    result = await client.submit_thread_message(
        thread_id="t_abc",
        input_data={"question": "and what about prorated?"},
    )
    url = captured["url"]
    assert isinstance(url, str)
    assert url.endswith("/api/v1/threads/t_abc/messages")
    body_bytes = captured["body"]
    assert isinstance(body_bytes, bytes)
    assert b"prorated" in body_bytes
    assert result["job_id"] == "j_x"


@pytest.mark.unit
async def test_thread_methods_raise_on_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"detail": "thread not found"})

    client = _mock_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_thread("missing")
    with pytest.raises(httpx.HTTPStatusError):
        await client.submit_thread_message(
            thread_id="missing", input_data={"q": "x"}
        )
