"""Numbered picker UX for ``mdk add`` — supports multi-select + zero-arg invocation.

Three behavior changes in this PR:

1. **Multi-pick**: the role-catalog picker accepts ``1 3 5`` or
   ``1,3,5`` (in addition to a single number). Each picked template
   is added in one ``mdk add`` batch invocation so the operator sees
   one combined summary + one post-add menu at the end.

2. **`mdk add` with no args**: when stdin + stdout are both ttys,
   the no-args path now invokes the picker instead of erroring.
   Scripted callers (no tty) still see the "template name required"
   diagnostic + exit 2.

3. **`mdk add --list` / `mdk add list` invoke the picker on TTY**:
   the legacy "just render the table" behavior is preserved for
   scripted use (no tty) — interactive operators get the
   render-plus-picker flow they almost always wanted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli import add_cmd
from movate.cli.add_cmd import _parse_pick_input
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# _parse_pick_input — pure-function unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParsePickInput:
    def test_single_number(self) -> None:
        assert _parse_pick_input("3", max_index=9) == [3]

    def test_space_separated(self) -> None:
        assert _parse_pick_input("1 3 5", max_index=9) == [1, 3, 5]

    def test_comma_separated(self) -> None:
        assert _parse_pick_input("1,3,5", max_index=9) == [1, 3, 5]

    def test_mixed_commas_and_spaces(self) -> None:
        assert _parse_pick_input("1, 3 5", max_index=9) == [1, 3, 5]

    def test_skip_sentinel_returns_none(self) -> None:
        assert _parse_pick_input("s", max_index=9) is None
        assert _parse_pick_input("S", max_index=9) is None
        assert _parse_pick_input("", max_index=9) is None
        assert _parse_pick_input("   ", max_index=9) is None

    def test_out_of_range_returns_none(self) -> None:
        """Anything > max_index or < 1 means re-prompt — we don't
        silently clamp because that would surprise the operator
        (they typed something specific)."""
        assert _parse_pick_input("99", max_index=9) is None
        assert _parse_pick_input("0", max_index=9) is None
        assert _parse_pick_input("1 99", max_index=9) is None
        # Negative ints are caught by the isdigit() filter (the `-`
        # makes the token non-numeric) so they re-prompt too.
        assert _parse_pick_input("-1", max_index=9) is None

    def test_non_numeric_returns_none(self) -> None:
        assert _parse_pick_input("foo", max_index=9) is None
        assert _parse_pick_input("1 foo 3", max_index=9) is None
        assert _parse_pick_input("1.5", max_index=9) is None

    def test_deduplicates_preserving_first_occurrence_order(self) -> None:
        """Picking the same number twice shouldn't add the template
        twice (it'd fail with "already exists" on the second add).
        Preserve order so the post-add summary lists templates in
        the order the operator typed them."""
        assert _parse_pick_input("1 1 3 5 3", max_index=9) == [1, 3, 5]


# ---------------------------------------------------------------------------
# `_pick_and_add_role_agent` — shells out with batch args when multi-pick
# ---------------------------------------------------------------------------


def _stub_picker_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pick_input: str,
    addable: list[str],
) -> list[list[str]]:
    """Patch the four dependencies of ``_pick_and_add_role_agent`` so
    the test stays hermetic + deterministic."""
    invocations: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        invocations.append(list(cmd))

        class _Stub:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Stub()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("movate.cli.add_cmd._installed_templates", set)
    monkeypatch.setattr(
        "movate.cli.add_cmd._render_role_catalog_numbered",
        lambda installed: addable,
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: pick_input)
    return invocations


@pytest.mark.unit
def test_picker_single_pick_runs_single_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations = _stub_picker_deps(
        monkeypatch, pick_input="2", addable=["faq", "rag-qa", "code-reviewer"]
    )
    add_cmd._pick_and_add_role_agent("mdk")

    assert len(invocations) == 1
    assert invocations[0] == ["mdk", "add", "rag-qa"]


@pytest.mark.unit
def test_picker_multi_pick_runs_one_batch_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-pick uses ``mdk add <a> <b> <c>`` — single subprocess,
    single combined summary panel at the end. Avoids the noise of
    three separate add invocations."""
    invocations = _stub_picker_deps(
        monkeypatch,
        pick_input="1 3",
        addable=["faq", "rag-qa", "code-reviewer"],
    )
    add_cmd._pick_and_add_role_agent("mdk")

    assert len(invocations) == 1
    assert invocations[0] == ["mdk", "add", "faq", "code-reviewer"]


@pytest.mark.unit
def test_picker_comma_separated_input_also_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations = _stub_picker_deps(
        monkeypatch,
        pick_input="1,3",
        addable=["faq", "rag-qa", "code-reviewer"],
    )
    add_cmd._pick_and_add_role_agent("mdk")

    assert invocations[0] == ["mdk", "add", "faq", "code-reviewer"]


@pytest.mark.unit
def test_picker_skip_input_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations = _stub_picker_deps(
        monkeypatch, pick_input="s", addable=["faq", "rag-qa"]
    )
    add_cmd._pick_and_add_role_agent("mdk")

    assert invocations == []


@pytest.mark.unit
def test_picker_short_circuits_on_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipes (CI / grep / etc.) skip the prompt entirely. The
    catalog renders so the script still sees the same output, but
    no Prompt.ask fires."""
    invocations: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        invocations.append(list(cmd))
        class _Stub:
            returncode = 0

        return _Stub()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("movate.cli.add_cmd._installed_templates", set)
    monkeypatch.setattr(
        "movate.cli.add_cmd._render_role_catalog_numbered",
        lambda installed: ["faq"],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("Prompt.ask should not be called when stdin is not a tty")

    monkeypatch.setattr("rich.prompt.Prompt.ask", boom)

    add_cmd._pick_and_add_role_agent("mdk")

    assert invocations == [], "non-tty must not shell out to `mdk add`"


# ---------------------------------------------------------------------------
# CLI surface: `mdk add` and `mdk add list` invoke the picker
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mdk_add_with_no_args_in_pipe_still_errors_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CliRunner has no tty — pipe behavior. Bare `mdk add` must
    still emit the diagnostic + exit 2 so CI scripts catch the
    operator error."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    # Scaffold a project so the loader doesn't error before we hit
    # the no-args branch.
    runner.invoke(
        app,
        ["init", "proj", "--skip-snapshot", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    monkeypatch.chdir(tmp_path / "proj")

    result = runner.invoke(app, ["add"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "template name required" in combined


@pytest.mark.unit
def test_mdk_add_list_in_pipe_renders_table_without_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When stdin/stdout are NOT ttys (CliRunner default = pipes),
    ``mdk add --list`` keeps the legacy "just render the table"
    behavior so scripts piping the output keep working."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        app,
        ["init", "proj", "--skip-snapshot", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    monkeypatch.chdir(tmp_path / "proj")

    result = runner.invoke(app, ["add", "--list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    # Table heading shows; no "Pick" prompt should appear.
    assert "Role-based templates" in combined
    # `Pick (one or more numbers …)` would surface only in tty mode.
    assert "(one or more numbers" not in combined


@pytest.mark.unit
def test_mdk_add_list_subcommand_alias_matches_flag_in_pipe_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk add list` — operators consistently type the subcommand
    form. Must produce the same table as `--list`."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        app,
        ["init", "proj", "--skip-snapshot", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    monkeypatch.chdir(tmp_path / "proj")

    result_flag = runner.invoke(app, ["add", "--list"], env={"COLUMNS": "200"})
    result_sub = runner.invoke(app, ["add", "list"], env={"COLUMNS": "200"})

    assert result_flag.exit_code == 0
    assert result_sub.exit_code == 0
    # Both should mention the role-based templates header.
    assert "Role-based templates" in (result_flag.stdout + result_flag.stderr)
    assert "Role-based templates" in (result_sub.stdout + result_sub.stderr)
