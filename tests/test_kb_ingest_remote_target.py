"""``mdk kb ingest --target <env>`` — upload KB docs to a deployed runtime.

Instead of ingesting into local sqlite, ``--target`` resolves the target's
URL + bearer from ``~/.movate/config.yaml`` and POSTs the supported files to
``POST /api/v1/agents/<agent>/kb``, where the runtime parses + embeds them
into ITS storage (Azure Postgres in prod). This is the "deploy KB to Azure"
path — no embedding key or DB connection needed on the laptop.

Mirrors the httpx-MockTransport pattern from test_run_remote_target.py.
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


def _kb_dir(tmp_path: Path) -> Path:
    """A kb/ dir with one supported file + one unsupported (client-skipped)."""
    d = tmp_path / "kb"
    d.mkdir()
    (d / "refund-policy.md").write_text("# Refund\nAnnual subs refundable within 14 days.\n")
    (d / "notes.xyz").write_text("unsupported extension — filtered client-side")
    return d


def _ingest_view(saved: int = 7) -> dict[str, object]:
    return {
        "agent_name": "rag-qa",
        "files": [
            {
                "source": "refund-policy.md",
                "status": "ingested",
                "chunks_total": saved,
                "chunks_saved": saved,
                "embedding_model": "openai/text-embedding-3-small",
            }
        ],
        "total_chunks_saved": saved,
    }


def _common_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_user_config(tmp_path)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    # Isolate from the developer's real ~/.movate/credentials autoload.
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "nocreds"))


@pytest.mark.unit
def test_target_uploads_supported_files_and_renders_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _common_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")
    kb = _kb_dir(tmp_path)

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json=_ingest_view())

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["kb", "ingest", "rag-qa", str(kb), "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["method"] == "POST"
    assert "/api/v1/agents/rag-qa/kb" in str(captured["url"])
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    # The supported file is in the multipart body; the unsupported one is
    # filtered out client-side before upload.
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b"refund-policy.md" in body
    assert b"notes.xyz" not in body
    out = result.stdout + result.stderr
    assert "refund-policy.md" in out
    assert "mdk_kb_ingest_summary:" in out
    assert "target=dev" in out
    assert "ingested=1" in out
    assert "ok=true" in out


@pytest.mark.unit
def test_target_dry_run_lists_without_uploading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _common_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")
    kb = _kb_dir(tmp_path)

    hit = {"called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        hit["called"] = True
        return httpx.Response(200, json=_ingest_view())

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app,
        ["kb", "ingest", "rag-qa", str(kb), "--target", "dev", "--dry-run"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert hit["called"] is False  # nothing uploaded
    out = result.stdout + result.stderr
    assert "Would upload" in out
    assert "refund-policy.md" in out
    assert "dry-run" in out


@pytest.mark.unit
def test_target_missing_bearer_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    kb = _kb_dir(tmp_path)

    result = runner.invoke(
        app, ["kb", "ingest", "rag-qa", str(kb), "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    assert "MDK_DEV_KEY" in (result.stdout + result.stderr)


@pytest.mark.unit
def test_target_404_hints_deploy_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")
    kb = _kb_dir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "agent not found"})

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["kb", "ingest", "rag-qa", str(kb), "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    out = (result.stdout + result.stderr).lower()
    assert "not found" in out or "deploy" in out
