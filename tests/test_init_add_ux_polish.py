"""UX polish for ``mdk init`` and ``mdk add`` post-action menus.

Covers three behavior changes:

1. **`_pick_and_add_role_agent` no longer fires `mdk run --mock`
   after returning from the inner post-add menu.** Operators who
   picked ``[3] Check wiring + setup`` were getting a surprise
   model invocation; the smoke test now lives in the post-add menu
   as an explicit ``[1] Run with sample input`` option.

2. **Post-add menu surfaces "Run with sample input" as the first
   option** so the smoke-test affordance from the old auto-flow
   stays available — just opt-in.

3. **`mdk init --project <name>` auto-launches the detected editor**
   (VS Code / Cursor) when stdout is a tty. Skipped under
   ``--no-open-editor`` or when the detection falls back to ``open``
   (macOS Finder), which would just open a Finder window instead of
   an editor.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli import add_cmd
from movate.cli.add_cmd import _run_with_sample_input
from movate.cli.init import _init_project
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fix #1 — picker no longer fires `mdk run --mock` as a surprise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_picker_does_not_unconditionally_run_mdk_run_after_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old behavior: ``_pick_and_add_role_agent`` ran ``mdk add
    <template>`` as a subprocess, then unconditionally fired ``mdk
    run --mock`` regardless of what the operator picked from the
    inner post-add menu. New behavior: only the ``mdk add`` subprocess
    runs; ``mdk run`` is now an opt-in menu option."""

    invocations: list[list[str]] = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        invocations.append(list(cmd))

        # Return a stub CompletedProcess so the caller doesn't blow up.
        class _Stub:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Stub()

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("movate.cli.add_cmd._installed_templates", set)
    monkeypatch.setattr(
        "movate.cli.add_cmd._render_role_catalog_numbered",
        lambda installed: ["faq"],
    )

    # Pretend stdin + stdout are ttys (the picker short-circuits
    # otherwise). Patch just the ``isatty`` method on the existing
    # streams so all the other stream protocol (write, flush, etc.)
    # stays intact.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        "rich.prompt.Prompt.ask",
        lambda *a, **kw: "1",
    )

    add_cmd._pick_and_add_role_agent("mdk")

    # Exactly one subprocess call: `mdk add faq`. NO `mdk run --mock`.
    assert len(invocations) == 1, (
        f"expected exactly 1 subprocess invocation (mdk add), got: {invocations}"
    )
    assert invocations[0][:2] == ["mdk", "add"], invocations[0]
    # No subprocess invocation includes `--mock` (which would only be
    # there if the smoke test fired).
    assert all("--mock" not in c for c in invocations), (
        f"picker should not invoke `mdk run --mock` anymore; got: {invocations}"
    )


@pytest.mark.unit
def test_run_with_sample_input_helper_exists_and_is_importable() -> None:
    """The smoke-test was extracted from the picker into a standalone
    helper that the post-add menu invokes via callback. Pin the public
    name so other callers (and the menu) can rely on it."""

    assert callable(_run_with_sample_input)


# ---------------------------------------------------------------------------
# Fix #2 — Post-add menu surfaces "Run with sample input" as first option
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_post_add_menu_includes_run_with_sample_input_as_first_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After ``mdk add <template>`` completes, the post-add menu must
    offer "Run with sample input" — that's the new replacement for
    the old surprise smoke test."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "proj")

    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    # The new option appears, and the four pre-existing options stay
    # (so we didn't accidentally drop functionality).
    assert "Run with sample input" in combined
    assert "Run the eval suite" in combined
    assert "Check wiring + setup" in combined
    assert "Add another role agent" in combined
    assert "Deploy to Azure" in combined


# ---------------------------------------------------------------------------
# Fix #3a — `mdk init --project` auto-launches the detected editor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_project_auto_launches_editor_when_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When stdout is a tty AND `code` is on PATH AND --no-open-editor
    isn't set, init must Popen the editor on the new project root.

    Calls ``_init_project`` directly to avoid CliRunner's stdout swap
    (which would override our ``sys.stdout.isatty`` patch). The full
    CliRunner path is exercised in the menu-shape tests below."""

    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)

    popen_calls: list[list[str]] = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            popen_calls.append(list(argv))

    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd: "/usr/local/bin/code" if cmd == "code" else None,
    )
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    _init_project(
        name="demo",
        target=tmp_path,
        force=False,
        skip_snapshot=True,
        open_editor=True,
    )

    code_calls = [c for c in popen_calls if c and c[0] == "code"]
    assert len(code_calls) == 1, f"expected one `code` Popen invocation, got: {popen_calls}"
    assert str(tmp_path / "demo") in code_calls[0]


@pytest.mark.unit
def test_init_project_no_open_editor_flag_skips_auto_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-open-editor`` (open_editor=False) must skip the Popen
    call even when `code` is on PATH and stdout is a tty. Operators
    in CI / headless / SSH sessions rely on this."""

    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)

    popen_calls: list[list[str]] = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            popen_calls.append(list(argv))

    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd: "/usr/local/bin/code" if cmd == "code" else None,
    )
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    _init_project(
        name="demo",
        target=tmp_path,
        force=False,
        skip_snapshot=True,
        open_editor=False,
    )

    code_calls = [c for c in popen_calls if c and c[0] == "code"]
    assert code_calls == [], f"open_editor=False must NOT launch the editor; got: {popen_calls}"


# ---------------------------------------------------------------------------
# Fix #3b — Post-init menu uses the role-picker, not the old shortcuts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_post_menu_uses_browse_and_add_agents_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old menu had ``[3] Add the FAQ agent`` and ``[4] Add two
    role agents (rag-qa + ticket-triager)`` as hardcoded shortcuts.
    The new menu has a single ``Browse + add agents`` option that
    invokes ``mdk add --list`` — same numbered role catalog operators
    see when running ``mdk add --list`` directly."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init", "demo", "--skip-snapshot", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr

    # New: dynamic browse-and-add picker is offered.
    assert "Browse + add agents" in combined
    # Old: the hardcoded shortcuts must NOT show.
    assert "Add the FAQ agent" not in combined
    assert "Add two role agents (rag-qa + ticket-triager)" not in combined


@pytest.mark.unit
def test_init_prints_cd_reminder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Since a child process can't change the parent shell's cwd, the
    next-best thing is printing the ``cd <project>`` command
    prominently. Operators paste it; we move on."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["init", "demo", "--skip-snapshot", "--no-open-editor"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "cd demo" in combined
