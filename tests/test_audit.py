"""Sprint N Day 8-10 — `mdk audit` tests.

Three layers:

1. **Scanners** — each scanner is a pure function over an agent dir.
   Test each independently: catch the failure mode it targets,
   ignore everything else.
2. **Orchestrator** — :func:`audit_current` walks every agent + runs
   every scanner; :func:`audit_snapshot` does the same for a captured
   snapshot's files/ directory.
3. **CLI** — `mdk audit current` + `mdk audit <hash>` render the
   report, --strict promotes warnings to errors, --json emits
   parseable output, --category filters scanners.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.audit import (
    AuditReport,
    Severity,
    audit_current,
    audit_snapshot,
)
from movate.audit.scanners import (
    scan_empty_prompt,
    scan_exposed_secrets,
    scan_missing_description,
    scan_missing_evals,
    scan_missing_owner,
    scan_no_test_signal,
)
from movate.cli.main import app
from movate.snapshot import create_snapshot

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers — build agent fixtures that fail / pass each scanner
# ---------------------------------------------------------------------------


def _make_agent(
    root: Path,
    name: str,
    *,
    yaml_body: str | None = None,
    prompt: str = "You are an assistant. Answer in JSON.",
    dataset: str | None = '{"input": {"q": "hi"}, "expected": {"a": "hello"}}',
    examples: bool = False,
) -> Path:
    """Build an agent dir under root/agents/<name>/.

    Each kwarg flips one auditable property:
      * ``yaml_body=None`` uses a minimal valid agent.yaml
      * ``prompt=""`` triggers `empty-prompt`
      * ``dataset=None`` triggers `missing-evals`
      * ``examples=False`` (default) + ``dataset=None`` triggers `no-test-signal`
    """
    agent_dir = root / "agents" / name
    agent_dir.mkdir(parents=True)
    if yaml_body is None:
        yaml_body = (
            f"api_version: movate/v1\n"
            f"kind: Agent\n"
            f"name: {name}\n"
            f"version: 0.1.0\n"
            f"description: 'test agent {name}'\n"
            f"owner: 'test-team@example.com'\n"
            f"model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
            f"prompt: ./prompt.md\n"
            f"schema:\n  input: {{ q: string }}\n  output: {{ a: string }}\n"
            + (
                "examples:\n  - input: { q: 'hi' }\n    output: { a: 'hello' }\n"
                if examples
                else ""
            )
        )
    (agent_dir / "agent.yaml").write_text(yaml_body)
    (agent_dir / "prompt.md").write_text(prompt)
    if dataset is not None:
        evals_dir = agent_dir / "evals"
        evals_dir.mkdir()
        (evals_dir / "dataset.jsonl").write_text(dataset + "\n")
    return agent_dir


def _scaffold_project(root: Path) -> Path:
    """Empty project shell — agents added per-test."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("# test\n")
    return root


# ---------------------------------------------------------------------------
# Scanner: missing-evals
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScanMissingEvals:
    def test_clean_when_dataset_present(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a")
        assert scan_missing_evals(agent_dir, "a") == []

    def test_flags_when_dataset_absent(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a", dataset=None)
        findings = scan_missing_evals(agent_dir, "a")
        assert len(findings) == 1
        assert findings[0].severity == Severity.ERROR
        assert findings[0].category == "missing-evals"
        assert "evals/dataset.jsonl" in findings[0].message


# ---------------------------------------------------------------------------
# Scanner: missing-description
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScanMissingDescription:
    def test_clean_when_description_present(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a")
        assert scan_missing_description(agent_dir, "a") == []

    def test_warns_when_description_empty(self, tmp_path: Path) -> None:
        yaml_body = (
            "api_version: movate/v1\nkind: Agent\nname: a\nversion: 0.1.0\n"
            "description: ''\nowner: 't'\n"
            "model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n  input: { q: string }\n  output: { a: string }\n"
        )
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a", yaml_body=yaml_body)
        findings = scan_missing_description(agent_dir, "a")
        assert len(findings) == 1
        assert findings[0].severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Scanner: missing-owner
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScanMissingOwner:
    def test_warns_when_owner_empty(self, tmp_path: Path) -> None:
        yaml_body = (
            "api_version: movate/v1\nkind: Agent\nname: a\nversion: 0.1.0\n"
            "description: 'd'\nowner: ''\n"
            "model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n  input: { q: string }\n  output: { a: string }\n"
        )
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a", yaml_body=yaml_body)
        findings = scan_missing_owner(agent_dir, "a")
        assert len(findings) == 1
        assert findings[0].category == "missing-owner"


# ---------------------------------------------------------------------------
# Scanner: exposed-secret
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScanExposedSecrets:
    def test_clean_when_no_secrets(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a")
        assert scan_exposed_secrets(agent_dir, "a") == []

    def test_flags_openai_key_in_prompt(self, tmp_path: Path) -> None:
        """The classic mistake — paste a key into prompt.md for testing."""
        # Use a SECRET-shaped string that LOOKS leaked. (Fake — never
        # commit a real key.)
        leaked = "sk-fakeproj01234567890abcdefghij"
        agent_dir = _make_agent(
            _scaffold_project(tmp_path / "p"),
            "a",
            prompt=f"Use this API key for tool calls: {leaked}\nThen answer in JSON.",
        )
        findings = scan_exposed_secrets(agent_dir, "a")
        assert len(findings) == 1
        assert findings[0].severity == Severity.ERROR
        # Output truncates the secret to a preview — never echo it fully
        assert leaked not in findings[0].message
        assert "..." in findings[0].message

    def test_flags_aws_access_key(self, tmp_path: Path) -> None:
        """AWS access key pattern: AKIA + 16 uppercase alphanum."""
        agent_dir = _make_agent(
            _scaffold_project(tmp_path / "p"),
            "a",
            prompt="Reference: AKIA0123456789EXAMPLE for s3 access.",
        )
        findings = scan_exposed_secrets(agent_dir, "a")
        assert any(f.category == "exposed-secret" for f in findings)

    def test_flags_github_token(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(
            _scaffold_project(tmp_path / "p"),
            "a",
            prompt="Auth: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa for the API.",
        )
        findings = scan_exposed_secrets(agent_dir, "a")
        assert any(f.category == "exposed-secret" for f in findings)


# ---------------------------------------------------------------------------
# Scanner: empty-prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScanEmptyPrompt:
    def test_flags_empty_prompt(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a", prompt="   ")
        findings = scan_empty_prompt(agent_dir, "a")
        assert len(findings) == 1
        assert findings[0].severity == Severity.ERROR


# ---------------------------------------------------------------------------
# Scanner: no-test-signal
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScanNoTestSignal:
    def test_flags_when_neither_examples_nor_dataset(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(
            _scaffold_project(tmp_path / "p"), "a", dataset=None, examples=False
        )
        findings = scan_no_test_signal(agent_dir, "a")
        assert len(findings) == 1
        assert findings[0].severity == Severity.ERROR

    def test_clean_with_examples_only(self, tmp_path: Path) -> None:
        """examples without a dataset is fine — there's still some test
        signal. `missing-evals` will still fire (separate scanner), but
        no-test-signal won't."""
        agent_dir = _make_agent(
            _scaffold_project(tmp_path / "p"),
            "a",
            dataset=None,
            examples=True,
        )
        assert scan_no_test_signal(agent_dir, "a") == []

    def test_clean_with_dataset_only(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(_scaffold_project(tmp_path / "p"), "a", examples=False)
        assert scan_no_test_signal(agent_dir, "a") == []


# ---------------------------------------------------------------------------
# Orchestrator: audit_current
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuditCurrent:
    def test_clean_project_has_no_findings(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p")
        _make_agent(project, "good")
        report = audit_current(project)
        assert report.scanned_agents == 1
        assert report.is_clean
        assert not report.errors
        assert not report.warnings

    def test_aggregates_findings_across_agents(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p")
        _make_agent(project, "ok")
        _make_agent(project, "no-dataset", dataset=None)
        report = audit_current(project)
        assert report.scanned_agents == 2
        # Errors include missing-evals from no-dataset agent
        assert any(
            f.target == "no-dataset" and f.category == "missing-evals" for f in report.errors
        )

    def test_category_filter_runs_only_named_scanners(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p")
        _make_agent(project, "bad", dataset=None, examples=False)
        # Only run missing-evals; no-test-signal would also fire normally
        report = audit_current(project, categories=["missing-evals"])
        # missing-evals fired
        assert any(f.category == "missing-evals" for f in report.findings)
        # no-test-signal didn't (filtered out)
        assert not any(f.category == "no-test-signal" for f in report.findings)


# ---------------------------------------------------------------------------
# Orchestrator: audit_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuditSnapshot:
    def test_audits_files_inside_a_snapshot(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p")
        _make_agent(project, "captured", dataset=None)
        snap = create_snapshot(project_root=project, description="t")
        short = snap.hash.removeprefix("sha256:")[:8]

        # Now mutate the live state — snapshot audit should still
        # see the original (no-dataset) state.
        evals_dir = project / "agents" / "captured" / "evals"
        evals_dir.mkdir(exist_ok=True)
        (evals_dir / "dataset.jsonl").write_text('{"input":{"q":"x"},"expected":{}}\n')

        # Live audit: clean (dataset added)
        live_report = audit_current(project)
        live_evals_errors = [f for f in live_report.errors if f.category == "missing-evals"]
        assert not live_evals_errors

        # Snapshot audit: should still flag missing dataset
        snap_report = audit_snapshot(project, short)
        snap_evals_errors = [f for f in snap_report.errors if f.category == "missing-evals"]
        assert len(snap_evals_errors) == 1


# ---------------------------------------------------------------------------
# AuditReport — gate semantics + JSON
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuditReport:
    def test_gate_fails_on_errors_always(self) -> None:
        from movate.audit.report import Finding  # noqa: PLC0415

        report = AuditReport(
            findings=(
                Finding(
                    category="x",
                    severity=Severity.ERROR,
                    target="t",
                    message="bad",
                ),
            ),
            scanned_agents=1,
        )
        assert report.gate_fails(strict=False)
        assert report.gate_fails(strict=True)

    def test_gate_fails_on_warnings_only_with_strict(self) -> None:
        from movate.audit.report import Finding  # noqa: PLC0415

        report = AuditReport(
            findings=(
                Finding(
                    category="x",
                    severity=Severity.WARNING,
                    target="t",
                    message="meh",
                ),
            ),
            scanned_agents=1,
        )
        assert not report.gate_fails(strict=False)
        assert report.gate_fails(strict=True)

    def test_clean_report_passes_both_modes(self) -> None:
        report = AuditReport(findings=(), scanned_agents=1)
        assert report.is_clean
        assert not report.gate_fails(strict=False)
        assert not report.gate_fails(strict=True)

    def test_to_json_includes_summary_and_findings(self) -> None:
        from movate.audit.report import Finding  # noqa: PLC0415

        report = AuditReport(
            findings=(
                Finding(
                    category="x",
                    severity=Severity.ERROR,
                    target="agent-a",
                    message="bad",
                    hint="fix it",
                ),
            ),
            scanned_agents=2,
        )
        payload = json.loads(report.to_json())
        assert payload["scanned_agents"] == 2
        assert payload["summary"]["errors"] == 1
        assert payload["summary"]["is_clean"] is False
        assert payload["findings"][0]["category"] == "x"
        assert payload["findings"][0]["hint"] == "fix it"


# ---------------------------------------------------------------------------
# CLI — `mdk audit`
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = _scaffold_project(tmp_path / "p")
    _make_agent(project, "clean-agent")
    _make_agent(project, "no-dataset", dataset=None)
    monkeypatch.chdir(project)
    return project


@pytest.mark.unit
def test_cli_audit_clean_project_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _scaffold_project(tmp_path / "p")
    _make_agent(project, "clean")
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (
        "clean" in result.stdout.lower()
        or "no production-readiness issues" in result.stdout.lower()
    )


@pytest.mark.unit
def test_cli_audit_with_errors_exits_one(project_with_agents: Path) -> None:
    """no-dataset agent triggers missing-evals (error)."""
    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 1
    # Render shows the finding
    assert "missing-evals" in result.stdout
    assert "no-dataset" in result.stdout


@pytest.mark.unit
def test_cli_audit_strict_promotes_warnings_to_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent with no description = warning (not error). With --strict
    it should fail."""
    yaml_body = (
        "api_version: movate/v1\nkind: Agent\nname: warned\nversion: 0.1.0\n"
        "description: ''\nowner: 't'\n"
        "model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n  input: { q: string }\n  output: { a: string }\n"
    )
    project = _scaffold_project(tmp_path / "p")
    _make_agent(project, "warned", yaml_body=yaml_body)
    monkeypatch.chdir(project)

    # Default mode: warnings don't fail
    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 0

    # Strict: warnings fail
    result_strict = runner.invoke(app, ["audit", "current", "--strict"])
    assert result_strict.exit_code == 1


@pytest.mark.unit
def test_cli_audit_json_output_is_parseable(project_with_agents: Path) -> None:
    result = runner.invoke(app, ["audit", "current", "--json"])
    # exit 1 because there's an error in the fixture
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["scanned_agents"] == 2
    assert "findings" in payload
    assert payload["summary"]["errors"] >= 1


@pytest.mark.unit
def test_cli_audit_category_filter(project_with_agents: Path) -> None:
    """`--category missing-owner` should ignore the missing-evals error."""
    result = runner.invoke(app, ["audit", "current", "--category", "missing-owner"])
    # No missing-owner failures in the fixture (both agents have owners)
    assert result.exit_code == 0
    assert "missing-evals" not in result.stdout


@pytest.mark.unit
def test_cli_audit_unknown_category_exits_two(project_with_agents: Path) -> None:
    """Typo in --category surfaces clean error, not empty result set."""
    result = runner.invoke(app, ["audit", "current", "--category", "nope"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "unknown scanner category" in combined.lower()


@pytest.mark.unit
def test_cli_audit_snapshot_target(
    project_with_agents: Path,
) -> None:
    """`mdk audit <hash>` runs the audit against a snapshot."""
    snap = create_snapshot(project_root=project_with_agents, description="for audit")
    short = snap.hash.removeprefix("sha256:")[:8]

    result = runner.invoke(app, ["audit", short])
    # The fixture's no-dataset agent is in the snapshot too → exit 1
    assert result.exit_code == 1
    assert "no-dataset" in result.stdout


@pytest.mark.unit
def test_cli_audit_unknown_snapshot_exits_one(project_with_agents: Path) -> None:
    result = runner.invoke(app, ["audit", "ffffffff"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "no snapshot" in combined.lower()
