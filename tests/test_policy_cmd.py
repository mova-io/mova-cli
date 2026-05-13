"""Tests for ``mdk policy export | import | diff``.

Three subcommands; each gets unit coverage for the happy path and
the operator-facing error paths. The CLI surface is small (each
command is a handful of lines around the shared serializer +
validator) so the tests focus on:

* deterministic, defaults-stripped output;
* round-trip stability (export → import produces the same on-disk
  content);
* validation rejection of malformed input before any disk write;
* refusal to clobber an existing policy.yaml without ``--force``;
* the diff exit-code contract (0 = same, 1 = drift) so CI can
  gate on policy parity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


_FULL_POLICY = (
    "policy:\n"
    "  allowed_providers: [openai, anthropic]\n"
    "  max_cost_per_run_usd: 0.50\n"
    "runtime:\n"
    "  allowed: [litellm]\n"
)


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp project dir with a populated policy.yaml. Chdir is the
    only side effect — load_project_config looks for policy.yaml in cwd."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text(_FULL_POLICY)
    return tmp_path


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_to_stdout_yaml(project_dir: Path) -> None:
    """Default invocation prints YAML on stdout. Keys are sorted so
    operators get a deterministic diff between runs / environments."""
    result = runner.invoke(app, ["policy", "export"])
    assert result.exit_code == 0
    out = result.stdout
    assert "policy:" in out
    assert "openai" in out
    assert "anthropic" in out
    assert "runtime:" in out


def test_export_strips_defaults(project_dir: Path) -> None:
    """Fields equal to ProjectConfig's defaults are stripped so
    operators only see what they actually set. ``agents_dir`` and
    ``workflows_dir`` are defaults — should NOT appear in the export."""
    result = runner.invoke(app, ["policy", "export"])
    assert "agents_dir" not in result.stdout
    assert "workflows_dir" not in result.stdout


def test_export_json_format(project_dir: Path) -> None:
    result = runner.invoke(app, ["policy", "export", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["policy"]["allowed_providers"] == ["openai", "anthropic"]
    assert data["policy"]["max_cost_per_run_usd"] == 0.50


def test_export_to_file(project_dir: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "exported.yaml"
    result = runner.invoke(app, ["policy", "export", "--output", str(out_path)])
    assert result.exit_code == 0
    assert out_path.exists()
    body = out_path.read_text()
    assert "policy:" in body
    assert "openai" in body


def test_export_inferred_json_from_extension(project_dir: Path, tmp_path: Path) -> None:
    """When ``--output`` ends in ``.json`` and no ``--format`` is set,
    format defaults to JSON. Lets operators pipe a single output flag
    in promotion scripts without specifying the format twice."""
    out_path = tmp_path / "exported.json"
    result = runner.invoke(app, ["policy", "export", "--output", str(out_path)])
    assert result.exit_code == 0
    # Should be valid JSON now, not YAML.
    data = json.loads(out_path.read_text())
    assert "policy" in data


def test_export_rejects_unknown_format(project_dir: Path) -> None:
    result = runner.invoke(app, ["policy", "export", "--format", "toml"])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "unsupported" in combined


def test_export_no_policy_yaml_emits_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No policy.yaml in cwd ⇒ load_project_config returns the
    defaults. With ``exclude_defaults=True``, the export is empty
    (or near-empty). Operator gets a clean "nothing set" signal
    instead of a stack trace."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["policy", "export"])
    assert result.exit_code == 0
    # Empty doc, or just blank/sparse output — definitely no junk.
    assert "allowed_providers" not in result.stdout


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_writes_policy_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "new-policy.yaml"
    src.write_text(_FULL_POLICY)
    result = runner.invoke(app, ["policy", "import", str(src)])
    assert result.exit_code == 0
    body = (tmp_path / "policy.yaml").read_text()
    assert "openai" in body
    assert "max_cost_per_run_usd" in body


def test_import_refuses_overwrite_without_force(project_dir: Path) -> None:
    """Existing policy.yaml + no --force ⇒ refuse and exit 2 so an
    accidental import doesn't clobber a working policy."""
    src = project_dir / "candidate.yaml"
    src.write_text(_FULL_POLICY)
    result = runner.invoke(app, ["policy", "import", str(src)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "already exists" in combined


def test_import_force_overwrites(project_dir: Path) -> None:
    new_policy = (
        "policy:\n"
        "  allowed_providers: [openai]\n"  # narrower than the existing
        "  max_cost_per_run_usd: 1.00\n"
    )
    src = project_dir / "candidate.yaml"
    src.write_text(new_policy)
    result = runner.invoke(app, ["policy", "import", str(src), "--force"])
    assert result.exit_code == 0
    body = (project_dir / "policy.yaml").read_text()
    assert "anthropic" not in body
    assert "max_cost_per_run_usd: 1.0" in body


def test_import_rejects_malformed_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "broken.yaml"
    src.write_text("policy: { [invalid")
    result = runner.invoke(app, ["policy", "import", str(src)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "parse" in combined.lower() or "failed" in combined.lower()


def test_import_rejects_invalid_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A document that parses but doesn't match ProjectConfig is
    rejected before any disk write. The error message includes the
    offending field path."""
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "bad.yaml"
    src.write_text(
        "policy:\n  allowed_providers: not-a-list\n"  # type violation
    )
    result = runner.invoke(app, ["policy", "import", str(src)])
    assert result.exit_code == 2
    # policy.yaml in cwd is untouched (didn't exist; should still not exist).
    assert not (tmp_path / "policy.yaml").exists()


def test_import_rejects_top_level_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "list.yaml"
    src.write_text("- not\n- an\n- object\n")
    result = runner.invoke(app, ["policy", "import", str(src)])
    assert result.exit_code == 2


def test_import_to_custom_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--target`` lets operators stage a policy at a non-standard
    path. Used in monorepos that check in multiple env policies."""
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "new.yaml"
    src.write_text(_FULL_POLICY)
    target = tmp_path / "infra" / "staging-policy.yaml"
    result = runner.invoke(app, ["policy", "import", str(src), "--target", str(target)])
    assert result.exit_code == 0
    assert target.exists()


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_identical_exits_0(project_dir: Path) -> None:
    """A candidate that's identical to active emits a success message
    and exits 0. CI gate friendly."""
    src = project_dir / "identical.yaml"
    src.write_text(_FULL_POLICY)
    result = runner.invoke(app, ["policy", "diff", str(src)])
    assert result.exit_code == 0
    assert "identical" in result.stdout


def test_diff_different_exits_1(project_dir: Path) -> None:
    """Different content ⇒ unified-diff on stdout + exit 1. The
    nonzero exit is the CI gate signal ('your prod policy drifted')."""
    src = project_dir / "diverged.yaml"
    src.write_text(
        "policy:\n"
        "  allowed_providers: [openai]\n"
        "  max_cost_per_run_usd: 1.00\n"
        "runtime:\n"
        "  allowed: [litellm]\n"
    )
    result = runner.invoke(app, ["policy", "diff", str(src)])
    assert result.exit_code == 1
    # Unified diff markers appear on either side.
    assert "---" in result.stdout
    assert "+++" in result.stdout


def test_diff_missing_source_errors(project_dir: Path) -> None:
    result = runner.invoke(app, ["policy", "diff", "/nonexistent/policy.yaml"])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "not found" in combined


def test_diff_rejects_malformed_source(project_dir: Path) -> None:
    src = project_dir / "broken.yaml"
    src.write_text("policy: { [invalid")
    result = runner.invoke(app, ["policy", "diff", str(src)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_export_then_import_is_stable(project_dir: Path, tmp_path: Path) -> None:
    """Round-trip: export the active policy, then import it back to a
    fresh location. The imported file should normalize-match the
    original (modulo whitespace + sort-order)."""
    intermediate = tmp_path / "exported.yaml"
    export_result = runner.invoke(app, ["policy", "export", "--output", str(intermediate)])
    assert export_result.exit_code == 0

    target = tmp_path / "reimport-target.yaml"
    import_result = runner.invoke(
        app, ["policy", "import", str(intermediate), "--target", str(target)]
    )
    assert import_result.exit_code == 0

    # Diff the active policy against the round-tripped target. The
    # imported file's content should match the source's normalized
    # form — diff returns 0.
    diff_result = runner.invoke(app, ["policy", "diff", str(target)])
    assert diff_result.exit_code == 0, diff_result.stdout
