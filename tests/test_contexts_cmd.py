"""Unit tests for ``mdk contexts list`` and ``mdk contexts show``.

Covers both sub-commands of the ``contexts_app`` Typer sub-app
registered on the main CLI as ``mdk contexts``.  All tests use real
temporary directory trees (no mocking) because the commands are pure
filesystem reads.

Test topology summary
---------------------
* ``test_list_*``   — ``mdk contexts list [--project P] [--agent A] [--verbose]``
* ``test_show_*``   — ``mdk contexts show <name> [--project P]``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

# ---------------------------------------------------------------------------
# Helpers — common tree-building utilities
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so assertions are terminal-width-agnostic."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _write_context(project: Path, name: str, body: str) -> Path:
    """Write ``<project>/contexts/<name>.md`` and return its path."""
    ctx_dir = project / "contexts"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    path = ctx_dir / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _write_agent(
    project: Path,
    agent_name: str,
    *,
    contexts: list[str] | None = None,
) -> Path:
    """Create ``<project>/agents/<agent_name>/agent.yaml`` and return the agent dir."""
    agent_dir = project / "agents" / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    ctx_block = ""
    if contexts:
        ctx_block = "  contexts:\n" + "".join(f"    - {c}\n" for c in contexts)
    yaml_body = (
        "spec:\n"
        f"  name: {agent_name}\n"
        f"{ctx_block}"
    )
    (agent_dir / "agent.yaml").write_text(yaml_body, encoding="utf-8")
    return agent_dir


def _write_agent_local_context(agent_dir: Path, filename: str, body: str) -> Path:
    """Write ``<agent_dir>/contexts/<filename>`` and return its path."""
    ctx_dir = agent_dir / "contexts"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    path = ctx_dir / filename
    path.write_text(body, encoding="utf-8")
    return path


def _invoke_list(project: Path, *extra_args: str) -> object:
    return runner.invoke(
        app,
        ["contexts", "list", "--project", str(project), *extra_args],
        env={"COLUMNS": "200"},
    )


def _invoke_show(name: str, project: Path) -> object:
    return runner.invoke(
        app,
        ["contexts", "show", name, "--project", str(project)],
        env={"COLUMNS": "200"},
    )


# ===========================================================================
# mdk contexts list
# ===========================================================================


# ---------------------------------------------------------------------------
# Empty project
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_empty_project_exits_zero(tmp_path: Path) -> None:
    """A project with no contexts/ dir and no agents/ dir exits 0 and
    prints a 'no context files found' hint so the operator knows what
    to do next."""
    result = _invoke_list(tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    assert "no context files found" in combined


@pytest.mark.unit
def test_list_empty_project_hint_mentions_mkdir(tmp_path: Path) -> None:
    """The hint in the empty-project case should be actionable — mention
    how to create the directory so the operator doesn't have to guess."""
    result = _invoke_list(tmp_path)
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    # The command suggests creating contexts/ and adding a .md file.
    assert "contexts" in combined


# ---------------------------------------------------------------------------
# Project-level context, unreferenced
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_unreferenced_context_appears_in_table(tmp_path: Path) -> None:
    """A project context not referenced by any agent shows up in the
    table with its name visible."""
    _write_context(tmp_path, "policy", "# Policy\nDo no harm.")
    result = _invoke_list(tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "policy" in _strip_ansi(result.stdout)


@pytest.mark.unit
def test_list_unreferenced_context_shows_warning(tmp_path: Path) -> None:
    """An unreferenced project context triggers the 'not referenced by
    any agent' warning so operators notice unused files."""
    _write_context(tmp_path, "policy", "# Policy\nDo no harm.")
    # No agents/ dir → nothing references policy.
    result = _invoke_list(tmp_path)
    assert result.exit_code == 0
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    # The warning uses the phrase "not referenced" (or similar).
    assert "not referenced" in combined or "unreferenced" in combined.lower()


# ---------------------------------------------------------------------------
# Project-level context, referenced by one agent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_referenced_context_shows_agent_name(tmp_path: Path) -> None:
    """When an agent's agent.yaml declares a context, the 'used by agents'
    column in the table shows that agent's directory name."""
    _write_context(tmp_path, "policy", "# Policy\nDo no harm.")
    _write_agent(tmp_path, "rag-qa", contexts=["policy"])

    result = _invoke_list(tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = _strip_ansi(result.stdout)
    assert "policy" in out
    assert "rag-qa" in out


@pytest.mark.unit
def test_list_referenced_context_no_unreferenced_warning(tmp_path: Path) -> None:
    """A context referenced by at least one agent must NOT trigger the
    unreferenced-context warning."""
    _write_context(tmp_path, "policy", "# Policy\nDo no harm.")
    _write_agent(tmp_path, "rag-qa", contexts=["policy"])

    result = _invoke_list(tmp_path)
    assert result.exit_code == 0
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    assert "not referenced" not in combined
    assert "unreferenced" not in combined.lower()


# ---------------------------------------------------------------------------
# --verbose adds preview snippet
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_verbose_shows_preview_snippet(tmp_path: Path) -> None:
    """``--verbose`` adds a preview column containing the first characters
    of each context file, ending in '…' when the body is long."""
    long_body = "A" * 300 + " end"
    _write_context(tmp_path, "policy", long_body)

    result = _invoke_list(tmp_path, "--verbose")
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = _strip_ansi(result.stdout)
    # The preview ellipsis should appear since the body exceeds 200 chars.
    assert "…" in out


@pytest.mark.unit
def test_list_verbose_short_body_no_ellipsis(tmp_path: Path) -> None:
    """A short body (under the preview threshold) must not be truncated —
    no trailing '…' should appear."""
    _write_context(tmp_path, "tiny", "short content")

    result = _invoke_list(tmp_path, "--verbose")
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout)
    assert "short content" in out


# ---------------------------------------------------------------------------
# --agent filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_agent_filter_known_agent_exits_zero(tmp_path: Path) -> None:
    """``--agent <known>`` exits 0 and shows only that agent's contexts."""
    _write_agent(tmp_path, "rag-qa", contexts=["policy"])
    _write_context(tmp_path, "policy", "Policy body.")
    agent_dir = tmp_path / "agents" / "rag-qa"
    _write_agent_local_context(agent_dir, "faq.md", "FAQ content")

    result = _invoke_list(tmp_path, "--agent", "rag-qa")
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


@pytest.mark.unit
def test_list_agent_filter_unknown_agent_exits_2(tmp_path: Path) -> None:
    """``--agent <unknown>`` exits with code 2 and emits an error message
    naming the unknown agent."""
    _write_agent(tmp_path, "rag-qa")

    result = _invoke_list(tmp_path, "--agent", "no-such-agent")
    assert result.exit_code == 2
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    assert "no-such-agent" in combined


@pytest.mark.unit
def test_list_agent_filter_no_agents_dir_exits_2(tmp_path: Path) -> None:
    """``--agent <name>`` with no agents/ dir at all also exits 2."""
    # No agents dir created.
    result = _invoke_list(tmp_path, "--agent", "ghost")
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Agent-local contexts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_agent_local_contexts_appear_in_output(tmp_path: Path) -> None:
    """Agent-local contexts (``agents/<n>/contexts/<file>.md``) are shown
    in a separate table section with the agent name in the title."""
    agent_dir = _write_agent(tmp_path, "ticket-triager")
    _write_agent_local_context(agent_dir, "faq.md", "Frequently asked questions.")

    result = _invoke_list(tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = _strip_ansi(result.stdout)
    assert "faq.md" in out
    assert "ticket-triager" in out


@pytest.mark.unit
def test_list_agent_local_contexts_section_title_mentions_agent(tmp_path: Path) -> None:
    """The per-agent section header must name the agent so operators can
    distinguish sections when multiple agents have local contexts."""
    agent_dir = _write_agent(tmp_path, "code-reviewer")
    _write_agent_local_context(agent_dir, "rubric.md", "Review rubric.")

    result = _invoke_list(tmp_path)
    out = _strip_ansi(result.stdout)
    # The table title contains the agent directory name.
    assert "code-reviewer" in out


# ---------------------------------------------------------------------------
# Invalid project path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_invalid_project_path_exits_2(tmp_path: Path) -> None:
    """A project path that doesn't exist on disk exits with code 2 and
    prints a 'project path not found' error."""
    nonexistent = tmp_path / "no-such-project"
    result = _invoke_list(nonexistent)
    assert result.exit_code == 2
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    assert "project path not found" in combined or "not found" in combined


# ---------------------------------------------------------------------------
# Multiple agents, multiple contexts, cross-references
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_multiple_agents_multiple_contexts(tmp_path: Path) -> None:
    """With multiple agents referencing overlapping contexts, each row in
    the project-contexts table lists all referencing agents."""
    _write_context(tmp_path, "policy", "# Policy")
    _write_context(tmp_path, "tone", "# Tone guidelines")
    _write_agent(tmp_path, "agent-a", contexts=["policy", "tone"])
    _write_agent(tmp_path, "agent-b", contexts=["policy"])

    result = _invoke_list(tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = _strip_ansi(result.stdout)
    # Both context names appear.
    assert "policy" in out
    assert "tone" in out
    # Both agent names appear.
    assert "agent-a" in out
    assert "agent-b" in out


@pytest.mark.unit
def test_list_shared_context_lists_all_referencing_agents(tmp_path: Path) -> None:
    """A context referenced by two agents shows both agent names in the
    'used by agents' column (order may vary)."""
    _write_context(tmp_path, "policy", "# Policy")
    _write_agent(tmp_path, "agent-a", contexts=["policy"])
    _write_agent(tmp_path, "agent-b", contexts=["policy"])

    result = _invoke_list(tmp_path)
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout)
    assert "agent-a" in out
    assert "agent-b" in out


@pytest.mark.unit
def test_list_unreferenced_context_named_in_warning(tmp_path: Path) -> None:
    """The warning for unreferenced contexts includes the context name so
    operators can take direct action."""
    _write_context(tmp_path, "orphan-policy", "# Orphan")
    _write_agent(tmp_path, "agent-x")  # no contexts referenced

    result = _invoke_list(tmp_path)
    assert result.exit_code == 0
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    assert "orphan-policy" in combined


@pytest.mark.unit
def test_list_both_project_and_agent_local_contexts(tmp_path: Path) -> None:
    """A project with both project-level and agent-local contexts renders
    two separate table sections."""
    _write_context(tmp_path, "global-policy", "Global rules.")
    agent_dir = _write_agent(tmp_path, "rag-qa", contexts=["global-policy"])
    _write_agent_local_context(agent_dir, "local-faq.md", "Local FAQ for rag-qa.")

    result = _invoke_list(tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = _strip_ansi(result.stdout)
    assert "global-policy" in out
    assert "local-faq.md" in out


# ===========================================================================
# mdk contexts show
# ===========================================================================


# ---------------------------------------------------------------------------
# Project-level context — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_known_project_context_exits_zero(tmp_path: Path) -> None:
    """``mdk contexts show <name>`` exits 0 when the context exists."""
    _write_context(tmp_path, "policy", "# Policy\nDo no harm.")

    result = _invoke_show("policy", tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


@pytest.mark.unit
def test_show_known_project_context_prints_body(tmp_path: Path) -> None:
    """The command prints the full content of the context file."""
    body = "# My Policy\n\nBe nice to robots."
    _write_context(tmp_path, "policy", body)

    result = _invoke_show("policy", tmp_path)
    assert result.exit_code == 0
    assert "Be nice to robots." in result.stdout


@pytest.mark.unit
def test_show_project_context_filename_in_header(tmp_path: Path) -> None:
    """The filename (with extension) appears in the Rule header so the
    operator knows which file was loaded."""
    _write_context(tmp_path, "policy", "Content here.")

    result = _invoke_show("policy", tmp_path)
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout)
    assert "policy.md" in out


# ---------------------------------------------------------------------------
# Agent-local context via agent-name/file-name form
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_agent_local_context_exits_zero(tmp_path: Path) -> None:
    """``mdk contexts show rag-qa/faq`` exits 0 for an existing agent-local context."""
    agent_dir = _write_agent(tmp_path, "rag-qa")
    _write_agent_local_context(agent_dir, "faq.md", "Agent FAQ content.")

    result = _invoke_show("rag-qa/faq", tmp_path)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


@pytest.mark.unit
def test_show_agent_local_context_prints_body(tmp_path: Path) -> None:
    """Body of the agent-local context is printed to stdout."""
    agent_dir = _write_agent(tmp_path, "rag-qa")
    _write_agent_local_context(agent_dir, "faq.md", "This is the local FAQ.")

    result = _invoke_show("rag-qa/faq", tmp_path)
    assert result.exit_code == 0
    assert "This is the local FAQ." in result.stdout


@pytest.mark.unit
def test_show_agent_local_context_header_includes_agent_and_filename(tmp_path: Path) -> None:
    """The Rule header for an agent-local context uses the ``agent/filename``
    form so it's clear which agent the file belongs to."""
    agent_dir = _write_agent(tmp_path, "rag-qa")
    _write_agent_local_context(agent_dir, "rubric.md", "Review rubric body.")

    result = _invoke_show("rag-qa/rubric", tmp_path)
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout)
    # The header should contain both the agent name and the filename.
    assert "rag-qa" in out
    assert "rubric.md" in out


# ---------------------------------------------------------------------------
# Unknown context → exit 2
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_unknown_project_context_exits_2(tmp_path: Path) -> None:
    """An unrecognised context name exits with code 2."""
    result = _invoke_show("ghost", tmp_path)
    assert result.exit_code == 2


@pytest.mark.unit
def test_show_unknown_context_error_contains_not_found(tmp_path: Path) -> None:
    """The error message for an unknown context includes 'not found' so
    the operator understands the problem immediately."""
    result = _invoke_show("missing-context", tmp_path)
    assert result.exit_code == 2
    combined = _strip_ansi(result.stdout + (result.stderr or ""))
    assert "not found" in combined


@pytest.mark.unit
def test_show_unknown_agent_local_context_exits_2(tmp_path: Path) -> None:
    """``mdk contexts show rag-qa/ghost`` exits 2 when the file doesn't exist."""
    _write_agent(tmp_path, "rag-qa")
    result = _invoke_show("rag-qa/ghost", tmp_path)
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Extension fallback — stem-only lookup tries .md, .markdown, .txt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_no_extension_finds_md_file(tmp_path: Path) -> None:
    """Passing a stem without extension resolves to the ``.md`` file."""
    _write_context(tmp_path, "style-guide", "Style rules.")

    # Invoke without extension — the command should add .md automatically.
    result = _invoke_show("style-guide", tmp_path)
    assert result.exit_code == 0
    assert "Style rules." in result.stdout


@pytest.mark.unit
def test_show_no_extension_finds_markdown_file(tmp_path: Path) -> None:
    """A ``.markdown`` file is found when the stem is passed without extension."""
    ctx_dir = tmp_path / "contexts"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "guide.markdown").write_text("Markdown extension content.", encoding="utf-8")

    result = _invoke_show("guide", tmp_path)
    assert result.exit_code == 0
    assert "Markdown extension content." in result.stdout


@pytest.mark.unit
def test_show_no_extension_finds_txt_file(tmp_path: Path) -> None:
    """A ``.txt`` context file is found when the stem is passed without extension."""
    ctx_dir = tmp_path / "contexts"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "notes.txt").write_text("Plain text context.", encoding="utf-8")

    result = _invoke_show("notes", tmp_path)
    assert result.exit_code == 0
    assert "Plain text context." in result.stdout


@pytest.mark.unit
def test_show_explicit_extension_also_works(tmp_path: Path) -> None:
    """Passing ``name.md`` (with extension) resolves to the file when the
    exact-name path matches (the first lookup with ext='' hits it)."""
    ctx_dir = tmp_path / "contexts"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "policy.md").write_text("Policy with explicit ext.", encoding="utf-8")

    result = _invoke_show("policy.md", tmp_path)
    assert result.exit_code == 0
    assert "Policy with explicit ext." in result.stdout


# ---------------------------------------------------------------------------
# show — line/size footer is present
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_footer_mentions_lines_and_size(tmp_path: Path) -> None:
    """The footer printed after the body mentions line count and byte size
    so operators can quickly assess the context's footprint."""
    body = "Line one.\nLine two.\nLine three.\n"
    _write_context(tmp_path, "policy", body)

    result = _invoke_show("policy", tmp_path)
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout)
    # Footer should contain both "lines" and a size indicator (B/KB/MB).
    assert "lines" in out
    # At minimum, a byte-count indicator appears.
    assert any(unit in out for unit in (" B", "KB", "MB"))
