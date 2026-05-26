"""Inner-loop UX polish for ``mdk run`` (P1/P4/P5/P7).

All four are additive, guarded hints that never change exit codes, the
``--json`` output shape, or run semantics:

* **P1** — after a run, surface the ``trace_id`` (+ a
  ``mdk runs show <run_id> --target`` view pointer on the deployed path).
  Suppressed when there's no trace id or under ``--json`` (JSON stdout must
  stay machine-parseable).
* **P4** — on (status=error + schema/output-validation error + ``--mock``),
  append an actionable hint that MockProvider output can't satisfy the agent's
  output_schema. A real-provider schema error keeps today's bare message.
* **P5** — when a deployed ``--target`` submission comes back queued
  (202 + ``{job_id, status: queued}``), print the poll/cancel follow-ups.
* **P7** — the deployed ``--target`` path auto-wraps a bare string into the
  agent's single required string field, at parity with the local path.

The helper-level tests (P1/P4) are unit tests of the pure render helpers — the
local execution path's trace_id is empty under the default SilentTracer, so we
assert the render decision directly rather than wiring a tracer. The remote
path is exercised end-to-end through ``httpx.MockTransport``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli._output import Run
from movate.cli.main import app
from movate.cli.run import _maybe_mock_schema_hint, _maybe_trace_line

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# P1 / P4 — pure render-helper unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTraceLineHelper:
    def test_prints_plain_trace_line_when_no_target(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _maybe_trace_line("tid-123", output_format=Run.TEXT, target=None)
        err = capsys.readouterr().err
        assert "trace:" in err
        assert "tid-123" in err
        # No deployed view pointer for a local run.
        assert "mdk runs show" not in err

    def test_prints_view_pointer_for_target(self, capsys: pytest.CaptureFixture[str]) -> None:
        _maybe_trace_line("tid-123", output_format=Run.TEXT, target="dev", run_id="run-789")
        err = capsys.readouterr().err
        assert "tid-123" in err
        # Deployed runs get the actionable view command + App Insights pointer.
        # The command must be a REAL one — `mdk runs show <run_id> --target` —
        # not the dead-end `mdk trace <id> --target` (#125).
        assert "mdk runs show run-789 --target dev" in err.replace("\n", "")
        assert "mdk trace" not in err
        assert "App Insights" in err.replace("\n", "")

    def test_falls_back_to_plain_line_without_run_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `mdk runs show` needs a run_id; with none, omit the dead-end pointer
        # and just print the bare trace line.
        _maybe_trace_line("tid-123", output_format=Run.TEXT, target="dev", run_id=None)
        err = capsys.readouterr().err
        assert "trace:" in err
        assert "tid-123" in err
        assert "mdk runs show" not in err

    def test_suppressed_under_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        _maybe_trace_line("tid-123", output_format=Run.JSON, target="dev", run_id="run-789")
        assert capsys.readouterr().err == ""

    def test_suppressed_when_trace_id_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        _maybe_trace_line("", output_format=Run.TEXT, target="dev", run_id="run-789")
        _maybe_trace_line(None, output_format=Run.TEXT, target="dev", run_id="run-789")
        _maybe_trace_line("   ", output_format=Run.TEXT, target="dev", run_id="run-789")
        assert capsys.readouterr().err == ""


@pytest.mark.unit
class TestMockSchemaHintHelper:
    def test_fires_on_mock_plus_schema_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        _maybe_mock_schema_hint(error_type="schema_error", mock=True)
        err = capsys.readouterr().err
        assert "MockProvider" in err
        assert "--mock" in err

    def test_matches_schema_substring_variants(self, capsys: pytest.CaptureFixture[str]) -> None:
        _maybe_mock_schema_hint(error_type="output_validation_error", mock=True)
        assert "MockProvider" in capsys.readouterr().err

    def test_silent_for_real_provider_schema_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No --mock → today's bare message; no hint appended.
        _maybe_mock_schema_hint(error_type="schema_error", mock=False)
        assert capsys.readouterr().err == ""

    def test_silent_for_non_schema_error_under_mock(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _maybe_mock_schema_hint(error_type="provider_error", mock=True)
        _maybe_mock_schema_hint(error_type=None, mock=True)
        assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Remote (--target) fixtures — mirror test_run_remote_target.py
# ---------------------------------------------------------------------------


def _make_client_factory(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.Client", factory)


def _write_user_config(home: Path, target_name: str = "dev") -> None:
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        f"active: {target_name}\n"
        f"targets:\n"
        f"  {target_name}:\n"
        f"    url: https://fake.example.com\n"
        f"    key_env: MDK_DEV_KEY\n"
    )


def _bootstrap_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    return tmp_path / "proj"


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap_project(tmp_path, monkeypatch)
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(tmp_path / ".movate" / "config.yaml"))
    _write_user_config(tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_t1_k1_secret")


def _run_view(*, status: str, error: dict | None, trace_id: str = "") -> dict:
    return {
        "run_id": "11111111-2222-3333-4444-555555555555",
        "job_id": "job-xyz",
        "agent": "faq",
        "agent_version": "0.1.0",
        "prompt_hash": "deadbeef",
        "provider": "mock",
        "provider_version": "1.0",
        "pricing_version": "2024.05",
        "status": status,
        "input": {"question": "hello"},
        "output": {"answer": "hi"} if status == "success" else None,
        "metrics": {
            "cost_usd": 0.0012,
            "latency_ms": 480,
            "tokens": {"input": 12, "output": 4},
            "trace_id": trace_id,
        },
        "error": error,
        "created_at": "2026-05-15T12:00:00Z",
        "workflow_run_id": None,
        "node_id": None,
    }


# ---------------------------------------------------------------------------
# P1 — trace surfacing on the deployed path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_success_surfaces_trace_with_view_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful deployed run with a trace_id prints the trace line +
    the ``mdk runs show <run_id> --target`` view pointer on stderr; stdout
    stays the machine-parseable RunView."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run_view(status="success", error=None, trace_id="tr-abc"))

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "-o", "text"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "trace: tr-abc" in result.stderr
    # The view pointer references `mdk runs show <run_id> --target` (a real
    # remote command), not the dead-end `mdk trace <id> --target` (#125).
    stderr_flat = result.stderr.replace("\n", "")
    assert "mdk runs show 11111111-2222-3333-4444-555555555555 --target dev" in stderr_flat
    assert "App Insights" in result.stderr
    # stdout untouched by the hint.
    assert "trace:" not in result.stdout


@pytest.mark.unit
def test_target_trace_line_suppressed_under_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under ``-o json`` the trace hint is omitted entirely — JSON output
    must stay clean for ``jq``."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run_view(status="success", error=None, trace_id="tr-abc"))

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "-o", "json"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "view: mdk runs show" not in result.stderr
    # The trace_id still rides along inside the JSON body on stdout (it's part
    # of the RunView the runtime returned) — we only suppressed the hint line.
    assert "tr-abc" in result.stdout


@pytest.mark.unit
def test_target_no_trace_id_no_trace_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No trace_id (tracing off on the runtime) → no trace line, clean exit."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run_view(status="success", error=None, trace_id=""))

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "-o", "text"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "view: mdk runs show" not in result.stderr


# ---------------------------------------------------------------------------
# P4 — mock + schema-error hint on the deployed path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_mock_schema_error_appends_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status=error + schema_error + --mock → the actionable hint is appended
    beneath the surfaced error message."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_run_view(
                status="error",
                error={"type": "schema_error", "message": "output failed validation"},
            ),
        )

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "--mock"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 1
    assert "schema_error" in result.stderr
    assert "MockProvider" in result.stderr


@pytest.mark.unit
def test_target_real_provider_schema_error_no_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema error WITHOUT --mock keeps today's bare message — no hint."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_run_view(
                status="error",
                error={"type": "schema_error", "message": "output failed validation"},
            ),
        )

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(app, ["run", "faq", "hello", "--target", "dev"], env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "schema_error" in result.stderr
    assert "MockProvider" not in result.stderr


@pytest.mark.unit
def test_target_mock_non_schema_error_no_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--mock + a NON-schema error (e.g. provider failure) → no hint."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_run_view(
                status="error",
                error={"type": "ProviderError", "message": "rate limited"},
            ),
        )

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "--mock"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 1
    assert "ProviderError" in result.stderr
    assert "MockProvider" not in result.stderr


# ---------------------------------------------------------------------------
# P5 — async (queued) submission poll/cancel hint
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_queued_submission_prints_poll_cancel_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 202 + RunAccepted (queued job) → exit 0, the job body on stdout, and
    the poll/cancel follow-ups on stderr."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"job_id": "job-7", "status": "queued"})

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "-o", "text"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "job job-7 queued" in result.stderr.replace("\n", "")
    assert "mdk jobs wait job-7 --target dev" in result.stderr.replace("\n", "")
    assert "mdk jobs cancel job-7 --target dev" in result.stderr.replace("\n", "")
    # The job body is on stdout for piping; summary marks the handoff ok=true.
    assert '"job_id": "job-7"' in result.stdout
    assert "ok=true" in result.stderr


@pytest.mark.unit
def test_target_queued_submission_hint_suppressed_under_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under ``-o json`` the queued body still goes to stdout, but the
    human poll/cancel hint is omitted from stderr."""
    _configure(tmp_path, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"job_id": "job-7", "status": "queued"})

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "-o", "json"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "mdk jobs wait" not in result.stderr
    assert '"job_id": "job-7"' in result.stdout


# ---------------------------------------------------------------------------
# P7 — bare-string auto-wrap parity on the deployed path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_target_bare_string_auto_wraps_to_single_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare string reaches the deployed POST wrapped into the agent's single
    required string field — same convenience as the local path."""
    _configure(tmp_path, monkeypatch)

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json=_run_view(status="success", error=None))

    _make_client_factory(httpx.MockTransport(handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "what is movate?", "--target", "dev"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The scaffolded faq agent's single required string field is `question`.
    assert '"question":"what is movate?"' in captured["body"]
