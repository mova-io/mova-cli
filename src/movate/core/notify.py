"""Notification dispatch — email a user when their job finishes.

Pluggable Protocol with two backends:

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

Choose the active backend via :func:`build_dispatcher`:

* No ``MOVATE_SMTP_HOST`` set → :class:`ConsoleBackend` (logs only).
* ``MOVATE_SMTP_HOST`` set → :class:`SmtpEmailBackend`.

SMS is **deferred**. Phone-number provisioning + carrier registration
(A2P 10DLC for US numbers, equivalents elsewhere) is a multi-week
business setup, not a code question. Email covers the dev-team
``movate submit ... --notify-email me@example.com`` workflow; that's
the 90% case for now.

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

    async def notify_alert(self, *, subject: str, body: str, email: str | None) -> None:
        """Send an ad-hoc operational alert not tied to a :class:`JobRecord`.

        Used by the continuous-eval loop (ADR 016 D2) to fire a drift
        alert when a scheduled eval regresses vs. its baseline. Same
        never-raise contract as :meth:`notify_terminal`: log + return on
        any operational failure. ``email=None`` means "no delivery target"
        — the console backend still logs the intent so operators see it."""


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

    async def notify_alert(self, *, subject: str, body: str, email: str | None) -> None:
        logger.info(
            "notify_console_alert would_email=%s subject=%s body=%s",
            email,
            subject,
            body,
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
            self._send_message(
                to_addr=job.notify_email,
                subject=_subject_for(job),
                body=_body_for(job),
            )
            logger.info(
                "notify_smtp_sent job_id=%s to=%s status=%s",
                job.job_id,
                job.notify_email,
                job.status.value,
            )
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

    async def notify_alert(self, *, subject: str, body: str, email: str | None) -> None:
        if not email:
            # No target — log the intent (so the alert isn't silently lost)
            # and let the structured log event at the call site carry it.
            logger.info("notify_smtp_alert_no_target subject=%s", subject)
            return
        try:
            self._send_message(to_addr=email, subject=subject, body=body)
            logger.info("notify_smtp_alert_sent to=%s subject=%s", email, subject)
        except Exception:
            logger.warning(
                "notify_smtp_alert_failed to=%s host=%s subject=%s — alert "
                "delivery only; the underlying event is unchanged",
                email,
                self._host,
                subject,
                exc_info=True,
            )

    def _send_message(self, *, to_addr: str, subject: str, body: str) -> None:
        """Synchronous SMTP. The worker calls this from an async
        method; ``smtplib`` is blocking, but the call is short (<1s
        typical) and runs inside the worker's per-job slot so other
        jobs aren't blocked. If SMTP latency becomes a problem, swap
        to ``aiosmtplib`` — same envelope, async API."""
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self._from_addr
        msg["To"] = to_addr

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


# ---------------------------------------------------------------------------
# Factory — env-driven backend selection
# ---------------------------------------------------------------------------


def build_dispatcher() -> NotificationDispatcher:
    """Select a backend from env vars.

    Selection rule: ``MOVATE_SMTP_HOST`` set → SMTP; otherwise console.
    Operators wire SMTP via env on the worker container; nothing else
    in movate changes when they do.

    Idempotent + side-effect-free. Each worker startup calls this
    once; the worker holds the result for its lifetime.
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
    "NotificationDispatcher",
    "SmtpEmailBackend",
    "build_dispatcher",
]
