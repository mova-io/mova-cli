"""Agent registry — scan filesystem, load valid agents, skip invalid.

Robustness invariant: a single broken agent.yaml MUST NOT prevent the
registry from loading other agents. The runtime should boot with
whatever's valid and surface skipped entries in the operator log.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.runtime.registry import scan_agents

runner = CliRunner(mix_stderr=False)


def _scaffold(parent: Path, name: str, template: str = "default") -> Path:
    """Use ``movate init`` to produce a real, valid agent on disk.

    Reusing init keeps the fixture honest — we test the same shape
    the registry will see in production.
    """
    result = runner.invoke(app, ["init", "--bare", name, "-t", template, "--target", str(parent)])
    assert result.exit_code == 0, result.stdout
    return parent / name


# ---------------------------------------------------------------------------
# Edge cases — empty / missing / invalid roots
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scan_returns_empty_for_missing_root(tmp_path: Path) -> None:
    """No directory at all → empty list, no exception."""
    bundles = scan_agents(tmp_path / "does-not-exist")
    assert bundles == []


@pytest.mark.unit
def test_scan_returns_empty_for_file_root(tmp_path: Path) -> None:
    """Root is a file, not a dir → empty list (defensive)."""
    f = tmp_path / "not-a-dir"
    f.write_text("hi")
    assert scan_agents(f) == []


@pytest.mark.unit
def test_scan_returns_empty_for_empty_dir(tmp_path: Path) -> None:
    """Dir exists but contains nothing → empty list."""
    assert scan_agents(tmp_path) == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scan_loads_valid_agents(tmp_path: Path) -> None:
    _scaffold(tmp_path, "alpha")
    _scaffold(tmp_path, "bravo")

    bundles = scan_agents(tmp_path)
    names = [b.spec.name for b in bundles]
    assert names == ["alpha", "bravo"]  # sorted ascending


@pytest.mark.unit
def test_scan_walks_only_one_level_deep(tmp_path: Path) -> None:
    """Nested test fixtures and dev scratch dirs shouldn't pollute the
    catalog. Only top-level subdirectories with agent.yaml count."""
    _scaffold(tmp_path, "real-agent")

    # Plant a nested agent inside the real one — this should NOT be
    # discovered (loader can't handle it; we don't want to either).
    nested = tmp_path / "real-agent" / "nested"
    nested.mkdir()
    (nested / "agent.yaml").write_text("api_version: movate/v1\n")

    bundles = scan_agents(tmp_path)
    assert {b.spec.name for b in bundles} == {"real-agent"}


@pytest.mark.unit
def test_scan_skips_dirs_without_agent_yaml(tmp_path: Path) -> None:
    """Sibling directories without agent.yaml — eval datasets, scratch
    workspaces, .git — should be silently skipped."""
    _scaffold(tmp_path, "valid")

    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "shared.json").write_text("{}")
    (tmp_path / ".git").mkdir()  # we don't recurse, so this is fine

    bundles = scan_agents(tmp_path)
    assert [b.spec.name for b in bundles] == ["valid"]


# ---------------------------------------------------------------------------
# Partial failure — broken agent doesn't blackhole the catalog
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scan_skips_invalid_yaml_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _scaffold(tmp_path, "good")

    # Synthesize a broken sibling: directory + agent.yaml that's malformed.
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "agent.yaml").write_text("this is: not: valid: yaml: : :")

    with caplog.at_level(logging.WARNING, logger="movate.runtime.registry"):
        bundles = scan_agents(tmp_path)

    assert [b.spec.name for b in bundles] == ["good"]
    assert any("agent_load_skipped" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_scan_skips_unknown_api_version(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An agent.yaml with a wrong api_version is a structured-but-invalid
    case — must surface in logs and be skipped, not blow up startup."""
    _scaffold(tmp_path, "good")

    bad = tmp_path / "v2-future"
    bad.mkdir()
    (bad / "agent.yaml").write_text(
        "api_version: movate/v2\nkind: Agent\nname: future\nversion: 0.1.0\n"
    )

    with caplog.at_level(logging.WARNING, logger="movate.runtime.registry"):
        bundles = scan_agents(tmp_path)

    assert [b.spec.name for b in bundles] == ["good"]
    assert any("v2-future" in rec.message for rec in caplog.records)
