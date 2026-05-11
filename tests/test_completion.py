"""Unit tests for the shell-completion helpers.

Completion runs on every TAB the user presses, so:
* It must be sync (no event loop).
* It must never raise — bad return only spawns an empty list.
* It must be cheap (sub-50ms feel) and not require auth/network.

These tests exercise the helpers in :mod:`movate.cli._completion`
directly. We don't try to drive Click's completion subprocess
machinery — that's the shell's problem; we just guarantee the inputs
we feed Click are correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.cli._completion import (
    _complete_agent_name_impl as complete_agent_name,
)
from movate.cli._completion import (
    _complete_agent_path_impl as complete_agent_path,
)


@pytest.fixture
def agents_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A faux ``./agents/`` with three real-looking agent dirs and
    one decoy (no ``agent.yaml``) that completion must ignore."""
    root = tmp_path / "agents"
    root.mkdir()
    for name in ("faq-agent", "case-reasoner", "returns-router"):
        d = root / name
        d.mkdir()
        (d / "agent.yaml").write_text("api_version: movate/v1\nkind: Agent\nname: " + name + "\n")
    # Decoy: looks like an agent dir but no agent.yaml — must be skipped.
    (root / "scratch").mkdir()
    monkeypatch.setenv("MOVATE_AGENTS_PATH", str(root))
    return root


@pytest.mark.unit
def test_complete_agent_name_empty_prefix_returns_all(agents_root: Path) -> None:
    """Empty prefix (typical first-TAB) returns every agent name in
    sorted order. The decoy ``scratch`` dir must NOT appear — it
    has no ``agent.yaml``."""
    out = complete_agent_name("")
    assert out == ["case-reasoner", "faq-agent", "returns-router"]


@pytest.mark.unit
def test_complete_agent_name_filters_by_prefix(agents_root: Path) -> None:
    """Typing ``fa<TAB>`` narrows to names starting with ``fa``."""
    assert complete_agent_name("fa") == ["faq-agent"]


@pytest.mark.unit
def test_complete_agent_name_missing_root_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the configured agents root doesn't exist, completion must
    return ``[]`` rather than raise — a missing dir is just an empty
    catalog from the user's perspective."""
    monkeypatch.setenv("MOVATE_AGENTS_PATH", str(tmp_path / "does-not-exist"))
    assert complete_agent_name("") == []


@pytest.mark.unit
def test_complete_agent_path_returns_both_name_and_relative(agents_root: Path) -> None:
    """``run`` and friends accept either ``faq-agent`` or
    ``agents/faq-agent`` as the path. Completion offers both."""
    out = complete_agent_path("")
    # Sorted by dir-walk order, name-then-path interleaved. Just
    # spot-check that both shapes are present for at least one agent.
    assert "faq-agent" in out
    full = next((c for c in out if c.endswith("/faq-agent")), None)
    assert full is not None, f"no agents/faq-agent form in {out!r}"


@pytest.mark.unit
def test_complete_agent_path_prefix_filter(agents_root: Path) -> None:
    """Prefix filter narrows the bare-name suggestions."""
    out = complete_agent_path("case")
    assert "case-reasoner" in out
    # 'faq-agent' starts with 'fa', not 'case' — must be excluded.
    assert not any(c == "faq-agent" for c in out)


@pytest.mark.unit
def test_completion_swallows_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Completion runs on every keystroke; an exception would spew a
    traceback into the user's prompt. Force the root resolver to
    explode and verify both helpers degrade to an empty list."""
    import movate.cli._completion as compl  # noqa: PLC0415

    def boom() -> Path:
        raise RuntimeError("simulated environment failure")

    monkeypatch.setattr(compl, "_agents_root", boom)
    assert complete_agent_name("") == []
    assert complete_agent_path("") == []
