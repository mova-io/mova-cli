"""``mdk certify`` — the first-class certification-run front door.

Hermetic: the suite core (``_invoke_suite``) and every ``az`` touchpoint
(``_az_available`` / ``_run_az``) are monkeypatched — no live runtime, no
Azure CLI required. Covers the wiring contract: argv passed to the suite,
the env-then-credentials-file MDK_DEV_KEY fallback, the already-running
concurrency guard (refusal, --force override, graceful skip without az),
exit-code passthrough, and the --in-env start → poll → logs → exit-mirror
flow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli import certify_cmd
from movate.cli.main import app as cli_app

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def quiet_az(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default az posture: CLI present, no executions running."""
    monkeypatch.setattr(certify_cmd, "_az_available", lambda: True)
    monkeypatch.setattr(certify_cmd, "_running_executions", lambda rg, job: [])


@pytest.fixture
def dev_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_test_demo_KID_SECRET")


def _capture_suite(monkeypatch: pytest.MonkeyPatch, exit_code: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_suite(argv: list[str]) -> int:
        calls.append(argv)
        return exit_code

    monkeypatch.setattr(certify_cmd, "_invoke_suite", fake_suite)
    return calls


# ---------------------------------------------------------------------------
# Local mode — invocation wiring + credential fallback + exit codes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLocalMode:
    def test_invokes_suite_with_target_and_scenario(
        self, monkeypatch: pytest.MonkeyPatch, quiet_az: None, dev_key_env: None
    ) -> None:
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(
            cli_app, ["certify", "--target", "dev", "--scenario", "expense-approval"]
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert calls == [["--target", "dev", "--scenario", "expense-approval"]]

    def test_json_flag_passes_through(
        self, monkeypatch: pytest.MonkeyPatch, quiet_az: None, dev_key_env: None
    ) -> None:
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify", "--json"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert calls == [["--target", "dev", "--json"]]

    def test_suite_failure_exit_code_passes_through(
        self, monkeypatch: pytest.MonkeyPatch, quiet_az: None, dev_key_env: None
    ) -> None:
        _capture_suite(monkeypatch, exit_code=1)
        result = runner.invoke(cli_app, ["certify"])
        assert result.exit_code == 1

    def test_dev_key_falls_back_to_credentials_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        quiet_az: None,
    ) -> None:
        """No MDK_DEV_KEY in the env → the ~/.movate/credentials line wins."""
        creds = tmp_path / "credentials"
        creds.write_text("MDK_DEV_KEY=mvt_test_demo_FROMFILE\n")
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(creds))
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert calls  # the suite ran
        assert os.environ.get("MDK_DEV_KEY") == "mvt_test_demo_FROMFILE"

    def test_env_key_beats_credentials_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        quiet_az: None,
    ) -> None:
        creds = tmp_path / "credentials"
        creds.write_text("MDK_DEV_KEY=mvt_test_demo_FROMFILE\n")
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(creds))
        monkeypatch.setenv("MDK_DEV_KEY", "mvt_test_demo_FROMSHELL")
        _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify"])
        assert result.exit_code == 0
        assert os.environ.get("MDK_DEV_KEY") == "mvt_test_demo_FROMSHELL"

    def test_missing_key_everywhere_is_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        quiet_az: None,
    ) -> None:
        monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "absent"))
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify"])
        assert result.exit_code == 2
        assert "MDK_DEV_KEY" in result.stderr
        assert not calls  # the suite never ran


# ---------------------------------------------------------------------------
# Concurrency guard — the 429 lesson
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConcurrencyGuard:
    def test_refuses_when_execution_already_running(
        self, monkeypatch: pytest.MonkeyPatch, dev_key_env: None
    ) -> None:
        monkeypatch.setattr(certify_cmd, "_az_available", lambda: True)
        monkeypatch.setattr(
            certify_cmd, "_running_executions", lambda rg, job: ["movate-cert-suite-x1"]
        )
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify"])
        assert result.exit_code == 2
        assert "already in progress" in result.stderr
        assert "--force" in result.stderr
        assert not calls

    def test_force_overrides_the_guard(
        self, monkeypatch: pytest.MonkeyPatch, dev_key_env: None
    ) -> None:
        monkeypatch.setattr(certify_cmd, "_az_available", lambda: True)
        monkeypatch.setattr(
            certify_cmd, "_running_executions", lambda rg, job: ["movate-cert-suite-x1"]
        )
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify", "--force"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert calls  # the suite ran despite the Running execution

    def test_guard_skips_gracefully_without_az(
        self, monkeypatch: pytest.MonkeyPatch, dev_key_env: None
    ) -> None:
        """No az → warn + run locally anyway (laptops without Azure tooling)."""
        monkeypatch.setattr(certify_cmd, "_az_available", lambda: False)
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "concurrency guard" in result.stderr
        assert calls

    def test_guard_skips_gracefully_when_listing_fails(
        self, monkeypatch: pytest.MonkeyPatch, dev_key_env: None
    ) -> None:
        monkeypatch.setattr(certify_cmd, "_az_available", lambda: True)
        monkeypatch.setattr(certify_cmd, "_running_executions", lambda rg, job: None)
        calls = _capture_suite(monkeypatch)
        result = runner.invoke(cli_app, ["certify"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert calls


# ---------------------------------------------------------------------------
# --in-env — start → poll → logs → exit-code mirror
# ---------------------------------------------------------------------------


class FakeAz:
    """Dispatching fake for ``_run_az`` covering the --in-env call sequence."""

    def __init__(self, *, final_status: str = "Succeeded", polls: int = 2) -> None:
        self.final_status = final_status
        self.polls = polls
        self.poll_count = 0
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        if args[:3] == ["containerapp", "job", "start"]:
            return 0, json.dumps({"name": "movate-cert-suite-zz9"}), ""
        if args[:4] == ["containerapp", "job", "execution", "list"]:
            self.poll_count += 1
            status = self.final_status if self.poll_count >= self.polls else "Running"
            return (
                0,
                json.dumps([{"name": "movate-cert-suite-zz9", "properties": {"status": status}}]),
                "",
            )
        if args[:4] == ["monitor", "log-analytics", "workspace", "list"]:
            return 0, json.dumps([{"customerId": "ws-1234"}]), ""
        if args[:3] == ["monitor", "log-analytics", "query"]:
            return 0, json.dumps([{"Log_s": "scenario x capability MATRIX"}]), ""
        raise AssertionError(f"unexpected az call: {args}")


def _wire_in_env(
    monkeypatch: pytest.MonkeyPatch, fake: FakeAz, *, running: list[str] | None = None
) -> None:
    monkeypatch.setattr(certify_cmd, "_az_available", lambda: True)
    monkeypatch.setattr(certify_cmd, "_run_az", fake)
    monkeypatch.setattr(certify_cmd, "_POLL_INTERVAL_S", 0.0)
    if running is not None:
        monkeypatch.setattr(certify_cmd, "_running_executions", lambda rg, job: list(running))
    else:
        monkeypatch.setattr(certify_cmd, "_running_executions", lambda rg, job: [])


@pytest.mark.unit
class TestInEnv:
    def test_succeeded_execution_exits_zero_and_prints_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeAz(final_status="Succeeded")
        _wire_in_env(monkeypatch, fake)
        result = runner.invoke(cli_app, ["certify", "--in-env"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "movate-cert-suite-zz9" in result.stdout
        assert "MATRIX" in result.stdout
        # the log query targets the execution's container group prefix
        query = next(c for c in fake.calls if c[:3] == ["monitor", "log-analytics", "query"])
        analytics_query = query[query.index("--analytics-query") + 1]
        assert 'startswith "movate-cert-suite-zz9"' in analytics_query

    def test_failed_execution_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeAz(final_status="Failed")
        _wire_in_env(monkeypatch, fake)
        result = runner.invoke(cli_app, ["certify", "--in-env"])
        assert result.exit_code == 1
        assert "Failed" in result.stderr

    def test_overridden_rg_and_job_names_flow_into_az_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeAz()
        _wire_in_env(monkeypatch, fake)
        result = runner.invoke(
            cli_app,
            ["certify", "--in-env", "-g", "other-rg", "--job-name", "other-job"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        start = next(c for c in fake.calls if c[:3] == ["containerapp", "job", "start"])
        assert start[start.index("-g") + 1] == "other-rg"
        assert start[start.index("-n") + 1] == "other-job"

    def test_explicit_workspace_id_skips_discovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeAz()
        _wire_in_env(monkeypatch, fake)
        result = runner.invoke(cli_app, ["certify", "--in-env", "--workspace-id", "ws-given"])
        assert result.exit_code == 0
        assert not any(
            c[:4] == ["monitor", "log-analytics", "workspace", "list"] for c in fake.calls
        )
        query = next(c for c in fake.calls if c[:3] == ["monitor", "log-analytics", "query"])
        assert query[query.index("-w") + 1] == "ws-given"

    def test_in_env_requires_az(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(certify_cmd, "_az_available", lambda: False)
        result = runner.invoke(cli_app, ["certify", "--in-env"])
        assert result.exit_code == 2
        assert "az" in result.stderr

    def test_in_env_respects_concurrency_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeAz()
        _wire_in_env(monkeypatch, fake, running=["movate-cert-suite-old"])
        result = runner.invoke(cli_app, ["certify", "--in-env"])
        assert result.exit_code == 2
        assert "already in progress" in result.stderr
        assert not any(c[:3] == ["containerapp", "job", "start"] for c in fake.calls)


# ---------------------------------------------------------------------------
# Plumbing units — status parsing across az output shapes
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"properties": {"status": "Running"}}, "Running"),
        ({"status": "Succeeded"}, "Succeeded"),
        ({"properties": {}, "status": "Failed"}, "Failed"),
        ({}, ""),
    ],
)
def test_execution_status_handles_both_az_shapes(item: dict[str, Any], expected: str) -> None:
    assert certify_cmd._execution_status(item) == expected
