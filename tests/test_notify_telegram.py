"""Telegram notification dispatch — backend selection + HTTP path + worker fan-out.

Same three-layer pattern as ``tests/test_notify.py`` and
``tests/test_notify_sms.py``:

1. **build_telegram_backend** (env-driven) — picks ConsoleTelegramBackend
   by default, TelegramBackend when both env vars present.
2. **Backends** — ConsoleTelegramBackend logs; TelegramBackend POSTs to
   Telegram's Bot API via an injected httpx fake (no network in CI).
3. **MultiDispatcher integration** — composing Telegram with the other
   channels works; failures in one don't sink others.
4. **Worker integration** — the worker invokes the dispatcher on every
   terminal job (no per-job gate), so a job without ``notify_email``
   or ``notify_sms`` still fires Telegram.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx
import pytest

from movate.core.models import (
    JobKind,
    JobRecord,
    JobStatus,
)
from movate.core.notify import ConsoleBackend, MultiDispatcher
from movate.core.notify_sms import ConsoleSmsBackend
from movate.core.notify_telegram import (
    ConsoleTelegramBackend,
    TelegramBackend,
    build_telegram_backend,
)
from movate.runtime.dispatch import WorkerDispatch
from movate.runtime.worker import Worker
from movate.testing import InMemoryStorage


def _make_job(**overrides: Any) -> JobRecord:
    base = {
        "job_id": str(uuid4()),
        "tenant_id": "tenant-a",
        "kind": JobKind.AGENT,
        "target": "alpha",
        "status": JobStatus.SUCCESS,
        "input": {"text": "hi"},
    }
    base.update(overrides)
    return JobRecord(**base)


# ---------------------------------------------------------------------------
# build_telegram_backend — env-driven selection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_telegram_backend_returns_console_when_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default: neither env var set → ConsoleTelegramBackend."""
    monkeypatch.delenv("MOVATE_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MOVATE_TELEGRAM_CHAT_ID", raising=False)
    d = build_telegram_backend()
    assert isinstance(d, ConsoleTelegramBackend)
    assert d.name == "console-telegram"


@pytest.mark.unit
def test_build_telegram_backend_falls_back_on_partial_config(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Token set but no chat_id (or vice versa) → Console + LOUD warning.
    Partial config is always a misconfiguration."""
    monkeypatch.setenv("MOVATE_TELEGRAM_BOT_TOKEN", "1234567890:ABCDEFghijklmnop")
    monkeypatch.delenv("MOVATE_TELEGRAM_CHAT_ID", raising=False)
    with caplog.at_level(logging.WARNING, logger="movate.core.notify_telegram"):
        d = build_telegram_backend()
    assert isinstance(d, ConsoleTelegramBackend)
    assert any("notify_telegram_partial_config" in r.message for r in caplog.records)


@pytest.mark.unit
def test_build_telegram_backend_returns_real_when_fully_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both env vars set → TelegramBackend with the right config."""
    monkeypatch.setenv("MOVATE_TELEGRAM_BOT_TOKEN", "1234567890:ABCDEFghijklmnop")
    monkeypatch.setenv("MOVATE_TELEGRAM_CHAT_ID", "987654321")
    monkeypatch.setenv("MOVATE_TELEGRAM_TIMEOUT_SECONDS", "15")
    d = build_telegram_backend()
    assert isinstance(d, TelegramBackend)
    assert d.name == "telegram"
    assert d._chat_id == "987654321"
    assert d._timeout_seconds == 15.0


@pytest.mark.unit
def test_build_telegram_backend_bad_timeout_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage timeout value → silently use the 10s default. Don't
    crash the worker on a typo."""
    monkeypatch.setenv("MOVATE_TELEGRAM_BOT_TOKEN", "1234567890:ABCDEFghijklmnop")
    monkeypatch.setenv("MOVATE_TELEGRAM_CHAT_ID", "987654321")
    monkeypatch.setenv("MOVATE_TELEGRAM_TIMEOUT_SECONDS", "not-a-number")
    d = build_telegram_backend()
    assert isinstance(d, TelegramBackend)
    assert d._timeout_seconds == 10.0


# ---------------------------------------------------------------------------
# ConsoleTelegramBackend — logs on every terminal (no per-job gate)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_console_telegram_backend_logs_every_terminal_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ConsoleTelegramBackend fires on EVERY terminal job — different
    from console-email + console-sms which gate on per-job fields.
    That's the operator-wide alert pattern: no opt-in needed."""
    job = _make_job(notify_email=None, notify_sms=None)
    with caplog.at_level(logging.INFO, logger="movate.core.notify_telegram"):
        await ConsoleTelegramBackend().notify_terminal(job)
    assert any("notify_telegram_console" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TelegramBackend — sends via a fake httpx client
# ---------------------------------------------------------------------------


def _fake_httpx_client(
    *, return_status: int = 200, return_body: dict[str, Any] | None = None
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """Build an httpx.AsyncClient backed by an in-process MockTransport
    that records every request and returns a fixed response.

    Returns the client + a list that the caller can inspect after
    invocations to assert what was sent."""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = {}
        if request.content:
            import json  # noqa: PLC0415

            body = json.loads(request.content)
        sent.append({"url": str(request.url), "method": request.method, "body": body})
        return httpx.Response(return_status, json=return_body or {"ok": True})

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport), sent


@pytest.mark.unit
async def test_telegram_backend_posts_correct_message() -> None:
    """Happy path: POST to sendMessage with chat_id + Markdown body."""
    client, sent = _fake_httpx_client()
    backend = TelegramBackend(
        bot_token="1234567890:TESTTOKEN",
        chat_id="987654321",
        http_client=client,
    )
    job = _make_job(target="alpha", status=JobStatus.SUCCESS)

    await backend.notify_terminal(job)

    assert len(sent) == 1
    call = sent[0]
    assert call["url"] == "https://api.telegram.org/bot1234567890:TESTTOKEN/sendMessage"
    assert call["method"] == "POST"
    assert call["body"]["chat_id"] == "987654321"
    assert "alpha" in call["body"]["text"]
    assert "success" in call["body"]["text"]
    # Markdown mode + no preview (cleaner notifications).
    assert call["body"]["parse_mode"] == "Markdown"
    assert call["body"]["disable_web_page_preview"] is True


@pytest.mark.unit
async def test_telegram_backend_includes_error_details_on_failed_job() -> None:
    """For failed jobs, the body includes the error type so the operator
    can triage from the notification alone."""
    from movate.core.models import ErrorInfo  # noqa: PLC0415

    client, sent = _fake_httpx_client()
    backend = TelegramBackend(
        bot_token="t",
        chat_id="c",
        http_client=client,
    )
    job = _make_job(
        status=JobStatus.ERROR,
        error=ErrorInfo(type="BudgetExceeded", message="run cost $1.50 exceeded $1.00 cap"),
    )

    await backend.notify_terminal(job)

    body_text = sent[0]["body"]["text"]
    assert "BudgetExceeded" in body_text
    assert "error" in body_text.lower()


@pytest.mark.unit
async def test_telegram_backend_swallows_network_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same contract as SMTP/ACS: notification is courtesy. Connection
    errors log a warning, never raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = TelegramBackend(
        bot_token="t",
        chat_id="c",
        http_client=client,
    )
    job = _make_job()

    with caplog.at_level(logging.WARNING, logger="movate.core.notify_telegram"):
        await backend.notify_terminal(job)  # must not raise

    assert any("notify_telegram_failed" in r.message for r in caplog.records)


@pytest.mark.unit
async def test_telegram_backend_logs_4xx_response_as_unsuccessful(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telegram 4xx (bad token, blocked by user, etc.) logs a warning;
    we don't retry — the worker's retry policy is for the JOB, not
    notifications."""
    client, _ = _fake_httpx_client(
        return_status=401,
        return_body={"ok": False, "error_code": 401, "description": "Unauthorized"},
    )
    backend = TelegramBackend(
        bot_token="t",
        chat_id="c",
        http_client=client,
    )
    job = _make_job()

    with caplog.at_level(logging.WARNING, logger="movate.core.notify_telegram"):
        await backend.notify_terminal(job)

    assert any("notify_telegram_send_unsuccessful" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Composition in MultiDispatcher
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_multidispatcher_invokes_telegram_alongside_email_and_sms() -> None:
    """MultiDispatcher fans out to all three channels. Email + SMS no-op
    when their per-job fields are None; Telegram still fires."""
    client, sent = _fake_httpx_client()
    d = MultiDispatcher(
        [
            ConsoleBackend(),
            ConsoleSmsBackend(),
            TelegramBackend(bot_token="t", chat_id="c", http_client=client),
        ]
    )
    job = _make_job(notify_email=None, notify_sms=None)

    await d.notify_terminal(job)

    # Telegram fires unconditionally; email + SMS are no-ops because
    # their per-job fields are unset.
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Worker integration — fires dispatcher on every terminal regardless
# ---------------------------------------------------------------------------


class _RecordingDispatcher:
    """Test double; captures every call."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[JobRecord] = []

    async def notify_terminal(self, job: JobRecord) -> None:
        self.calls.append(job)


@pytest.mark.unit
async def test_worker_fires_dispatcher_even_without_per_job_notify_fields() -> None:
    """Pre-Telegram, the worker only fired the dispatcher when
    notify_email OR notify_sms was set on the job. That gate was wrong
    once we added an operator-wide channel (Telegram) — we now invoke
    the dispatcher on EVERY terminal job and let each backend decide
    internally whether it's in scope."""
    storage = InMemoryStorage()
    await storage.init()

    job = _make_job(
        status=JobStatus.QUEUED,
        notify_email=None,  # ← key: neither per-job field set
        notify_sms=None,
        target="ghost",  # unknown → terminal ERROR, predictable
    )
    await storage.save_job(job)

    notifier = _RecordingDispatcher()
    dispatch = WorkerDispatch(storage=storage, executor=None, agents=[])  # type: ignore[arg-type]
    worker = Worker(storage=storage, dispatch=dispatch, notifier=notifier)

    await worker.run_one_cycle()

    # Worker invoked the dispatcher even though no per-job notify fields
    # were set. The Telegram backend would have fired on this job; email
    # + SMS backends would have no-op'd.
    assert len(notifier.calls) == 1
    assert notifier.calls[0].job_id == job.job_id
    assert notifier.calls[0].status == JobStatus.ERROR
