"""Typed-name agent picker fallback for the Chainlit playground.

The action-button picker is fragile with many agents, so a user can also just
*type* an agent's name to select it. ``match_agent_name`` is the pure resolver
behind that fallback — exact (case-insensitive) match, else a UNIQUE
case-insensitive prefix, else ``None`` (ambiguous/no match → normal chat turn).
"""

from __future__ import annotations

import pytest

# The playground app imports chainlit at module scope; skip when absent.
pytest.importorskip("chainlit")

from movate.playground.app import match_agent_name

_AGENTS = ["demo-faq", "demo-support", "code-reviewer", "Lead-Qualifier"]


@pytest.mark.unit
def test_exact_match_case_insensitive() -> None:
    assert match_agent_name("code-reviewer", _AGENTS) == "code-reviewer"
    assert match_agent_name("CODE-REVIEWER", _AGENTS) == "code-reviewer"
    assert match_agent_name("  lead-qualifier  ", _AGENTS) == "Lead-Qualifier"  # trims + case


@pytest.mark.unit
def test_unique_prefix_matches() -> None:
    assert match_agent_name("code", _AGENTS) == "code-reviewer"
    assert match_agent_name("lead", _AGENTS) == "Lead-Qualifier"


@pytest.mark.unit
def test_ambiguous_prefix_returns_none() -> None:
    # "demo" prefixes BOTH demo-faq and demo-support → ambiguous → None.
    assert match_agent_name("demo", _AGENTS) is None


@pytest.mark.unit
def test_no_match_and_empties_return_none() -> None:
    assert match_agent_name("nonexistent-agent", _AGENTS) is None
    assert match_agent_name("", _AGENTS) is None
    assert match_agent_name("demo-faq", []) is None
    # An exact match still wins even when it's also a prefix of another.
    assert match_agent_name("demo-faq", _AGENTS) == "demo-faq"
