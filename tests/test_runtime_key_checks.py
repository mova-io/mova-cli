"""Unit tests for the shared runtime-key auth-check helpers (#103).

Both helpers were previously duplicated across ``cli/auth.py`` and
``cli/deploy.py`` and are now consolidated in
``movate.cli._runtime_key_checks``. These tests import each function
straight from the NEW shared module and exercise its core behavior:

* :func:`_warn_if_shell_shadows_runtime_key` — shell-shadow detection,
  both the warn and the silent branches.
* :func:`_verify_bearer_roundtrip` — the admin-capability round-trip,
  success and failure paths, against a mocked HTTP client.

The credentials store is auto-isolated per test by the
``_isolate_credentials_store`` autouse fixture in ``conftest.py`` (tmp
path, no real ``~/.movate/credentials``), so ``key_source`` resolves a
set-but-unsaved env var to ``"shell"`` deterministically.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from movate.cli._runtime_key_checks import (
    _verify_bearer_roundtrip,
    _warn_if_shell_shadows_runtime_key,
)


def _collapsed(text: str) -> str:
    """Whitespace-collapsed view of Rich-rendered stderr for substring asserts."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# _warn_if_shell_shadows_runtime_key — shell-shadow detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_warn_fires_when_shell_shadows_differing_saved_key(capsys, monkeypatch) -> None:
    """A stale shell export differing from the freshly-saved key → warn on
    stderr with the unset hint and the one-command fix."""
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_STALE_shell_value")
    _warn_if_shell_shadows_runtime_key(key_env="MDK_DEV_KEY", fresh_key="mvt_dev_FRESH")
    out = _collapsed(capsys.readouterr().err)
    assert "unset MDK_DEV_KEY" in out
    assert "OVERRIDE" in out
    assert "mdk fix unshadow-runtime-keys --apply" in out


@pytest.mark.unit
def test_warn_silent_when_shell_matches_saved_key(capsys, monkeypatch) -> None:
    """A shell value identical to the saved key is harmless → no warning."""
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_same")
    _warn_if_shell_shadows_runtime_key(key_env="MDK_DEV_KEY", fresh_key="mvt_dev_same")
    assert _collapsed(capsys.readouterr().err) == ""


@pytest.mark.unit
def test_warn_silent_when_shell_unset(capsys, monkeypatch) -> None:
    """No shell export → nothing can shadow the saved key → no warning."""
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    _warn_if_shell_shadows_runtime_key(key_env="MDK_DEV_KEY", fresh_key="mvt_dev_FRESH")
    assert _collapsed(capsys.readouterr().err) == ""


# ---------------------------------------------------------------------------
# _verify_bearer_roundtrip — admin-capability round-trip
# ---------------------------------------------------------------------------


def _mock_client(monkeypatch, handler) -> None:
    """Pin the module's ``httpx.Client`` to a MockTransport.

    ``_verify_bearer_roundtrip`` constructs the client off the shared
    module's ``httpx``, so patching it there controls the round-trip
    without any real network call."""
    transport = httpx.MockTransport(handler)

    class _MockClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr("movate.cli._runtime_key_checks.httpx.Client", _MockClient)


@pytest.mark.unit
def test_verify_bearer_roundtrip_true_on_2xx(monkeypatch) -> None:
    """2xx from the admin-scoped probe → verified, empty reason."""
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"keys": []}))
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_x")
    assert ok is True
    assert reason == ""


@pytest.mark.unit
def test_verify_bearer_roundtrip_probes_admin_endpoint_with_bearer(monkeypatch) -> None:
    """The probe hits the admin-scoped ``GET /api/v1/auth/keys`` and presents
    the candidate key as the bearer (not whatever is in the environment)."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("Authorization", "")
        return httpx.Response(200, json={"keys": []})

    _mock_client(monkeypatch, handler)
    _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_live_candidate")
    assert seen["path"] == "/api/v1/auth/keys"
    assert seen["auth"] == "Bearer mvt_live_candidate"


@pytest.mark.unit
def test_verify_bearer_roundtrip_false_on_403_lacking_admin(monkeypatch) -> None:
    """403 → authenticated but not admin-capable → rejected with an
    admin-naming reason (uploads need admin)."""
    _mock_client(monkeypatch, lambda req: httpx.Response(403, json={"detail": "no admin"}))
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_ro")
    assert ok is False
    assert "403" in reason
    assert "admin" in reason


@pytest.mark.unit
def test_verify_bearer_roundtrip_false_on_401(monkeypatch) -> None:
    """401 → bad/unknown bearer → rejected with the bare status reason."""
    _mock_client(monkeypatch, lambda req: httpx.Response(401, json={"detail": "nope"}))
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_x")
    assert ok is False
    assert reason == "HTTP 401"


@pytest.mark.unit
def test_verify_bearer_roundtrip_false_on_transport_error(monkeypatch) -> None:
    """A transport failure is reported as unreachable, never raised."""

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    _mock_client(monkeypatch, boom)
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_x")
    assert ok is False
    assert "unreachable" in reason
