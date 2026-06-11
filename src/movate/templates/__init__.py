"""Agent templates registry.

Each entry in :data:`TEMPLATES` maps a friendly name (used by ``movate init -t
<name>``) to the directory under ``src/movate/templates/`` that holds the
scaffold files. Adding a new template = drop a directory and add one line.

ADR 028 — discoverability metadata. Each template directory may carry a
``template.yaml`` file with human-readable metadata (title, description,
tags, shape, recommended_for) consumed by ``mdk templates list/show`` and
the interactive ``mdk init`` picker. The metadata lives next to the template
files so the source of truth is the template itself — no central registry
to drift. :func:`load_template_info` reads it; :func:`list_template_infos`
returns metadata for every registered template. The original :func:`list_templates`
return type is preserved (rule 5 — backward compat).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

TEMPLATES_DIR = Path(__file__).parent


# ADR 028 — template categories. Agent templates scaffold a single agent;
# workflow templates scaffold a multi-step workflow (workflow.yaml + agent
# subdirs). Skill templates are reached via ``mdk skills scaffold`` and stay
# in their own registry (SKILL_TEMPLATES).
TemplateShape = Literal[
    "agent",  # single-agent scaffolds (default, faq, classifier, …)
    "workflow",  # multi-step workflow scaffolds (workflow-starter)
    "skill",  # reserved for skill templates (today via SKILL_TEMPLATES)
]


@dataclass(frozen=True)
class TemplateInfo:
    """Discoverability metadata for one template (ADR 028).

    Loaded from a sibling ``template.yaml`` file inside the template
    directory. Keep the field set small + stable — every field surfaces in
    the ``mdk templates list`` and ``mdk templates show`` views, and any
    addition needs a corresponding update to the JSON shape (rule 5).

    Attributes:
        name: Friendly name the operator types (matches TEMPLATES /
            WORKFLOW_TEMPLATES key).
        title: One-line headline ("Grounded Q&A with citations").
        description: One-sentence elaboration; longer than ``title`` but
            still readable in a table row (~80 chars).
        tags: Lowercased capability tags ("rag", "tool-use", "workflow",
            "starter") — used by the interactive picker for grouping +
            future search.
        shape: Which template family this is — agent / workflow / skill.
        recommended_for: One-sentence "when to reach for this" hint
            shown in ``show`` and on prompt.
        directory: Resolved absolute path to the template dir on disk.
            Excluded from the JSON view; callers serialize on their side.
    """

    name: str
    title: str
    description: str
    tags: tuple[str, ...]
    shape: TemplateShape
    recommended_for: str
    directory: Path = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly dict for ``mdk templates list --json``.

        Stable contract — public ``--json`` surface (rule 5). The
        directory path is included as a relative-to-TEMPLATES_DIR
        string so output is reproducible across installs.
        """
        try:
            relative_dir = self.directory.relative_to(TEMPLATES_DIR).as_posix()
        except ValueError:
            relative_dir = self.directory.as_posix()
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "shape": self.shape,
            "recommended_for": self.recommended_for,
            "directory": relative_dir,
        }


TEMPLATES: dict[str, str] = {
    # Minimal echo agent — string-in, string-out. Default.
    "default": "agent_init",
    # FAQ agent: question → answer + confidence; ships with a judge.yaml.example.
    "faq": "faq_agent",
    # Summarizer agent: text + max_words → summary + word_count; ships with a judge.yaml.example.
    "summarizer": "summarizer_agent",
    # Classifier agent: text + label list → chosen label (exact-match-friendly).
    "classifier": "classifier_agent",
    # Chatbot: single message → single reply. Designed for `movate chat` with
    # conversation memory (each turn sees prior turns via the REPL's history).
    "chatbot": "chatbot_agent",
    # Structured-field extractor: free-form text → strict typed fields.
    # Demonstrates strict output-schema enforcement for LLM extraction.
    "extractor": "extractor_agent",
    # --- Role-based templates (post-v1.0) ---
    # Each one is a complete, runnable agent for a high-frequency
    # enterprise use case. Datasets exercise the output schema so the
    # template passes `mdk eval` out of the box.
    #
    # RAG Q&A: grounded answer with citation indices.
    "rag-qa": "rag_qa_agent",
    # Support ticket triager: category + priority + routing + draft reply.
    "ticket-triager": "ticket_triager_agent",
    # Email responder: tone-aware drafted reply with needs-review flag.
    "email-responder": "email_responder_agent",
    # Text-to-SQL: schema-grounded query + plain-English explanation.
    "sql-writer": "sql_writer_agent",
    # Code reviewer: unified-diff → structured findings (file/line/severity).
    "code-reviewer": "code_reviewer_agent",
    # Lead qualifier: BANT scoring + next-best-action + objections.
    "lead-qualifier": "lead_qualifier_agent",
    # Meeting summarizer: transcript → decisions + action items + blockers.
    "meeting-summarizer": "meeting_summarizer_agent",
    # Resume screener: JD + resume → match score + strengths + gaps.
    "resume-screener": "resume_screener_agent",
    # Compliance checker: text + ruleset → violations + rewordings.
    "compliance-checker": "compliance_checker_agent",
    # Research agent: topic + sources → executive summary with citations.
    "research-agent": "research_agent",
    # HR policy agent: employee questions → grounded policy answer +
    # citations + escalation flag. Multi-format KB (MD, HTML, PDF,
    # DOCX, images). Best demo of the full KB ingest pipeline.
    "hr-policy": "hr_policy_agent",
    # --- Skill-using demo templates ---
    # calc-agent: arithmetic agent wired to a Python calculator skill.
    # Ships with the skill impl — demonstrates Python skill kind.
    "calc-agent": "calc_agent",
    # lookup-agent: user-lookup agent wired to an HTTP skill calling
    # JSONPlaceholder (public, no API key). Swap the URL to use a real
    # CRM — demonstrates HTTP skill kind.
    "lookup-agent": "lookup_agent",
}

# Skill templates live alongside agent templates but are reached via
# ``mdk skills scaffold`` rather than ``mdk init``. Each entry maps a
# skill name to its packaged directory; the `default` key is the
# fallback when an agent declares a skill that has no curated
# template (auto-scaffold copies the default echo skill).
#
# The named templates ship REAL impls — operators can run them
# directly via ``mdk skills run <name>`` after scaffolding without
# replacing any code. Demo flow uses:
#
# * web-search — DuckDuckGo HTML scrape (rag-qa)
# * lint-runner — subprocess `ruff check` (code-reviewer)
# * kb-lookup — mock-data corpus search (ticket-triager)
# * kb-vector-lookup — semantic search via OpenAI embeddings +
#   ``mdk kb ingest <agent>`` pipeline (rag-qa, post-0.8.2.13)
SKILL_TEMPLATES: dict[str, str] = {
    "default": "skill_init",
    "web-search": "skill_web_search",
    "lint-runner": "skill_lint_runner",
    "kb-lookup": "skill_kb_lookup",
    "kb-vector-lookup": "skill_kb_vector_lookup",
}


# Role templates — opinionated personas surfaced by ``mdk add``. These
# differ from TEMPLATES (above) in two ways:
#
#   1. **Scope:** TEMPLATES are generic shapes (faq, summarizer,
#      classifier). ROLE_TEMPLATES are specific personas built on top
#      of those shapes (support-triage, sql-writer, etc.). The Mova
#      iO catalog surfaces roles in the wizard's "Choose a template"
#      dropdown — each one is a polished, ready-to-deploy agent.
#
#   2. **Discovery:** ``mdk add <name> --template <role>`` looks up
#      this registry first; ``mdk init <name> --template <name>``
#      stays on the legacy TEMPLATES registry. Both forms work for
#      back-compat; the role flavor is the recommended path going
#      forward.
#
# Each role's directory lives under ``roles/<name>/`` and ships:
#   * agent.yaml      — fully-populated spec with marketplace metadata
#   * prompt.md       — role-specific prompt with rubrics + examples
#   * evals/dataset.jsonl — 2-3 sample cases for day-1 measurement
#   * ROLE.md         — when-to-use + customization guidance
ROLE_TEMPLATES: dict[str, str] = {
    # Read incoming tickets, assign priority + team + category, decide
    # escalation, write a 1-line summary. Strict enum output.
    "support-triage": "roles/support-triage",
    # Draft replies for emails/Slack/tickets with explicit tone +
    # intent control. No-placeholder rule (always ready to send).
    "reply-drafter": "roles/reply-drafter",
    # Classify text into a caller-provided taxonomy with confidence +
    # reasoning. Strict label-from-taxonomy enforcement.
    "text-classifier": "roles/text-classifier",
    # Summarize long-form text into summary + key_points +
    # action_items + open_questions. Audience-aware.
    "document-summarizer": "roles/document-summarizer",
    # NOTE: sql-writer was moved from roles/sql-writer to sql_writer_agent
    # (the TEMPLATES registry) so it ships external schema files and
    # a richer context bundle. get_template_path('sql-writer') now
    # resolves via TEMPLATES. roles/sql-writer is kept on disk for
    # reference but removed from ROLE_TEMPLATES to avoid the lookup
    # collision (ROLE_TEMPLATES is checked first in get_template_path).
}


# Agent-pattern templates (ADR 038) — surfaced by ``mdk init --pattern <name>``
# and ``mdk patterns list``. These are GOVERNED realizations of the functional
# agent patterns: each bakes in bounds (budgets, fan-out caps, max-iterations /
# turn caps), eval-gates, and full tracing, composed from the EXISTING workflow
# primitives (ADR 017) — never a new engine.
#
# Two shapes:
#   * "chatbot" is a single AGENT (INPUT → AGENT → OUTPUT) — scaffolds an agent
#     dir (agent.yaml + prompt + schemas + dataset + judge), same on-disk shape
#     as the TEMPLATES above (so ``mdk run``/``eval`` work on it directly).
#   * the other four are WORKFLOW bundles (workflow.yaml + state.json + nested
#     agents/ + workflow-level dataset/judge) — scaffold a workflow dir.
#
# Each entry: (relative dir, is_workflow, one-line description, topology).
PATTERN_TEMPLATES: dict[str, tuple[str, bool, str, str]] = {
    "chatbot": (
        "pattern_chatbot",
        False,
        "Single governed agent answering one turn under an enforced output contract.",
        "INPUT → AGENT → OUTPUT",
    ),
    "task-oriented": (
        "pattern_task_oriented",
        True,
        "Bounded supervisor fan-out: a planner decomposes into a fixed, capped task set, then collects.",  # noqa: E501
        "SUPERVISOR → task-a → task-b → collector",
    ),
    "goal-oriented": (
        "pattern_goal_oriented",
        True,
        "Bounded supervisor loop: a worker iterates while a JUDGE/GATE checks the goal, exiting on satisfaction or a max-iterations cap.",  # noqa: E501
        "SUPERVISOR → (worker → JUDGE/GATE) x2 → done",
    ),
    "monitor": (
        "pattern_monitor",
        True,
        "Observe a signal, VALIDATE/GATE it against a threshold, and on breach fire an allowlisted action (stub). Schedule/trigger-friendly.",  # noqa: E501
        "observer → VALIDATE/GATE → {action | no-op}",
    ),
    "simulation": (
        "pattern_simulation",
        True,
        "Bounded multi-agent simulation: a FIXED roster of two participants under a supervisor, hard-capped turns, terminating JUDGE. NOT a swarm.",  # noqa: E501
        "SUPERVISOR → (A → B → JUDGE) x2 → done",
    ),
    "expense-approval": (
        "pattern_expense_approval",
        True,
        "Tiered expense approval (runtime: temporal). A DECISION node routes on amount (no LLM), each tier pauses durably at a HUMAN gate that routes its own approve/reject decision (ADR 099) — zero LLM classifiers, all tiers converging on ONE shared ERP-post/finalize/rejected tail (ADR 094/098/099).",  # noqa: E501
        "DECISION(amount) → [HUMAN routes approve|reject] → shared ERP-post|rejected → finalize",
    ),
    "itsm-request": (
        "pattern_itsm_request",
        True,
        "ITSM service-request fulfilment over a parameterized catalog (runtime: temporal). A DECISION node routes the portal's auto_approved flag (no LLM); needs-approval services pause at ONE HUMAN gate routing its own approve/reject decision (ADR 099); fulfilment is a TOOL node calling the workflow-local sim-provision python skill (ADR 097) — auto + approve paths converge on the shared provision→notify tail (ADR 094/097/098/099).",  # noqa: E501
        "DECISION(auto_approved) → [HUMAN routes approve|reject] → shared TOOL provision → notify | rejected",  # noqa: E501
    ),
    "purchase-order": (
        "pattern_purchase_order",
        True,
        "Tiered purchase-order approval with a SEQUENTIAL APPROVAL CHAIN (runtime: temporal). A DECISION node tiers on the amount (no LLM): ≤500 auto-creates the PO; everything else pauses at the manager HUMAN gate, and a second DECISION chains >5000 orders into the director gate — both must approve. PO creation is a TOOL node calling the workflow-local sim-create-po python skill (ADR 097); all approve paths converge on the shared create-po→notify tail (ADR 094/097/098/099).",  # noqa: E501
        "DECISION(amount) → [HUMAN manager] → DECISION(escalate) → [HUMAN director] → shared TOOL create-po → notify | rejected",  # noqa: E501
    ),
    "approval-timeout": (
        "pattern_approval_timeout",
        True,
        "Approval with DURABLE TIMEOUT + escalation (runtime: temporal) — the live shape of ADR 062 D4. The primary HUMAN gate carries a 90s durable deadline whose expiry escalates to a second HUMAN gate (the alternate approver); ITS expiry fails safe to rejected — silence can never fulfil. Fulfilment is a TOOL node calling the workflow-local sim-fulfill python skill (ADR 097); both approve paths converge on the shared fulfill→notify tail (ADR 062/097/098/099).",  # noqa: E501
        "[HUMAN primary ⏲90s] → on_timeout → [HUMAN escalation ⏲90s] → shared TOOL fulfill → notify | rejected",  # noqa: E501
    ),
    "human-escalation": (
        "pattern_human_escalation",
        True,
        "Low-confidence human escalation with RESUME-WITH-FEEDBACK (runtime: temporal). A triage agent drafts an answer + a calibrated numeric confidence; a DECISION node routes confidence ≥ 0.8 straight to finalize (no second LLM judging the first), everything else pauses at the review HUMAN gate (output_contract [decision, feedback]) — the reviewer's feedback merges into state and the finalize agent incorporates it (ADR 094/098/099).",  # noqa: E501
        "triage → DECISION(confidence) → {finalize | [HUMAN review + feedback]} → finalize | rejected",  # noqa: E501
    ),
    "pii-detection": (
        "pattern_pii_detection",
        True,
        "PII document scanning + masking (runtime: temporal). A deterministic redact-pii TOOL node (anchored regexes, no LLM) masks emails/SSNs/phones to [EMAIL]/[SSN]/[PHONE]; a DECISION node routes pii_found to a quarantine or clean-store TOOL (auditable dlp ledger rows), converging on ONE notify agent that sees only the redacted text (ADR 094/097/098).",  # noqa: E501
        "TOOL redact → DECISION(pii_found) → {TOOL quarantine | TOOL store-clean} → notify",
    ),
    "data-privacy": (
        "pattern_data_privacy",
        True,
        "Classify → policy-route → AUDITED storage (runtime: temporal). A calibrated enum-pinned classify agent feeds a DECISION node routing public/internal/regulated; regulated documents are masked by the redact-pii TOOL first; ALL paths converge on one sim-audit-store TOOL recording the classification-keyed audit row no path can skip (ADR 094/097/098).",  # noqa: E501
        "classify → DECISION(classification) → {TOOL redact → TOOL audit-store | TOOL audit-store} → summary",  # noqa: E501
    ),
    "content-publishing": (
        "pattern_content_publishing",
        True,
        "Multi-stage content review chain + HITL final gate (runtime: temporal). Calibrated compliance-review and brand-review agents each feed a DECISION node failing safe to a shared rejected agent; content passing BOTH still publishes nothing until a HUMAN gate routes its own approve/reject decision (ADR 099) into the sim-publish TOOL's auditable cms ledger row (ADR 094/097/098/099).",  # noqa: E501
        "compliance → DECISION → brand → DECISION → [HUMAN routes approve|reject] → TOOL publish → notify | rejected",  # noqa: E501
    ),
    # NOTE: the react / map-reduce / supervisor workflow patterns were reverted —
    # they were pushed directly to main substantially incomplete (sub-agents
    # missing canonical YAML schemas + judge examples; templates missing root
    # GOVERNANCE.md + judge), which broke the required lint-and-test check and
    # jammed the merge queue. Re-land them complete via a proper PR.
}


def list_patterns() -> list[str]:
    """Sorted list of agent-pattern names (``mdk init --pattern <name>``)."""
    return sorted(PATTERN_TEMPLATES.keys())


def get_pattern_path(name: str) -> Path:
    """Resolve a pattern name to its packaged directory.

    Raises ``ValueError`` with the available list if ``name`` is unknown.
    """
    entry = PATTERN_TEMPLATES.get(name)
    if entry is None:
        raise ValueError(
            f"unknown pattern {name!r}; available patterns: {', '.join(list_patterns())}"
        )
    path = TEMPLATES_DIR / entry[0]
    if not path.is_dir():  # pragma: no cover — install-time invariant
        raise FileNotFoundError(f"pattern {name!r} dir missing on disk: {path}")
    return path


def pattern_is_workflow(name: str) -> bool:
    """True if the named pattern scaffolds a WORKFLOW bundle (vs a single agent)."""
    entry = PATTERN_TEMPLATES.get(name)
    if entry is None:
        raise ValueError(f"unknown pattern {name!r}")
    return entry[1]


# ADR 028 — Workflow templates. Separate from TEMPLATES because they
# scaffold a multi-step workflow (workflow.yaml + agent subdirs), not a
# single agent. They're surfaced via ``mdk templates list/show`` for
# discoverability; the on-disk dir is the canonical "this is how you
# build a workflow" reference. The existing ``mdk init -t <name>`` agent
# scaffold path is unchanged — workflow names live in this registry, not
# TEMPLATES, so the agent-template invariants (every TEMPLATES entry has
# an ``agent.yaml`` at its root) are preserved.
WORKFLOW_TEMPLATES: dict[str, str] = {
    # Two-step "draft → review" pipeline demonstrating agent-to-agent
    # state flow, a state_schema, eval dataset, and the canonical
    # workflow.yaml structure (ADR 017 IR). The starter referenced by
    # ADR 028 D2.
    "workflow-starter": "workflow_starter",
    # Self-improving reflection loop (ADR 056 D4): produce → JUDGE →
    # (revise → produce)* bounded by max_iterations, with the judge's
    # feedback threaded into each revision. The canonical "judge node +
    # bounded loop" reference.
    "reflective-agent": "reflective_agent",
}


def list_templates() -> list[str]:
    """Sorted list of (shape) template names.

    [bold]Stable contract[/bold] — return type and content preserved
    across ADR 028 (rule 5). Workflow templates surface via
    :func:`list_workflow_templates` / :func:`list_template_infos`, not
    here, so existing callers (``mdk init -t`` validation, the legacy
    ``--list`` view) keep their original behavior.
    """
    return sorted(TEMPLATES.keys())


def list_workflow_templates() -> list[str]:
    """Sorted list of workflow-template names (ADR 028).

    Workflow templates live in their own registry (:data:`WORKFLOW_TEMPLATES`)
    so they don't disturb the agent-template surface. ``mdk templates``
    surfaces both via :func:`list_template_infos`.
    """
    return sorted(WORKFLOW_TEMPLATES.keys())


def list_roles() -> list[str]:
    """Sorted list of role-template names. Companion to
    :func:`list_templates`; see :data:`ROLE_TEMPLATES` for the
    distinction between shape templates and role templates."""
    return sorted(ROLE_TEMPLATES.keys())


def get_template_path(name: str) -> Path:
    """Resolve a friendly template name to its packaged directory.

    Looks up ``name`` in :data:`ROLE_TEMPLATES` first, falling back to
    :data:`TEMPLATES`, then :data:`WORKFLOW_TEMPLATES`. This lets
    ``mdk add my-agent --template support-triage`` resolve to the role
    template AND ``mdk init my-agent --template faq`` still resolve to
    the shape template, without users needing to know which registry
    the name lives in. Workflow lookup is additive (ADR 028): if name
    matches an agent template it resolves there first; new workflow-only
    names route through the workflow registry.

    Raises ``ValueError`` with both available lists if ``name`` is
    unknown.
    """
    if name in ROLE_TEMPLATES:
        rel = ROLE_TEMPLATES[name]
    elif name in TEMPLATES:
        rel = TEMPLATES[name]
    elif name in WORKFLOW_TEMPLATES:
        rel = WORKFLOW_TEMPLATES[name]
    else:
        roles = ", ".join(list_roles())
        shapes = ", ".join(list_templates())
        workflows = ", ".join(list_workflow_templates())
        raise ValueError(
            f"unknown template {name!r}; available roles: {roles}; "
            f"available shapes: {shapes}; available workflows: {workflows}"
        )
    path = TEMPLATES_DIR / rel
    if not path.is_dir():  # pragma: no cover — install-time invariant
        raise FileNotFoundError(f"template {name!r} dir missing on disk: {path}")
    return path


class TemplateInfoLoadError(Exception):
    """Raised when a template's ``template.yaml`` is missing or invalid.

    ADR 028 makes ``template.yaml`` a hard requirement for every shipped
    template — discoverability metadata is part of the template's
    contract, not a nice-to-have. Failing loud here keeps the ``mdk
    templates`` surface honest (no silent empty rows when a maintainer
    forgets the file).
    """


def load_template_info(name: str) -> TemplateInfo:
    """Load discoverability metadata for one template (ADR 028).

    Reads ``<template_dir>/template.yaml`` and validates the required
    fields. The shape is inferred from which registry holds the name
    (agent vs. workflow vs. role) so the YAML doesn't have to repeat
    something the registry already knows; if the YAML declares ``shape``
    explicitly it must match.

    Raises :class:`TemplateInfoLoadError` on missing file, parse error,
    or required-field omission. Unknown names raise ``ValueError`` via
    :func:`get_template_path`.
    """
    path = get_template_path(name)
    meta_file = path / "template.yaml"
    if not meta_file.is_file():
        raise TemplateInfoLoadError(f"template {name!r}: missing template.yaml at {meta_file}")
    try:
        raw = yaml.safe_load(meta_file.read_text()) or {}
    except yaml.YAMLError as exc:
        raise TemplateInfoLoadError(
            f"template {name!r}: invalid YAML in template.yaml: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise TemplateInfoLoadError(
            f"template {name!r}: template.yaml must be a mapping, got {type(raw).__name__}"
        )

    # Required fields. Keep the error message specific so a maintainer
    # adding a new template sees exactly which key they missed.
    for field_name in ("title", "description", "recommended_for"):
        if not raw.get(field_name):
            raise TemplateInfoLoadError(
                f"template {name!r}: template.yaml missing required field {field_name!r}"
            )

    inferred_shape: TemplateShape
    if name in WORKFLOW_TEMPLATES:
        inferred_shape = "workflow"
    elif name in TEMPLATES or name in ROLE_TEMPLATES:
        inferred_shape = "agent"
    else:  # pragma: no cover — get_template_path would have raised
        inferred_shape = "agent"
    declared_shape = raw.get("shape")
    if declared_shape is not None and declared_shape != inferred_shape:
        raise TemplateInfoLoadError(
            f"template {name!r}: template.yaml shape={declared_shape!r} "
            f"contradicts registry-inferred shape={inferred_shape!r}"
        )

    raw_tags = raw.get("tags") or []
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        raise TemplateInfoLoadError(
            f"template {name!r}: template.yaml `tags` must be a list of strings"
        )
    tags = tuple(t.strip().lower() for t in raw_tags if t.strip())

    return TemplateInfo(
        name=name,
        title=str(raw["title"]).strip(),
        description=str(raw["description"]).strip(),
        tags=tags,
        shape=inferred_shape,
        recommended_for=str(raw["recommended_for"]).strip(),
        directory=path,
    )


def list_template_infos(*, include_workflows: bool = True) -> list[TemplateInfo]:
    """Return :class:`TemplateInfo` for every registered template (ADR 028).

    Iterates over both :data:`TEMPLATES` and :data:`WORKFLOW_TEMPLATES`
    (set ``include_workflows=False`` to keep the legacy agent-only view).
    Templates missing a ``template.yaml`` are SKIPPED rather than failing
    — the caller (``mdk templates``) prefers a partial view to a hard
    crash when one template lags behind. Individual lookups via
    :func:`load_template_info` still raise loudly.

    The result is sorted by name for deterministic CLI output.
    """
    names: list[str] = list(TEMPLATES.keys())
    if include_workflows:
        names.extend(WORKFLOW_TEMPLATES.keys())
    infos: list[TemplateInfo] = []
    for name in sorted(set(names)):
        try:
            infos.append(load_template_info(name))
        except TemplateInfoLoadError:
            # Skip rather than crash — see docstring. Surfacing partial
            # rows is the whole point of the discoverability command.
            continue
    return infos


__all__ = [
    "PATTERN_TEMPLATES",
    "ROLE_TEMPLATES",
    "TEMPLATES",
    "TEMPLATES_DIR",
    "WORKFLOW_TEMPLATES",
    "TemplateInfo",
    "TemplateInfoLoadError",
    "TemplateShape",
    "get_pattern_path",
    "get_template_path",
    "list_patterns",
    "list_roles",
    "list_template_infos",
    "list_templates",
    "list_workflow_templates",
    "load_template_info",
    "pattern_is_workflow",
]
