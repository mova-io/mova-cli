"""``mdk kb <cmd> --target`` 401 auto-recovery (ADR 012a / D1).

When a ``--target`` call returns 401, the CLI transparently attempts ONE
programmatic key refresh (reusing the existing
``refresh_runtime_key_inline``) and retries the request exactly once with
the fresh bearer. A second 401 — or a refresh that couldn't run — falls
back to today's behavior: the manual ``refresh-runtime-key`` hint + exit 2.

Mirrors the httpx-MockTransport + ``MOVATE_CONFIG_PATH`` fixture pattern
from ``test_kb_management_remote_target.py``. ``refresh_runtime_key_inline``
is always mocked here so no real ``az`` is invoked.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.auth import RefreshRuntimeKeyError
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

_OLD_KEY = "mvt_dev_t1_k1_oldsecret"
_NEW_KEY = "mvt_dev_t1_k1_freshsecret"


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
    monkeypatch.setenv("MDK_DEV_KEY", _OLD_KEY)


class _SequenceTransport:
    """A MockTransport-like handler that returns a scripted list of
    responses, one per request, and records every request's bearer."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.bearers: list[str | None] = []
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.bearers.append(request.headers.get("authorization"))
        # Last scripted response repeats if more requests arrive than scripted.
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


def _mock_refresh_success(
    monkeypatch: pytest.MonkeyPatch,
    *,
    counter: list[int],
    new_key: str = _NEW_KEY,
) -> None:
    """Patch ``refresh_runtime_key_inline`` to "succeed": bumps ``counter``,
    returns a fresh ``(key, env_var)`` like the real function. The recovery
    helper mirrors the returned key into ``MDK_DEV_KEY`` so the retry
    re-resolves the new bearer — exactly what we assert."""

    def fake_refresh(target: str, **kwargs: object) -> tuple[str, str]:
        counter.append(1)
        return new_key, "MDK_DEV_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)


def _mock_refresh_raises(monkeypatch: pytest.MonkeyPatch, *, counter: list[int]) -> None:
    """Patch ``refresh_runtime_key_inline`` to raise ``RefreshRuntimeKeyError``
    (the "target isn't an Azure Container App / az unavailable" case)."""

    def fake_refresh(target: str, **kwargs: object) -> tuple[str, str]:
        counter.append(1)
        raise RefreshRuntimeKeyError("not an Azure Container App")

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)


# ---------------------------------------------------------------------------
# 1. 401 → refresh succeeds → retry → 200
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_401_refresh_then_retry_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    refreshed: list[int] = []
    _mock_refresh_success(monkeypatch, counter=refreshed)

    body = {
        "agent_name": "rag-qa",
        "total_chunks": 5,
        "total_chars": 1234,
        "ocr_chunks": 0,
        "sources": [],
        "models": ["openai/text-embedding-3-small"],
    }
    transport_handler = _SequenceTransport(
        [
            httpx.Response(401, json={"detail": "auth"}),
            httpx.Response(200, json=body),
        ]
    )
    _make_client_factory(httpx.MockTransport(transport_handler), monkeypatch)

    result = runner.invoke(
        app, ["kb", "stats", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    # Refreshed exactly once.
    assert len(refreshed) == 1
    # Two requests went out: original (old key) + retry (new key).
    assert transport_handler.calls == 2
    assert transport_handler.bearers[0] == f"Bearer {_OLD_KEY}"
    assert transport_handler.bearers[1] == f"Bearer {_NEW_KEY}"
    # Success body rendered, no manual hint surfaced.
    out = result.stdout + result.stderr
    assert "total chunks" in out
    assert "refresh-runtime-key" not in out


# ---------------------------------------------------------------------------
# 2. 401 → refresh succeeds → retry still 401  (no loop; exit 2 + hint)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_401_refresh_then_retry_still_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    refreshed: list[int] = []
    _mock_refresh_success(monkeypatch, counter=refreshed)

    # Every request 401s — models the ephemeral-backend case where the fresh
    # key dies immediately. Recovery must NOT loop.
    transport_handler = _SequenceTransport([httpx.Response(401, json={"detail": "auth"})])
    _make_client_factory(httpx.MockTransport(transport_handler), monkeypatch)

    result = runner.invoke(
        app, ["kb", "stats", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 2
    # Refreshed exactly once — no thundering-herd of refreshes.
    assert len(refreshed) == 1
    # Exactly one retry after the original: two requests total, no more.
    assert transport_handler.calls == 2
    assert transport_handler.bearers[0] == f"Bearer {_OLD_KEY}"
    assert transport_handler.bearers[1] == f"Bearer {_NEW_KEY}"
    assert "refresh-runtime-key" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# 3. 401 → refresh raises RefreshRuntimeKeyError → manual hint + exit 2
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_401_refresh_raises_falls_back_to_manual_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _common_env(tmp_path, monkeypatch)
    refreshed: list[int] = []
    _mock_refresh_raises(monkeypatch, counter=refreshed)

    transport_handler = _SequenceTransport([httpx.Response(401, json={"detail": "auth"})])
    _make_client_factory(httpx.MockTransport(transport_handler), monkeypatch)

    result = runner.invoke(
        app, ["kb", "stats", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 2
    # Refresh was attempted once (and raised), and there was NO retry request.
    assert len(refreshed) == 1
    assert transport_handler.calls == 1
    assert transport_handler.bearers[0] == f"Bearer {_OLD_KEY}"
    assert "refresh-runtime-key" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# 4a. Non-401 error path unchanged — a 500 never triggers a refresh.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_500_does_not_trigger_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _common_env(tmp_path, monkeypatch)
    refreshed: list[int] = []
    # If recovery were (wrongly) invoked, this would bump the counter.
    _mock_refresh_success(monkeypatch, counter=refreshed)

    transport_handler = _SequenceTransport([httpx.Response(500, text="boom")])
    _make_client_factory(httpx.MockTransport(transport_handler), monkeypatch)

    result = runner.invoke(
        app, ["kb", "stats", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 2
    assert len(refreshed) == 0
    assert transport_handler.calls == 1
    out = result.stdout + result.stderr
    assert "HTTP 500" in out
    assert "refresh-runtime-key" not in out


# ---------------------------------------------------------------------------
# 4b. Network-error path unchanged — no refresh attempt.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_network_error_does_not_trigger_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _common_env(tmp_path, monkeypatch)
    refreshed: list[int] = []
    _mock_refresh_success(monkeypatch, counter=refreshed)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _make_client_factory(httpx.MockTransport(boom), monkeypatch)

    result = runner.invoke(
        app, ["kb", "stats", "rag-qa", "--target", "dev"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 2
    assert len(refreshed) == 0
    assert "could not reach" in (result.stdout + result.stderr)
