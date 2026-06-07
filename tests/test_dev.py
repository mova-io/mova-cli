"""``mdk dev`` — unit tests for the guided authoring loop.

The watch⇄menu loop and interactive prompts aren't unit-tested (they'd
need a TTY + fiddly input mocks); the integration test_watch covers the
poll mechanics. Here we assert the pieces ``dev`` is built from:

* ``dispatch_run_once`` — runs against the scaffold under --mock, and
  fails cleanly (exit 2) on a broken agent, proving fresh reload.
* ``_compute_watched_paths`` now includes contexts.
* ``_print_output_diff`` — the unchanged/changed signal in the live loop.
* The non-interactive CLI surface prints the command sequence.

(The ``contexts:`` attach/detach helpers now live in contexts_cmd and are
tested in test_contexts_cmd.py.)
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import movate.cli.dev_cmd as dc
from movate.cli.main import app as cli_app
from movate.cli.watch import (
    _compute_watched_paths,
    _print_output_diff,
    dispatch_run_once,
    run_loop,
)
from movate.core.eval import DimensionalMeans
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# dispatch_run_once
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_run_once_clean_agent_returns_0_and_output(tmp_path: Path) -> None:
    """Scaffold runs end-to-end under --mock → exit 0 + captured output."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    rc, output = dispatch_run_once(agent_dir, '{"text": "hello"}', mock=True)
    assert rc == 0
    assert output  # non-empty captured stdout, for diffing successive runs


@pytest.mark.unit
def test_dispatch_run_once_broken_agent_returns_2_and_none(tmp_path: Path) -> None:
    """Deleting prompt.md makes load_agent fail → dispatch returns (2, None)
    and does not raise (the dev loop must survive it). Proves the run path
    reloads from disk each call rather than caching the bundle."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "prompt.md").unlink()
    rc, output = dispatch_run_once(agent_dir, '{"text": "hello"}', mock=True)
    assert rc == 2
    assert output is None


@pytest.mark.unit
def test_print_output_diff_signals_change(capsys: pytest.CaptureFixture[str]) -> None:
    # No baseline yet, or a failed run → nothing printed.
    _print_output_diff(None, "hi")
    _print_output_diff("hi", None)
    assert capsys.readouterr().err == ""

    # Unchanged → a one-line marker.
    _print_output_diff("same", "same")
    assert "unchanged" in capsys.readouterr().err

    # Changed → a diff that shows both sides.
    _print_output_diff("answer: 1", "answer: 2")
    err = capsys.readouterr().err
    assert "changed" in err
    assert "answer: 2" in err


# ---------------------------------------------------------------------------
# _compute_watched_paths now includes contexts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_watched_paths_includes_agent_local_contexts(tmp_path: Path) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    ctx_dir = agent_dir / "contexts"
    ctx_dir.mkdir()
    (ctx_dir / "policy.md").write_text("# policy")

    watched = _compute_watched_paths(agent_dir)
    names = {p.name for p in watched.paths}
    assert "policy.md" in names


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_dev_help_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin a wide terminal so rich doesn't ellipsize the option name.
    # monkeypatch.setenv patches the real os.environ (which rich reads),
    # which is more reliable across CI than CliRunner's env= param.
    monkeypatch.setenv("COLUMNS", "200")
    r = runner.invoke(cli_app, ["dev", "--help"])
    assert r.exit_code == 0
    # Strip ANSI + box-drawing + whitespace so the assertion is independent
    # of rich's terminal-width wrapping (differs local vs CI).
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
    cleaned = re.sub(r"[\s│╭╮╰╯─├┤┌┐└┘|]+", "", cleaned)
    assert "--template" in cleaned


@pytest.mark.unit
def test_cli_dev_non_interactive_prints_guide(tmp_path: Path) -> None:
    """CliRunner stdin is not a tty → dev prints the command sequence and
    exits 0 instead of opening a live session."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    r = runner.invoke(cli_app, ["dev", str(agent_dir)])
    assert r.exit_code == 0
    assert "mdk_dev_summary" in r.stdout
    assert "agent=demo" in r.stdout


# ---------------------------------------------------------------------------
# Actions: grounding check + test-on-deployed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grounding_action_invokes_eval_scorecard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    dc._grounding_action(agent_dir)

    assert calls, "expected a subprocess invocation"
    assert calls[0][1] == "eval-scorecard"
    assert str(agent_dir) in calls[0]


@pytest.mark.unit
def test_test_deployed_action_runs_against_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    out = dc._test_deployed_action(agent_dir, "hello", "prod")

    assert out == "prod"  # target remembered
    assert calls and calls[0][1] == "run"
    assert "--target" in calls[0] and "prod" in calls[0]
    assert "-i" in calls[0] and "hello" in calls[0]


@pytest.mark.unit
def test_test_deployed_action_skips_without_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No target and a non-interactive _ensure_target → skip, no subprocess."""

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(dc, "_ensure_target", lambda target, *, purpose: None)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    assert dc._test_deployed_action(agent_dir, "hello", None) is None
    assert calls == []


# ---------------------------------------------------------------------------
# Action: ingest knowledge base (the `k` key)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ingest_kb_action_ingests_to_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`k` shells out to `kb ingest <agent> <path> --target <env>` and
    remembers the target."""

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    kb_dir = agent_dir / "kb"
    kb_dir.mkdir()
    (kb_dir / "faq.md").write_text("# faq")
    # Path defaults to agents/<name>/kb/ — accept the default by returning "".
    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: str(kb_dir)))

    out = dc._ingest_kb_action("demo", agent_dir, "prod")

    assert out == "prod"  # target remembered
    assert calls and calls[0][1:3] == ["kb", "ingest"]
    assert "demo" in calls[0]
    assert str(kb_dir) in calls[0]
    assert "--target" in calls[0] and "prod" in calls[0]


@pytest.mark.unit
def test_ingest_kb_action_local_when_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No target → ingest into the local store (no --target flag)."""

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(dc, "_ensure_target", lambda target, *, purpose: None)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    kb_dir = agent_dir / "kb"
    kb_dir.mkdir()
    (kb_dir / "faq.md").write_text("# faq")
    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: str(kb_dir)))

    out = dc._ingest_kb_action("demo", agent_dir, None)

    assert out is None
    assert calls and calls[0][1:3] == ["kb", "ingest"]
    assert "--target" not in calls[0]


@pytest.mark.unit
def test_ingest_kb_action_skips_missing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that doesn't exist → no subprocess; target preserved."""

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: str(tmp_path / "nope")))

    out = dc._ingest_kb_action("demo", agent_dir, "prod")

    assert out == "prod"
    assert calls == []


@pytest.mark.unit
def test_ingest_kb_action_accepts_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL source is passed straight to `kb ingest` (no path-exists gate)."""

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(dc, "_ensure_target", lambda target, *, purpose: None)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: "https://example.test/docs"))

    out = dc._ingest_kb_action("demo", agent_dir, None)

    assert out is None
    assert calls and calls[0][1:3] == ["kb", "ingest"]
    assert "https://example.test/docs" in calls[0]


# ---------------------------------------------------------------------------
# D7c (#134): the proactive "RAG agent, empty KB" grounding-gap offer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grounding_gap_offer_silent_when_no_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No gap (non-RAG agent or populated KB) → no prompt, no delegation,
    no output. The regression guard for the dominant path."""

    monkeypatch.setattr(dc, "_has_grounding_gap", lambda agent_dir: False)
    # If the offer wrongly proceeded, these would fire — assert they don't.
    confirmed: list[bool] = []
    monkeypatch.setattr(dc.typer, "confirm", lambda *a, **k: confirmed.append(True) or True)
    delegated: list[tuple] = []
    monkeypatch.setattr(dc, "_ingest_kb_action", lambda *a, **k: delegated.append(a) or "prod")
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    out = dc._grounding_gap_offer("demo", agent_dir, "prod")

    assert out == "prod"  # target unchanged
    assert confirmed == []  # never prompted
    assert delegated == []  # never delegated
    captured = capsys.readouterr()
    assert "knowledge base" not in (captured.out + captured.err)


@pytest.mark.unit
def test_grounding_gap_offer_delegates_to_ingest_when_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap present + operator confirms → delegates to the EXISTING ingest path
    (`_ingest_kb_action` / `mdk kb ingest`), not a new ingest implementation."""

    monkeypatch.setattr(dc, "_has_grounding_gap", lambda agent_dir: True)
    monkeypatch.setattr(dc.typer, "confirm", lambda *a, **k: True)
    delegated: list[tuple] = []
    monkeypatch.setattr(dc, "_ingest_kb_action", lambda *a, **k: delegated.append(a) or "prod")
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    out = dc._grounding_gap_offer("demo", agent_dir, "prod")

    assert out == "prod"  # target threaded back through the delegate
    assert delegated, "expected delegation to the existing _ingest_kb_action"
    assert delegated[0] == ("demo", agent_dir, "prod")


@pytest.mark.unit
def test_grounding_gap_offer_declined_skips_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap present but operator declines → confirm-gated: no delegation,
    target preserved."""

    monkeypatch.setattr(dc, "_has_grounding_gap", lambda agent_dir: True)
    monkeypatch.setattr(dc.typer, "confirm", lambda *a, **k: False)
    delegated: list[tuple] = []
    monkeypatch.setattr(dc, "_ingest_kb_action", lambda *a, **k: delegated.append(a) or "prod")
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    out = dc._grounding_gap_offer("demo", agent_dir, "prod")

    assert out == "prod"  # target unchanged
    assert delegated == []  # confirm-gated: nothing ingested


@pytest.mark.unit
def test_has_grounding_gap_swallows_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A storage / load failure in the detector returns False, never raises —
    the offer must never crash or block the session."""

    def _boom(*a: object, **k: object) -> object:
        raise RuntimeError("no database here")

    monkeypatch.setattr("movate.storage.build_storage", _boom)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    # Must not raise; a non-RAG scaffold is False anyway, but the broad
    # except also covers the storage explosion for a RAG-shaped agent.
    assert dc._has_grounding_gap(agent_dir) is False


# ---------------------------------------------------------------------------
# Eval-in-the-loop (--eval-sample-size)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_in_loop_disabled_by_default(tmp_path: Path) -> None:
    """--eval-sample-size 0 (default) → disabled: no hook, no eval, no output.

    The byte-for-byte back-compat guard: a default dev session never touches the
    eval engine, so the live loop is exactly what it was before this flag.
    """
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    loop = dc._EvalInLoop(agent_dir, sample_size=0, mock=True)
    assert loop.enabled is False
    # after_run is a no-op when disabled, even on a clean run.
    loop.after_run(0)
    assert loop._prev_mean is None


@pytest.mark.unit
def test_eval_in_loop_scores_first_n_cases_under_mock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--eval-sample-size N>0 scores the first N cases and prints a scorecard.

    Hermetic via --mock (dataset-aware MockProvider), so the loop is free /
    offline. The scaffold dataset has 8 rows; capping to 3 must score exactly 3.
    """
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    loop = dc._EvalInLoop(agent_dir, sample_size=3, mock=True)
    assert loop.enabled is True

    loop.after_run(0)
    err = capsys.readouterr().err
    assert "eval" in err
    assert "3 case" in err  # capped to the requested sample size
    assert "accuracy" in err
    assert "baseline" in err  # first iteration has no prior mean to diff


@pytest.mark.unit
def test_eval_in_loop_shows_delta_across_iterations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two successive scorecards print the mean-accuracy delta (▲/▼/=).

    Drives ``_print_scorecard`` directly with synthetic summaries so the delta
    math is asserted independent of provider behavior: 0.50 → 0.80 is a rise,
    0.80 → 0.40 is a drop, 0.40 → 0.40 is unchanged.
    """

    def _summary(accuracy: float) -> object:
        return SimpleNamespace(
            sample_count=2,
            mean_score=accuracy,
            dimensional_means=DimensionalMeans(accuracy=accuracy),
        )

    loop = dc._EvalInLoop(Path("."), sample_size=2, mock=True)

    loop._print_scorecard(_summary(0.50))  # type: ignore[arg-type]
    assert "baseline" in capsys.readouterr().err

    loop._print_scorecard(_summary(0.80))  # type: ignore[arg-type]
    rise = capsys.readouterr().err
    assert "▲" in rise
    assert "+0.30" in rise

    loop._print_scorecard(_summary(0.40))  # type: ignore[arg-type]
    drop = capsys.readouterr().err
    assert "▼" in drop
    assert "-0.40" in drop

    loop._print_scorecard(_summary(0.40))  # type: ignore[arg-type]
    same = capsys.readouterr().err
    assert "no change" in same


@pytest.mark.unit
def test_eval_in_loop_degrades_gracefully_without_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No eval dataset → a one-line hint, then quiet; never crashes the loop.

    The dataset-less agent shouldn't spam the loop, so the hint fires once and
    subsequent iterations stay silent. ``after_run`` must not raise either way.
    """
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "evals" / "dataset.jsonl").unlink()  # drop the dataset file

    loop = dc._EvalInLoop(agent_dir, sample_size=3, mock=True)
    loop.after_run(0)
    first = capsys.readouterr().err
    assert "no eval dataset" in first

    loop.after_run(0)  # second pass: silent (no repeat spam)
    assert "no eval dataset" not in capsys.readouterr().err


@pytest.mark.unit
def test_eval_in_loop_survives_a_buggy_eval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash inside the eval run degrades to a one-line note, never raises —
    the dev loop must survive a bad dataset / engine error (rule 10)."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    loop = dc._EvalInLoop(agent_dir, sample_size=2, mock=True)

    def _boom(self: object) -> object:
        raise RuntimeError("eval blew up")

    monkeypatch.setattr(dc._EvalInLoop, "_run_capped_eval", _boom)
    loop.after_run(0)  # must not raise
    assert "eval-in-loop skipped" in capsys.readouterr().err


@pytest.mark.unit
def test_eval_in_loop_skips_failed_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A failed live run (exit_code != 0) scores nothing — there's no useful
    output to evaluate, and the run's own error already surfaced."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    loop = dc._EvalInLoop(agent_dir, sample_size=2, mock=True)
    loop.after_run(2)
    assert capsys.readouterr().err == ""
    assert loop._prev_mean is None


@pytest.mark.unit
def test_run_loop_hook_default_none_is_unchanged(tmp_path: Path) -> None:
    """``run_loop``'s new ``on_iteration`` defaults to None — the existing
    callers (``mdk watch --run``) pass nothing, so behavior is unchanged. We
    just assert the signature is back-compatible (keyword-defaulted)."""
    sig = inspect.signature(run_loop)
    assert sig.parameters["on_iteration"].default is None
