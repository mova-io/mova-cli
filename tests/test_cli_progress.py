"""Progress UI doesn't break automation paths.

Three things matter most to the dev team:

1. JSON output (``-o json``) MUST stay valid JSON — the test scripts
   in CI and Slack bots that do ``movate eval ... -o json | jq`` can't
   tolerate progress chars on stdout.
2. Non-TTY environments (CI logs, redirected output) must NOT emit
   ANSI escape codes. Rich auto-detects this; the test confirms it.
3. The engine callbacks are best-effort — a buggy callback must not
   sink the whole eval / bench / worker. Crashes in the UI layer
   are decorative.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.eval import CaseSummary, EvalEngine
from movate.core.models import (
    JobKind,
    JobRecord,
    JobStatus,
)
from movate.runtime.dispatch import WorkerDispatch
from movate.runtime.worker import Worker
from movate.testing import InMemoryStorage

runner = CliRunner(mix_stderr=False)


def _scaffold(parent: Path, name: str = "demo") -> Path:
    result = runner.invoke(app, ["init", "--bare", name, "-t", "default", "--target", str(parent)])
    assert result.exit_code == 0, result.stdout
    return parent / name


# ---------------------------------------------------------------------------
# JSON output stays clean even with progress wired in
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_json_output_stays_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``movate eval ... -o json`` must produce valid JSON on stdout
    even though stage 4 wires a progress callback into the engine.
    Progress UI lives on stderr; stdout is contract."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold(tmp_path)

    result = runner.invoke(app, ["eval", str(agent_dir), "--mock", "--gate", "0.0", "-o", "json"])
    assert result.exit_code == 0, result.stdout

    # Must round-trip through json.loads cleanly. Any progress bytes
    # bleeding into stdout would crash this.
    payload = json.loads(result.stdout)
    assert "eval_id" in payload
    assert "cases" in payload


@pytest.mark.unit
def test_eval_markdown_output_stays_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same contract for markdown — wire automation might pipe to a
    review bot that expects a markdown body."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold(tmp_path)

    result = runner.invoke(
        app, ["eval", str(agent_dir), "--mock", "--gate", "0.0", "-o", "markdown"]
    )
    assert result.exit_code == 0
    # Markdown output: starts with a heading, no Rich progress bytes.
    assert result.stdout.lstrip().startswith("#")


# ---------------------------------------------------------------------------
# Engine callback is decorative — buggy callback must not kill the run
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_eval_engine_swallows_callback_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the on_case_complete callback raises, the eval still
    completes — UI is decorative, never load-bearing."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold(tmp_path)

    from movate.core.executor import Executor  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.providers.mock import MockProvider  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415
    from movate.testing import NullTracer  # noqa: PLC0415

    bundle = load_agent(agent_dir)
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )

    call_count = 0

    def buggy_callback(done: int, total: int, summary: CaseSummary) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("ui exploded")

    engine = EvalEngine(
        executor=executor,
        provider=MockProvider(),
        on_case_complete=buggy_callback,
    )
    summary = await engine.run(bundle)

    # Run completed despite the callback raising on every case.
    assert summary.sample_count > 0
    assert call_count == summary.sample_count


@pytest.mark.unit
async def test_worker_swallows_callback_exceptions() -> None:
    """Same decorative-not-load-bearing contract for the worker
    callback."""
    storage = InMemoryStorage()
    await storage.init()

    job = JobRecord(
        job_id="j1",
        tenant_id="t1",
        kind=JobKind.AGENT,
        target="ghost",  # unknown agent → terminal ERROR
        status=JobStatus.QUEUED,
        input={},
    )
    await storage.save_job(job)

    callback_fired = False

    def buggy_callback(j, o, d):
        nonlocal callback_fired
        callback_fired = True
        raise RuntimeError("ui exploded")

    dispatch = WorkerDispatch(
        storage=storage,
        executor=None,  # type: ignore[arg-type]  # never called for unknown agent
        agents=[],
    )
    worker = Worker(storage=storage, dispatch=dispatch, on_job_complete=buggy_callback)

    handled = await worker.run_one_cycle()

    # Worker handled the job AND fired the callback (which raised).
    # The job still landed in a terminal state.
    assert handled is not None
    assert callback_fired
    final = await storage.get_job("j1", tenant_id="t1")
    assert final is not None
    assert final.status == JobStatus.ERROR


# ---------------------------------------------------------------------------
# Worker live feed prints one line per completed job
# ---------------------------------------------------------------------------


def _read_latest_run_id(home: Path) -> str:
    import sqlite3  # noqa: PLC0415

    db_path = home / ".movate" / "local.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None
    return row[0]


# (No CliRunner test for `movate worker` itself — the worker loop
# blocks forever, and the ``test_phase2plus_stub_commands_exit_nonzero``
# fixture explicitly *excludes* it for that reason. Worker progress
# UI is exercised via the on_job_complete callback test above + a
# real-binary smoke test walked through pre-commit.)


# ---------------------------------------------------------------------------
# progress_bar / spinner helpers — non-TTY behaves
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_progress_bar_silent_on_non_tty() -> None:
    """When stderr isn't a TTY, the progress bar should not emit
    visible artifacts. Rich's Console captures into a buffer for
    tests; we use a Console with ``force_terminal=False`` to simulate
    the CI / piped case."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    from movate.cli._progress import progress_bar  # noqa: PLC0415

    captured = StringIO()
    fake_stderr = Console(file=captured, force_terminal=False, width=80)

    with progress_bar(description="cases", total=3, console=fake_stderr) as advance:
        advance()
        advance()
        advance()

    # Disabled progress bars on non-TTY produce no output (or at
    # most a final "completed" line). We tolerate either; just confirm
    # there are no escape sequences visible in the captured text.
    assert "\x1b[" not in captured.getvalue()


@pytest.mark.unit
def test_spinner_silent_on_non_tty() -> None:
    """Spinner is a strict no-op when stderr isn't a TTY."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    from movate.cli._progress import spinner  # noqa: PLC0415

    captured = StringIO()
    fake_stderr = Console(file=captured, force_terminal=False, width=80)

    with spinner("calling provider...", console=fake_stderr):
        pass

    assert captured.getvalue() == ""


@pytest.mark.unit
def test_print_event_writes_to_stderr_console() -> None:
    """``print_event`` should write to the provided console.

    Sanity check that the API is the one we expect and that callers can
    inject a custom Console for capture."""
    from io import StringIO  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    from movate.cli._progress import print_event  # noqa: PLC0415

    captured = StringIO()
    fake_stderr = Console(file=captured, force_terminal=False, width=80)

    print_event("✓ job done", style="green", console=fake_stderr)
    print_event("plain text", console=fake_stderr)

    out = captured.getvalue()
    assert "job done" in out
    assert "plain text" in out
