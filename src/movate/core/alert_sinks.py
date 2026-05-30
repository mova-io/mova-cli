"""Concrete alert sinks — thin HTTP adapters behind :class:`AlertSink` (ADR 057 D3).

These are the alerting-side companions to ``core/notify.py``'s SMTP / console
backends: each is a small adapter that turns an :class:`AlertEvent` into one
outbound HTTP POST. They add **no new shipped dependency** — delivery rides
``httpx`` (already a core dep) and HMAC signing reuses ``core/webhooks``'
stripe-style :func:`sign_payload`.

Sinks here (ADR 057 D3):

* :class:`SlackSink` — POST to a Slack Incoming Webhook (``SLACK_WEBHOOK_URL``).
* :class:`TeamsSink` — POST a MessageCard to a Teams Incoming Webhook
  (``TEAMS_WEBHOOK_URL``).
* :class:`GenericWebhookSink` — POST a typed JSON envelope to any URL, HMAC-
  signed (``X-MDK-Signature``) when a secret is configured.

Credentials ride the existing **BYOK env seam** (ADR 018) — no new credential
model: ``SLACK_WEBHOOK_URL`` / ``TEAMS_WEBHOOK_URL`` / a per-sink URL + secret.
:func:`build_sinks_from_env` autoloads whichever are configured into a
:class:`SinkRegistry`; absent ⇒ that sink simply isn't registered (opt-in).

Best-effort contract (ADR 057 D5): :meth:`deliver` returns ``True`` on a 2xx and
``False`` on a non-2xx or transport error — it does **not** raise for ordinary
delivery failure. The :class:`AlertRouter` is the final best-effort guard either
way (it also catches a raise), so the never-break-the-source invariant holds
even if a sink misbehaves.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from movate.core.alerts import AlertEvent, Severity, SinkRegistry
from movate.core.webhooks import sign_payload

logger = logging.getLogger(__name__)

# Bounded per-delivery timeout. Alerting must never hang the caller's path;
# the router runs delivery best-effort, but a tight timeout is the first guard.
DEFAULT_TIMEOUT_SECONDS = 5.0

# Slack / Teams render severity with a small visual cue.
_SEVERITY_EMOJI = {
    Severity.INFO: ":information_source:",
    Severity.WARNING: ":warning:",
    Severity.CRITICAL: ":rotating_light:",
}
# Teams MessageCard accent color (hex, no '#').
_SEVERITY_COLOR = {
    Severity.INFO: "2EB67D",
    Severity.WARNING: "ECB22E",
    Severity.CRITICAL: "E01E5A",
}


def _suppressed_suffix(suppressed_count: int) -> str:
    """`` (+37 suppressed)`` tail when duplicates were throttled (D4), else ``""``."""
    if suppressed_count > 0:
        return f" (+{suppressed_count} suppressed since last alert)"
    return ""


_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


def _is_ok(status_code: int) -> bool:
    return _HTTP_OK_MIN <= status_code < _HTTP_OK_MAX


# ---------------------------------------------------------------------------
# SlackSink
# ---------------------------------------------------------------------------


class SlackSink:
    """POST an :class:`AlertEvent` to a Slack Incoming Webhook.

    Slack incoming webhooks accept a JSON ``{"text": ...}`` (plus optional
    blocks). We keep it to a single ``text`` line — one alert, one message —
    so this stays a thin adapter (the dashboard panel is a later step).
    """

    def __init__(
        self,
        *,
        webhook_url: str,
        name: str = "slack",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._webhook_url = webhook_url
        self.name = name
        self._timeout = timeout

    def _payload(self, event: AlertEvent, suppressed_count: int) -> dict[str, Any]:
        emoji = _SEVERITY_EMOJI.get(event.severity, "")
        text = (
            f"{emoji} *[{event.severity.label.upper()}] {event.kind.value}* "
            f"— {event.summary}"
            f"{_suppressed_suffix(suppressed_count)}\n"
            f"tenant: `{event.tenant_id}` · subject: `{event.subject}`"
        )
        return {"text": text}

    async def deliver(self, event: AlertEvent, *, suppressed_count: int = 0) -> bool:
        payload = self._payload(event, suppressed_count)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._webhook_url, json=payload)
        except httpx.HTTPError:
            logger.warning(
                "alert_slack_transport_error sink=%s event_id=%s — delivery only",
                self.name,
                event.id,
                exc_info=True,
            )
            return False
        if not _is_ok(resp.status_code):
            logger.warning(
                "alert_slack_non2xx sink=%s event_id=%s status=%s",
                self.name,
                event.id,
                resp.status_code,
            )
            return False
        return True


# ---------------------------------------------------------------------------
# TeamsSink
# ---------------------------------------------------------------------------


class TeamsSink:
    """POST an :class:`AlertEvent` to a Microsoft Teams Incoming Webhook.

    Teams connectors accept a legacy ``MessageCard`` JSON. We emit a minimal
    card (title + text + severity accent color) — enough to be actionable
    without pulling in the Adaptive Card schema.
    """

    def __init__(
        self,
        *,
        webhook_url: str,
        name: str = "teams",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._webhook_url = webhook_url
        self.name = name
        self._timeout = timeout

    def _payload(self, event: AlertEvent, suppressed_count: int) -> dict[str, Any]:
        return {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": _SEVERITY_COLOR.get(event.severity, "808080"),
            "summary": f"[{event.severity.label.upper()}] {event.kind.value}",
            "title": (
                f"[{event.severity.label.upper()}] {event.kind.value}"
                f"{_suppressed_suffix(suppressed_count)}"
            ),
            "text": event.summary,
            "sections": [
                {
                    "facts": [
                        {"name": "Tenant", "value": event.tenant_id},
                        {"name": "Subject", "value": event.subject},
                    ]
                }
            ],
        }

    async def deliver(self, event: AlertEvent, *, suppressed_count: int = 0) -> bool:
        payload = self._payload(event, suppressed_count)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._webhook_url, json=payload)
        except httpx.HTTPError:
            logger.warning(
                "alert_teams_transport_error sink=%s event_id=%s — delivery only",
                self.name,
                event.id,
                exc_info=True,
            )
            return False
        if not _is_ok(resp.status_code):
            logger.warning(
                "alert_teams_non2xx sink=%s event_id=%s status=%s",
                self.name,
                event.id,
                resp.status_code,
            )
            return False
        return True


# ---------------------------------------------------------------------------
# GenericWebhookSink
# ---------------------------------------------------------------------------


class GenericWebhookSink:
    """POST a typed JSON envelope to any URL, HMAC-signed (ADR 057 D3).

    The envelope is the :class:`AlertEvent` serialized plus a ``suppressed_count``
    (D4). When a ``secret`` is configured the POST carries an ``X-MDK-Signature``
    header (stripe-style ``t=<ts>,v1=<hmac-sha256>``) computed over the exact
    bytes on the wire — reusing :func:`movate.core.webhooks.sign_payload`, so
    the format matches the rest of the platform's outbound webhooks and a
    receiver can verify with the existing ``verify_signature``.
    """

    def __init__(
        self,
        *,
        url: str,
        secret: str | None = None,
        name: str = "webhook",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        self._secret = secret
        self.name = name
        self._timeout = timeout

    def _body(self, event: AlertEvent, suppressed_count: int) -> bytes:
        envelope = {
            "type": "alert",
            "suppressed_count": suppressed_count,
            "alert": event.model_dump(mode="json"),
        }
        # Compact, stable separators so the signed bytes match the wire bytes.
        return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")

    async def deliver(self, event: AlertEvent, *, suppressed_count: int = 0) -> bool:
        body = self._body(event, suppressed_count)
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-MDK-Signature"] = sign_payload(secret=self._secret, body=body)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, content=body, headers=headers)
        except httpx.HTTPError:
            logger.warning(
                "alert_webhook_transport_error sink=%s event_id=%s — delivery only",
                self.name,
                event.id,
                exc_info=True,
            )
            return False
        if not _is_ok(resp.status_code):
            logger.warning(
                "alert_webhook_non2xx sink=%s event_id=%s status=%s",
                self.name,
                event.id,
                resp.status_code,
            )
            return False
        return True


# ---------------------------------------------------------------------------
# BYOK autoload (ADR 018) — env → SinkRegistry
# ---------------------------------------------------------------------------


def build_sinks_from_env(
    env: dict[str, str] | None = None,
) -> SinkRegistry:
    """Build a :class:`SinkRegistry` from configured env vars (BYOK, ADR 018).

    Registers whichever sinks have credentials present; absent ⇒ not registered
    (opt-in, zero behavior change). Recognized vars:

    * ``SLACK_WEBHOOK_URL``  → :class:`SlackSink` registered as ``slack``.
    * ``TEAMS_WEBHOOK_URL``  → :class:`TeamsSink` registered as ``teams``.
    * ``MDK_ALERT_WEBHOOK_URL`` (+ optional ``MDK_ALERT_WEBHOOK_SECRET``)
      → :class:`GenericWebhookSink` registered as ``webhook``.

    The registry's sink names are what routes reference. Operators who want
    several Slack channels (or several webhooks) register additional sinks
    programmatically; this autoload covers the common single-target case.
    """
    source = env if env is not None else dict(os.environ)
    registry = SinkRegistry()

    slack_url = (source.get("SLACK_WEBHOOK_URL") or "").strip()
    if slack_url:
        registry.register(SlackSink(webhook_url=slack_url, name="slack"))

    teams_url = (source.get("TEAMS_WEBHOOK_URL") or "").strip()
    if teams_url:
        registry.register(TeamsSink(webhook_url=teams_url, name="teams"))

    webhook_url = (source.get("MDK_ALERT_WEBHOOK_URL") or "").strip()
    if webhook_url:
        secret = (source.get("MDK_ALERT_WEBHOOK_SECRET") or "").strip() or None
        registry.register(GenericWebhookSink(url=webhook_url, secret=secret, name="webhook"))

    return registry


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "GenericWebhookSink",
    "SlackSink",
    "TeamsSink",
    "build_sinks_from_env",
]
