"""Unit tests for the external-orchestrator adapters (ADR 017 D3).

Covers the three D3 deliverables:

* the shared ``submit → poll → fetch`` engine
  (:func:`movate.integrations.orchestration.run_target_async`) — drives the
  EXISTING ``MovateClient`` async API and returns the run's ``output``;
* the Prefect ``run_agent`` / ``run_workflow`` task wrappers;
* the Airflow ``MovateAgentOperator``.

The MovateClient is stubbed at the class level — no real Prefect/Airflow
runtime, no FastAPI app, no network. We assert the adapters call the client
with the right ``(kind, target, input)`` and return the right ``output``,
that a non-success terminal status / timeout raises, and that the
``mdk[...]`` extras are required with a clear error (not an opaque
ImportError) when the lib is missing.

Tests that need the actual optional lib (the real ``@task`` /
``BaseOperator``) skip-guard with ``pytest.importorskip`` so CI without the
extras still passes.

The lazy-import contract (importing ``movate`` / ``movate.cli.main`` must
NOT pull in prefect or airflow) is asserted in a subprocess, mirroring
``tests/test_authoring_mcp_server.py::test_cli_main_import_does_not_load_mcp_server``.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from movate.core.client import MovateClientError
from movate.core.models import ErrorInfo, JobKind, JobStatus, Metrics, TokenUsage
from movate.integrations.orchestration import (
    MovateConnection,
    OrchestrationError,
    run_target_async,
)
from movate.runtime.schemas import JobView, RunAccepted, RunView

# ---------------------------------------------------------------------------
# Fakes — stub the MovateClient the adapters build internally.
# ---------------------------------------------------------------------------


def _metrics() -> Metrics:
    return Metrics(tokens=TokenUsage(input=1, output=1), cost_usd=0.0, latency_ms=1)


def _job(
    *,
    status: JobStatus,
    result_run_id: str | None = "run-1",
    error: ErrorInfo | None = None,
) -> JobView:
    return JobView(
        job_id="job-1",
        kind=JobKind.AGENT,
        target="triage-bot",
        status=status,
        input={"ticket_id": "T-1"},
        result_run_id=result_run_id,
        error=error,
        created_at=datetime.now(UTC),
    )


def _run(output: dict[str, Any] | None) -> RunView:
    return RunView(
        run_id="run-1",
        job_id="job-1",
        agent="triage-bot",
        agent_version="1",
        prompt_hash="h",
        provider="mock",
        provider_version="",
        pricing_version="",
        status=JobStatus.SUCCESS,
        input={"ticket_id": "T-1"},
        output=output,
        metrics=_metrics(),
        created_at=datetime.now(UTC),
    )


class _FakeClient:
    """Records calls + returns canned responses, swappable for MovateClient.

    Supports being used as an async context manager exactly like the real
    client, so ``async with MovateClient(...) as c`` works unchanged.
    """

    def __init__(
        self,
        *,
        job: JobView,
        run: RunView | None,
        submit_error: MovateClientError | None = None,
        timeout: bool = False,
    ) -> None:
        self._job = job
        self._run = run
        self._submit_error = submit_error
        self._timeout = timeout
        self.submit_calls: list[dict[str, Any]] = []
        self.closed = False

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def submit_job(
        self, *, kind: JobKind, target: str, input: dict[str, Any], notify_email: str | None = None
    ) -> RunAccepted:
        self.submit_calls.append(
            {"kind": kind, "target": target, "input": input, "notify_email": notify_email}
        )
        if self._submit_error is not None:
            raise self._submit_error
        return RunAccepted(job_id="job-1", status=JobStatus.QUEUED)

    async def wait_for_terminal(
        self,
        job_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        max_wait_seconds: float | None = None,
    ) -> JobView:
        if self._timeout:
            raise TimeoutError("did not finish")
        return self._job

    async def get_run(self, run_id: str) -> RunView:
        assert self._run is not None
        return self._run


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    """Make ``MovateClient(...)`` return ``fake`` regardless of args.

    The adapters construct ``MovateClient`` internally; we replace the
    symbol the orchestration module bound at import time so every adapter
    routes through the fake."""

    def _factory(*args: Any, **kwargs: Any) -> _FakeClient:
        return fake

    monkeypatch.setattr("movate.integrations.orchestration.MovateClient", _factory)


_CONN = MovateConnection(base_url="https://rt.example", api_key="mvt_test")


# ---------------------------------------------------------------------------
# run_target_async — the shared engine
# ---------------------------------------------------------------------------


async def test_run_target_async_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        job=_job(status=JobStatus.SUCCESS),
        run=_run({"category": "billing", "summary": "refund request"}),
    )
    _patch_client(monkeypatch, fake)

    out = await run_target_async(
        connection=_CONN,
        target="triage-bot",
        payload={"ticket_id": "T-1"},
    )

    assert out == {"category": "billing", "summary": "refund request"}
    # Submitted with the right kind/target/input.
    assert fake.submit_calls == [
        {
            "kind": JobKind.AGENT,
            "target": "triage-bot",
            "input": {"ticket_id": "T-1"},
            "notify_email": None,
        }
    ]
    assert fake.closed  # context manager exited → client closed


async def test_run_target_async_workflow_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"done": True}))
    _patch_client(monkeypatch, fake)

    out = await run_target_async(
        connection=_CONN,
        target="returns-pipeline",
        payload={"order_id": "O-9"},
        kind=JobKind.WORKFLOW,
    )

    assert out == {"done": True}
    assert fake.submit_calls[0]["kind"] == JobKind.WORKFLOW


async def test_run_target_async_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        job=_job(
            status=JobStatus.ERROR,
            error=ErrorInfo(type="provider_error", message="upstream 500"),
        ),
        run=None,
    )
    _patch_client(monkeypatch, fake)

    with pytest.raises(OrchestrationError) as exc:
        await run_target_async(connection=_CONN, target="triage-bot", payload={})
    # The typed runtime error is surfaced for an actionable task log.
    assert "error" in str(exc.value)
    assert "provider_error" in str(exc.value)


async def test_run_target_async_safety_blocked_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(job=_job(status=JobStatus.SAFETY_BLOCKED, result_run_id=None), run=None)
    _patch_client(monkeypatch, fake)
    with pytest.raises(OrchestrationError):
        await run_target_async(connection=_CONN, target="triage-bot", payload={})


async def test_run_target_async_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=None, timeout=True)
    _patch_client(monkeypatch, fake)
    with pytest.raises(OrchestrationError) as exc:
        await run_target_async(connection=_CONN, target="triage-bot", payload={}, poll_timeout=5.0)
    # The job_id is in the message so an operator can resume tracking it.
    assert "job-1" in str(exc.value)
    assert "server-side" in str(exc.value)


async def test_run_target_async_submit_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        job=_job(status=JobStatus.SUCCESS),
        run=None,
        submit_error=MovateClientError(status_code=404, code="not_found", message="no agent"),
    )
    _patch_client(monkeypatch, fake)
    with pytest.raises(OrchestrationError) as exc:
        await run_target_async(connection=_CONN, target="ghost", payload={})
    assert "submit failed" in str(exc.value)


async def test_run_target_async_success_without_run_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SUCCESS but no result_run_id → return {} rather than crash.
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS, result_run_id=None), run=None)
    _patch_client(monkeypatch, fake)
    out = await run_target_async(connection=_CONN, target="triage-bot", payload={})
    assert out == {}


async def test_run_target_async_null_output_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run whose output is None comes back as {} (never None).
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run(None))
    _patch_client(monkeypatch, fake)
    out = await run_target_async(connection=_CONN, target="triage-bot", payload={})
    assert out == {}


# ---------------------------------------------------------------------------
# MovateConnection.from_env
# ---------------------------------------------------------------------------


def test_connection_from_env_happy_path() -> None:
    conn = MovateConnection.from_env(
        {"MOVATE_RUNTIME_URL": "https://rt", "MOVATE_API_KEY": "mvt_x"}
    )
    assert conn.base_url == "https://rt"
    assert conn.api_key == "mvt_x"
    assert conn.timeout == 30.0


def test_connection_from_env_custom_timeout() -> None:
    conn = MovateConnection.from_env(
        {"MOVATE_RUNTIME_URL": "https://rt", "MOVATE_API_KEY": "k", "MOVATE_RUNTIME_TIMEOUT": "12"}
    )
    assert conn.timeout == 12.0


@pytest.mark.parametrize(
    "env, missing",
    [
        ({"MOVATE_API_KEY": "k"}, "MOVATE_RUNTIME_URL"),
        ({"MOVATE_RUNTIME_URL": "https://rt"}, "MOVATE_API_KEY"),
        ({}, "MOVATE_RUNTIME_URL"),
    ],
)
def test_connection_from_env_missing_var_raises(env: dict[str, str], missing: str) -> None:
    with pytest.raises(OrchestrationError) as exc:
        MovateConnection.from_env(env)
    assert missing in str(exc.value)


# ---------------------------------------------------------------------------
# Prefect adapter — lib stubbed
# ---------------------------------------------------------------------------


def _install_fake_prefect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a minimal fake ``prefect`` module exposing a no-op ``@task``.

    The real Prefect runtime isn't needed to assert our wiring: ``@task``
    just needs to return the function (so calling it runs the body). Reset
    the per-process task cache so each test re-decorates against the fake.
    """
    import types  # noqa: PLC0415

    fake_prefect = types.ModuleType("prefect")

    def task(*dargs: Any, **dkwargs: Any) -> Any:
        def deco(fn: Any) -> Any:
            return fn

        return deco

    fake_prefect.task = task  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "prefect", fake_prefect)

    import movate.integrations.prefect as prefect_adapter  # noqa: PLC0415

    monkeypatch.setattr(prefect_adapter, "_tasks", {})


def test_prefect_run_agent_calls_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_agent is SYNCHRONOUS (it bridges to the async core via asyncio.run),
    # so the test must NOT be async — a fresh loop can't nest in pytest's loop.
    _install_fake_prefect(monkeypatch)
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"ok": 1}))
    _patch_client(monkeypatch, fake)

    from movate.integrations.prefect import run_agent  # noqa: PLC0415

    out = run_agent(
        "triage-bot",
        {"ticket_id": "T-1"},
        base_url="https://rt.example",
        api_key="mvt_test",
    )
    assert out == {"ok": 1}
    assert fake.submit_calls[0]["target"] == "triage-bot"
    assert fake.submit_calls[0]["kind"] == JobKind.AGENT


def test_prefect_run_workflow_uses_workflow_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_prefect(monkeypatch)
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"done": True}))
    _patch_client(monkeypatch, fake)

    from movate.integrations.prefect import run_workflow  # noqa: PLC0415

    out = run_workflow(
        "returns-pipeline",
        {"order_id": "O-1"},
        base_url="https://rt.example",
        api_key="mvt_test",
    )
    assert out == {"done": True}
    assert fake.submit_calls[0]["kind"] == JobKind.WORKFLOW


def test_prefect_run_agent_uses_env_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_prefect(monkeypatch)
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"ok": 1}))
    _patch_client(monkeypatch, fake)
    monkeypatch.setenv("MOVATE_RUNTIME_URL", "https://rt.example")
    monkeypatch.setenv("MOVATE_API_KEY", "mvt_env")

    from movate.integrations.prefect import run_agent  # noqa: PLC0415

    out = run_agent("triage-bot", {"ticket_id": "T-1"})  # no base_url/api_key
    assert out == {"ok": 1}


def test_prefect_missing_dep_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No prefect installed → a clear 'install mdk[prefect]' error, not ImportError."""
    # Force the import to fail even if prefect happens to be installed.
    monkeypatch.setitem(sys.modules, "prefect", None)
    import movate.integrations.prefect as prefect_adapter  # noqa: PLC0415

    monkeypatch.setattr(prefect_adapter, "_tasks", {})
    with pytest.raises(OrchestrationError) as exc:
        prefect_adapter.run_agent("a", {}, base_url="https://x", api_key="k")
    assert "mdk[prefect]" in str(exc.value) or "prefect` extra" in str(exc.value)


# ---------------------------------------------------------------------------
# Airflow adapter — lib stubbed
# ---------------------------------------------------------------------------


def _install_fake_airflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a minimal fake ``airflow.models.BaseOperator``.

    A bare base class with an ``__init__`` that swallows kwargs is enough to
    exercise our subclass's ``__init__`` + ``execute`` without the real
    Airflow runtime. Reset the per-process operator-class cache so the
    subclass rebinds against the fake base.
    """
    import types  # noqa: PLC0415

    airflow_mod = types.ModuleType("airflow")
    models_mod = types.ModuleType("airflow.models")

    class BaseOperator:
        def __init__(self, **kwargs: Any) -> None:
            self.task_id = kwargs.get("task_id")

    models_mod.BaseOperator = BaseOperator  # type: ignore[attr-defined]
    airflow_mod.models = models_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.models", models_mod)

    import movate.integrations.airflow as airflow_adapter  # noqa: PLC0415

    monkeypatch.setattr(airflow_adapter, "_operator_cls", None)


def test_airflow_operator_execute_calls_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_airflow(monkeypatch)
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"category": "billing"}))
    _patch_client(monkeypatch, fake)

    from movate.integrations.airflow import MovateAgentOperator  # noqa: PLC0415

    op = MovateAgentOperator(
        task_id="triage",
        agent="triage-bot",
        payload={"ticket_id": "T-1"},
        base_url="https://rt.example",
        api_key="mvt_test",
    )
    out = op.execute(context={})
    assert out == {"category": "billing"}
    assert fake.submit_calls[0]["target"] == "triage-bot"
    assert fake.submit_calls[0]["kind"] == JobKind.AGENT


def test_airflow_operator_workflow_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_airflow(monkeypatch)
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"done": True}))
    _patch_client(monkeypatch, fake)

    from movate.integrations.airflow import MovateAgentOperator  # noqa: PLC0415

    op = MovateAgentOperator(
        task_id="wf",
        agent="returns-pipeline",
        payload={"order_id": "O-1"},
        kind="workflow",
        base_url="https://rt.example",
        api_key="mvt_test",
    )
    out = op.execute(context={})
    assert out == {"done": True}
    assert fake.submit_calls[0]["kind"] == JobKind.WORKFLOW


def test_airflow_operator_bad_kind_raises_at_construct(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_airflow(monkeypatch)
    from movate.integrations.airflow import MovateAgentOperator  # noqa: PLC0415

    with pytest.raises(ValueError):  # JobKind("nonsense") rejects the typo
        MovateAgentOperator(
            task_id="x",
            agent="a",
            payload={},
            kind="nonsense",
            base_url="https://x",
            api_key="k",
        )


def test_airflow_operator_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_airflow(monkeypatch)
    fake = _FakeClient(
        job=_job(status=JobStatus.ERROR, error=ErrorInfo(type="boom", message="bad")),
        run=None,
    )
    _patch_client(monkeypatch, fake)
    from movate.integrations.airflow import MovateAgentOperator  # noqa: PLC0415

    op = MovateAgentOperator(task_id="x", agent="a", payload={}, base_url="https://x", api_key="k")
    with pytest.raises(OrchestrationError):
        op.execute(context={})


def test_airflow_missing_dep_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No airflow installed → clear 'install mdk[airflow]' error, not ImportError."""
    monkeypatch.setitem(sys.modules, "airflow", None)
    monkeypatch.setitem(sys.modules, "airflow.models", None)
    import movate.integrations.airflow as airflow_adapter  # noqa: PLC0415

    monkeypatch.setattr(airflow_adapter, "_operator_cls", None)
    with pytest.raises(OrchestrationError) as exc:
        airflow_adapter.make_movate_agent_operator()
    assert "mdk[airflow]" in str(exc.value) or "airflow` extra" in str(exc.value)


def test_airflow_template_fields_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """``payload``/``agent`` are templatable so DAG authors can use Jinja."""
    _install_fake_airflow(monkeypatch)
    from movate.integrations.airflow import make_movate_agent_operator  # noqa: PLC0415

    cls = make_movate_agent_operator()
    assert "payload" in cls.template_fields
    assert "agent" in cls.template_fields


# ---------------------------------------------------------------------------
# Lazy-import contract — the base CLI must not pull prefect/airflow
# ---------------------------------------------------------------------------


def test_cli_main_import_does_not_load_prefect_or_airflow() -> None:
    """Importing movate / movate.cli.main must NOT import prefect or airflow.

    Mirrors test_cli_main_import_does_not_load_mcp_server: the orchestrator
    adapters are opt-in, lazy-imported glue — the base install and every
    existing command stay unaffected."""
    code = (
        "import sys\n"
        "import movate  # noqa: F401\n"
        "import movate.cli.main  # noqa: F401\n"
        "import movate.integrations.prefect  # noqa: F401 — importing the module is fine\n"
        "import movate.integrations.airflow  # noqa: F401 — importing the module is fine\n"
        "assert 'prefect' not in sys.modules, 'prefect was eagerly imported'\n"
        "assert 'airflow' not in sys.modules, 'airflow was eagerly imported'\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# ---------------------------------------------------------------------------
# Real-lib skip-guarded smoke (only when the extra is actually installed)
# ---------------------------------------------------------------------------


def test_prefect_run_agent_with_real_prefect(monkeypatch: pytest.MonkeyPatch) -> None:
    """With REAL prefect installed, run_agent still drives the stubbed client.

    Skips when the [prefect] extra isn't installed so CI without it passes."""
    pytest.importorskip(
        "prefect", reason="prefect not installed — install with: uv add 'movate-cli[prefect]'"
    )
    import movate.integrations.prefect as prefect_adapter  # noqa: PLC0415

    monkeypatch.setattr(prefect_adapter, "_tasks", {})
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"ok": 1}))
    _patch_client(monkeypatch, fake)
    out = prefect_adapter.run_agent(
        "triage-bot", {"ticket_id": "T-1"}, base_url="https://rt", api_key="k"
    )
    assert out == {"ok": 1}


def test_airflow_operator_with_real_airflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """With REAL airflow installed, the operator subclasses the real
    BaseOperator and execute() drives the stubbed client. Skips without it."""
    pytest.importorskip(
        "airflow", reason="airflow not installed — install with: uv add 'movate-cli[airflow]'"
    )
    import movate.integrations.airflow as airflow_adapter  # noqa: PLC0415

    monkeypatch.setattr(airflow_adapter, "_operator_cls", None)
    fake = _FakeClient(job=_job(status=JobStatus.SUCCESS), run=_run({"ok": 1}))
    _patch_client(monkeypatch, fake)
    cls = airflow_adapter.make_movate_agent_operator()
    op = cls(task_id="t", agent="a", payload={}, base_url="https://rt", api_key="k")
    assert op.execute(context={}) == {"ok": 1}
