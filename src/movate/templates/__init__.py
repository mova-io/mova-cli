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


def list_templates() -> list[str]:
    """Sorted list of template names."""
    return sorted(TEMPLATES.keys())


def get_template_path(name: str) -> Path:
    """Resolve a friendly template name to its packaged directory.

    Raises ``ValueError`` with the available list if ``name`` is unknown.
    """
    if name not in TEMPLATES:
        raise ValueError(f"unknown template {name!r}; available: {', '.join(list_templates())}")
    path = TEMPLATES_DIR / TEMPLATES[name]
    if not path.is_dir():  # pragma: no cover — install-time invariant
        raise FileNotFoundError(f"template {name!r} dir missing on disk: {path}")
    return path


__all__ = ["TEMPLATES", "TEMPLATES_DIR", "get_template_path", "list_templates"]
