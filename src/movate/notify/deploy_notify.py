"""Deploy-success notifications: Telegram + generic webhook.

The :func:`notify_deploy_success` entry point checks both backends
and fires whichever are configured. Both can fire on the same
deploy — operators who route to BOTH a Telegram channel and a
Slack incoming-webhook get both messages.

Env-var contract:

* ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` — Telegram bot path.
  Get the token from BotFather; the chat ID from
  ``getUpdates`` after sending a /start to the bot.
* ``MOVATE_DEPLOY_WEBHOOK`` — generic HTTP webhook URL. Movate POSTs
  a JSON body with all DeployEvent fields. Receiver shapes the
  message its own way.

All HTTP calls are bounded by a short timeout (5s) so a slow Telegram
or webhook can't hang the deploy summary. Failures log a warning
and return — never raise.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass

import httpx

logger = logging.getLogger(__name__)


# Bounded so a hung receiver can't hang the deploy. Operators who
# need bigger payloads can shape via the receiver, not by extending
# the client timeout.
_HTTP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class DeployEvent:
    """Structured payload sent to every configured notification sink.

    Fields are deliberately stable + minimal so receivers can pattern-
    match without parsing free-form prose. The Telegram backend renders
    these to a human-readable message; the webhook backend POSTs them
    verbatim as JSON.
    """

    target: str
    """Deployment target name (e.g. 'prod', 'staging')."""

    image_tag: str
    """ACR image tag pushed in this deploy (e.g. 'movate:0.6.1-a1b2c3d')."""

    runtime_url: str
    """Public URL of the deployed runtime, e.g. https://faq-runtime.example.com."""

    git_sha: str
    """7-char git SHA at deploy time. Empty string if outside a git tree."""

    deployer: str
    """Who deployed — usually `$USER` or a CI workflow identifier."""

    duration_seconds: float
    """Wall-clock duration of the deploy from build start to /healthz green."""

    version: str
    """Package version that landed (from movate.__version__)."""


def notify_deploy_success(event: DeployEvent) -> None:
    """Fire notifications for all configured backends.

    Idempotent + non-raising. A backend with missing env vars is a
    silent no-op; a backend whose HTTP call fails logs the error and
    moves on. Operators check delivery via the ``mdk_notify_summary:``
    line printed at the end of ``mdk deploy``.
    """
    telegram_sent = _try_telegram(event)
    webhook_sent = _try_webhook(event)

    # Greppable single-line summary for CI parity with the other
    # diagnostic commands (audit/eval/doctor/init/add).
    import sys  # noqa: PLC0415

    sys.stderr.write(
        f"mdk_notify_summary: "
        f"telegram={str(telegram_sent).lower()} "
        f"webhook={str(webhook_sent).lower()}\n"
    )


def _try_telegram(event: DeployEvent) -> bool:
    """POST to ``https://api.telegram.org/bot<TOKEN>/sendMessage``.

    Returns True on confirmed 200 from Telegram, False otherwise
    (missing env, network error, non-200 response). The boolean is
    surfaced in the greppable summary line so CI can verify delivery.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    text = _format_telegram_message(event)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Telegram notification failed: %s", exc)
        return False

    if resp.status_code != _HTTP_OK:
        logger.warning(
            "Telegram notification returned %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    return True


_HTTP_OK = 200
_HTTP_SUCCESS_RANGE = range(200, 300)


def _try_webhook(event: DeployEvent) -> bool:
    """POST event as JSON to ``MOVATE_DEPLOY_WEBHOOK``.

    Generic — works for Slack incoming-webhooks (text field), Teams
    connector cards (raw JSON), Discord webhooks, or any custom
    receiver. Movate doesn't reshape the body per receiver type; the
    receiver shapes the message its own way using the structured
    fields.
    """
    url = os.environ.get("MOVATE_DEPLOY_WEBHOOK", "").strip()
    if not url:
        return False

    try:
        resp = httpx.post(url, json=asdict(event), timeout=_HTTP_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("Webhook notification failed: %s", exc)
        return False

    # 2xx is success. Slack returns 200 with "ok" body; Teams
    # connector returns 200 with "1"; Discord returns 204. Treat
    # everything in [200, 300) uniformly.
    if resp.status_code not in _HTTP_SUCCESS_RANGE:
        logger.warning(
            "Webhook notification returned %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    return True


def _format_telegram_message(event: DeployEvent) -> str:
    """Render a Markdown message for Telegram.

    Telegram's Markdown mode supports `*bold*`, `_italic_`, `` `code` ``,
    and `[text](url)`. We keep the message terse — operators read it
    on a phone notification, not in a wide terminal.
    """
    sha_line = f"\nGit SHA: `{event.git_sha}`" if event.git_sha else ""
    return (
        f"*✓ Deployed to {event.target}*\n"
        f"`{event.image_tag}`{sha_line}\n"
        f"v{event.version} · deployed by `{event.deployer}` · "
        f"{event.duration_seconds:.1f}s\n\n"
        f"[Open runtime]({event.runtime_url})"
    )
