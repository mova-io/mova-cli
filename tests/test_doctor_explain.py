"""Tests for `mdk doctor --explain`.

The --explain flag prints a per-check explanation block beneath the
main doctor table. Each entry has what / why / failure_impact / fix.

These tests assert:
  * --explain flag is wired into the CLI
  * Every check named in the doctor table has a matching explanation
    entry (catches drift when a new check lands but its explanation
    doesn't)
  * The explanation block actually renders for a few representative
    checks
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from movate.cli._doctor_explanations import EXPLANATIONS
from movate.cli.doctor import _PROVIDER_KEYS, _RUNTIME_PROBES, _TRACING_KEYS
from movate.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Registry coverage — every doctor-rendered check must have an explanation
# ---------------------------------------------------------------------------


def test_every_required_dep_has_explanation() -> None:
    """Drift guard: if someone adds a new required dep to doctor.py's
    `_REQUIRED_DEPS` but forgets to write an explanation, --explain will
    skip that row silently. This catches the omission."""
    from movate.cli.doctor import _REQUIRED_DEPS  # noqa: PLC0415

    for dep in _REQUIRED_DEPS:
        assert f"dep: {dep}" in EXPLANATIONS, (
            f"Required dep {dep!r} has no entry in EXPLANATIONS — "
            f"add one in cli/_doctor_explanations.py."
        )


def test_every_optional_dep_has_explanation() -> None:
    from movate.cli.doctor import _OPTIONAL_DEPS  # noqa: PLC0415

    for dep in _OPTIONAL_DEPS:
        assert f"opt: {dep}" in EXPLANATIONS, f"Optional dep {dep!r} has no entry in EXPLANATIONS."


def test_every_runtime_has_explanation() -> None:
    for runtime_name, _probe, _extra in _RUNTIME_PROBES:
        assert f"runtime: {runtime_name}" in EXPLANATIONS, (
            f"Runtime {runtime_name!r} has no entry in EXPLANATIONS."
        )


def test_every_provider_key_has_explanation() -> None:
    for env_var, _label in _PROVIDER_KEYS:
        assert env_var in EXPLANATIONS, f"Provider key {env_var!r} has no entry in EXPLANATIONS."


def test_every_tracing_key_has_explanation() -> None:
    for env_var, _label in _TRACING_KEYS:
        assert env_var in EXPLANATIONS, f"Tracing key {env_var!r} has no entry in EXPLANATIONS."


# ---------------------------------------------------------------------------
# Explanation content quality
# ---------------------------------------------------------------------------


def test_every_explanation_has_required_fields() -> None:
    """`what` and `why` and `failure_impact` are mandatory; `fix` may be
    empty for facts that can't fail (e.g. fixed metadata rows)."""
    for check_id, entry in EXPLANATIONS.items():
        assert entry.what.strip(), f"{check_id}: empty 'what'"
        assert entry.why.strip(), f"{check_id}: empty 'why'"
        assert entry.failure_impact.strip(), f"{check_id}: empty 'failure_impact'"


@pytest.mark.parametrize("check_id", list(EXPLANATIONS.keys()))
def test_each_explanation_what_is_a_sentence(check_id: str) -> None:
    """Lightweight prose check: 'what' ends with punctuation (sentence)
    rather than dangling. Stops "WHAT: aiosqlite" style entries."""
    what = EXPLANATIONS[check_id].what
    assert what.endswith((".", "?", "!")), (
        f"{check_id}: 'what' should end with punctuation: {what!r}"
    )


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_doctor_without_explain_does_not_print_details() -> None:
    """Backwards compat: existing doctor output unchanged when --explain
    isn't passed."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Check details" not in result.stdout


def test_doctor_with_explain_prints_details() -> None:
    result = runner.invoke(app, ["doctor", "--explain"])
    assert result.exit_code == 0
    # The "Check details" header always renders.
    assert "Check details" in result.stdout
    # Section headers all appear.
    assert "Required dependencies" in result.stdout
    assert "Runtime adapters" in result.stdout
    assert "Provider API keys" in result.stdout
    # At least one specific WHAT line surfaces — proves entries render,
    # not just the section structure.
    assert "Typer CLI framework" in result.stdout


def test_doctor_explain_shows_fix_for_missing_provider_key() -> None:
    """The fix command (e.g. `export OPENAI_API_KEY=...`) is the
    operator's call-to-action — make sure it's not lost in rendering."""
    result = runner.invoke(app, ["doctor", "--explain"])
    assert result.exit_code == 0
    assert "export OPENAI_API_KEY" in result.stdout
    assert "export ANTHROPIC_API_KEY" in result.stdout
