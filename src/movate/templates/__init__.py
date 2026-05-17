"""Agent templates registry.

Each entry in :data:`TEMPLATES` maps a friendly name (used by ``movate init -t
<name>``) to the directory under ``src/movate/templates/`` that holds the
scaffold files. Adding a new template = drop a directory and add one line.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent

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
SKILL_TEMPLATES: dict[str, str] = {
    "default": "skill_init",
    "web-search": "skill_web_search",
    "lint-runner": "skill_lint_runner",
    "kb-lookup": "skill_kb_lookup",
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
    # Natural-language → SQL with dialect awareness + safety warnings
    # on destructive ops. Generates queries; does not execute them.
    "sql-writer": "roles/sql-writer",
    # Draft replies for emails/Slack/tickets with explicit tone +
    # intent control. No-placeholder rule (always ready to send).
    "reply-drafter": "roles/reply-drafter",
    # Classify text into a caller-provided taxonomy with confidence +
    # reasoning. Strict label-from-taxonomy enforcement.
    "text-classifier": "roles/text-classifier",
    # Summarize long-form text into summary + key_points +
    # action_items + open_questions. Audience-aware.
    "document-summarizer": "roles/document-summarizer",
}


def list_templates() -> list[str]:
    """Sorted list of (shape) template names."""
    return sorted(TEMPLATES.keys())


def list_roles() -> list[str]:
    """Sorted list of role-template names. Companion to
    :func:`list_templates`; see :data:`ROLE_TEMPLATES` for the
    distinction between shape templates and role templates."""
    return sorted(ROLE_TEMPLATES.keys())


def get_template_path(name: str) -> Path:
    """Resolve a friendly template name to its packaged directory.

    Looks up ``name`` in :data:`ROLE_TEMPLATES` first, falling back to
    :data:`TEMPLATES`. This lets ``mdk add my-agent --template
    support-triage`` resolve to the role template AND ``mdk init
    my-agent --template faq`` still resolve to the shape template,
    without users needing to know which registry the name lives in.

    Raises ``ValueError`` with both available lists if ``name`` is
    unknown.
    """
    if name in ROLE_TEMPLATES:
        rel = ROLE_TEMPLATES[name]
    elif name in TEMPLATES:
        rel = TEMPLATES[name]
    else:
        roles = ", ".join(list_roles())
        shapes = ", ".join(list_templates())
        raise ValueError(
            f"unknown template {name!r}; available roles: {roles}; available shapes: {shapes}"
        )
    path = TEMPLATES_DIR / rel
    if not path.is_dir():  # pragma: no cover — install-time invariant
        raise FileNotFoundError(f"template {name!r} dir missing on disk: {path}")
    return path


__all__ = [
    "ROLE_TEMPLATES",
    "TEMPLATES",
    "TEMPLATES_DIR",
    "get_template_path",
    "list_roles",
    "list_templates",
]
