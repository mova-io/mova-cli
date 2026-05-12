"""Telegram notification dispatch — operator alerts via a Telegram bot.

Sister module to :mod:`movate.core.notify` (email) and
:mod:`movate.core.notify_sms` (SMS). Same :class:`NotificationDispatcher`
Protocol; composed by :class:`~movate.core.notify.MultiDispatcher`.

The use case is **operator-level personal alerts** — "ping me when the
worker finishes a job I submitted." Unlike email + SMS (which are
*per-job* opt-in via ``job.notify_email`` / ``job.notify_sms``),
Telegram is *operator-wide*: when env config is present, every terminal
job fires the alert to the operator's chat. There's no per-job
``notify_telegram`` field because the use case (single operator pinging
themselves on every job) doesn't need it. Multi-user / per-job routing
is a clean extension if it ever becomes a need.

Two backends:

* :class:`ConsoleTelegramBackend` — logs the intended message. Default
  when the env isn't configured, or when the operator wants a no-op
  dry-run.
* :class:`TelegramBackend` — POSTs to Telegram's Bot API
  (``https://api.telegram.org/bot<TOKEN>/sendMessage``). Pure HTTP via
  ``httpx`` (already a movate dep), no SDK needed.

Setup (one-time, ~5 min on a phone):

1. Open Telegram, search for ``@BotFather``, send ``/newbot``, give it
   a name (e.g. ``movate-jeremy-dev``). BotFather returns a token.
2. Search for your new bot in Telegram, send ``/start`` — this
   activates the chat between you and the bot.
3. Open ``https://api.telegram.org/bot<TOKEN>/getUpdates`` in your
   browser. Find your ``"chat":{"id": ...}`` — that's your chat_id.
4. Stash both in the worker's env:
     * ``MOVATE_TELEGRAM_BOT_TOKEN`` (secret — KV reference recommended)
     * ``MOVATE_TELEGRAM_CHAT_ID`` (non-secret; can be a literal env var)

If either env var is missing → falls back to :class:`ConsoleTelegramBackend`.

Why Telegram over SMS for this use case:

* **Free** (no per-message cost, no monthly number rental).
* **No regulatory hurdles** (US SMS requires A2P 10DLC brand
  registration; ~2-3 weeks of ops before the first message goes out).
* **Cross-platform** — same notification on phone, desktop, web.
* **Richer formatting** — Markdown, inline buttons, file attachments
  if we ever want them (not used today; the door's open).

SMS via Azure Communication Services (sister module) remains the right
choice for **customer-facing** notification surfaces where SMS is the
expected channel. Telegram is for the operator's personal dev loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from movate.core.models import JobRecord

logger = logging.getLogger(__name__)

# Telegram's Bot API hostname. Kept module-level for testability —
# tests inject a fake httpx client and never hit the real API.
_TELEGRAM_API_BASE = "https://api.telegram.org"

# HTTP status threshold for "this didn't work" — anything ≥ here gets
# logged as send_unsuccessful. Module-level so it's not a "magic number"
# that ruff complains about.
_HTTP_FAILURE_THRESHOLD = 400


# ---------------------------------------------------------------------------
# ConsoleTelegramBackend — logs only (dev default + soft fallback)
# ---------------------------------------------------------------------------


class ConsoleTelegramBackend:
    """Logs the intended notification at INFO. Used when ``MOVATE_TELEGRAM_*``
    env isn't configured. Operators see what would be sent and can wire
    up real delivery later.

    Fires on every terminal job — same trigger semantics as the real
    :class:`TelegramBackend` so the operator's "would this be noisy?"
    question gets the same answer in both modes.
    """

    name = "console-telegram"

    async def notify_terminal(self, job: JobRecord) -> None:
        logger.info(
            "notify_telegram_console job_id=%s target=%s status=%s body=%r",
            job.job_id,
            job.target,
            job.status.value,
            _body_for(job),
        )


# ---------------------------------------------------------------------------
# TelegramBackend — real delivery via Telegram Bot API
# ---------------------------------------------------------------------------


class TelegramBackend:
    """Sends one Telegram message per terminal job via Bot API.

    Async-native: uses ``httpx.AsyncClient`` so the worker's event loop
    isn't blocked (unlike the SMTP + ACS backends, both of which use
    sync SDKs). Telegram's Bot API is purely HTTP, so this is the
    natural fit.

    The ``http_client`` constructor parameter is the test injection
    point. In production it's None (we lazily build a default
    ``AsyncClient``); tests pass a fake that records calls instead of
    hitting the network.
    """

    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client  # may be None; built lazily

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy + cached AsyncClient. Built once per backend instance;
        re-used across calls so the connection pool stays warm.

        We don't close it on shutdown — the worker exits and the OS
        reclaims fds. If we ever need explicit cleanup, surface a
        ``close()`` method here and call it from the worker's shutdown
        hook.
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._http_client

    async def notify_terminal(self, job: JobRecord) -> None:
        try:
            await self._send(job)
        except Exception:
            # Same contract as SMTP / ACS backends: notification is
            # courtesy. Never let it sink the worker. Operators see
            # the warning in logs and can diagnose Telegram separately.
            logger.warning(
                "notify_telegram_failed job_id=%s chat_id=%s — job state "
                "is unchanged; this is the notification path only",
                job.job_id,
                self._chat_id,
                exc_info=True,
            )

    async def _send(self, job: JobRecord) -> None:
        client = await self._get_client()
        url = f"{_TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        body = {
            "chat_id": self._chat_id,
            "text": _body_for(job),
            # Telegram Markdown-V2 has annoying escape rules; the simpler
            # legacy "Markdown" mode handles *bold*, _italic_, `code`,
            # links. That's all we need; if we ever want tables / code
            # blocks with arbitrary content we'll flip to MarkdownV2 +
            # escape.
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        response = await client.post(url, json=body)
        # 4xx/5xx → log and move on. We don't retry — the worker's
        # retry policy is for the JOB itself, not the notification.
        if response.status_code >= _HTTP_FAILURE_THRESHOLD:
            logger.warning(
                "notify_telegram_send_unsuccessful job_id=%s http=%s body=%r",
                job.job_id,
                response.status_code,
                response.text[:200],  # truncate; Telegram error responses are small
            )
        else:
            logger.info(
                "notify_telegram_sent job_id=%s chat_id=%s http=%s",
                job.job_id,
                self._chat_id,
                response.status_code,
            )


# ---------------------------------------------------------------------------
# Factory — env-driven backend selection
# ---------------------------------------------------------------------------


def build_telegram_backend() -> ConsoleTelegramBackend | TelegramBackend:
    """Select a Telegram backend from env vars.

    Selection rule:

    1. Both ``MOVATE_TELEGRAM_BOT_TOKEN`` AND ``MOVATE_TELEGRAM_CHAT_ID``
       set → :class:`TelegramBackend`.
    2. Otherwise (any missing) → :class:`ConsoleTelegramBackend` with a
       warning if the config is *partial* (one set, the other unset —
       always a misconfiguration).

    Idempotent + side-effect-free. Each worker startup calls this once.
    """
    bot_token = os.environ.get("MOVATE_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("MOVATE_TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        if bot_token or chat_id:
            logger.warning(
                "notify_telegram_partial_config: one of "
                "MOVATE_TELEGRAM_BOT_TOKEN (%s) / MOVATE_TELEGRAM_CHAT_ID (%s) "
                "is set without the other; falling back to console "
                "telegram backend",
                "set" if bot_token else "unset",
                "set" if chat_id else "unset",
            )
        return ConsoleTelegramBackend()

    try:
        timeout = float(os.environ.get("MOVATE_TELEGRAM_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10.0

    return TelegramBackend(
        bot_token=bot_token,
        chat_id=chat_id,
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# Message composition — same shape as SMS (terse, status-first)
# ---------------------------------------------------------------------------


def _body_for(job: JobRecord) -> str:
    """Render the message body. Telegram supports Markdown so we use a
    bit of light formatting (status icon, code-tagged target) for
    skim-ability on a phone notification.

    Kept under ~250 chars so the message fits in the notification
    preview on iOS / Android without expansion.
    """
    icon = {
        "success": "✅",
        "error": "❌",
        "safety_blocked": "🛑",
        "dead_letter": "💀",
    }.get(job.status.value, "❓")

    lines = [
        f"{icon} *movate* {job.kind.value}/`{job.target}` — *{job.status.value}*",
    ]
    if job.error:
        lines.append(f"error: `{job.error.type}` — {job.error.message[:120]}")
    elif job.claimed_at and job.completed_at:
        elapsed_ms = int((job.completed_at - job.claimed_at).total_seconds() * 1000)
        lines.append(f"elapsed: {elapsed_ms}ms")
    if job.result_run_id:
        lines.append(f"run: `{job.result_run_id[:8]}`")
    return "\n".join(lines)


# Keep _body_for exported indirectly for testing without leaking the
# internal name into the public surface.
__all__ = [
    "ConsoleTelegramBackend",
    "TelegramBackend",
    "build_telegram_backend",
]


# Re-export _body_for under a stable name for tests that need to
# assert on rendered message content. Underscore prefix is the
# "intentionally unexported" hint; tests import explicitly.
def _render_body_for_testing(job: JobRecord) -> str:  # pragma: no cover
    return _body_for(job)


# Imported by `Any` to satisfy mypy on the loose typing of the message
# body (Markdown-V2 escape rules aren't typed). Kept at module scope
# so the static analyzer sees the import.
_ = Any
