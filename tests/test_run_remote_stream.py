"""``mdk run <agent> --target dev --stream`` — consume the runtime's SSE
stream and render tokens live (BACKLOG #75).

The streaming sibling of test_run_remote_target.py. Covers:

1. The pure SSE-parsing helper (``parse_sse_events``) — frame splitting,
   JSON decode, comment/heartbeat skipping, malformed-frame tolerance.
2. Full CLI consumption: token deltas printed to stderr, final output to
   stdout, summary line carries ``target=`` + ``ok=true``, and the
   request hits ``/runs/stream``.
3. An ``error`` SSE frame → exit 1 + surfaced message.
4. A non-200 SSE response (403 missing scope) → friendly mapping.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.cli.run import parse_sse_events

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Pure parser unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_sse_events_basic_frames() -> None:
    """Two well-formed frames split on the blank line; data JSON-decoded."""
    lines = [
        "event: token",
        'data: {"text": "he"}',
        "",
        "event: token",
        'data: {"text": "llo"}',
        "",
        "event: done",
        'data: {"run_id": "r1", "status": "success"}',
        "",
    ]
    events = list(parse_sse_events(lines))
    assert events == [
        ("token", {"text": "he"}),
        ("token", {"text": "llo"}),
        ("done", {"run_id": "r1", "status": "success"}),
    ]


@pytest.mark.unit
def test_parse_sse_events_skips_comments_and_tolerates_garbage() -> None:
    """``:`` heartbeat lines are ignored; a non-JSON data line is wrapped
    as ``{"raw": ...}`` rather than crashing."""
    lines = [
        ": keep-alive heartbeat",
        "event: token",
        "data: not-json",
        "",
    ]
    events = list(parse_sse_events(lines))
    assert events == [("token", {"raw": "not-json"})]


@pytest.mark.unit
def test_parse_sse_events_trailing_crlf() -> None:
    """Trailing CR/LF on lines (as a raw byte stream might carry) is
    stripped before parsing."""
    lines = ["event: done\r", 'data: {"ok": true}\r', "\r"]
    assert list(parse_sse_events(lines)) == [("done", {"ok": True})]


# ---------------------------------------------------------------------------
# CLI fixtures (mirror test_run_remote_target.py)
# ---------------------------------------------------------------------------


def _async_client_factory(transport: httpx.MockTransport):
    real_async = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_async(*args, **kwargs)  # type: ignore[arg-type]

    return factory


def _write_user_config(home: Path, target_name: str = "dev") -> None:
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        f"active: {target_name}\n"
        f"targets:\n"
        f"  {target_name}:\n"
        f"    url: https://fake.example.com\n"
        f"    key_env: MDK_DEV_KEY\n"
    )


def _bootstrap_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    return tmp_path / "proj"


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")


# ---------------------------------------------------------------------------
# Full CLI consumption
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_renders_deltas_and_final_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI streams token deltas to stderr live and writes the final
    output to stdout; the summary line carries ``target=`` + ``ok=true``;
    the request hits the ``/runs/stream`` endpoint with the bearer."""
    _setup(tmp_path, monkeypatch)

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["accept"] = request.headers.get("accept")
        # Two token deltas reconstructing {"answer": "hi there"} + a done.
        sse = (
            'event: token\ndata: {"text": "{\\"answer\\": "}\n\n'
            'event: token\ndata: {"text": "\\"hi there\\"}"}\n\n'
            'event: done\ndata: {"run_id": "abcd1234-0000", "status": "success", '
            '"metrics": {"cost_usd": 0.0021, "latency_ms": 42}, '
            '"output": {"answer": "hi there"}}\n\n'
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    monkeypatch.setattr("httpx.AsyncClient", _async_client_factory(httpx.MockTransport(handler)))

    result = runner.invoke(
        app, ["run", "faq", "hi", "--target", "dev", "--stream"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # Hit the SSE endpoint with the bearer + SSE Accept header.
    assert captured["method"] == "POST"
    assert "/api/v1/agents/faq/runs/stream" in str(captured["url"])
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    assert "text/event-stream" in str(captured["accept"])

    # Token deltas were rendered live on stderr (concatenated).
    assert '{"answer": "hi there"}' in result.stderr
    # Final output (the done frame's output) rendered to stdout.
    assert '"answer": "hi there"' in result.stdout
    # Summary line is the remote shape with target= + ok=true.
    assert "mdk_run_summary:" in result.stderr
    assert "target=dev" in result.stderr
    assert "ok=true" in result.stderr
    assert "cost_usd=0.0021" in result.stderr


@pytest.mark.unit
def test_stream_error_frame_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``error`` SSE frame → exit 1 + the message surfaced on stderr;
    the summary line records ``ok=false``."""
    _setup(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        sse = (
            'event: token\ndata: {"text": "partial"}\n\n'
            'event: error\ndata: {"message": "model output is not valid JSON", '
            '"code": "schema_error"}\n\n'
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    monkeypatch.setattr("httpx.AsyncClient", _async_client_factory(httpx.MockTransport(handler)))

    result = runner.invoke(
        app, ["run", "faq", "hi", "--target", "dev", "--stream"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 1
    assert "run errored" in result.stderr
    assert "schema_error" in result.stderr
    assert "ok=false" in result.stderr


@pytest.mark.unit
def test_stream_403_missing_scope_maps_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 403 on the SSE endpoint (token lacks the run scope) → exit 1 +
    a friendly hint about the run scope."""
    _setup(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "missing required scope(s): run"})

    monkeypatch.setattr("httpx.AsyncClient", _async_client_factory(httpx.MockTransport(handler)))

    result = runner.invoke(
        app, ["run", "faq", "hi", "--target", "dev", "--stream"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 1
    assert "run" in result.stderr  # scope hint
    assert "ok=false" in result.stderr
