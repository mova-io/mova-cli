"""Tests for the ``MDK_*`` ↔ ``MOVATE_*`` env-var aliasing.

Part of the MDK rename (Sprint A). The aliasing runs once at CLI
startup; we verify it bridges in both directions, handles precedence
correctly when both prefixes are set, and emits a one-shot deprecation
warning for legacy usage.
"""

from __future__ import annotations

import os

import pytest

import movate.cli._env_aliases as env_aliases_mod
from movate.cli._env_aliases import sync_env_aliases


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-arm the one-shot warning for every test and scrub any
    MDK_/MOVATE_ env vars that leaked in from the host shell."""
    monkeypatch.setattr(env_aliases_mod, "_WARN_FIRED", False)
    # Remove any test-stale or shell-set vars that would confuse assertions.
    for key in list(os.environ.keys()):
        if key.startswith(("MDK_", "MOVATE_")):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Bridge in both directions
# ---------------------------------------------------------------------------


def test_canonical_mdk_var_copied_down_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDK_X=value`` should also surface as ``MOVATE_X=value`` so
    existing read sites (every ``os.environ.get("MOVATE_X")``) work."""
    monkeypatch.setenv("MDK_TRACER", "stdout")
    sync_env_aliases()
    assert os.environ["MDK_TRACER"] == "stdout"
    assert os.environ["MOVATE_TRACER"] == "stdout"


def test_legacy_movate_var_copied_up_to_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MOVATE_X=value`` should also surface as ``MDK_X=value`` so new
    code reading the canonical name works on legacy configs."""
    monkeypatch.setenv("MOVATE_DB_URL", "postgresql://...")
    sync_env_aliases()
    assert os.environ["MDK_DB_URL"] == "postgresql://..."
    assert os.environ["MOVATE_DB_URL"] == "postgresql://..."


def test_both_set_canonical_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the operator sets both, the canonical (MDK_*) value wins —
    they clearly intended the new name. No silent overwrite."""
    monkeypatch.setenv("MDK_TRACER", "canonical-value")
    monkeypatch.setenv("MOVATE_TRACER", "legacy-value")
    sync_env_aliases()
    # The legacy var is left as-is (we don't overwrite an explicit value);
    # the canonical var also stays as set.
    assert os.environ["MDK_TRACER"] == "canonical-value"
    assert os.environ["MOVATE_TRACER"] == "legacy-value"


def test_no_movate_or_mdk_vars_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env vars to bridge → no-op. No env mutation, no warning."""
    before = dict(os.environ)
    sync_env_aliases()
    after = dict(os.environ)
    assert before == after


# ---------------------------------------------------------------------------
# Deprecation warning
# ---------------------------------------------------------------------------


def test_warning_fires_when_legacy_var_in_use(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MOVATE_TRACER", "stdout")
    sync_env_aliases()
    captured = capsys.readouterr()
    assert "MOVATE_* env vars are deprecated" in captured.err
    assert "MOVATE_TRACER" in captured.err


def test_no_warning_when_only_canonical_in_use(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Operator already migrated to MDK_*. No legacy in use → no warning."""
    monkeypatch.setenv("MDK_TRACER", "stdout")
    sync_env_aliases()
    captured = capsys.readouterr()
    assert "deprecated" not in captured.err


def test_no_warning_when_both_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the operator has both, they probably know — they're mid-migration
    or running in a transitional CI setup. Skip the warning to avoid noise."""
    monkeypatch.setenv("MDK_TRACER", "x")
    monkeypatch.setenv("MOVATE_TRACER", "y")
    sync_env_aliases()
    captured = capsys.readouterr()
    assert "deprecated" not in captured.err


def test_warning_fires_once_per_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """sync_env_aliases is idempotent + the warning is one-shot. Multiple
    calls should only emit one deprecation line."""
    monkeypatch.setenv("MOVATE_DB_URL", "x")
    sync_env_aliases()
    sync_env_aliases()
    sync_env_aliases()
    captured = capsys.readouterr()
    assert captured.err.count("MOVATE_* env vars are deprecated") == 1


def test_warning_lists_up_to_three_vars(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """For 1-3 legacy vars, show them all. For more, show 3 + a count."""
    for var in ("MOVATE_TRACER", "MOVATE_DB", "MOVATE_AGENTS_PATH", "MOVATE_TARGET"):
        monkeypatch.setenv(var, "x")
    sync_env_aliases()
    captured = capsys.readouterr()
    # First three (alphabetical) listed; the fourth condenses into "+1 more".
    assert "MOVATE_AGENTS_PATH" in captured.err
    assert "MOVATE_DB" in captured.err
    assert "MOVATE_TARGET" in captured.err
    assert "+1 more" in captured.err


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_no_double_propagation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling sync twice with the same env should produce the same state
    as calling it once. No values get clobbered."""
    monkeypatch.setenv("MOVATE_FOO", "legacy")
    monkeypatch.setenv("MDK_BAR", "canonical")
    sync_env_aliases()
    sync_env_aliases()
    assert os.environ["MOVATE_FOO"] == "legacy"
    assert os.environ["MDK_FOO"] == "legacy"
    assert os.environ["MDK_BAR"] == "canonical"
    assert os.environ["MOVATE_BAR"] == "canonical"
