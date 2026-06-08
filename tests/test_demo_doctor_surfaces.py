"""``mdk demo doctor --surfaces`` — live deployed-surface health probe.

The pre-demo "is everything up?" pre-flight: HTTP-probes the hosted surfaces
listed in ``MDK_DEMO_SURFACES`` (never hardcoded). A reachable service is green;
a connection failure / 5xx is red; an auth-gate (401/403/405) still counts as up.
"""

from __future__ import annotations

import urllib.error

import pytest

from movate.cli._demo_doctor import _check_surfaces, _demo_surfaces, _probe_surface


@pytest.mark.unit
def test_demo_surfaces_parses_name_url_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MDK_DEMO_SURFACES",
        " api = https://x/health , langfuse=http://y:3000 ,broken,=http://z ,name= ",
    )
    assert _demo_surfaces() == [("api", "https://x/health"), ("langfuse", "http://y:3000")]


@pytest.mark.unit
def test_demo_surfaces_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDK_DEMO_SURFACES", raising=False)
    assert _demo_surfaces() == []


@pytest.mark.unit
async def test_check_surfaces_soft_skip_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MDK_DEMO_SURFACES", raising=False)
    checks = await _check_surfaces()
    assert len(checks) == 1
    assert checks[0].name == "live surfaces"
    assert not checks[0].ok and not checks[0].hard  # advisory, not a hard NO-GO


@pytest.mark.unit
def test_probe_surface_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx → reachable green row; no real network (urlopen is stubbed)."""

    class _Resp:
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    c = _probe_surface("api", "https://x/health")
    assert c.ok and c.hard and "HTTP 200" in c.detail


@pytest.mark.unit
def test_probe_surface_auth_gated_counts_as_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401/403 means the service is alive but gated — still 'up'."""

    def _raise(*_a: object, **_k: object) -> None:
        raise urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    c = _probe_surface("api", "https://x")
    assert c.ok and "HTTP 401" in c.detail


@pytest.mark.unit
def test_probe_surface_unreachable_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    c = _probe_surface("temporal", "http://down:7233")
    assert not c.ok and c.hard and "unreachable" in c.detail
