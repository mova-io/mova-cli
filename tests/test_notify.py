"""Notification dispatch — backend selection, SMTP path, worker integration.

Three concentric layers:

1. **build_dispatcher** (env-driven) — picks ConsoleBackend by default,
   SmtpEmailBackend when MOVATE_SMTP_HOST is set, with graceful
   degradation on bad config.
2. **Backends** — ConsoleBackend logs; SmtpEmailBackend talks to a
   stdlib SMTP server. We use a faked smtplib.SMTP class in
   monkeypatch instead of spinning up an actual server — fast,
   no port, no DNS lookups.
3. **Worker integration** — when a job with ``notify_email`` lands
   in a terminal state, the worker fires the dispatcher exactly
   once with the post-update view.
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
from movate.core.notify import (
    ConsoleBackend,
    MultiDispatcher,
    SmtpEmailBackend,
    build_dispatcher,
    build_email_backend,
)
from movate.runtime.dispatch import WorkerDispatch
from movate.runtime.worker import Worker
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_job(**overrides) -> JobRecord:
    base = {
        "job_id": str(uuid4()),
        "tenant_id": "tenant-a",
        "kind": JobKind.AGENT,
        "target": "alpha",
        "status": JobStatus.SUCCESS,
        "input": {"text": "hi"},
        "notify_email": "ops@example.com",
    }
    base.update(overrides)
    return JobRecord(**base)


# ---------------------------------------------------------------------------
# Backend selection via env
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_email_backend_returns_console_when_no_smtp_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default: no SMTP env vars → ConsoleBackend (logs only).
    Operators see what would have been sent; the worker doesn't break."""
    monkeypatch.delenv("MOVATE_SMTP_HOST", raising=False)
    d = build_email_backend()
    assert isinstance(d, ConsoleBackend)
    assert d.name == "console"


@pytest.mark.unit
def test_build_email_backend_returns_smtp_when_host_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MOVATE_SMTP_HOST set → SmtpEmailBackend, with sane port/timeout defaults."""
    monkeypatch.setenv("MOVATE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MOVATE_SMTP_USER", "user")
    monkeypatch.setenv("MOVATE_SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MOVATE_SMTP_FROM", "movate@example.com")
    d = build_email_backend()
    assert isinstance(d, SmtpEmailBackend)
    assert d.name == "smtp"


@pytest.mark.unit
def test_build_email_backend_falls_back_on_bad_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad MOVATE_SMTP_PORT → fall back to console (don't crash the worker)."""
    monkeypatch.setenv("MOVATE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MOVATE_SMTP_PORT", "not-a-number")
    d = build_email_backend()
    assert isinstance(d, ConsoleBackend)


@pytest.mark.unit
def test_build_email_backend_smtp_with_use_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOVATE_SMTP_USE_SSL=true selects SMTP_SSL on send."""
    monkeypatch.setenv("MOVATE_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MOVATE_SMTP_PORT", "465")
    monkeypatch.setenv("MOVATE_SMTP_USE_SSL", "true")
    d = build_email_backend()
    assert isinstance(d, SmtpEmailBackend)
    assert d._use_ssl is True
    assert d._port == 465


@pytest.mark.unit
def test_build_dispatcher_returns_multidispatcher_with_email_and_sms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_dispatcher`` always returns a MultiDispatcher composing the
    email + SMS channel backends. Each child handles its own no-op when
    the job didn't address its channel. Without env config, both children
    are the console fallbacks."""
    monkeypatch.delenv("MOVATE_SMTP_HOST", raising=False)
    monkeypatch.delenv("MOVATE_ACS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("MOVATE_ACS_FROM_NUMBER", raising=False)
    d = build_dispatcher()
    assert isinstance(d, MultiDispatcher)
    # Composite name reflects each child for operator visibility.
    assert "console" in d.name
    assert "console-sms" in d.name


# ---------------------------------------------------------------------------
# ConsoleBackend — logs the intent (default; safe in dev)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_console_backend_logs_when_email_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _make_job(notify_email="ops@example.com")
    with caplog.at_level(logging.INFO, logger="movate.core.notify"):
        await ConsoleBackend().notify_terminal(job)
    assert any("notify_console" in r.message for r in caplog.records)
    assert any("ops@example.com" in r.message for r in caplog.records)


@pytest.mark.unit
async def test_console_backend_silent_when_no_email() -> None:
    """No notify_email → no-op (don't log noise for jobs that didn't request notification)."""
    job = _make_job(notify_email=None)
    await ConsoleBackend().notify_terminal(job)  # must not raise / log


# ---------------------------------------------------------------------------
# SmtpEmailBackend — sends via a faked smtplib.SMTP
# ---------------------------------------------------------------------------


class _FakeSmtp:
    """Drop-in for smtplib.SMTP that records calls instead of opening sockets.

    Tests assert what was sent rather than spinning up an actual SMTP server.
    Class-level lists make state inspection easy across with-block exits.
    """

    instances: ClassVar[list[_FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_calls: list[tuple[str, str]] = []
        self.sent_messages: list[Any] = []
        type(self).instances.append(self)

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.login_calls.append((user, password))

    def send_message(self, msg: Any) -> None:
        self.sent_messages.append(msg)


@pytest.fixture(autouse=False)
def fake_smtp(monkeypatch: pytest.MonkeyPatch):
    """Patch smtplib.SMTP + SMTP_SSL with a single fake; reset state."""
    _FakeSmtp.instances = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSmtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeSmtp)
    return _FakeSmtp


@pytest.mark.unit
async def test_smtp_backend_sends_one_message(fake_smtp) -> None:
    backend = SmtpEmailBackend(
        host="smtp.example.com",
        port=587,
        username="u",
        password="p",
        from_addr="bot@example.com",
    )
    job = _make_job(notify_email="ops@example.com", status=JobStatus.SUCCESS)

    await backend.notify_terminal(job)

    assert len(fake_smtp.instances) == 1
    inst = fake_smtp.instances[0]
    assert inst.host == "smtp.example.com"
    assert inst.port == 587
    # STARTTLS upgrade happens before login on non-SSL connections.
    assert inst.starttls_called is True
    assert inst.login_calls == [("u", "p")]
    assert len(inst.sent_messages) == 1
    msg = inst.sent_messages[0]
    assert msg["To"] == "ops@example.com"
    assert msg["From"] == "bot@example.com"
    assert "success" in msg["Subject"].lower()


@pytest.mark.unit
async def test_smtp_backend_skips_starttls_on_ssl(fake_smtp) -> None:
    """SSL connections don't STARTTLS — the channel is already encrypted."""
    backend = SmtpEmailBackend(
        host="smtp.example.com", port=465, use_ssl=True, from_addr="bot@example.com"
    )
    job = _make_job()

    await backend.notify_terminal(job)

    inst = fake_smtp.instances[0]
    assert inst.starttls_called is False


@pytest.mark.unit
async def test_smtp_backend_silent_when_no_email(fake_smtp) -> None:
    """No notify_email → don't even open the connection."""
    backend = SmtpEmailBackend(host="smtp.example.com", port=587)
    job = _make_job(notify_email=None)
    await backend.notify_terminal(job)
    assert fake_smtp.instances == []


@pytest.mark.unit
async def test_smtp_backend_swallows_send_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Notification is courtesy — SMTP errors log a warning, never raise."""

    class _BrokenSmtp(_FakeSmtp):
        def send_message(self, msg: Any) -> None:  # type: ignore[override]
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", _BrokenSmtp)

    backend = SmtpEmailBackend(host="smtp.example.com", port=587)
    job = _make_job(notify_email="ops@example.com")

    with caplog.at_level(logging.WARNING, logger="movate.core.notify"):
        await backend.notify_terminal(job)  # must not raise

    assert any("notify_smtp_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Worker integration — fires dispatcher on terminal
# ---------------------------------------------------------------------------


class _RecordingDispatcher:
    """Test double for NotificationDispatcher. Captures every call."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[JobRecord] = []

    async def notify_terminal(self, job: JobRecord) -> None:
        self.calls.append(job)


@pytest.mark.unit
async def test_worker_fires_notifier_when_email_set() -> None:
    """End-to-end: job with notify_email set → worker drains it → notifier
    receives the post-update JobRecord with terminal status."""
    storage = InMemoryStorage()
    await storage.init()

    job = _make_job(
        status=JobStatus.QUEUED,
        notify_email="ops@example.com",
        target="ghost",  # unknown → terminal ERROR, predictable
    )
    await storage.save_job(job)

    notifier = _RecordingDispatcher()
    dispatch = WorkerDispatch(storage=storage, executor=None, agents=[])  # type: ignore[arg-type]
    worker = Worker(storage=storage, dispatch=dispatch, notifier=notifier)

    await worker.run_one_cycle()

    assert len(notifier.calls) == 1
    seen = notifier.calls[0]
    assert seen.job_id == job.job_id
    assert seen.notify_email == "ops@example.com"
    # Worker fetches the POST-update view — the dispatcher sees the
    # terminal status, not the RUNNING snapshot from claim_next_job.
    assert seen.status == JobStatus.ERROR


@pytest.mark.unit
async def test_worker_fires_notifier_on_every_terminal_regardless_of_email() -> None:
    """The worker invokes the dispatcher on EVERY terminal job, not just
    those with ``notify_email`` set. Per-channel filtering happens INSIDE
    the dispatcher (email backend no-ops when ``job.notify_email`` is
    None, etc.) — that's needed because operator-wide channels like
    Telegram fire on every terminal regardless of per-job fields. See
    test_notify_telegram.py for the full story."""
    storage = InMemoryStorage()
    await storage.init()

    job = _make_job(status=JobStatus.QUEUED, notify_email=None, target="ghost")
    await storage.save_job(job)

    notifier = _RecordingDispatcher()
    dispatch = WorkerDispatch(storage=storage, executor=None, agents=[])  # type: ignore[arg-type]
    worker = Worker(storage=storage, dispatch=dispatch, notifier=notifier)

    await worker.run_one_cycle()

    # Worker DID fire the dispatcher even without notify_email. A
    # ConsoleBackend would have logged + no-op'd on the per-job check;
    # a TelegramBackend (if env-configured) would have actually sent.
    assert len(notifier.calls) == 1
    assert notifier.calls[0].notify_email is None


@pytest.mark.unit
async def test_worker_swallows_notifier_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even though the dispatcher contract says "never raise", we
    belt-and-suspender the worker's call — a future buggy dispatcher
    must not crash the worker loop."""

    class _BrokenDispatcher:
        name = "broken"

        async def notify_terminal(self, job: JobRecord) -> None:
            raise RuntimeError("dispatcher exploded")

    storage = InMemoryStorage()
    await storage.init()
    job = _make_job(status=JobStatus.QUEUED, notify_email="ops@example.com", target="ghost")
    await storage.save_job(job)

    dispatch = WorkerDispatch(storage=storage, executor=None, agents=[])  # type: ignore[arg-type]
    worker = Worker(storage=storage, dispatch=dispatch, notifier=_BrokenDispatcher())

    with caplog.at_level(logging.WARNING, logger="movate.runtime.worker"):
        handled = await worker.run_one_cycle()

    assert handled is not None  # job was processed
    assert any("notify_dispatcher_raised" in r.message for r in caplog.records)
    # Job still landed in a terminal state — the notification failure
    # didn't bubble up and corrupt the worker.
    final = await storage.get_job(job.job_id, tenant_id="tenant-a")
    assert final is not None
    assert final.status == JobStatus.ERROR


# ---------------------------------------------------------------------------
# Schema round-trip — notify_email survives save → get
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_storage_round_trip_preserves_notify_email() -> None:
    """The new column persists through save_job → get_job (sqlite + memory).
    Postgres backend coverage is handled by the parametrized storage
    fixture in tests/test_jobs_storage.py."""
    storage = InMemoryStorage()
    await storage.init()
    original = _make_job(notify_email="ops@example.com", status=JobStatus.QUEUED)
    await storage.save_job(original)

    got = await storage.get_job(original.job_id, tenant_id="tenant-a")
    assert got is not None
    assert got.notify_email == "ops@example.com"

    # And jobs without an email round-trip with None.
    silent = _make_job(notify_email=None, status=JobStatus.QUEUED)
    await storage.save_job(silent)
    got_silent = await storage.get_job(silent.job_id, tenant_id="tenant-a")
    assert got_silent is not None
    assert got_silent.notify_email is None
