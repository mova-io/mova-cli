"""HITL escalation Notifier seam (ADR 083 / ADR 077 D3).

When a workflow pauses at a HUMAN node, an approver must actually be **told** —
otherwise the run just sits in ``?status=paused`` and the "escalate to a human"
box is a dead end. This module is the delivery seam: a small Protocol with a
no-op default and concrete Teams / webhook backends, selected by env.

Mirrors the ``build_dispatcher`` / ``build_cache`` / ``AlertSink`` patterns —
**opt-in, fail-safe, wired at the edge**:

* Execution logic (the native runner's HUMAN-pause branch, the Temporal
  ``call_human_activity``) calls the one-liner :func:`notify_human_pause_safe`.
* That resolves the env-selected backend (cached, :func:`get_notifier`) and
  delivers **fire-and-forget** — a transport error or misconfig logs and
  returns; it can never sink a workflow (CLAUDE.md rules 6/7/10).
* Concrete backends live in :mod:`movate.core.notifier_sinks` and are imported
  ONLY by :func:`build_notifier` — never by execution logic, so ``core`` depends
  on this Protocol, not a concrete Teams/webhook transport.

Selection (``MOVATE_NOTIFIER``):

* unset / ``none`` / ``console`` → :class:`NoOpNotifier` (logs only; the default,
  so the native + Temporal paths are byte-for-byte unchanged when unconfigured).
* ``teams``   → POST a MessageCard to ``MOVATE_NOTIFIER_TEAMS_WEBHOOK_URL``.
* ``webhook`` → POST a JSON envelope to ``MOVATE_NOTIFIER_WEBHOOK_URL``
  (optionally HMAC-signed with ``MOVATE_NOTIFIER_WEBHOOK_SECRET``).

A selected backend with its URL missing falls back to no-op (fail-safe — a
half-config must not break paused-run handling).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HumanPause:
    """The escalation context delivered when a workflow pauses at a HUMAN node.

    Built identically from the native runner's pause write and the Temporal
    ``call_human_activity`` so both backends notify with the same shape. Carries
    only pause metadata — never secrets or full state.
    """

    run_id: str
    workflow_name: str
    workflow_version: str
    node_id: str
    prompt: str
    output_contract: list[str]
    approvers: list[str]
    tenant_id: str
    runtime: str  # "native" | "temporal" — which backend owns the resume.

    def signal_path(self) -> str:
        """The HTTP door that resolves this pause (the resume endpoint)."""
        return f"/api/v1/workflow-runs/{self.run_id}/signal"

    def resume_url(self) -> str:
        """Full resume URL when ``MOVATE_RUNTIME_URL`` is set, else the path.

        Execution logic doesn't know the runtime's public origin, so we prefix
        with ``MOVATE_RUNTIME_URL`` when an operator has set it (the same env the
        playground/teams modules use); otherwise the actionable path alone.
        """
        base = os.environ.get("MOVATE_RUNTIME_URL", "").strip().rstrip("/")
        return f"{base}{self.signal_path()}" if base else self.signal_path()


@runtime_checkable
class NotifierProvider(Protocol):
    """Deliver a HITL escalation to approvers. Implementations never raise."""

    name: str

    async def notify_human_pause(self, pause: HumanPause) -> bool:
        """Deliver ``pause`` to the approval channel. Returns delivered?-ok."""
        ...


class NoOpNotifier:
    """Default backend — logs at debug, delivers nowhere. Zero behavior change."""

    name = "noop"

    async def notify_human_pause(self, pause: HumanPause) -> bool:
        logger.debug(
            "notifier_noop run_id=%s node=%s approvers=%s (MOVATE_NOTIFIER unset)",
            pause.run_id,
            pause.node_id,
            pause.approvers,
        )
        return True


def build_notifier() -> NotifierProvider:
    """Select the HITL notifier from env. Fail-safe to no-op (never raises)."""
    selector = os.environ.get("MOVATE_NOTIFIER", "").strip().lower()
    if selector in ("", "none", "off", "noop", "console"):
        return NoOpNotifier()
    if selector == "teams":
        url = os.environ.get("MOVATE_NOTIFIER_TEAMS_WEBHOOK_URL", "").strip()
        if not url:
            logger.warning(
                "MOVATE_NOTIFIER=teams but MOVATE_NOTIFIER_TEAMS_WEBHOOK_URL is unset "
                "— HITL notifications disabled (no-op)."
            )
            return NoOpNotifier()
        from movate.core.notifier_sinks import TeamsNotifier  # noqa: PLC0415

        return TeamsNotifier(webhook_url=url)
    if selector == "webhook":
        url = os.environ.get("MOVATE_NOTIFIER_WEBHOOK_URL", "").strip()
        if not url:
            logger.warning(
                "MOVATE_NOTIFIER=webhook but MOVATE_NOTIFIER_WEBHOOK_URL is unset "
                "— HITL notifications disabled (no-op)."
            )
            return NoOpNotifier()
        from movate.core.notifier_sinks import GenericWebhookNotifier  # noqa: PLC0415

        secret = os.environ.get("MOVATE_NOTIFIER_WEBHOOK_SECRET", "").strip() or None
        return GenericWebhookNotifier(webhook_url=url, secret=secret)
    logger.warning(
        "unknown MOVATE_NOTIFIER=%r — HITL notifications disabled (no-op). "
        "Expected: teams | webhook | none.",
        selector,
    )
    return NoOpNotifier()


# Cached singleton — resolved once per process from env, so the execution-edge
# hooks call get_notifier() without any constructor threading. A dict holder
# (not a module global) matches the no-`global`-statement house style.
_STATE: dict[str, NotifierProvider | None] = {"notifier": None}


def get_notifier() -> NotifierProvider:
    """Return the process-wide notifier, building it from env on first use."""
    notifier = _STATE["notifier"]
    if notifier is None:
        notifier = build_notifier()
        _STATE["notifier"] = notifier
    return notifier


def reset_notifier_cache() -> None:
    """Test seam: drop the cached notifier so the next resolve re-reads env."""
    _STATE["notifier"] = None


async def notify_human_pause_safe(pause: HumanPause) -> None:
    """Deliver a HITL pause notification, fire-and-forget. NEVER raises.

    The pause record is already persisted (the source of truth); this is
    best-effort courtesy delivery. Any transport error / misconfig is logged and
    swallowed so a notifier problem can never fail the paused run (CLAUDE.md
    rules 10/11). No-op by default (NoOpNotifier) until ``MOVATE_NOTIFIER`` is set.
    """
    try:
        await get_notifier().notify_human_pause(pause)
    except Exception:  # pragma: no cover - defensive; sinks already never raise
        logger.warning(
            "notifier_delivery_failed run_id=%s node=%s — run is paused but "
            "approvers may not have been notified.",
            pause.run_id,
            pause.node_id,
            exc_info=True,
        )


__all__ = [
    "HumanPause",
    "NoOpNotifier",
    "NotifierProvider",
    "build_notifier",
    "get_notifier",
    "notify_human_pause_safe",
    "reset_notifier_cache",
]
