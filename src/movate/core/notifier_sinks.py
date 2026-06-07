"""Concrete HITL Notifier backends (ADR 083 / ADR 077 D3).

Thin HTTP adapters behind the :class:`~movate.core.notifier.NotifierProvider`
Protocol. Imported ONLY by :func:`movate.core.notifier.build_notifier` — never by
execution logic, so ``core`` stays decoupled from any concrete transport
(CLAUDE.md rule 6/7). Mirrors :mod:`movate.core.alert_sinks` (Teams MessageCard +
generic HMAC-signed webhook). Every method is **fire-and-forget**: a transport
error or non-2xx logs and returns ``False`` — it never raises (a notification is
courtesy, never load-bearing).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from movate.core.notifier import HumanPause

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10.0
# Teams card accent — amber: "action needed", not an error/alert red.
_THEME_COLOR = "D29200"


def _approvers_label(approvers: list[str]) -> str:
    return ", ".join(approvers) if approvers else "(any approver)"


class TeamsNotifier:
    """POST a Microsoft Teams MessageCard for a paused HUMAN node."""

    def __init__(
        self,
        *,
        webhook_url: str,
        name: str = "teams",
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._webhook_url = webhook_url
        self.name = name
        self._timeout = timeout

    def _payload(self, pause: HumanPause) -> dict[str, Any]:
        return {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": _THEME_COLOR,
            "summary": f"Approval needed: {pause.workflow_name}",
            "title": f"⏸ Human approval needed — {pause.workflow_name}",
            "text": pause.prompt or "A workflow is paused awaiting a human decision.",
            "sections": [
                {
                    "facts": [
                        {"name": "Run", "value": pause.run_id},
                        {"name": "Node", "value": pause.node_id},
                        {
                            "name": "Workflow",
                            "value": f"{pause.workflow_name} v{pause.workflow_version}",
                        },
                        {"name": "Backend", "value": pause.runtime},
                        {"name": "Approvers", "value": _approvers_label(pause.approvers)},
                        {
                            "name": "Decide via",
                            "value": pause.resume_url(),
                        },
                        {
                            "name": "Decision must supply",
                            "value": ", ".join(pause.output_contract) or "(no contract)",
                        },
                    ]
                }
            ],
        }

    async def notify_human_pause(self, pause: HumanPause) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._webhook_url, json=self._payload(pause))
        except httpx.HTTPError:
            logger.warning(
                "notifier_teams_transport_error run_id=%s node=%s — not delivered",
                pause.run_id,
                pause.node_id,
                exc_info=True,
            )
            return False
        if not resp.is_success:
            logger.warning(
                "notifier_teams_non2xx run_id=%s node=%s status=%s",
                pause.run_id,
                pause.node_id,
                resp.status_code,
            )
            return False
        return True


class GenericWebhookNotifier:
    """POST a typed JSON envelope to any URL, optionally HMAC-signed.

    Envelope: ``{"type": "hitl.human_pause", ...pause fields..., "resume_url"}``.
    When ``secret`` is set, an ``X-MDK-Signature: t=<ts>,v1=<hmac-sha256>`` header
    (stripe-style, computed over the exact bytes) lets the receiver verify
    authenticity — same scheme as :class:`movate.core.alert_sinks.GenericWebhookSink`.
    """

    def __init__(
        self,
        *,
        webhook_url: str,
        secret: str | None = None,
        name: str = "webhook",
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._webhook_url = webhook_url
        self._secret = secret
        self.name = name
        self._timeout = timeout

    def _envelope(self, pause: HumanPause) -> dict[str, Any]:
        return {
            "type": "hitl.human_pause",
            "run_id": pause.run_id,
            "workflow": pause.workflow_name,
            "workflow_version": pause.workflow_version,
            "node_id": pause.node_id,
            "prompt": pause.prompt,
            "output_contract": list(pause.output_contract),
            "approvers": list(pause.approvers),
            "tenant_id": pause.tenant_id,
            "runtime": pause.runtime,
            "resume_url": pause.resume_url(),
        }

    def _headers(self, body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._secret:
            ts = str(int(time.time()))
            mac = hmac.new(
                self._secret.encode("utf-8"),
                f"{ts}.".encode() + body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-MDK-Signature"] = f"t={ts},v1={mac}"
        return headers

    async def notify_human_pause(self, pause: HumanPause) -> bool:
        body = json.dumps(self._envelope(pause), separators=(",", ":")).encode("utf-8")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._webhook_url, content=body, headers=self._headers(body)
                )
        except httpx.HTTPError:
            logger.warning(
                "notifier_webhook_transport_error run_id=%s node=%s — not delivered",
                pause.run_id,
                pause.node_id,
                exc_info=True,
            )
            return False
        if not resp.is_success:
            logger.warning(
                "notifier_webhook_non2xx run_id=%s node=%s status=%s",
                pause.run_id,
                pause.node_id,
                resp.status_code,
            )
            return False
        return True


class SlackNotifier:
    """POST a Slack message (incoming webhook) for a paused HUMAN node.

    Uses Slack's incoming-webhook contract: a ``text`` fallback plus Block Kit
    ``blocks`` for a readable card. Same fire-and-forget posture as the other
    sinks — a transport error / non-2xx logs and returns ``False``, never raises.
    """

    def __init__(
        self,
        *,
        webhook_url: str,
        name: str = "slack",
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._webhook_url = webhook_url
        self.name = name
        self._timeout = timeout

    def _payload(self, pause: HumanPause) -> dict[str, Any]:
        prompt = pause.prompt or "A workflow is paused awaiting a human decision."
        contract = ", ".join(pause.output_contract) or "(no contract)"
        fields = (
            f"*Run:* {pause.run_id}\n"
            f"*Node:* {pause.node_id}\n"
            f"*Workflow:* {pause.workflow_name} v{pause.workflow_version}\n"
            f"*Backend:* {pause.runtime}\n"
            f"*Approvers:* {_approvers_label(pause.approvers)}\n"
            f"*Decision must supply:* {contract}\n"
            f"*Decide via:* {pause.resume_url()}"
        )
        return {
            # Plain-text fallback (notifications / no-Block-Kit clients).
            "text": f":pause_button: Human approval needed — {pause.workflow_name}: {prompt}",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"⏸ Human approval needed — {pause.workflow_name}",
                    },
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": prompt}},
                {"type": "section", "text": {"type": "mrkdwn", "text": fields}},
            ],
        }

    async def notify_human_pause(self, pause: HumanPause) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._webhook_url, json=self._payload(pause))
        except httpx.HTTPError:
            logger.warning(
                "notifier_slack_transport_error run_id=%s node=%s — not delivered",
                pause.run_id,
                pause.node_id,
                exc_info=True,
            )
            return False
        if not resp.is_success:
            logger.warning(
                "notifier_slack_non2xx run_id=%s node=%s status=%s",
                pause.run_id,
                pause.node_id,
                resp.status_code,
            )
            return False
        return True


__all__ = ["GenericWebhookNotifier", "SlackNotifier", "TeamsNotifier"]
