"""Tests for the ``MDK_*`` ↔ ``MOVATE_*`` env-var aliasing.

Part of the MDK rename (Sprint A). The aliasing runs once at CLI
startup; we verify it bridges in both directions, handles precedence
correctly when both prefixes are set, and emits a one-shot deprecation
warning for legacy usage.
"""

from __future__ import annotations

import os

import pytest

# The implementation lives in movate.core.env_aliases (so the runtime can
# call it without importing cli — see docs/architecture-principles.md); the
# one-shot _WARN_FIRED flag is owned there, so the reset fixture re-arms it on
# that module. We import the public entrypoint via the legacy cli shim to keep
# that compat path (movate.cli.main imports from here) covered.
import movate.core.env_aliases as env_aliases_mod
from movate.cli._env_aliases import sync_env_aliases


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-arm the one-shot warning for every test, scrub any
    MDK_/MOVATE_ env vars that leaked in from the host shell, AND
    scrub anything sync_env_aliases() wrote directly to os.environ
    before the next test (in this file or another) runs.

    ``sync_env_aliases()`` uses ``os.environ[key] = value`` (direct
    assignment) so monkeypatch's setenv-tracking never sees those
    writes. Without an explicit teardown scrub the MDK_/MOVATE_ vars
    a test populates via sync leak into subsequent tests (in
    particular tests/test_quiet_propagation.py, which reads
    ``MDK_TARGET`` via Typer's envvar list)."""
    monkeypatch.setattr(env_aliases_mod, "_WARN_FIRED", False)
    # Pre-test: scrub host-shell leaks via monkeypatch (auto-reverts on teardown).
    for key in list(os.environ.keys()):
        if key.startswith(("MDK_", "MOVATE_")):
            monkeypatch.delenv(key, raising=False)
    yield
    # Post-test: nuke ANY MDK_/MOVATE_ var still in os.environ. Covers
    # the direct-assignment writes sync_env_aliases() does.
    for key in list(os.environ.keys()):
        if key.startswith(("MDK_", "MOVATE_")):
            del os.environ[key]


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


# ---------------------------------------------------------------------------
# Empty-shadow edge case (#67 — the deploy-401 root cause)
#
# In the deployed Azure Container App, MOVATE_DB_URL / MOVATE_SEED_API_KEY
# were PRESENT but set to "" while bicep set the canonical MDK_* names to
# real values. A pure presence check ("other key not in os.environ") left
# the empty legacy var in place, so os.environ.get("MOVATE_DB_URL") readers
# saw "" and fell back to ephemeral SQLite → keys vanished on revision
# recycle → recurring deploy-401. An empty/blank destination must count as
# "needs filling" in BOTH directions.
# ---------------------------------------------------------------------------


def test_canonical_fills_empty_legacy_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    """MDK_DB_URL set, MOVATE_DB_URL="" present → after sync the empty
    legacy shadow is overwritten with the real canonical value."""
    monkeypatch.setenv("MDK_DB_URL", "postgresql://real")
    monkeypatch.setenv("MOVATE_DB_URL", "")
    sync_env_aliases()
    assert os.environ["MDK_DB_URL"] == "postgresql://real"
    assert os.environ["MOVATE_DB_URL"] == "postgresql://real"


def test_canonical_fills_whitespace_only_legacy_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only legacy shadow ("   ") is also treated as unset."""
    monkeypatch.setenv("MDK_SEED_API_KEY", "mvt_real_key")
    monkeypatch.setenv("MOVATE_SEED_API_KEY", "   ")
    sync_env_aliases()
    assert os.environ["MOVATE_SEED_API_KEY"] == "mvt_real_key"


def test_legacy_fills_empty_canonical_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symmetric direction: MOVATE_X set, MDK_X="" present → the empty
    canonical shadow is filled from the legacy value."""
    monkeypatch.setenv("MOVATE_DB_URL", "postgresql://legacy")
    monkeypatch.setenv("MDK_DB_URL", "")
    sync_env_aliases()
    assert os.environ["MDK_DB_URL"] == "postgresql://legacy"
    assert os.environ["MOVATE_DB_URL"] == "postgresql://legacy"


def test_both_set_nonempty_mdk_wins_legacy_unchanged_no_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both set non-empty → MDK_* wins, the (non-empty) legacy var is left
    untouched, and no copy/warning happens (existing semantics preserved)."""
    monkeypatch.setenv("MDK_DB_URL", "postgresql://canonical")
    monkeypatch.setenv("MOVATE_DB_URL", "postgresql://legacy")
    sync_env_aliases()
    assert os.environ["MDK_DB_URL"] == "postgresql://canonical"
    assert os.environ["MOVATE_DB_URL"] == "postgresql://legacy"
    assert "deprecated" not in capsys.readouterr().err


def test_canonical_only_still_bridges_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canonical-only (no legacy var at all) still copies down — the
    empty-shadow fix must not regress the absent-destination path."""
    monkeypatch.setenv("MDK_TRACER", "stdout")
    sync_env_aliases()
    assert os.environ["MOVATE_TRACER"] == "stdout"


def test_legacy_only_still_bridges_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy-only (no canonical var at all) still copies up."""
    monkeypatch.setenv("MOVATE_TRACER", "stdout")
    sync_env_aliases()
    assert os.environ["MDK_TRACER"] == "stdout"


def test_warning_fires_for_bridged_legacy_not_for_empty_shadow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deprecation warning fires once for a genuinely-bridged non-empty
    legacy var (MOVATE_TRACER → MDK_TRACER) but NOT for an empty legacy
    shadow that was overwritten by a canonical value (MOVATE_DB_URL="")."""
    monkeypatch.setenv("MOVATE_TRACER", "stdout")  # genuinely bridged up
    monkeypatch.setenv("MDK_DB_URL", "postgresql://real")
    monkeypatch.setenv("MOVATE_DB_URL", "")  # empty shadow — not "in use"
    sync_env_aliases()
    err = capsys.readouterr().err
    assert "MOVATE_* env vars are deprecated" in err
    assert "MOVATE_TRACER" in err
    # The empty shadow must NOT be listed as a legacy var "in use".
    assert "MOVATE_DB_URL" not in err
