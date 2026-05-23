"""``mdk kb reindex <agent> --target <env>`` — remote KB reindex.

The remote twin of the local reindex command: ``--target`` resolves the
target's URL + bearer from ``~/.movate/config.yaml`` and calls
``POST /api/v1/agents/<agent>/kb/reindex`` with the ``reembed`` flag in
the body. Mirrors the httpx-MockTransport pattern from
``test_kb_management_remote_target.py`` — assert the right
method/path/bearer/body go out, that ``--reembed`` is conveyed, and that
401 / 404 surface as actionable exit-code-2 errors. The ``--reembed``
confirmation guard is bypassed with ``--yes`` in scripted tests.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _make_client_factory(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.unit
def test_reindex_target_default_no_reembed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    body = {
        "agent": "rag-qa",
        "reembed": False,
        "chunks_reembedded": 0,
        "index_rebuilt": True,
        "backend": "postgres",
    }
    _make_client_factory(
        httpx.MockTransport(_capture_handler(captured, httpx.Response(200, json=body))),  # type: ignore[arg-type]
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "reindex", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["method"] == "POST"
    assert "/api/v1/agents/rag-qa/kb/reindex" in str(captured["url"])
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    sent = captured["body"]
    assert isinstance(sent, bytes)
    # Default path conveys reembed=false.
    assert b'"reembed":false' in sent.replace(b" ", b"")
    out = result.stdout + result.stderr
    assert "rebuilt the vector index" in out


@pytest.mark.unit
def test_reindex_target_reembed_conveys_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _common_env(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    body = {
        "agent": "rag-qa",
        "reembed": True,
        "chunks_reembedded": 12,
        "index_rebuilt": True,
        "backend": "postgres",
    }
    _make_client_factory(
        httpx.MockTransport(_capture_handler(captured, httpx.Response(200, json=body))),  # type: ignore[arg-type]
        monkeypatch,
    )
    # --yes bypasses the cost confirmation prompt.
    result = runner.invoke(
        app,
        ["kb", "reindex", "rag-qa", "--reembed", "--yes", "--target", "dev"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    sent = captured["body"]
    assert isinstance(sent, bytes)
    assert b'"reembed":true' in sent.replace(b" ", b"")
    out = result.stdout + result.stderr
    assert "re-embedded 12" in out


@pytest.mark.unit
def test_reindex_target_reembed_aborts_without_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--reembed without --yes prompts for confirmation; declining aborts
    BEFORE any HTTP request is made (no transport configured → if it
    tried to call out, the test would error)."""
    _common_env(tmp_path, monkeypatch)
    # Answer "n" to the cost-confirmation prompt.
    result = runner.invoke(
        app,
        ["kb", "reindex", "rag-qa", "--reembed", "--target", "dev"],
        input="n\n",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    assert "aborted" in (result.stdout + result.stderr).lower()


@pytest.mark.unit
def test_reindex_target_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": "nope"})),
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "reindex", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    out = (result.stdout + result.stderr).lower()
    assert "not found" in out or "deploy" in out


@pytest.mark.unit
def test_reindex_target_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(401, json={"detail": "auth"})),
        monkeypatch,
    )
    result = runner.invoke(
        app, ["kb", "reindex", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    assert "refresh-runtime-key" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_reindex_target_no_local_key_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remote reindex re-embeds server-side, so the CLI must NOT
    require a local OPENAI_API_KEY even with --reembed."""
    _common_env(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = {
        "agent": "rag-qa",
        "reembed": True,
        "chunks_reembedded": 3,
        "index_rebuilt": True,
        "backend": "postgres",
    }
    _make_client_factory(
        httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
        monkeypatch,
    )
    result = runner.invoke(
        app,
        ["kb", "reindex", "rag-qa", "--reembed", "--yes", "--target", "dev"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no API key" not in (result.stdout + result.stderr)
