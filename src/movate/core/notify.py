"""Notification dispatch — notify a user when their job finishes.

Two channels (email + SMS), composed by :class:`MultiDispatcher`. Each
channel has its own pluggable backend pair (console + real-vendor):

Email (this module):

* :class:`ConsoleBackend` — logs notifications instead of sending.
  Default in dev / tests / when SMTP isn't configured. Means jobs
  with ``notify_email`` set don't crash the worker on misconfigured
  deployments; the operator sees the intent in logs and can wire up
  real delivery later.
* :class:`SmtpEmailBackend` — sends via SMTP (universal: ACS Email,
  SendGrid, Mailgun, AWS SES, Gmail, etc. all speak SMTP). Reads
  config from env vars so secrets stay out of code:
    * ``MOVATE_SMTP_HOST`` — required to activate
    * ``MOVATE_SMTP_PORT`` (default 587 for STARTTLS, 465 for SSL)
    * ``MOVATE_SMTP_USER`` / ``MOVATE_SMTP_PASSWORD`` — credentials
    * ``MOVATE_SMTP_FROM`` — sender address (e.g. ``movate@yourdomain.com``)
    * ``MOVATE_SMTP_USE_SSL`` (default 'false'; flip to 'true' for port 465)
    * ``MOVATE_SMTP_TIMEOUT_SECONDS`` (default 10)

SMS (sister module :mod:`movate.core.notify_sms`):

* :class:`~movate.core.notify_sms.ConsoleSmsBackend` — logs only.
* :class:`~movate.core.notify_sms.AcsSmsBackend` — Azure Communication
  Services (locked vendor per docs/v1.0-azure-design.md §10).

Choose the active dispatcher via :func:`build_dispatcher`: it composes
one email + one SMS backend selected from env vars and returns a
:class:`MultiDispatcher` that forwards every terminal job to both. Each
backend is a no-op for jobs that don't address its channel
(``ConsoleSmsBackend.notify_terminal`` returns early when
``job.notify_sms is None``, etc.), so composing them is cheap.

The worker fires this fire-and-forget after each terminal transition.
Failure logs but never re-queues the job — notification is courtesy,
the work is the source of truth.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from typing import Protocol

from movate.core.models import JobRecord
from movate.core.notify_sms import build_sms_backend
from movate.core.notify_telegram import build_telegram_backend

logger = logging.getLogger(__name__)


class NotificationDispatcher(Protocol):
    """Async dispatcher contract.

    Implementations must be safe to call from the worker's event loop.
    They MUST NOT raise on operational failure (network down, bad
    credentials, etc.) — instead, log + return. The whole point is
    that notifications are courtesy; never sink the worker.
    """

    name: str
    """Short identifier for ops logging (``console`` / ``smtp`` / etc.)."""

    async def notify_terminal(self, job: JobRecord) -> None:
        """Called once per job that reaches a terminal state with
        ``notify_email`` set. No-op for jobs without an email target."""


# ---------------------------------------------------------------------------
# ConsoleBackend — logs only (dev default, test-friendly)
# ---------------------------------------------------------------------------


class ConsoleBackend:
    """Logs the intended notification at INFO. Useful for dev runs +
    automated tests + deployments where SMTP isn't (yet) configured.

    Operators see the log line and know what would have been sent if
    SMTP were wired up.
    """

    name = "console"

    async def notify_terminal(self, job: JobRecord) -> None:
        if not job.notify_email:
            return
        logger.info(
            "notify_console job_id=%s target=%s status=%s would_email=%s subject=%s",
            job.job_id,
            job.target,
            job.status.value,
            job.notify_email,
            _subject_for(job),
        )


# ---------------------------------------------------------------------------
# SmtpEmailBackend — real delivery (production)
# ---------------------------------------------------------------------------


class SmtpEmailBackend:
    """Sends one email per terminal job via SMTP.

    Reads config from env vars; the constructor takes them as
    parameters for testability (tests pass an in-memory SMTP server
    address instead of relying on env state).
    """

    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        from_addr: str = "movate@localhost",
        use_ssl: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_addr = from_addr
        self._use_ssl = use_ssl
        self._timeout = timeout

    async def notify_terminal(self, job: JobRecord) -> None:
        if not job.notify_email:
            return
        try:
            self._send_sync(job)
        except Exception:
            # Never sink the worker. Operators see the warning in
            # logs and can diagnose SMTP separately from job execution.
            logger.warning(
                "notify_smtp_failed job_id=%s to=%s host=%s — job state "
                "is unchanged; this is the notification path only",
                job.job_id,
                job.notify_email,
                self._host,
                exc_info=True,
            )

    def _send_sync(self, job: JobRecord) -> None:
        """Synchronous SMTP. The worker calls this from an async
        method; ``smtplib`` is blocking, but the call is short (<1s
        typical) and runs inside the worker's per-job slot so other
        jobs aren't blocked. If SMTP latency becomes a problem, swap
        to ``aiosmtplib`` — same envelope, async API."""
        msg = MIMEText(_body_for(job), "plain", "utf-8")
        msg["Subject"] = _subject_for(job)
        msg["From"] = self._from_addr
        msg["To"] = job.notify_email or ""

        smtp_cls = smtplib.SMTP_SSL if self._use_ssl else smtplib.SMTP
        with smtp_cls(self._host, self._port, timeout=self._timeout) as smtp:
            if not self._use_ssl:
                # STARTTLS upgrade. If the server doesn't support it,
                # SMTP.starttls() raises — that's correct: refuse to
                # send credentials over plaintext.
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)
        logger.info(
            "notify_smtp_sent job_id=%s to=%s status=%s",
            job.job_id,
            job.notify_email,
            job.status.value,
        )


# ---------------------------------------------------------------------------
# Factory — env-driven backend selection
# ---------------------------------------------------------------------------


class MultiDispatcher:
    """Forwards every terminal job to N child dispatchers.

    Each child is responsible for ignoring jobs that don't address its
    channel (e.g. the email backend's ``notify_terminal`` is a no-op
    when ``job.notify_email is None``). Composing them is cheap because
    of that — fanning out always touches the no-op fast path for
    channels the job didn't request.

    Exceptions in one child don't sink the others: we wrap each
    forwarded call in a try/except. This is belt-and-suspender on top
    of the per-backend contract (backends ARE supposed to swallow
    their own errors — see the dispatcher Protocol docstring) — a
    buggy backend that breaks the contract must not cascade.
    """

    def __init__(self, children: list[NotificationDispatcher]) -> None:
        self._children = children
        # Composite name = "smtp+acs-sms", "console+console-sms", etc.
        # Useful for the operator-visible log on worker boot.
        self.name = "+".join(c.name for c in children) if children else "noop"

    async def notify_terminal(self, job: JobRecord) -> None:
        for child in self._children:
            try:
                await child.notify_terminal(job)
            except Exception:
                logger.warning(
                    "notify_multi_child_raised backend=%s job_id=%s — "
                    "continuing with remaining backends",
                    child.name,
                    job.job_id,
                    exc_info=True,
                )


def build_email_backend() -> ConsoleBackend | SmtpEmailBackend:
    """Select the email channel backend from env vars.

    Selection rule: ``MOVATE_SMTP_HOST`` set + parseable port → SMTP;
    otherwise console. Split out from :func:`build_dispatcher` so tests
    can exercise each channel in isolation.
    """
    smtp_host = os.environ.get("MOVATE_SMTP_HOST", "").strip()
    if not smtp_host:
        return ConsoleBackend()

    try:
        port = int(os.environ.get("MOVATE_SMTP_PORT", "587"))
    except ValueError:
        logger.warning(
            "MOVATE_SMTP_PORT is not an int (%r); falling back to console backend",
            os.environ.get("MOVATE_SMTP_PORT"),
        )
        return ConsoleBackend()

    try:
        timeout = float(os.environ.get("MOVATE_SMTP_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10.0

    use_ssl = os.environ.get("MOVATE_SMTP_USE_SSL", "false").lower() in (
        "true",
        "1",
        "yes",
    )

    return SmtpEmailBackend(
        host=smtp_host,
        port=port,
        username=os.environ.get("MOVATE_SMTP_USER") or None,
        password=os.environ.get("MOVATE_SMTP_PASSWORD") or None,
        from_addr=os.environ.get("MOVATE_SMTP_FROM", "movate@localhost"),
        use_ssl=use_ssl,
        timeout=timeout,
    )


def build_dispatcher() -> NotificationDispatcher:
    """Compose a :class:`MultiDispatcher` that fires every configured
    channel for each terminal job.

    Each channel's backend is selected by its own env-driven factory
    (:func:`build_email_backend`, :func:`build_sms_backend`,
    :func:`build_telegram_backend`) so adding a fourth channel later
    (Slack, PagerDuty, etc.) is a one-line change here.

    Idempotent + side-effect-free. Each worker startup calls this once;
    the worker holds the result for its lifetime.
    """
    email_backend = build_email_backend()
    sms_backend = build_sms_backend()
    telegram_backend = build_telegram_backend()
    return MultiDispatcher([email_backend, sms_backend, telegram_backend])


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def _subject_for(job: JobRecord) -> str:
    icon = {
        "success": "✓",
        "error": "✗",
        "safety_blocked": "⊘",
    }.get(job.status.value, "?")
    return f"[movate] {icon} {job.kind.value}/{job.target} — {job.status.value}"


def _body_for(job: JobRecord) -> str:
    lines = [
        f"Job {job.job_id} reached terminal status: {job.status.value}",
        "",
        f"Kind:     {job.kind.value}",
        f"Target:   {job.target}",
        f"Tenant:   {job.tenant_id}",
    ]
    if job.result_run_id:
        lines.append(f"Run id:   {job.result_run_id}")
    if job.created_at and job.completed_at:
        elapsed = (job.completed_at - job.created_at).total_seconds()
        lines.append(f"Elapsed:  {elapsed:.1f}s (queued → terminal)")
    if job.error:
        lines += [
            "",
            f"Error type: {job.error.type}",
            f"Error message: {job.error.message}",
        ]
    lines += [
        "",
        "— movate",
    ]
    return "\n".join(lines)


__all__ = [
    "ConsoleBackend",
    "MultiDispatcher",
    "NotificationDispatcher",
    "SmtpEmailBackend",
    "build_dispatcher",
    "build_email_backend",
]
