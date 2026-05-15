"""Outbound notifications for long-running operations.

Today: ``mdk deploy`` fires a ``deploy_succeeded`` event via
:func:`notify_deploy_success`. The implementation supports two
backends out of the box:

* **Telegram bot** — set ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``
  and the notifier posts a structured message to the chat.
* **Generic webhook** — set ``MOVATE_DEPLOY_WEBHOOK`` to a URL and
  the notifier POSTs the same payload as JSON. Works for Slack
  (incoming-webhooks), Microsoft Teams (connector cards), Discord
  webhooks, or any custom HTTP receiver.

Notification failures are NEVER fatal — they log a warning and the
caller's success path continues. Operators can grep the
``mdk_notify_summary:`` line on stderr to verify delivery in CI.

Design intent: notifications are observability, not control flow.
A flaky Telegram outage can't take down a deploy.
"""

from __future__ import annotations

from movate.notify.deploy_notify import (
    DeployEvent,
    notify_deploy_success,
)

__all__ = ["DeployEvent", "notify_deploy_success"]
