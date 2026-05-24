"""PR #107 — ``mdk run <agent> --target dev`` runs against a deployed runtime.

Closes the demo loop: scaffold → eval → deploy → run on Azure with one
command. Symmetric to ``mdk deploy --target``: resolves the target's
URL + key_env from ``~/.movate/config.yaml``, reads the bearer from the
named env var, POSTs ``/api/v1/agents/<name>/runs?wait=true``, renders
the resulting :class:`RunView` to stdout, and emits a greppable
``mdk_run_summary: target=<name>`` line on stderr.

Tested here:

1. Happy path — RunView round-trips, summary line carries ``target=``.
2. Bearer env var resolution — empty/missing → exit 2 with a hint
   pointing at ``mdk auth save-runtime-key``.
3. HTTP 401 → friendly hint pointing at the env var + recovery path.
4. HTTP 404 → friendly hint suggesting ``mdk deploy --target`` first.
5. HTTP 422 → surfaces the runtime's validation envelope.
6. Mutex with ``--replay`` and ``--stream``.
7. Bare-name URL anchoring — ``./agents/faq`` and ``faq`` both reach
   ``/api/v1/agents/faq/runs``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _make_client_factory(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``httpx.Client`` so calls inside _dispatch_remote_agent
    route through a MockTransport instead of the network.

    The remote dispatch imports httpx lazily inside the function body;
    we patch the top-level module so the late import picks up the
    factory.
    """
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.Client", factory)


def _async_client_factory(transport: httpx.MockTransport):
    """Return a drop-in ``httpx.AsyncClient`` factory that forces calls
    through a MockTransport — the streaming remote path uses
    ``httpx.AsyncClient.stream`` rather than the sync ``httpx.Client``."""
    real_async = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_async(*args, **kwargs)  # type: ignore[arg-type]

    return factory


def _write_user_config(home: Path, target_name: str = "dev") -> None:
    """Stash a minimal ``~/.movate/config.yaml`` pointing at a fake URL.

    The target's ``key_env`` is ``MDK_DEV_KEY`` (same convention the
    autoload pattern from PR #96 uses). Tests set the env var
    separately via monkeypatch.
    """
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
    """Init + add one agent so the local bundle can auto-wrap a plain
    string into ``{question: "..."}``."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    return tmp_path / "proj"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_happy_path_renders_run_view_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-trip: POST to /api/v1/agents/faq/runs?wait=true, receive a
    RunView shape back, render JSON to stdout + summary on stderr. The
    summary's ``target=`` field is the PR #107 affordance — local runs
    don't have it; CI scrapers branch on its presence."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode() if request.content else ""
        return httpx.Response(
            200,
            json={
                "run_id": "11111111-2222-3333-4444-555555555555",
                "job_id": "job-xyz",
                "agent": "faq",
                "agent_version": "0.1.0",
                "prompt_hash": "deadbeef",
                "provider": "mock",
                "provider_version": "1.0",
                "pricing_version": "2024.05",
                "status": "success",
                "input": {"question": "hello"},
                "output": {"answer": "hi back"},
                "metrics": {
                    "cost_usd": 0.0012,
                    "latency_ms": 480,
                    "tokens": {"input": 12, "output": 4, "total": 16},
                },
                "error": None,
                "created_at": "2026-05-15T12:00:00Z",
                "workflow_run_id": None,
                "node_id": None,
            },
        )

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(app, ["run", "faq", "hello", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # Hit the agent-anchored endpoint with ?wait=true.
    assert captured["method"] == "POST"
    assert "/api/v1/agents/faq/runs" in str(captured["url"])
    assert "wait=true" in str(captured["url"])
    # Bearer threaded through from $MDK_DEV_KEY.
    assert captured["auth"] == "Bearer mvt_dev_t1_k1_secret"
    # Auto-wrapped the plain string from the local bundle's schema.
    assert '"question":"hello"' in str(captured["body"])
    # mock flag default false threaded through.
    assert '"mock":false' in str(captured["body"])
    # Output rendered to stdout (full RunView in JSON mode).
    assert '"answer": "hi back"' in result.stdout
    # Summary line on stderr carries the target= field.
    assert "mdk_run_summary:" in result.stderr
    assert "target=dev" in result.stderr
    assert "ok=true" in result.stderr
    assert "cost_usd=0.0012" in result.stderr


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_empty_env_var_exits_2_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the named env var is empty, the friendly preflight fires
    BEFORE any HTTP call — points the operator at
    ``mdk auth save-runtime-key`` (the autoload path from PR #96)."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)

    result = runner.invoke(app, ["run", "faq", "hello", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    assert "MDK_DEV_KEY" in result.stderr
    assert "mdk auth save-runtime-key" in result.stderr


@pytest.mark.unit
def test_target_401_renders_token_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 401 from the runtime → exit 1 + hint pointing at the env
    var + the recovery path. Symmetric with the 401 mapping in deploy."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_stale_key_value_xxxxxxxx")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(app, ["run", "faq", "hello", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "rejected the bearer token" in result.stderr
    # First 16 chars of the bearer included for ID without leaking the
    # secret in full.
    assert "mvt_stale_key_va" in result.stderr
    # Recovery hint surfaces the autoload command.
    assert "mdk auth save-runtime-key dev" in result.stderr
    # Summary line still emitted on the failure path so CI can scrape.
    assert "mdk_run_summary:" in result.stderr
    assert "ok=false" in result.stderr


@pytest.mark.unit
def test_target_404_suggests_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 404 → exit 1 + suggests ``mdk deploy --target <name>``.
    Common when an operator scaffolds a new agent and forgets to push
    it before running."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "agent ghost not found"})

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(app, ["run", "faq", "hello", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "not found on" in result.stderr or "not found" in result.stderr
    assert "mdk deploy --target dev" in result.stderr


@pytest.mark.unit
def test_target_422_surfaces_validation_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 422 → exit 1 + the raw detail envelope so the operator
    sees the failing field. Common shape from FastAPI: nested ``loc``
    + ``msg`` per field."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "input", "question"], "msg": "field required"}]},
        )

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(app, ["run", "faq", "hello", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "input rejected by runtime (422)" in result.stderr
    assert "field required" in result.stderr


@pytest.mark.unit
def test_target_run_status_error_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime returns 200 + a RunView with status=error (the
    agent ran but the executor errored — provider failure, etc.).
    Surface the error message + exit 1 so CI gates on it."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "run_id": "aaaa-bbbb",
                "job_id": "j",
                "agent": "faq",
                "agent_version": "0.1.0",
                "prompt_hash": "x",
                "provider": "litellm",
                "provider_version": "1.0",
                "pricing_version": "2024.05",
                "status": "error",
                "input": {"question": "hi"},
                "output": None,
                "metrics": {
                    "cost_usd": 0.0,
                    "latency_ms": 250,
                    "tokens": {"input": 5, "output": 0, "total": 5},
                },
                "error": {"type": "ProviderError", "message": "rate limited"},
                "created_at": "2026-05-15T12:00:00Z",
                "workflow_run_id": None,
                "node_id": None,
            },
        )

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(app, ["run", "faq", "hello", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "ProviderError" in result.stderr
    assert "rate limited" in result.stderr
    assert "mdk_run_summary:" in result.stderr
    assert "ok=false" in result.stderr


# ---------------------------------------------------------------------------
# Mutex flags
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_mutex_with_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--target`` + ``--replay`` is incoherent — remote RunRecord
    storage isn't replay-driver-aware in v0.7."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    result = runner.invoke(
        app, ["run", "faq", "--target", "dev", "--replay", "abcd"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


@pytest.mark.unit
def test_target_stream_is_now_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--target`` + ``--stream`` is no longer a mutex error (BACKLOG
    #75): it POSTs to the runtime's SSE endpoint and renders tokens.

    Regression guard against the old v0.7 guard creeping back. Full SSE
    consumption is covered in test_run_remote_stream.py — here we just
    confirm the CLI accepts the combination and hits the stream URL."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        sse = (
            'event: token\ndata: {"text": "{\\"answer\\": \\"hi\\"}"}\n\n'
            'event: done\ndata: {"run_id": "r1", "status": "success", '
            '"metrics": {"cost_usd": 0.0, "latency_ms": 5}, "output": {"answer": "hi"}}\n\n'
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    monkeypatch.setattr(
        "httpx.AsyncClient",
        _async_client_factory(httpx.MockTransport(handler)),
    )

    result = runner.invoke(
        app, ["run", "faq", "hi", "--target", "dev", "--stream"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "/api/v1/agents/faq/runs/stream" in str(captured["url"])


# ---------------------------------------------------------------------------
# Mock flag passthrough
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_mock_flag_threaded_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--target --mock`` sets ``mock: true`` in the request body so
    the runtime's inline-mode dispatch routes through MockProvider."""
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "run_id": "r1",
                "job_id": "j",
                "agent": "faq",
                "agent_version": "0.1.0",
                "prompt_hash": "x",
                "provider": "mock",
                "provider_version": "1.0",
                "pricing_version": "2024.05",
                "status": "success",
                "input": {"question": "hi"},
                "output": {"answer": "mock"},
                "metrics": {
                    "cost_usd": 0.0,
                    "latency_ms": 10,
                    "tokens": {"input": 1, "output": 1, "total": 2},
                },
                "error": None,
                "created_at": "2026-05-15T12:00:00Z",
                "workflow_run_id": None,
                "node_id": None,
            },
        )

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hi", "--target", "dev", "--mock"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert '"mock":true' in captured["body"]
