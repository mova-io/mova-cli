"""HITL escalation Notifier seam (ADR 083 / ADR 077 D3).

Covers the contract that makes a HUMAN-node pause an actual hand-off:

* env-driven backend selection, fail-safe to no-op (a half-config never breaks
  paused-run handling);
* the cached singleton + its test-reset seam;
* the resume-URL composition (path alone, or prefixed when MOVATE_RUNTIME_URL set);
* fire-and-forget safety: notify_human_pause_safe never raises, even if the
  backend throws;
* the Teams MessageCard + signed-webhook payload shapes (faked HTTP transport).

The HTTP transport is always faked — no network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from movate.core import notifier as notifier_mod
from movate.core.notifier import (
    HumanPause,
    NoOpNotifier,
    build_notifier,
    get_notifier,
    notify_human_pause_safe,
    reset_notifier_cache,
)
from movate.core.notifier_sinks import GenericWebhookNotifier, TeamsNotifier

_NOTIFIER_ENV = (
    "MOVATE_NOTIFIER",
    "MOVATE_NOTIFIER_TEAMS_WEBHOOK_URL",
    "MOVATE_NOTIFIER_WEBHOOK_URL",
    "MOVATE_NOTIFIER_WEBHOOK_SECRET",
    "MOVATE_RUNTIME_URL",
)


@pytest.fixture(autouse=True)
def _clean_notifier_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from a clean notifier env + cache."""
    for var in _NOTIFIER_ENV:
        monkeypatch.delenv(var, raising=False)
    reset_notifier_cache()
    yield
    reset_notifier_cache()


def _pause(**over: object) -> HumanPause:
    base: dict[str, object] = {
        "run_id": "wf-123",
        "workflow_name": "refund_approval",
        "workflow_version": "0.1.0",
        "node_id": "human_gate",
        "prompt": "Approve the refund?",
        "output_contract": ["decision"],
        "approvers": ["alice@acme.com"],
        "tenant_id": "acme",
        "runtime": "native",
    }
    base.update(over)
    return HumanPause(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_build_notifier_default_is_noop() -> None:
    assert isinstance(build_notifier(), NoOpNotifier)


@pytest.mark.unit
def test_build_notifier_teams_without_url_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_NOTIFIER", "teams")
    assert isinstance(build_notifier(), NoOpNotifier)


@pytest.mark.unit
def test_build_notifier_teams_with_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_NOTIFIER", "teams")
    monkeypatch.setenv("MOVATE_NOTIFIER_TEAMS_WEBHOOK_URL", "https://teams.test/x")
    assert isinstance(build_notifier(), TeamsNotifier)


@pytest.mark.unit
def test_build_notifier_webhook_with_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_NOTIFIER", "webhook")
    monkeypatch.setenv("MOVATE_NOTIFIER_WEBHOOK_URL", "https://hook.test/x")
    assert isinstance(build_notifier(), GenericWebhookNotifier)


@pytest.mark.unit
def test_build_notifier_unknown_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_NOTIFIER", "carrier-pigeon")
    assert isinstance(build_notifier(), NoOpNotifier)


@pytest.mark.unit
def test_get_notifier_caches_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_notifier()
    assert get_notifier() is first  # cached
    monkeypatch.setenv("MOVATE_NOTIFIER", "teams")
    monkeypatch.setenv("MOVATE_NOTIFIER_TEAMS_WEBHOOK_URL", "https://teams.test/x")
    assert get_notifier() is first  # still cached — env change not yet picked up
    reset_notifier_cache()
    assert isinstance(get_notifier(), TeamsNotifier)  # re-read after reset


# --------------------------------------------------------------------------- #
# HumanPause URL composition
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_resume_url_is_path_without_runtime_url() -> None:
    assert _pause().resume_url() == "/api/v1/workflow-runs/wf-123/signal"


@pytest.mark.unit
def test_resume_url_prefixed_when_runtime_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOVATE_RUNTIME_URL", "https://rt.acme.com/")
    assert _pause().resume_url() == "https://rt.acme.com/api/v1/workflow-runs/wf-123/signal"


# --------------------------------------------------------------------------- #
# Fire-and-forget safety
# --------------------------------------------------------------------------- #
async def test_noop_notifier_returns_true() -> None:
    assert await NoOpNotifier().notify_human_pause(_pause()) is True


async def test_notify_safe_never_raises_when_backend_throws() -> None:
    class _Boom:
        name = "boom"

        async def notify_human_pause(self, pause: HumanPause) -> bool:
            raise RuntimeError("transport exploded")

    notifier_mod._STATE["notifier"] = _Boom()
    # Must NOT raise — the pause is already persisted; delivery is best-effort.
    await notify_human_pause_safe(_pause())


# --------------------------------------------------------------------------- #
# Sink payload shapes (faked HTTP transport)
# --------------------------------------------------------------------------- #
class _Capture:
    def __init__(self, status: int = 200) -> None:
        self.request: httpx.Request | None = None
        self._status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(self._status)


def _patched_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    orig = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return orig(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_teams_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Capture()
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    ok = await TeamsNotifier(webhook_url="https://teams.test/x").notify_human_pause(_pause())
    assert ok is True
    assert cap.request is not None
    body = json.loads(cap.request.content)
    assert body["@type"] == "MessageCard"
    assert "refund_approval" in body["title"]
    facts = {f["name"]: f["value"] for f in body["sections"][0]["facts"]}
    assert facts["Run"] == "wf-123"
    assert facts["Approvers"] == "alice@acme.com"
    assert facts["Decide via"].endswith("/api/v1/workflow-runs/wf-123/signal")


async def test_teams_returns_false_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    _patched_client(monkeypatch, httpx.MockTransport(boom))
    # Returns False, does NOT raise.
    sink = TeamsNotifier(webhook_url="https://teams.test/x")
    assert await sink.notify_human_pause(_pause()) is False


async def test_webhook_envelope_and_hmac_header(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Capture()
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    sink = GenericWebhookNotifier(webhook_url="https://hook.test/x", secret="topsecret")
    ok = await sink.notify_human_pause(_pause(approvers=["a@x.com", "b@x.com"]))
    assert ok is True
    assert cap.request is not None
    envelope = json.loads(cap.request.content)
    assert envelope["type"] == "hitl.human_pause"
    assert envelope["run_id"] == "wf-123"
    assert envelope["approvers"] == ["a@x.com", "b@x.com"]
    assert envelope["resume_url"].endswith("/api/v1/workflow-runs/wf-123/signal")
    # Signed because a secret is configured (stripe-style header).
    sig = cap.request.headers.get("X-MDK-Signature", "")
    assert sig.startswith("t=") and ",v1=" in sig


async def test_webhook_unsigned_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Capture()
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    await GenericWebhookNotifier(webhook_url="https://hook.test/x").notify_human_pause(_pause())
    assert cap.request is not None
    assert "X-MDK-Signature" not in cap.request.headers
