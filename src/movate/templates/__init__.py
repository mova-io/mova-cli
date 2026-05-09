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
