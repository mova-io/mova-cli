"""SMS notification dispatch — sister module to :mod:`movate.core.notify`.

Three backends, same shape as the email side:

* :class:`ConsoleSmsBackend` — logs the intended SMS instead of sending.
  Default when no SMS provider env is configured, or when the ACS SDK
  isn't installed. Means jobs with ``notify_sms`` set don't crash the
  worker on misconfigured deployments; the operator sees the intent in
  logs and can wire up real delivery later.
* :class:`AcsSmsBackend` — sends via Azure Communication Services.
  Picked over Twilio for v1.0 because the connection string lives in
  Key Vault under the same managed identity as our Postgres password
  (see [docs/v1.0-azure-design.md §10](../../../docs/v1.0-azure-design.md)).
* (Twilio backend is a future option — same Protocol, different SDK.)

Both backends implement the SAME :class:`NotificationDispatcher`
Protocol as the email backends — :func:`notify_terminal` is a no-op for
jobs without ``notify_sms``, so a composite dispatcher can hold one of
each and each backend ignores jobs aimed at the OTHER channel. That
keeps the worker integration trivial (one ``await
self._notifier.notify_terminal(job)`` regardless of channels).

The actual messages are SHORT by design — SMS is a 160-character
budget per segment. We render terse, status-first messages that fit
without segmentation in the common cases.

Env vars (read by :func:`build_sms_backend`):

* ``MOVATE_ACS_CONNECTION_STRING`` — required to activate ACS. The
  value is opaque; ACS hands it out as ``endpoint=https://...;accesskey=...``.
* ``MOVATE_ACS_FROM_NUMBER`` — required to activate ACS. E.164 number
  provisioned via :mod:`Microsoft.Communication/PhoneNumbers` (the Bicep
  module). Non-secret.
* ``MOVATE_ACS_TIMEOUT_SECONDS`` — optional, default 10.

If the connection string OR the from-number is missing, we return
:class:`ConsoleSmsBackend`. We do NOT half-activate — partial config
is always a misconfiguration.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from movate.core.models import JobRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConsoleSmsBackend — logs only (dev default + soft fallback)
# ---------------------------------------------------------------------------


class ConsoleSmsBackend:
    """Logs the intended SMS at INFO. Useful for dev, tests, and any
    deployment where ACS isn't (yet) configured.

    Operators see the log line and know what would have been sent if
    SMS were wired up. The log includes the recipient phone number so
    the operator can verify the destination is what they expected.
    """

    name = "console-sms"

    async def notify_terminal(self, job: JobRecord) -> None:
        if not job.notify_sms:
            return
        logger.info(
            "notify_sms_console job_id=%s target=%s status=%s would_text=%s body=%r",
            job.job_id,
            job.target,
            job.status.value,
            job.notify_sms,
            _body_for(job),
        )


# ---------------------------------------------------------------------------
# AcsSmsBackend — Azure Communication Services
# ---------------------------------------------------------------------------


class AcsSmsBackend:
    """Sends one SMS per terminal job via Azure Communication Services.

    The :mod:`azure.communication.sms` SDK is a SOFT dependency — operators
    on Azure install it via ``pip install movate[sms-acs]``. If the SDK
    isn't importable when :func:`build_sms_backend` runs, we fall back to
    :class:`ConsoleSmsBackend` with a noisy log; the worker keeps running.

    The connection string is opaque to us — we don't parse it. ACS will
    surface auth errors at send time, and we log + swallow them (same
    contract as the SMTP backend: notification failure must not sink
    the worker).
    """

    name = "acs-sms"

    def __init__(
        self,
        *,
        connection_string: str,
        from_number: str,
        timeout_seconds: float = 10.0,
        sms_client: Any | None = None,
    ) -> None:
        """``sms_client`` is the injection point for tests — they pass a
        fake that records calls instead of opening real connections. In
        production we build a real ``SmsClient`` from the connection
        string lazily on first ``notify_terminal`` so import-time
        failures don't crash the worker on construction."""
        self._connection_string = connection_string
        self._from_number = from_number
        self._timeout_seconds = timeout_seconds
        self._sms_client = sms_client  # may be None; built lazily

    def _get_sms_client(self) -> Any:
        if self._sms_client is not None:
            return self._sms_client
        # Local import keeps the SDK out of base movate's import tree —
        # operators without the SDK installed never hit this line because
        # build_sms_backend() returns ConsoleSmsBackend before touching us.
        from azure.communication.sms import (  # type: ignore[import-not-found]  # noqa: PLC0415
            SmsClient,
        )

        self._sms_client = SmsClient.from_connection_string(self._connection_string)
        return self._sms_client

    async def notify_terminal(self, job: JobRecord) -> None:
        if not job.notify_sms:
            return
        try:
            self._send_sync(job)
        except Exception:
            # Never sink the worker. Operators see the warning in logs
            # and can diagnose ACS separately from job execution.
            logger.warning(
                "notify_sms_failed job_id=%s to=%s from=%s — job state "
                "is unchanged; this is the notification path only",
                job.job_id,
                job.notify_sms,
                self._from_number,
                exc_info=True,
            )

    def _send_sync(self, job: JobRecord) -> None:
        """Synchronous ACS send. The SDK is blocking; we accept the same
        trade-off as the SMTP backend (sub-second per send, called from
        the worker's per-job slot so other jobs aren't blocked).

        ACS's ``send`` returns a list of per-recipient results — when we
        send to a single number, that's a one-element list. A failed
        send shows up as ``successful=False`` with an HTTP status; we
        log either way so operators can audit the wire.
        """
        client = self._get_sms_client()
        body = _body_for(job)
        # The SDK's `send` is sync; we don't need to thread to a pool for
        # the v1.0 volume (single SMS per terminal job, ~hundreds per day
        # at most). If volume grows we'll swap to an aio backend.
        results = client.send(
            from_=self._from_number,
            to=[job.notify_sms],
            message=body,
        )
        # Results is iterable of SmsSendResult; coerce to list for logging.
        results_list = list(results) if results is not None else []
        for r in results_list:
            successful = getattr(r, "successful", None)
            http_status = getattr(r, "http_status_code", None)
            to = getattr(r, "to", job.notify_sms)
            if successful is False:
                logger.warning(
                    "notify_sms_send_unsuccessful job_id=%s to=%s http=%s",
                    job.job_id,
                    to,
                    http_status,
                )
            else:
                logger.info(
                    "notify_sms_sent job_id=%s to=%s http=%s",
                    job.job_id,
                    to,
                    http_status,
                )


# ---------------------------------------------------------------------------
# Factory — env-driven backend selection
# ---------------------------------------------------------------------------


def build_sms_backend() -> ConsoleSmsBackend | AcsSmsBackend:
    """Select an SMS backend from env vars.

    Selection rule:

    1. Both ``MOVATE_ACS_CONNECTION_STRING`` AND ``MOVATE_ACS_FROM_NUMBER``
       set, AND the ``azure-communication-sms`` SDK importable →
       :class:`AcsSmsBackend`.
    2. Otherwise (any of the three missing) → :class:`ConsoleSmsBackend`,
       with a noisy log so operators know SMS will only print.

    Idempotent + side-effect-free. Each worker startup calls this once;
    the worker holds the result for its lifetime.
    """
    conn_str = os.environ.get("MOVATE_ACS_CONNECTION_STRING", "").strip()
    from_number = os.environ.get("MOVATE_ACS_FROM_NUMBER", "").strip()

    if not conn_str or not from_number:
        if conn_str or from_number:
            # Partial config is always a misconfiguration. Don't try to
            # rescue it — log loud and fall back so the operator sees
            # the issue immediately on worker boot.
            logger.warning(
                "notify_sms_partial_config: one of MOVATE_ACS_CONNECTION_STRING "
                "(%s) / MOVATE_ACS_FROM_NUMBER (%s) is set without the other; "
                "falling back to console SMS backend",
                "set" if conn_str else "unset",
                "set" if from_number else "unset",
            )
        return ConsoleSmsBackend()

    # SDK presence check — soft dep, see docstring.
    try:
        import azure.communication.sms  # type: ignore[import-not-found]  # noqa: F401, PLC0415
    except ImportError:
        logger.warning(
            "notify_sms_sdk_missing: MOVATE_ACS_* env is set but the "
            "azure-communication-sms package is not installed; falling back "
            "to console SMS backend. Install via: pip install movate[sms-acs]",
        )
        return ConsoleSmsBackend()

    try:
        timeout = float(os.environ.get("MOVATE_ACS_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10.0

    return AcsSmsBackend(
        connection_string=conn_str,
        from_number=from_number,
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# Message composition — SMS is terse on purpose
# ---------------------------------------------------------------------------


def _body_for(job: JobRecord) -> str:
    """Render a single-segment-friendly SMS body (≤160 chars in the
    common case). Status, target, optional error type — no run id, no
    URL, no salutation. Operators triage on email; SMS is "wake up".

    Format: ``[movate] ✓ agent/faq-agent — success (124ms)``
    or:     ``[movate] ✗ workflow/returns — error: BUDGET_EXCEEDED``
    """
    icon = {
        "success": "✓",
        "error": "✗",
        "safety_blocked": "⊘",
        "dead_letter": "☠",
    }.get(job.status.value, "?")
    base = f"[movate] {icon} {job.kind.value}/{job.target} — {job.status.value}"
    if job.error:
        base += f": {job.error.type}"
    elif job.claimed_at and job.completed_at:
        elapsed_ms = int((job.completed_at - job.claimed_at).total_seconds() * 1000)
        base += f" ({elapsed_ms}ms)"
    return base


__all__ = [
    "AcsSmsBackend",
    "ConsoleSmsBackend",
    "build_sms_backend",
]
