"""Tests for the ``policy.yaml`` / ``movate.yaml`` loader precedence.

Part of the MDK rename (Sprint A). ``policy.yaml`` is the canonical
project-config file name going forward; ``movate.yaml`` stays as a
transitional alias with a deprecation warning. Both must keep working
through v1.x.

These tests cover three states:
  1. Neither file present — defaults.
  2. Only ``movate.yaml`` present — load it + warn.
  3. Only ``policy.yaml`` present — load it, no warn.
  4. Both files present — ``policy.yaml`` wins, no warn.
  5. Explicit ``path=`` override — honor it regardless of file names.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import movate.core.config as cfg_mod
from movate.core.config import ProjectConfig, load_project_config


@pytest.fixture(autouse=True)
def _reset_deprecation_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the one-shot deprecation warning re-armed.

    Without this, the first test to trigger the warning would fire it
    and every subsequent test would see ``_LEGACY_WARN_FIRED == True``,
    making the "no-warn" assertions tautological-pass.
    """
    monkeypatch.setattr(cfg_mod, "_LEGACY_WARN_FIRED", False)
    # PR #85 added a second one-shot flag for the policy.yaml → project.yaml
    # deprecation. Reset that one too so per-test warning assertions hold.
    if hasattr(cfg_mod, "_POLICY_LEGACY_WARN_FIRED"):
        monkeypatch.setattr(cfg_mod, "_POLICY_LEGACY_WARN_FIRED", False)


@pytest.fixture
def in_empty_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Switch cwd to an empty temp dir for the test, then restore."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Lookup precedence
# ---------------------------------------------------------------------------


def test_neither_file_returns_defaults(in_empty_dir: Path) -> None:
    cfg = load_project_config()
    assert isinstance(cfg, ProjectConfig)
    assert cfg.policy.allowed_providers == []


def test_only_movate_yaml_loads_with_deprecation_warning(
    in_empty_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (in_empty_dir / "movate.yaml").write_text("policy:\n  allowed_providers: [legacy]\n")
    caplog.set_level(logging.WARNING)
    cfg = load_project_config()
    assert cfg.policy.allowed_providers == ["legacy"]
    assert "movate.yaml is deprecated" in caplog.text
    assert "project.yaml" in caplog.text


def test_only_policy_yaml_loads_without_warning(
    in_empty_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Post-PR #85 (project.yaml canonical) policy.yaml is itself legacy
    # — it still loads, but with a deprecation warning pointing at
    # project.yaml as the new canonical name.
    (in_empty_dir / "policy.yaml").write_text("policy:\n  allowed_providers: [canonical]\n")
    caplog.set_level(logging.WARNING)
    cfg = load_project_config()
    assert cfg.policy.allowed_providers == ["canonical"]
    assert "policy.yaml is deprecated" in caplog.text
    assert "project.yaml" in caplog.text


def test_both_files_present_policy_yaml_wins(
    in_empty_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Mid-rename state: an operator dropped policy.yaml in but didn't
    delete movate.yaml yet. policy.yaml wins; we warn for policy.yaml
    (post-PR #85 it's itself legacy) but the movate.yaml warning is
    silent because we never read that file."""
    (in_empty_dir / "movate.yaml").write_text("policy:\n  allowed_providers: [legacy]\n")
    (in_empty_dir / "policy.yaml").write_text("policy:\n  allowed_providers: [canonical]\n")
    caplog.set_level(logging.WARNING)
    cfg = load_project_config()
    assert cfg.policy.allowed_providers == ["canonical"]
    # policy.yaml warning fires (it's the loaded file).
    assert "policy.yaml is deprecated" in caplog.text
    # movate.yaml warning does NOT fire (we never opened that file).
    assert "movate.yaml is deprecated" not in caplog.text


# ---------------------------------------------------------------------------
# Explicit path overrides
# ---------------------------------------------------------------------------


def test_explicit_path_overrides_lookup_precedence(
    in_empty_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When a path is passed in, it's used as-is — no lookup, no warning,
    regardless of filename. Some tools point at config files in other
    locations (e.g. mdk inside a monorepo subdir)."""
    custom = in_empty_dir / "custom-config.yaml"
    custom.write_text("policy:\n  allowed_providers: [custom]\n")
    cfg = load_project_config(path=custom)
    assert cfg.policy.allowed_providers == ["custom"]
    captured = capsys.readouterr()
    assert "deprecated" not in captured.err


def test_explicit_path_to_missing_file_returns_defaults(
    in_empty_dir: Path,
) -> None:
    cfg = load_project_config(path=in_empty_dir / "does-not-exist.yaml")
    assert cfg.policy.allowed_providers == []


def test_explicit_path_to_movate_yaml_does_not_trigger_deprecation(
    in_empty_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Explicit ``path=`` means the operator KNOWS what file they're
    loading — no warning needed. The warning is only for the
    auto-discovery path where the operator might not realize they're
    relying on the legacy name."""
    legacy = in_empty_dir / "movate.yaml"
    legacy.write_text("policy:\n  allowed_providers: [via-path]\n")
    cfg = load_project_config(path=legacy)
    assert cfg.policy.allowed_providers == ["via-path"]
    captured = capsys.readouterr()
    assert "deprecated" not in captured.err


# ---------------------------------------------------------------------------
# One-shot warning behavior
# ---------------------------------------------------------------------------


def test_deprecation_warning_fires_once_per_process(
    in_empty_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Single CLI invocation may load the config multiple times (validate
    + run + deploy all call load_project_config independently). We only
    want the operator to see one deprecation line, not three."""
    (in_empty_dir / "movate.yaml").write_text("policy:\n  allowed_providers: [a]\n")
    caplog.set_level(logging.WARNING)
    load_project_config()
    load_project_config()
    load_project_config()
    # Count occurrences of the marker phrase across log records.
    assert caplog.text.count("movate.yaml is deprecated") == 1
