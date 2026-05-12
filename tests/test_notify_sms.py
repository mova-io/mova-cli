"""SMS notification dispatch — backend selection, ACS path, worker composition.

Same three-layer pattern as ``tests/test_notify.py``:

1. **build_sms_backend** (env-driven) — picks ConsoleSmsBackend by
   default, AcsSmsBackend when both env vars set AND the SDK is
   importable, with graceful degradation on partial config or missing
   SDK.
2. **Backends** — ConsoleSmsBackend logs; AcsSmsBackend talks to a
   faked SmsClient via constructor injection (no real connections, no
   azure-communication-sms install required in CI).
3. **Worker integration via MultiDispatcher** — a job with
   ``notify_sms`` set fires the SMS backend; a job with both
   ``notify_email`` and ``notify_sms`` fires both.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar
from uuid import uuid4

import pytest

from movate.core.models import (
    JobKind,
    JobRecord,
    JobStatus,
)
from movate.core.notify import ConsoleBackend, MultiDispatcher
from movate.core.notify_sms import (
    AcsSmsBackend,
    ConsoleSmsBackend,
    build_sms_backend,
)
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_job(**overrides: Any) -> JobRecord:
    base = {
        "job_id": str(uuid4()),
        "tenant_id": "tenant-a",
        "kind": JobKind.AGENT,
        "target": "alpha",
        "status": JobStatus.SUCCESS,
        "input": {"text": "hi"},
        "notify_sms": "+14155551234",
    }
    base.update(overrides)
    return JobRecord(**base)


# ---------------------------------------------------------------------------
# build_sms_backend — env-driven selection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_sms_backend_returns_console_when_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default: no ACS env → ConsoleSmsBackend (logs only).
    Operators see intent; the worker doesn't break on absent config."""
    monkeypatch.delenv("MOVATE_ACS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("MOVATE_ACS_FROM_NUMBER", raising=False)
    d = build_sms_backend()
    assert isinstance(d, ConsoleSmsBackend)
    assert d.name == "console-sms"


@pytest.mark.unit
def test_build_sms_backend_falls_back_on_partial_config(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ACS connection string set but no from-number (or vice versa) →
    Console + LOUD warning. Partial config is always a misconfiguration."""
    monkeypatch.setenv("MOVATE_ACS_CONNECTION_STRING", "endpoint=https://x;accesskey=k")
    monkeypatch.delenv("MOVATE_ACS_FROM_NUMBER", raising=False)
    with caplog.at_level(logging.WARNING, logger="movate.core.notify_sms"):
        d = build_sms_backend()
    assert isinstance(d, ConsoleSmsBackend)
    assert any("notify_sms_partial_config" in r.message for r in caplog.records)


@pytest.mark.unit
def test_build_sms_backend_falls_back_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ACS env set but azure-communication-sms not installed → Console +
    install hint. The soft-dep pattern lets base movate ship without
    the SDK; operators on Azure install via the extra."""
    monkeypatch.setenv("MOVATE_ACS_CONNECTION_STRING", "endpoint=https://x;accesskey=k")
    monkeypatch.setenv("MOVATE_ACS_FROM_NUMBER", "+18885551234")

    # Pretend the SDK isn't importable. We patch sys.modules so the
    # `import azure.communication.sms` inside build_sms_backend fails
    # at lookup time — the same shape as a missing pip install.
    import sys  # noqa: PLC0415 — local to keep the sys-modules munging scoped

    original = sys.modules.pop("azure.communication.sms", None)
    monkeypatch.setitem(sys.modules, "azure.communication.sms", None)
    try:
        with caplog.at_level(logging.WARNING, logger="movate.core.notify_sms"):
            d = build_sms_backend()
        assert isinstance(d, ConsoleSmsBackend)
        assert any("notify_sms_sdk_missing" in r.message for r in caplog.records)
        assert any("movate[sms-acs]" in r.message for r in caplog.records)
    finally:
        if original is not None:
            sys.modules["azure.communication.sms"] = original
        else:
            sys.modules.pop("azure.communication.sms", None)


@pytest.mark.unit
def test_build_sms_backend_returns_acs_when_fully_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both env vars set + SDK importable → AcsSmsBackend with the
    config the operator supplied."""
    monkeypatch.setenv("MOVATE_ACS_CONNECTION_STRING", "endpoint=https://x;accesskey=k")
    monkeypatch.setenv("MOVATE_ACS_FROM_NUMBER", "+18885551234")
    monkeypatch.setenv("MOVATE_ACS_TIMEOUT_SECONDS", "20")

    # Make sure the SDK presence-check passes — inject a stub so the
    # `import` inside build_sms_backend resolves. Tests that exercise
    # the actual send path use the constructor's sms_client= injection
    # instead, so this stub never gets called.
    import sys  # noqa: PLC0415 — local to keep the sys-modules munging scoped
    import types  # noqa: PLC0415

    sms_mod = types.ModuleType("azure.communication.sms")
    sms_mod.SmsClient = object  # type: ignore[attr-defined]  # sentinel; not invoked
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.communication", types.ModuleType("azure.communication"))
    monkeypatch.setitem(sys.modules, "azure.communication.sms", sms_mod)

    d = build_sms_backend()
    assert isinstance(d, AcsSmsBackend)
    assert d.name == "acs-sms"
    assert d._from_number == "+18885551234"
    assert d._timeout_seconds == 20.0


# ---------------------------------------------------------------------------
# ConsoleSmsBackend — logs intent (default; safe in dev)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_console_sms_backend_logs_when_sms_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _make_job(notify_sms="+14155551234")
    with caplog.at_level(logging.INFO, logger="movate.core.notify_sms"):
        await ConsoleSmsBackend().notify_terminal(job)
    assert any("notify_sms_console" in r.message for r in caplog.records)
    assert any("+14155551234" in r.message for r in caplog.records)


@pytest.mark.unit
async def test_console_sms_backend_silent_when_no_sms() -> None:
    """No notify_sms → no-op (don't log noise for jobs that didn't
    request notification on this channel)."""
    job = _make_job(notify_sms=None)
    await ConsoleSmsBackend().notify_terminal(job)  # must not raise / log


# ---------------------------------------------------------------------------
# AcsSmsBackend — sends via an injected fake SmsClient
# ---------------------------------------------------------------------------


class _FakeAcsResult:
    """Mirror of ``azure.communication.sms.SmsSendResult`` — what the
    backend reads off of each per-recipient result."""

    def __init__(self, *, to: str, successful: bool = True, http_status_code: int = 202) -> None:
        self.to = to
        self.successful = successful
        self.http_status_code = http_status_code


class _FakeSmsClient:
    """Records ``send`` calls instead of opening a connection.

    Public surface mirrors what AcsSmsBackend uses: a ``send(from_=, to=,
    message=)`` method that returns an iterable of per-recipient
    results. Class attrs (instead of instance attrs) make it easy for
    tests to assert across calls without holding a reference."""

    instances: ClassVar[list[_FakeSmsClient]] = []

    def __init__(self, *, raise_on_send: Exception | None = None) -> None:
        self.sent_calls: list[dict[str, Any]] = []
        self._raise_on_send = raise_on_send
        type(self).instances.append(self)

    def send(self, *, from_: str, to: list[str], message: str) -> list[_FakeAcsResult]:
        self.sent_calls.append({"from_": from_, "to": to, "message": message})
        if self._raise_on_send is not None:
            raise self._raise_on_send
        return [_FakeAcsResult(to=t) for t in to]


@pytest.fixture(autouse=False)
def fake_sms_client() -> _FakeSmsClient:
    """Hand a fresh fake into AcsSmsBackend's ``sms_client=`` constructor
    injection — bypasses the lazy import of the real SDK, so this test
    doesn't need azure-communication-sms installed."""
    _FakeSmsClient.instances = []
    return _FakeSmsClient()


@pytest.mark.unit
async def test_acs_sms_backend_sends_one_message(fake_sms_client: _FakeSmsClient) -> None:
    backend = AcsSmsBackend(
        connection_string="endpoint=https://x;accesskey=k",
        from_number="+18885551234",
        sms_client=fake_sms_client,
    )
    job = _make_job(notify_sms="+14155551234", status=JobStatus.SUCCESS)

    await backend.notify_terminal(job)

    assert len(fake_sms_client.sent_calls) == 1
    call = fake_sms_client.sent_calls[0]
    assert call["from_"] == "+18885551234"
    assert call["to"] == ["+14155551234"]
    # Body is a single line; status + target visible.
    assert "movate" in call["message"]
    assert "alpha" in call["message"]
    assert "success" in call["message"]


@pytest.mark.unit
async def test_acs_sms_backend_silent_when_no_sms(fake_sms_client: _FakeSmsClient) -> None:
    """No notify_sms → don't even resolve the SDK."""
    backend = AcsSmsBackend(
        connection_string="endpoint=https://x;accesskey=k",
        from_number="+18885551234",
        sms_client=fake_sms_client,
    )
    job = _make_job(notify_sms=None)
    await backend.notify_terminal(job)
    assert fake_sms_client.sent_calls == []


@pytest.mark.unit
async def test_acs_sms_backend_swallows_send_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Notification is courtesy — ACS errors log a warning, never raise."""
    fake = _FakeSmsClient(raise_on_send=OSError("connection refused"))
    backend = AcsSmsBackend(
        connection_string="endpoint=https://x;accesskey=k",
        from_number="+18885551234",
        sms_client=fake,
    )
    job = _make_job(notify_sms="+14155551234")

    with caplog.at_level(logging.WARNING, logger="movate.core.notify_sms"):
        await backend.notify_terminal(job)  # must not raise

    assert any("notify_sms_failed" in r.message for r in caplog.records)


@pytest.mark.unit
async def test_acs_sms_backend_logs_unsuccessful_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ACS returns a 4xx per-recipient result (number rejected, opted
    out, etc.) — we log a warning so the operator can audit, without
    raising."""

    class _RejectingClient(_FakeSmsClient):
        def send(self, *, from_: str, to: list[str], message: str) -> list[_FakeAcsResult]:
            self.sent_calls.append({"from_": from_, "to": to, "message": message})
            return [_FakeAcsResult(to=t, successful=False, http_status_code=400) for t in to]

    backend = AcsSmsBackend(
        connection_string="endpoint=https://x;accesskey=k",
        from_number="+18885551234",
        sms_client=_RejectingClient(),
    )
    job = _make_job(notify_sms="+14155551234")

    with caplog.at_level(logging.WARNING, logger="movate.core.notify_sms"):
        await backend.notify_terminal(job)

    assert any("notify_sms_send_unsuccessful" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# MultiDispatcher — composes email + SMS into one dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multidispatcher_forwards_to_every_child() -> None:
    """A MultiDispatcher with N children invokes every child once per
    notify_terminal call. Each child decides for itself whether the job
    addresses its channel — composing is cheap."""

    calls: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, n: str) -> None:
            self.name = n

        async def notify_terminal(self, job: JobRecord) -> None:
            calls.append((self.name, job.job_id))

    d = MultiDispatcher([_Recorder("a"), _Recorder("b"), _Recorder("c")])
    job = _make_job()
    await d.notify_terminal(job)

    assert [n for n, _ in calls] == ["a", "b", "c"]
    assert all(j == job.job_id for _, j in calls)


@pytest.mark.unit
async def test_multidispatcher_continues_after_child_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One buggy child must not sink the others. We wrap each forwarded
    call in try/except as belt-and-suspender on top of the per-backend
    contract."""

    succeeded: list[str] = []

    class _Broken:
        name = "broken"

        async def notify_terminal(self, job: JobRecord) -> None:
            raise RuntimeError("backend exploded")

    class _Working:
        name = "working"

        async def notify_terminal(self, job: JobRecord) -> None:
            succeeded.append(job.job_id)

    d = MultiDispatcher([_Broken(), _Working()])
    job = _make_job()

    with caplog.at_level(logging.WARNING, logger="movate.core.notify"):
        await d.notify_terminal(job)

    assert succeeded == [job.job_id]
    assert any("notify_multi_child_raised" in r.message for r in caplog.records)


@pytest.mark.unit
def test_multidispatcher_composite_name_for_ops_log() -> None:
    """Operators read the dispatcher name on worker boot to confirm
    which channels they wired up. Composite name = ``child1+child2+...``."""
    d = MultiDispatcher([ConsoleBackend(), ConsoleSmsBackend()])
    assert d.name == "console+console-sms"


# ---------------------------------------------------------------------------
# Schema round-trip — notify_sms survives save → get on InMemoryStorage
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_storage_round_trip_preserves_notify_sms() -> None:
    """The new column persists through save_job → get_job (in-memory).
    SQLite + Postgres backend coverage is handled by the parametrized
    storage fixture in tests/test_jobs_storage.py."""
    storage = InMemoryStorage()
    await storage.init()
    original = _make_job(notify_sms="+14155551234", status=JobStatus.QUEUED)
    await storage.save_job(original)

    got = await storage.get_job(original.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.notify_sms == "+14155551234"

    # And jobs without an SMS destination round-trip with None.
    silent = _make_job(notify_sms=None, status=JobStatus.QUEUED)
    await storage.save_job(silent)
    got_silent = await storage.get_job(silent.job_id, tenant_id="tenant-a")
    assert got_silent is not None
    assert got_silent.notify_sms is None
