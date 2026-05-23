"""``mdk kb {list,stats,search,clear} --target <env>`` — remote KB management.

The remote twins of the local inspect/manage commands: instead of hitting
local sqlite, ``--target`` resolves the target's URL + bearer from
``~/.movate/config.yaml`` and calls the matching runtime endpoint:

* list   → GET    /api/v1/agents/<agent>/kb
* stats  → GET    /api/v1/agents/<agent>/kb/stats
* search → POST   /api/v1/agents/<agent>/kb/search
* clear  → DELETE /api/v1/agents/<agent>/kb

Mirrors the httpx-MockTransport pattern from
``test_kb_ingest_remote_target.py`` — assert the right method/path/bearer
go out, and that 401 / 404 surface as actionable exit-code-2 errors.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _make_client_factory(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the lazily-imported ``httpx.Client`` through a MockTransport."""
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.Client", factory)


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


def _common_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_user_config(tmp_path)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    # Isolate from the developer's real ~/.movate/credentials autoload.
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "nocreds"))
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")


def _capture_handler(captured: dict[str, object], response: httpx.Response) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return response

    return handler


# ---------------------------------------------------------------------------
# list --target
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_target_calls_get_kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    body = {
        "agent_name": "rag-qa",
        "chunks": [
            {
                "chunk_id": "c1",
                "source": "refund.md",
                "text": "Annual subs refundable within 14 days.",
                "embedding_model": "openai/text-embedding-3-small",
                "content_hash": "abc",
                "ocr": False,
                "metadata": None,
                "created_at": "2026-05-22T00:00:00+00:00",
            }
        ],
        "count": 1,
    }
    _make_client_factory(
        httpx.MockTransport(_capture_handler(captured, httpx.Response(200, json=body))),  # type: ignore[arg-type]
        monkeypatch,
    )
    result = runner.invoke(
        app,
        ["kb", "list", "rag-qa", "--source", "refund.md", "--target", "dev"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["method"] == "GET"
    assert "/api/v1/agents/rag-qa/kb" in str(captured["url"])
    assert "source=refund.md" in str(captured["url"])
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    assert "refund.md" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_list_target_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": "nope"})),
        monkeypatch,
    )
    result = runner.invoke(app, ["kb", "list", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    out = (result.stdout + result.stderr).lower()
    assert "not found" in out or "deploy" in out


@pytest.mark.unit
def test_list_target_missing_bearer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    result = runner.invoke(app, ["kb", "list", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    assert "MDK_DEV_KEY" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# stats --target
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stats_target_calls_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    body = {
        "agent_name": "rag-qa",
        "total_chunks": 5,
        "total_chars": 1234,
        "ocr_chunks": 0,
        "sources": [
            {"source": "refund.md", "chunks": 3, "chars": 800},
            {"source": "hours.txt", "chunks": 2, "chars": 434},
        ],
        "models": ["openai/text-embedding-3-small"],
    }
    _make_client_factory(
        httpx.MockTransport(_capture_handler(captured, httpx.Response(200, json=body))),  # type: ignore[arg-type]
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "stats", "rag-qa", "--by-source", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["method"] == "GET"
    assert "/api/v1/agents/rag-qa/kb/stats" in str(captured["url"])
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    out = result.stdout + result.stderr
    assert "total chunks" in out
    assert "refund.md" in out


@pytest.mark.unit
def test_stats_target_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(401, json={"detail": "auth"})),
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "stats", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    assert "refresh-runtime-key" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# search --target
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_target_posts_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    body = {
        "agent_name": "rag-qa",
        "question": "Can I get a refund?",
        "results": [
            {
                "chunk_id": "c1",
                "source": "refund.md",
                "text": "Annual subs refundable within 14 days.",
                "embedding_model": "openai/text-embedding-3-small",
                "score": 0.91,
                "ocr": False,
                "metadata": None,
            }
        ],
        "count": 1,
    }
    _make_client_factory(
        httpx.MockTransport(_capture_handler(captured, httpx.Response(200, json=body))),  # type: ignore[arg-type]
        monkeypatch,
    )
    argv = [
        "kb",
        "search",
        "rag-qa",
        "Can I get a refund?",
        "--k",
        "3",
        "--hybrid",
        "--target",
        "dev",
    ]
    result = runner.invoke(app, argv, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["method"] == "POST"
    assert "/api/v1/agents/rag-qa/kb/search" in str(captured["url"])
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    sent = captured["body"]
    assert isinstance(sent, bytes)
    assert b"Can I get a refund?" in sent
    assert b'"hybrid":true' in sent.replace(b" ", b"")
    out = result.stdout + result.stderr
    assert "refund.md" in out
    assert "0.91" in out


@pytest.mark.unit
def test_search_target_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": "nope"})),
        monkeypatch,
    )
    result = runner.invoke(
        app,
        ["kb", "search", "rag-qa", "anything", "--target", "dev"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    out = (result.stdout + result.stderr).lower()
    assert "not found" in out or "deploy" in out


@pytest.mark.unit
def test_search_target_no_local_key_needed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The remote search embeds server-side, so the CLI must NOT require a
    local OPENAI_API_KEY (unlike the local search path)."""
    _common_env(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = {"agent_name": "rag-qa", "question": "x", "results": [], "count": 0}
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "search", "rag-qa", "x", "--target", "dev"], env={"COLUMNS": "200"}
    )
    # No "no API key found" error, and no crash — empty-KB hint is fine.
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no API key" not in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# clear --target
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_clear_target_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    body = {"agent_name": "rag-qa", "deleted": 7, "source": None}
    _make_client_factory(
        httpx.MockTransport(_capture_handler(captured, httpx.Response(200, json=body))),  # type: ignore[arg-type]
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "clear", "rag-qa", "--yes", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["method"] == "DELETE"
    assert "/api/v1/agents/rag-qa/kb" in str(captured["url"])
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    assert "deleted 7" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_clear_target_source_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    body = {"agent_name": "rag-qa", "deleted": 3, "source": "refund.md"}
    _make_client_factory(
        httpx.MockTransport(_capture_handler(captured, httpx.Response(200, json=body))),  # type: ignore[arg-type]
        monkeypatch,
    )
    result = runner.invoke(
        app,
        ["kb", "clear", "rag-qa", "--source", "refund.md", "--yes", "--target", "dev"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "source=refund.md" in str(captured["url"])
    assert "deleted 3" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_clear_target_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": "nope"})),
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "clear", "rag-qa", "--yes", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    out = (result.stdout + result.stderr).lower()
    assert "not found" in out or "deploy" in out
