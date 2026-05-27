"""ADR 025 (PR2) — the ``AGENTS.md`` scaffold.

Two ``AGENTS.md`` files make external coding agents (Claude Code,
Cursor, …) mdk-fluent:

1. **Project-root ``AGENTS.md``** — written by ``mdk init --project``.
   Teaches a coding agent how to evolve THIS mdk project: the canonical
   agent layout, the authoring-command catalog, the post-edit feedback
   loop, and the guardrails.
2. **Repo-root ``AGENTS.md``** — for contributors/agents working ON
   movate-cli itself. A short pointer to ``CLAUDE.md`` (the source of
   truth) + the verify gate + the CalVer/worktree notes.

Layers:

* the scaffold writes the file with the expected sections,
* a **command-existence guard** parses every ``mdk <…>`` command out of
  the scaffolded doc and asserts it resolves in the Typer app — so the
  doc can never reference a command that doesn't exist,
* the repo-root file exists and points at ``CLAUDE.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Project-root AGENTS.md — written by `mdk init --project`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_project_writes_agents_md(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + result.stderr

    agents_md = tmp_path / "my-proj" / "AGENTS.md"
    assert agents_md.is_file()
    text = agents_md.read_text()

    # Project name is substituted into the header.
    assert "my-proj" in text


@pytest.mark.unit
def test_init_project_in_place_writes_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-place bootstrap (`mdk init --project`, no name) also writes it."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--project"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "AGENTS.md").is_file()


@pytest.mark.unit
def test_project_agents_md_has_expected_sections(tmp_path: Path) -> None:
    """The scaffolded doc carries the four sections the ADR mandates:
    layout, command catalog, feedback loop, guardrails."""
    runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    text = (tmp_path / "my-proj" / "AGENTS.md").read_text()

    # Canonical layout (from #127 / docs/agent-layout.md).
    assert "Canonical agent layout" in text
    assert "agent.yaml" in text
    assert "prompt.md" in text
    assert "dataset.jsonl" in text
    assert "judge.yaml.example" in text
    assert "schema/" in text
    assert "input.yaml" in text
    assert "output.yaml" in text
    # Where contexts / skills / kb live.
    assert "contexts/" in text
    assert "skills/" in text
    assert "kb/" in text

    # Authoring command catalog.
    assert "command catalog" in text.lower()

    # Feedback loop (validate → run --mock → eval).
    assert "feedback loop" in text.lower()

    # Guardrails.
    assert "Guardrails" in text


@pytest.mark.unit
def test_project_agents_md_states_guardrails(tmp_path: Path) -> None:
    """The guardrails the ADR calls out are present: no ~/.movate/,
    use --mock, run from project root, prefer commands, both schema
    forms load."""
    runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    text = (tmp_path / "my-proj" / "AGENTS.md").read_text()

    assert "~/.movate/" in text
    assert "--mock" in text
    assert "project root" in text.lower()
    # Prefer the commands over hand-editing prompt.md is the exception.
    assert "hand-edit" in text.lower() or "hand-editing" in text.lower()
    # Both inline + schema/*.yaml forms load.
    assert "inline" in text.lower()


@pytest.mark.unit
def test_init_project_panel_mentions_agents_md(tmp_path: Path) -> None:
    """The success Panel lists AGENTS.md so operators discover it."""
    result = runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    assert "AGENTS.md" in result.stdout


# ---------------------------------------------------------------------------
# 2. Command-existence guard — the doc can't reference a missing command
# ---------------------------------------------------------------------------


def _all_command_paths(app_, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Every valid command path in the Typer app, as token tuples.

    e.g. ``("add",)``, ``("contexts", "create")``, ``("kb", "ingest")``.
    Group prefixes themselves are included so a bare group reference
    (rare) still resolves.
    """
    paths: set[tuple[str, ...]] = set()
    for cmd in app_.registered_commands:
        name = cmd.name or (cmd.callback.__name__.replace("_", "-") if cmd.callback else None)
        if name:
            paths.add((*prefix, name))
    for grp in app_.registered_groups:
        gname = grp.name
        if not gname:
            continue
        paths.add((*prefix, gname))
        if grp.typer_instance is not None:
            paths |= _all_command_paths(grp.typer_instance, (*prefix, gname))
    return paths


# `mdk <command...>` references inside fenced blocks / inline code /
# tables. We capture the run of bare lowercase-hyphen tokens directly
# after `mdk ` and stop at the first token that is NOT a plain
# subcommand word (a flag `--x`, a `<placeholder>`, a quote, etc).
_MDK_INVOCATION = re.compile(r"\bmdk\s+([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)*)")


def _referenced_commands(doc: str) -> set[tuple[str, ...]]:
    """Pull ``mdk <subcommand...>`` paths out of a doc as token tuples."""
    found: set[tuple[str, ...]] = set()
    for match in _MDK_INVOCATION.finditer(doc):
        tokens = tuple(match.group(1).split())
        if tokens:
            found.add(tokens)
    return found


def _longest_valid_prefix(
    tokens: tuple[str, ...], valid: set[tuple[str, ...]]
) -> tuple[str, ...] | None:
    """Longest leading slice of ``tokens`` that is a real command path.

    A doc line like ``mdk run <agent> --mock`` yields tokens
    ``("run",)`` (the regex already stops at ``<agent>``); but a line
    like ``mdk add list`` (where ``list`` is doc prose, not a
    subcommand) must still match on ``("add",)``. So we accept the
    longest leading slice that resolves.
    """
    for n in range(len(tokens), 0, -1):
        if tokens[:n] in valid:
            return tokens[:n]
    return None


@pytest.mark.unit
def test_scaffolded_agents_md_commands_all_resolve(tmp_path: Path) -> None:
    """Every `mdk <command>` referenced in the scaffolded project-root
    AGENTS.md resolves to a real command in the Typer app.

    This is the guard that keeps the doc honest: if someone documents a
    command (or renames an existing one) without it existing in the
    CLI, this test fails.
    """
    runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    text = (tmp_path / "my-proj" / "AGENTS.md").read_text()

    valid = _all_command_paths(app)
    referenced = _referenced_commands(text)

    # Sanity: the catalog the ADR mandates is actually documented.
    assert ("add",) in {_longest_valid_prefix(t, valid) for t in referenced}

    unresolved: list[tuple[str, ...]] = []
    for tokens in referenced:
        if _longest_valid_prefix(tokens, valid) is None:
            unresolved.append(tokens)

    assert not unresolved, (
        "AGENTS.md references commands that do not exist in the CLI: "
        + ", ".join("mdk " + " ".join(t) for t in sorted(unresolved))
    )


@pytest.mark.unit
def test_catalog_commands_are_documented(tmp_path: Path) -> None:
    """The specific authoring commands the ADR's catalog promises are
    each present in the scaffolded doc — and each resolves."""
    runner.invoke(app, ["init", "my-proj", "--project", "--target", str(tmp_path)])
    text = (tmp_path / "my-proj" / "AGENTS.md").read_text()
    valid = _all_command_paths(app)

    expected = [
        ("add",),
        ("contexts", "create"),
        ("kb", "ingest"),
        ("skills", "scaffold"),
        ("validate",),
        ("run",),
        ("eval",),
        ("dev",),
    ]
    for path in expected:
        # The command must be a real command…
        assert path in valid, f"mdk {' '.join(path)} is not a real command"
        # …and it must be documented in the scaffolded AGENTS.md.
        assert "mdk " + " ".join(path) in text, (
            f"mdk {' '.join(path)} is missing from the scaffolded AGENTS.md"
        )


@pytest.mark.unit
def test_guard_catches_a_fake_command() -> None:
    """Meta-test: the guard's parser + resolver actually flags a
    nonexistent command (so a regression in the guard itself shows up)."""
    valid = _all_command_paths(app)
    fake_doc = "Try `mdk frobnicate the-widget` to do the thing."
    referenced = _referenced_commands(fake_doc)
    unresolved = [t for t in referenced if _longest_valid_prefix(t, valid) is None]
    assert unresolved == [("frobnicate", "the-widget")]


# ---------------------------------------------------------------------------
# 3. Repo-root AGENTS.md — points at CLAUDE.md
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_repo_root_agents_md_exists_and_points_at_claude_md() -> None:
    agents_md = _REPO_ROOT / "AGENTS.md"
    assert agents_md.is_file(), "repo-root AGENTS.md is missing"
    text = agents_md.read_text()
    # Points contributors/agents at the authoritative rules.
    assert "CLAUDE.md" in text
    # Carries the verify gate so an agent knows how to self-check.
    assert "ruff check" in text
    assert "mypy src" in text
    assert 'pytest -m "not smoke"' in text
    assert "check_licenses.py" in text
    # CalVer / version-line note.
    assert "CalVer" in text
    # Worktree-isolation note.
    assert "worktree" in text.lower()
