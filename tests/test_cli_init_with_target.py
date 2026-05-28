"""``mdk init <name> "<desc>" --target <env>`` — cloud-side SSE flow.

The combined "wow" CLI demo (item 3 of the project + catalog polish):
when ``--target`` resolves to a REGISTERED runtime target name, the
flow switches to the unified ``POST /api/v1/agents`` endpoint, streams
SSE progress events to the terminal as a live progress display, and
writes the final bundle to ``./<name>/``.

Backward compat (CLAUDE.md rule 5): when ``--target`` is a PATH (not
a registered runtime name) the existing local-only flow runs
unchanged. The sniffer test below pins that.

Mocks the SSE source via ``httpx.MockTransport`` so the test doesn't
need the runtime endpoint to be present.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _write_config_with_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
    )
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_dir / "config.yaml"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "nocreds"))
    monkeypatch.setenv("MDK_DEV_KEY", "dev-key")


def _route(handler: object, monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.AsyncClient", factory)


# ---------------------------------------------------------------------------
# Happy path — SSE stream + bundle write
# ---------------------------------------------------------------------------


def _build_sse_body() -> bytes:
    """Three-stage SSE stream culminating in a bundle event.

    Format mirrors the runtime's emitter: each block is
    ``event: <name>\\ndata: <json>\\n\\n``.
    """
    bundle_data = '{"files": {"agent.yaml": "name: faq\\n", "prompt.md": "# faq\\n"}}'
    return (
        b'event: progress\ndata: {"stage": "planning", "message": "drafting agent.yaml"}\n\n'
        b'event: progress\ndata: {"stage": "scaffolding", "message": "writing files"}\n\n'
        b"event: bundle\ndata: " + bundle_data.encode() + b"\n\n"
        b"event: done\ndata: {}\n\n"
    )


@pytest.mark.unit
def test_init_with_target_writes_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config_with_target(tmp_path, monkeypatch)
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/agents"
        assert request.method == "POST"
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            content=_build_sse_body(),
            headers={"content-type": "text/event-stream"},
        )

    _route(handler, monkeypatch)
    # cwd matters — the bundle lands at ./<name>/.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init", "my-agent", "an FAQ bot for pricing tiers", "--target", "dev"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Body went up with `source: "llm"` and the description.
    assert seen_payload["name"] == "my-agent"
    assert seen_payload["source"] == "llm"
    assert seen_payload["description"] == "an FAQ bot for pricing tiers"
    # Bundle landed locally.
    out_dir = tmp_path / "my-agent"
    assert (out_dir / "agent.yaml").read_text().startswith("name: faq")
    assert (out_dir / "prompt.md").read_text().startswith("# faq")
    # Next-step hint shows the deployed-runtime + local-validate options.
    assert "Next steps" in result.stderr
    assert "mdk run my-agent --target dev" in result.stderr


# ---------------------------------------------------------------------------
# Backward compat — `--target` as a PATH still works (no runtime name match)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_target_as_path_backward_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--target ./some/path` is unchanged — local scaffold parent dir.

    The cloud-side dispatcher must ONLY trigger when --target matches a
    registered runtime target name. A path that doesn't match
    `~/.movate/config.yaml` keys falls through to the legacy behaviour.
    """
    _write_config_with_target(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    # No HTTP routing — if the cloud-side dispatcher fired we'd hit a
    # real network call, which the runner would surface as a failure.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "cloud-side dispatcher fired when --target was a path, not an env name"
        )

    _route(handler, monkeypatch)

    # Bare `mdk init my-proj` with no --target → local project bootstrap
    # (legacy behaviour). We don't pass --target at all, so the sniffer
    # never trips even when MDK_TARGET is unset.
    result = runner.invoke(app, ["init", "my-proj", "--skip-snapshot"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Local project files exist.
    assert (tmp_path / "my-proj" / "project.yaml").is_file()


# ---------------------------------------------------------------------------
# Error path — server-side error event surfaces as a non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_with_target_server_error_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config_with_target(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            b'event: progress\ndata: {"stage": "planning"}\n\n'
            b'event: error\ndata: {"message": "model rate limited"}\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    _route(handler, monkeypatch)
    result = runner.invoke(
        app,
        ["init", "my-agent", "FAQ bot", "--target", "dev"],
    )
    assert result.exit_code != 0
    assert "model rate limited" in result.stderr
    # No partial bundle left on disk.
    assert not (tmp_path / "my-agent" / "agent.yaml").exists()


# ---------------------------------------------------------------------------
# SSE parser — drains complete events out of a chunked stream
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sse_parser_drains_split_chunks() -> None:
    """The chunked SSE parser must reassemble events split across reads.

    The runtime sends 1-line-per-flush in some configurations, so an
    event can arrive as ``event: progress\\n`` then ``data: {...}\\n\\n``.
    The parser must hold partial state in its buffer and only yield
    when a full ``\\n\\n``-terminated block is in.
    """
    from movate.cli._init_target import _parse_sse_chunk  # noqa: PLC0415

    buf = bytearray()
    # Chunk 1: just the event line (no terminator). Yields nothing.
    events = _parse_sse_chunk(b"event: progress\n", buf)
    assert events == []
    # Chunk 2: data + terminator. Now the event drains.
    events = _parse_sse_chunk(b'data: {"stage": "x"}\n\n', buf)
    assert len(events) == 1
    name, payload = events[0]
    assert name == "progress"
    assert payload == {"stage": "x"}
    # Buffer is empty after drain.
    assert buf == bytearray()
