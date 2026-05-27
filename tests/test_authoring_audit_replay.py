"""D7e (#136) — authoring audit log + replay tests.

Hermetic + offline over tmp_path projects (no keys, no network), mirroring the
existing authoring test conventions (test_authoring_autopilot / _copilot). We
assert the brief's contract:

* the driver's apply/undo write the expected append-only audit records (action,
  args, outcome, cost);
* a corrupt / missing audit log degrades gracefully (warning, never crashes);
* replay re-drives a recorded sequence through the SAME confirm-gated
  ``driver.apply`` — never a raw re-write — so the D2/D3/D4 properties hold;
* the ``mdk authoring audit`` / ``replay`` CLI + the ``mdk dev`` view action
  surface the log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import movate.cli.dev_cmd as dc
from movate.authoring import (
    AuditLog,
    AuditOutcome,
    AuditRecord,
    AuthoringContext,
    AuthoringDriver,
    CostBudget,
    SessionCostTracker,
    replay_records,
    replayable,
)
from movate.authoring.audit import AUDIT_LOG_NAME
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: greeter
version: 0.1.0
description: A test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
  params:
    temperature: 0.0
prompt: ./prompt.md
schema:
  input:
    text: string
  output:
    message: string
evals:
  dataset: ./evals/dataset.jsonl
"""

_PROMPT = "You are a greeter. Reply with a greeting.\n"
_DATASET = '{"input": {"text": "hi"}, "expected": {"message": "hello"}}\n'


def _make_project(root: Path, *, agent: str = "greeter") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("agents_dir: ./agents\n")
    agent_dir = root / "agents" / agent
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML.replace("name: greeter", f"name: {agent}"))
    (agent_dir / "prompt.md").write_text(_PROMPT)
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text(_DATASET)
    return root


def _driver(root: Path) -> AuthoringDriver:
    return AuthoringDriver(AuthoringContext(project=root))


def _audit_path(root: Path) -> Path:
    return root / ".mdk" / AUDIT_LOG_NAME


# ---------------------------------------------------------------------------
# Driver apply/undo write the expected audit records
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_writes_applied_audit_record(tmp_path: Path) -> None:
    """A driven apply writes an append-only `applied` record (action/args/cost)."""
    root = _make_project(tmp_path / "proj")
    driver = _driver(root)
    driver.apply(
        "add-context",
        {"agent": "greeter", "name": "tone", "body": "# Tone\n"},
        fast_mode=True,
    )

    records = driver.audit_records()
    assert len(records) == 1
    rec = records[0]
    assert rec.action == "add-context"
    assert rec.agent == "greeter"
    assert rec.args["name"] == "tone"
    assert rec.outcome == AuditOutcome.APPLIED
    assert rec.cost_usd == 0.0  # add-context is free
    assert rec.created_at  # timestamp recorded
    assert rec.changed_paths  # the action recorded what it changed


@pytest.mark.unit
def test_audit_log_is_append_only_across_drivers(tmp_path: Path) -> None:
    """Each apply appends a line; a fresh driver reads the full history back."""
    root = _make_project(tmp_path / "proj")
    _driver(root).apply(
        "add-context", {"agent": "greeter", "name": "a", "body": "# A\n"}, fast_mode=True
    )
    _driver(root).apply(
        "add-context", {"agent": "greeter", "name": "b", "body": "# B\n"}, fast_mode=True
    )

    # Two distinct lines, oldest-first, read by a brand-new driver instance.
    records = _driver(root).audit_records()
    assert [r.args["name"] for r in records] == ["a", "b"]
    # The on-disk file really is one JSONL record per line (append-only).
    lines = [ln for ln in _audit_path(root).read_text().splitlines() if ln.strip()]
    assert len(lines) == 2


@pytest.mark.unit
def test_undo_writes_undone_audit_record(tmp_path: Path) -> None:
    """`undo` appends an `undone` record (the immutable log keeps the apply too)."""
    root = _make_project(tmp_path / "proj")
    driver = _driver(root)
    driver.apply(
        "add-context", {"agent": "greeter", "name": "tone", "body": "# Tone\n"}, fast_mode=True
    )
    undone = driver.undo()
    assert undone is not None

    records = driver.audit_records()
    # The apply record is still there (append-only) + a new undone record.
    assert [r.outcome for r in records] == [AuditOutcome.APPLIED, AuditOutcome.UNDONE]
    assert records[-1].action == "add-context"


@pytest.mark.unit
def test_reverted_apply_records_reverted_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verify-revert is recorded as `reverted` even though the undo stack skips it."""
    import movate.authoring.driver as drv  # noqa: PLC0415

    root = _make_project(tmp_path / "proj")

    # Force the D3 verify loop's validate to fail → the driver reverts to the
    # pre-apply checkpoint. The audit log must still record the attempt.
    def _boom_validate(_agent_dir: Path) -> object:
        raise drv.AgentLoadError("forced validate failure")

    monkeypatch.setattr(drv, "validate_agent", _boom_validate)
    driver = _driver(root)
    outcome = driver.apply(
        "edit-instructions", {"agent": "greeter", "body": "new prompt\n"}, fast_mode=True
    )
    assert outcome.verify is not None and outcome.verify.reverted

    records = driver.audit_records()
    assert len(records) == 1
    assert records[0].outcome == AuditOutcome.REVERTED
    # The undo stack (history) did NOT keep the reverted action.
    assert driver.history() == []


@pytest.mark.unit
def test_audit_append_swallows_os_error(tmp_path: Path) -> None:
    """The real `append` is best-effort: a write OSError is logged, not raised."""
    root = _make_project(tmp_path / "proj")
    log = AuditLog(root)
    # Make the target a DIRECTORY so opening it for append raises IsADirectoryError
    # (an OSError) — the real append must swallow it.
    log.path.parent.mkdir(parents=True, exist_ok=True)
    log.path.mkdir()
    log.append(AuditRecord(action="add-context", outcome=AuditOutcome.APPLIED))  # must not raise
    assert log.read() == []  # nothing written, nothing read — degraded cleanly


@pytest.mark.unit
def test_audit_write_failure_does_not_break_apply(tmp_path: Path) -> None:
    """An unwritable audit log must not break an apply that otherwise lands.

    The driver appends to its audit log best-effort; if that write fails (here,
    the audit path is occupied by a directory so opening for append raises), the
    apply still succeeds and writes its files — audit can never gate an apply.
    """
    root = _make_project(tmp_path / "proj")
    # Occupy the audit-log path with a directory so the real append fails.
    audit_path = root / ".mdk" / AUDIT_LOG_NAME
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.mkdir()

    outcome = _driver(root).apply(
        "add-context", {"agent": "greeter", "name": "x", "body": "# X\n"}, fast_mode=True
    )
    assert outcome.result is not None  # apply landed despite the audit write failure
    assert (root / "agents" / "greeter" / "contexts" / "x.md").is_file()


# ---------------------------------------------------------------------------
# Corrupt / missing audit log degrades gracefully
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_audit_log_reads_empty(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    assert _driver(root).audit_records() == []


@pytest.mark.unit
def test_corrupt_audit_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A garbage line is skipped (with a warning); valid lines still read."""
    root = _make_project(tmp_path / "proj")
    log = AuditLog(root)
    log.append(AuditRecord(action="add-context", outcome=AuditOutcome.APPLIED))
    # Corrupt the file: inject a non-JSON line between valid records.
    path = _audit_path(root)
    path.write_text(path.read_text() + "}{ this is not json\n")
    log.append(AuditRecord(action="set-model", outcome=AuditOutcome.APPLIED))

    records = log.read()  # must not raise
    assert [r.action for r in records] == ["add-context", "set-model"]


# ---------------------------------------------------------------------------
# Replay re-drives through the confirm-gated driver (not a raw re-write)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replayable_keeps_only_applied(tmp_path: Path) -> None:
    records = [
        AuditRecord(action="a", outcome=AuditOutcome.APPLIED),
        AuditRecord(action="b", outcome=AuditOutcome.SKIPPED),
        AuditRecord(action="c", outcome=AuditOutcome.REVERTED),
        AuditRecord(action="d", outcome=AuditOutcome.UNDONE),
        AuditRecord(action="e", outcome=AuditOutcome.APPLIED),
    ]
    assert [r.action for r in replayable(records)] == ["a", "e"]


@pytest.mark.unit
def test_replay_routes_through_driver_apply_not_raw_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay MUST go through driver.apply (the confirm-gated spine), not a raw write."""
    root = _make_project(tmp_path / "proj")
    records = [
        AuditRecord(
            action="add-context",
            agent="greeter",
            args={"agent": "greeter", "name": "replayed", "body": "# R\n"},
            outcome=AuditOutcome.APPLIED,
        )
    ]
    driver = _driver(root)
    calls: list[str] = []
    real_apply = driver.apply

    def _spy_apply(name: str, args: dict, **kw: object):  # type: ignore[no-untyped-def]
        calls.append(name)
        return real_apply(name, args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(driver, "apply", _spy_apply)
    steps = replay_records(driver, records, fast_mode=True)

    assert calls == ["add-context"]  # routed through driver.apply
    assert steps[0].applied
    # The replayed edit actually landed via the catalog driver.
    assert (root / "agents" / "greeter" / "contexts" / "replayed.md").is_file()


@pytest.mark.unit
def test_replay_confirm_gate_can_decline(tmp_path: Path) -> None:
    """A confirm callback that declines a step skips it (no write)."""
    root = _make_project(tmp_path / "proj")
    records = [
        AuditRecord(
            action="add-context",
            agent="greeter",
            args={"agent": "greeter", "name": "gated", "body": "# G\n"},
            outcome=AuditOutcome.APPLIED,
        )
    ]
    seen: list[str] = []

    def _decline(record: object, _plan: object) -> bool:
        seen.append(getattr(record, "action", "?"))
        return False

    steps = replay_records(_driver(root), records, confirm=_decline)
    assert seen == ["add-context"]  # the gate was consulted
    assert steps[0].skipped
    assert not (root / "agents" / "greeter" / "contexts" / "gated.md").exists()


@pytest.mark.unit
def test_replay_is_a_no_op_when_nothing_applied(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    records = [AuditRecord(action="add-context", outcome=AuditOutcome.SKIPPED)]
    assert replay_records(_driver(root), records, fast_mode=True) == []


@pytest.mark.unit
def test_replay_round_trips_a_recorded_session(tmp_path: Path) -> None:
    """Apply → undo to a clean tree → replay the audit log → the edit is back."""
    root = _make_project(tmp_path / "proj")
    driver = _driver(root)
    driver.apply(
        "add-context", {"agent": "greeter", "name": "rt", "body": "# RT\n"}, fast_mode=True
    )
    ctx = root / "agents" / "greeter" / "contexts" / "rt.md"
    assert ctx.is_file()

    driver.undo()
    assert not ctx.exists()  # back to clean

    # Replay the recorded (applied) sequence through the same driver.
    records = driver.audit_records()
    steps = replay_records(driver, records, fast_mode=True)
    assert any(s.applied for s in steps)
    assert ctx.is_file()  # the edit is back


# ---------------------------------------------------------------------------
# CLI surfaces: `mdk authoring audit` / `mdk authoring replay`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_audit_empty(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    result = runner.invoke(app, ["authoring", "audit", "-p", str(root)])
    assert result.exit_code == 0
    assert "no authoring audit records" in result.stdout


@pytest.mark.unit
def test_cli_audit_lists_records_text_and_json(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    _driver(root).apply(
        "add-context", {"agent": "greeter", "name": "tone", "body": "# Tone\n"}, fast_mode=True
    )

    text = runner.invoke(app, ["authoring", "audit", "-p", str(root)])
    assert text.exit_code == 0
    assert "add-context" in text.stdout
    assert "applied" in text.stdout

    js = runner.invoke(app, ["authoring", "audit", "-p", str(root), "-o", "json"])
    assert js.exit_code == 0
    payload = json.loads(js.stdout)
    assert payload[0]["action"] == "add-context"
    assert payload[0]["outcome"] == "applied"


@pytest.mark.unit
def test_cli_replay_reapplies_recorded_sequence(tmp_path: Path) -> None:
    """`mdk authoring replay --fast` re-applies the recorded applied actions."""
    root = _make_project(tmp_path / "proj")
    driver = _driver(root)
    driver.apply(
        "add-context", {"agent": "greeter", "name": "rep", "body": "# Rep\n"}, fast_mode=True
    )
    driver.undo()
    ctx = root / "agents" / "greeter" / "contexts" / "rep.md"
    assert not ctx.exists()

    result = runner.invoke(app, ["authoring", "replay", "-p", str(root), "--fast"])
    assert result.exit_code == 0
    assert ctx.is_file()  # replayed through the driver
    assert "applied" in result.stdout


@pytest.mark.unit
def test_cli_replay_nothing_to_replay(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    result = runner.invoke(app, ["authoring", "replay", "-p", str(root), "--fast"])
    assert result.exit_code == 0
    assert "nothing to replay" in result.stdout


# ---------------------------------------------------------------------------
# `mdk dev` view-audit action + menu wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_actions_menu_maps_v_to_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc.Prompt, "ask", staticmethod(lambda *a, **k: "v"))
    assert dc._actions_menu() == "audit"


@pytest.mark.unit
def test_dev_audit_action_reads_log_without_crashing(tmp_path: Path) -> None:
    """The dev view-audit action reads the driver's log + reports session cost."""
    root = _make_project(tmp_path / "proj")
    agent_dir = root / "agents" / "greeter"
    _driver(root).apply(
        "add-context", {"agent": "greeter", "name": "tone", "body": "# Tone\n"}, fast_mode=True
    )
    tracker = SessionCostTracker(budget=CostBudget(cap_usd=1.0))
    # Must not raise — pure read.
    dc._audit_action(agent_dir, tracker=tracker)


def test_dev_help_advertises_budget_flag() -> None:
    result = runner.invoke(app, ["dev", "--help"])
    assert result.exit_code == 0
    assert "--budget" in result.stdout
