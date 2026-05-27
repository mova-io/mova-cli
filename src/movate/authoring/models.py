"""Typed data shapes for the authoring action catalog (ADR 025, D1-D4).

These are the LLM-agnostic value objects every authoring action and the
plan→apply→verify driver exchange. They carry **no behavior** beyond
plain pydantic validation — the catalog actions (``movate.authoring.actions``)
and the driver (``movate.authoring.driver``) own all the logic.

The shapes are deliberately small + JSON-serializable so the three future
surfaces (conversational ``mdk dev``, ``AGENTS.md`` CLI, the MCP server) can
share them verbatim without re-deriving a wire format.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SideEffect(StrEnum):
    """A side effect an action can have when applied (ADR 025 D1).

    Drives the confirmation policy in D2: an action whose ``side_effects``
    include :attr:`NETWORK` or :attr:`COST` (or that is irreversible /
    destructive) always requires explicit confirmation; a purely
    ``FILESYSTEM`` additive+reversible action may auto-apply in fast mode.
    """

    FILESYSTEM = "filesystem"
    """Writes / edits / removes project files on disk."""

    NETWORK = "network"
    """Makes outbound network calls (e.g. crawl a docs site, embed via an API)."""

    COST = "cost"
    """Incurs monetary cost (token spend, embedding spend)."""


class ActionPlan(BaseModel):
    """The dry-run output of :meth:`AuthoringAction.plan` — **no writes**.

    A plan is everything a human (or PR3's planner) needs to decide whether
    to apply: a human-readable summary, a unified diff of the intended file
    changes, the declared side effects, an optional cost estimate, and the
    confirmation gate.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="The action name this plan was produced by.")
    summary: str = Field(..., description="One-line, human/LLM-facing description of the change.")
    diff: str = Field(
        default="",
        description=(
            "A unified-diff (or summary) preview of the intended file changes. "
            "Empty when the change is not a textual file edit (e.g. a network ingest)."
        ),
    )
    side_effects: list[SideEffect] = Field(
        default_factory=list,
        description="The side effects applying this plan will have.",
    )
    reversible: bool = Field(
        default=True,
        description="Whether applying can be cleanly undone via a checkpoint revert.",
    )
    requires_confirmation: bool = Field(
        default=False,
        description=(
            "When True the driver refuses to auto-apply (D2): the change is "
            "cost-incurring, networked, irreversible, or destructive and needs "
            "an explicit yes. Additive+reversible+free plans set this False."
        ),
    )
    estimated_cost_usd: float | None = Field(
        default=None,
        description="A rough monetary estimate for cost-bearing actions; None if free/unknown.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific structured detail (e.g. resolved paths, counts).",
    )


class ActionResult(BaseModel):
    """The outcome of :meth:`AuthoringAction.apply`.

    Carries what changed (``changed_paths``) so the driver/verify loop and
    the audit history can report it, plus an action-specific ``details`` bag.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="The action name that produced this result.")
    summary: str = Field(..., description="Human-readable summary of what was applied.")
    changed_paths: list[str] = Field(
        default_factory=list,
        description="Project-relative paths created or modified by the apply.",
    )
    cost_usd: float = Field(
        default=0.0, description="Actual monetary cost incurred (0.0 for free actions)."
    )
    details: dict[str, Any] = Field(default_factory=dict)


class VerifyReport(BaseModel):
    """Result of the post-apply verify-and-self-correct loop (ADR 025 D3).

    The driver runs ``validate`` → ``run --mock`` → (optional) ``eval`` after
    an apply. On a ``validate`` failure it REVERTS (D4) and reports the
    structured error so a caller (PR3's planner) can re-plan.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(..., description="True when every executed verify step passed.")
    validated: bool = Field(default=False, description="Whether `load_agent`/validate passed.")
    mock_ran: bool = Field(default=False, description="Whether `run --mock` was executed.")
    mock_ok: bool = Field(default=False, description="Whether the mock run returned success.")
    reverted: bool = Field(
        default=False,
        description="True when verify failed and the apply was rolled back to the checkpoint.",
    )
    error: str | None = Field(
        default=None,
        description="The structured/friendly error that caused the failure, if any.",
    )
    steps: list[str] = Field(
        default_factory=list, description="Ordered log of verify steps attempted."
    )


class ActionLogEntry(BaseModel):
    """One entry in the authoring action log (ADR 025 D4 — `history`).

    Records what action ran, against which agent, the checkpoint taken
    *before* it (the undo target), and the result summary.
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    agent: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    checkpoint_hash: str = Field(
        ..., description="Full snapshot hash captured BEFORE the apply (the undo target)."
    )
    summary: str = ""
    changed_paths: list[str] = Field(default_factory=list)
    created_at: str = ""
    undone: bool = Field(default=False, description="True once this entry has been undone.")
