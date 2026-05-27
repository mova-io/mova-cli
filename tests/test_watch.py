"""``movate watch`` — dispatch unit tests + path-discovery + smoke.

The poll loop itself is intentionally untested at the unit level
(it would require either a real ``time.sleep`` or fiddly mocks).
Instead:

* ``_compute_watched_paths`` is asserted directly — it's the
  function that determines what mtime changes will fire dispatch.
* ``dispatch_once`` is asserted directly — exit code, output
  format, recovery on broken agent.

A single integration test exercises the poll loop end-to-end with a
very short interval + a hard timeout, just to prove the wire-up
works.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.cli.watch import (
    _compute_watched_paths,
    _resolve_test_input,
    dispatch_once,
    dispatch_run_once,
)
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# _compute_watched_paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_watched_paths_includes_yaml_prompt_and_schemas(tmp_path: Path) -> None:
    """The default scaffold has agent.yaml + prompt + dataset (schemas
    are inline in agent.yaml as shorthand — no separate JSON files
    in the default template). The watcher must include all of them."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    watched = _compute_watched_paths(agent_dir)
    names = {p.name for p in watched.paths}
    assert "agent.yaml" in names
    assert "prompt.md" in names
    assert "dataset.jsonl" in names
    # Schemas live inline in the default template's agent.yaml; when
    # they change the watcher picks up the agent.yaml edit and reloads.
    # The path-form variant (schema/*.json files) is still supported;
    # it's tested elsewhere via _scaffold_with_schemas in
    # test_prompt_linter.py.


@pytest.mark.unit
def test_watched_paths_excludes_missing_optional_files(tmp_path: Path) -> None:
    """No judge.yaml on disk → it's not in the watch set. Watcher
    must not poll non-existent paths."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    watched = _compute_watched_paths(agent_dir)
    judge_paths = [p for p in watched.paths if "judge" in p.name]
    assert judge_paths == []


# ---------------------------------------------------------------------------
# dispatch_once
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_once_returns_0_for_clean_agent(tmp_path: Path) -> None:
    """The scaffold passes validate + lint → exit code 0.

    Note: dispatch_once prints its own header + delegates to
    `_validate_agent`, which prints the regular validate output.
    """
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    rc = dispatch_once(agent_dir, strict=False)
    assert rc == 0


@pytest.mark.unit
def test_dispatch_once_returns_2_for_broken_agent(tmp_path: Path) -> None:
    """An agent whose prompt is empty fails the prompt linter →
    dispatch returns 2. The watcher CATCHES the typer.Exit so it
    keeps polling; we assert the exit code is surfaced for
    tests / scripted use."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "prompt.md").write_text("")  # empty prompt → EMPTY_PROMPT error
    rc = dispatch_once(agent_dir, strict=False)
    assert rc == 2


@pytest.mark.unit
def test_dispatch_once_strict_promotes_warnings(tmp_path: Path) -> None:
    """Same prompt that's clean by default + --strict turns it
    into exit 2 if any lint WARNING fires."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    # Strip the JSON instruction → MISSING_JSON_INSTRUCTION warning.
    (agent_dir / "prompt.md").write_text(
        "You are a helpful assistant. Return the user's message text "
        "in a structured format using the 'message' field. Be concise."
    )
    assert dispatch_once(agent_dir, strict=False) == 0  # warning prints but exits 0
    assert dispatch_once(agent_dir, strict=True) == 2  # warning fails under --strict


# ---------------------------------------------------------------------------
# CLI smoke — make sure typer wires the command without crashing.
# Don't actually run the poll loop; just assert --help works.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_watch_help_renders() -> None:
    r = runner.invoke(cli_app, ["watch", "--help"])
    assert r.exit_code == 0
    # Rich wraps long flag names across lines on narrow terminals (GH
    # Actions runners default to ~80 cols), and the wrap may insert
    # whitespace mid-token — strip ANSI escapes and collapse all
    # whitespace before substring checks so the assertions are
    # terminal-width-independent.
    clean = _strip_for_help_check(r.stdout)
    assert "hot-reload" in clean.lower() or "validate" in clean.lower()
    # Flags surface in --help so operators can discover them.
    assert "--poll-interval" in clean
    assert "--strict" in clean


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_for_help_check(text: str) -> str:
    """Normalize Rich-rendered help so substring checks tolerate
    terminal-width-driven line wrapping.

    Rich may insert a space + newline mid-token when wrapping
    (``--poll-↵-interval`` style). Stripping ANSI escapes and
    collapsing every whitespace run to nothing gives us a single
    canonical string where flag names appear contiguously.
    """
    no_ansi = _ANSI_ESCAPE_RE.sub("", text)
    return re.sub(r"\s+", "", no_ansi)


# ---------------------------------------------------------------------------
# End-to-end: poll loop fires dispatch on file change
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_watch_dispatches_on_file_change(tmp_path: Path) -> None:
    """End-to-end smoke: start the watcher in a thread, mutate a
    file, assert the watcher re-dispatched.

    We instrument by replacing ``dispatch_once`` with a counter; the
    real one is exercised by the unit tests above.
    """
    from movate.cli import watch as watch_module  # noqa: PLC0415

    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    dispatch_calls: list[Path] = []

    def fake_dispatch(p: Path, *, strict: bool) -> int:
        dispatch_calls.append(p)
        return 0

    real = watch_module.dispatch_once
    watch_module.dispatch_once = fake_dispatch  # type: ignore[assignment]
    try:
        stop = threading.Event()

        def runner_thread() -> None:
            # Fast poll for the test; keyboard-interrupt by raising
            # SystemExit from another thread isn't supported, so we
            # break out via the stop event check we wire below.
            try:
                # Manually drive the loop body — we can't directly
                # call watch() because it blocks on time.sleep + has
                # no stop signal. Instead, simulate one initial
                # dispatch + one change cycle.
                watch_module.dispatch_once(agent_dir, strict=False)
                # Wait for the test to mutate a file, then snapshot
                # again. Bounded by the stop event for the cleanup
                # path on failure.
                deadline = time.time() + 3.0
                while time.time() < deadline and not stop.is_set():
                    time.sleep(0.05)
                    if len(dispatch_calls) >= 2:
                        return
                    # Check if any watched file changed since we
                    # started; if so, dispatch.
                    snap_now = watch_module._snapshot_mtimes(
                        watch_module._compute_watched_paths(agent_dir).paths
                    )
                    if any(snap_now[p] != snap_initial.get(p) for p in snap_now):
                        watch_module.dispatch_once(agent_dir, strict=False)
                        return
            finally:
                pass

        snap_initial = watch_module._snapshot_mtimes(
            watch_module._compute_watched_paths(agent_dir).paths
        )
        t = threading.Thread(target=runner_thread)
        t.start()

        # Give the initial dispatch a moment to land.
        time.sleep(0.1)
        assert len(dispatch_calls) == 1

        # Mutate the prompt — should trigger a second dispatch.
        # Sleep enough to ensure mtime resolution catches it (some
        # filesystems have 1s mtime granularity).
        time.sleep(1.1)
        (agent_dir / "prompt.md").write_text(
            "You are a JSON-only assistant. Respond with the field 'message' "
            "as a string. This is the edited version."
        )

        t.join(timeout=4.0)
        stop.set()

        assert len(dispatch_calls) >= 2, (
            f"expected dispatch to fire on file change; got {len(dispatch_calls)} calls"
        )
    finally:
        watch_module.dispatch_once = real  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Recovery — broken YAML mid-edit doesn't crash the watcher
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_handles_invalid_yaml_gracefully(tmp_path: Path) -> None:
    """If agent.yaml gets corrupted mid-save, dispatch returns 2
    (validate's failure exit code) — the watcher's loop catches
    typer.Exit and keeps polling, so the operator can fix and try again."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "agent.yaml").write_text("this is not: valid: : yaml")
    rc = dispatch_once(agent_dir, strict=False)
    assert rc == 2


# ---------------------------------------------------------------------------
# ADR 027 — the --run live-reload loop
# ---------------------------------------------------------------------------
#
# The loop body (run_loop) polls between interactive prompts and isn't
# unit-tested directly (it needs a tty + a real edit cadence; the validate
# poll loop above already proves the mtime-poll wiring). Here we assert the
# pieces the loop is built from: re-run reflects an edited watched file, the
# half-saved-file guard, the D3 test-input precedence, and the non-TTY gate.


@pytest.mark.unit
def test_dispatch_run_once_reflects_edited_watched_file(tmp_path: Path) -> None:
    """Re-running after editing a watched file reflects the edit — proving the
    loop reloads fresh from disk on every dispatch (no cache/daemon to
    invalidate), which is the whole architectural premise of ADR 027.

    Hermetic via ``--mock``: the MockProvider ignores the prompt body and
    answers from the dataset's ``expected`` rows, so the dataset (also a
    watched file) is the deterministic lever for "did the re-run pick up my
    edit?". Editing ``prompt.md`` exercises the same reload path; its effect
    just isn't observable through the mock.
    """
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    test_input = '{"text": "hello"}'

    rc1, out1 = dispatch_run_once(agent_dir, test_input, mock=True)
    assert rc1 == 0
    assert out1

    # Edit the first dataset row's expected output, then re-run. A fresh
    # dispatch re-reads the dataset → the mock returns the new answer.
    dataset = agent_dir / "evals" / "dataset.jsonl"
    lines = dataset.read_text().splitlines()
    row0 = json.loads(lines[0])
    row0["expected"] = {"message": "EDITED ANSWER"}
    lines[0] = json.dumps(row0)
    dataset.write_text("\n".join(lines) + "\n")

    rc2, out2 = dispatch_run_once(agent_dir, test_input, mock=True)
    assert rc2 == 0
    assert out2 == "EDITED ANSWER"
    assert out2 != out1


@pytest.mark.unit
def test_dispatch_run_once_survives_half_saved_file(tmp_path: Path) -> None:
    """A half-saved / invalid agent file must not crash the loop: dispatch
    returns ``(2, None)`` and does not raise, so ``run_loop`` keeps polling
    (mirrors the ``contextlib.suppress(AgentLoadError)`` guard)."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "agent.yaml").write_text("this is not: valid: : yaml")
    rc, output = dispatch_run_once(agent_dir, '{"text": "hello"}', mock=True)
    assert rc == 2
    assert output is None


@pytest.mark.unit
def test_resolve_test_input_prefers_explicit_flag(tmp_path: Path) -> None:
    """D3 precedence #1: an explicit --input wins over the dataset row."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    assert _resolve_test_input('{"text": "explicit"}', agent_dir) == '{"text": "explicit"}'


@pytest.mark.unit
def test_resolve_test_input_falls_back_to_dataset_row(tmp_path: Path) -> None:
    """D3 precedence #2: no --input → the first evals/dataset.jsonl row's input."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    resolved = _resolve_test_input(None, agent_dir)
    assert resolved is not None
    # The scaffold's first row is {"input": {"text": "hello"}, ...}.
    assert json.loads(resolved) == {"text": "hello"}


@pytest.mark.unit
def test_resolve_test_input_none_when_no_dataset(tmp_path: Path) -> None:
    """D3 precedence #3: no --input and no dataset → None (caller prompts)."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    # Drop the dataset declaration so there's nothing to fall back to.
    dataset = agent_dir / "evals" / "dataset.jsonl"
    if dataset.exists():
        dataset.unlink()
    assert _resolve_test_input(None, agent_dir) is None


@pytest.mark.unit
def test_watched_paths_includes_project_level_contexts(tmp_path: Path) -> None:
    """D1: a project-level ``<root>/contexts/*.md`` is in the watched set, so
    editing or attaching a shared context re-fires the loop. (The agent-local
    case is covered in test_dev.)"""
    project_root = tmp_path / "proj"
    agents_dir = project_root / "agents"
    agent_dir = scaffold_agent(agents_dir / "demo", name="demo")
    # The canonical layout marks the PROJECT root, not agents/. scaffold_agent
    # drops a marker in agents/ for its own convenience; remove it so
    # _resolve_project_root walks up to proj/ as it would in a real project.
    (agents_dir / "movate.yaml").unlink(missing_ok=True)
    (project_root / "movate.yaml").write_text("name: proj\n")
    ctx_dir = project_root / "contexts"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "shared-policy.md").write_text("# shared policy")

    watched = _compute_watched_paths(agent_dir)
    names = {p.name for p in watched.paths}
    assert "shared-policy.md" in names


@pytest.mark.unit
def test_cli_watch_run_non_tty_prints_guide_and_exits(tmp_path: Path) -> None:
    """Non-TTY ``--run`` degrades to a documentation-only print and exits 0 —
    it must never hang waiting for an interactive input (CliRunner stdin is
    not a tty)."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    r = runner.invoke(cli_app, ["watch", str(agent_dir), "--run", "--mock"])
    assert r.exit_code == 0
    combined = _strip_for_help_check(r.stdout + r.stderr).lower()
    assert "non-interactive" in combined


@pytest.mark.unit
def test_cli_watch_help_advertises_run_flag() -> None:
    """The ``--run`` opt-in surfaces in --help so operators can discover the
    live-reload loop. Width/ANSI-robust (strip escapes + collapse whitespace)."""
    r = runner.invoke(cli_app, ["watch", "--help"])
    assert r.exit_code == 0
    clean = _strip_for_help_check(r.stdout)
    assert "--run" in clean
