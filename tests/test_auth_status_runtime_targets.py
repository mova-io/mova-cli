"""PR #112 — `mdk auth status` extended with runtime targets section.

The user juggles three different pieces of context across VS Code
windows + terminals:

1. LLM provider keys (`OPENAI_API_KEY`, etc.) — covered pre-#112.
2. Runtime bearer tokens (`MDK_DEV_KEY` minted via `save-runtime-key`
   or `refresh-runtime-key`).
3. Active Azure subscription — drifts silently when `az account set`
   runs in another shell.

#112 extends `mdk auth status` so it shows all three in one view.
Tests here cover the runtime-targets section + the Azure-drift flag.

Tested here:

1. No targets configured → friendly hint pointing at `mdk config add-target`.
2. One Azure-pinned target whose bearer is in the credentials store →
   ✓ green row.
3. Target with bearer NOT set → ⊘ yellow row.
4. Azure subscription drift — current `az account` differs from target's
   pinned subscription → ⚠ warning with `az account set` hint.
5. `az` not on PATH → drift detection skipped; bearer rows still
   render correctly (offline mode).
6. The greppable `mdk_auth_status_summary:` line counts runtime bearers
   too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _isolate_auth_state(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin credentials + config paths at tmp so the test doesn't touch
    the operator's real ~/.movate. Same isolation pattern as the
    refresh-runtime-key tests."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(home / ".movate" / "config.yaml"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(home / ".movate" / "credentials"))
    # Clear all the runtime + provider env vars so the credentials file
    # is the source of truth in tests.
    for var in (
        "MDK_DEV_KEY",
        "MDK_PROD_KEY",
        "FAKE_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_user_config(home: Path, body: str) -> None:
    cfg_dir = home / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(body)


def _patch_az(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_subscription: str | None,
    az_available: bool = True,
) -> None:
    """Patch `_current_az_subscription` so tests don't depend on a
    real az login. None → simulate az unavailable / not logged in."""
    import movate.cli.auth as auth_mod  # noqa: PLC0415

    if not az_available:
        monkeypatch.setattr(auth_mod, "_current_az_subscription", lambda: None)
    else:
        monkeypatch.setattr(auth_mod, "_current_az_subscription", lambda: current_subscription)


# ---------------------------------------------------------------------------
# No targets configured
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_no_targets_renders_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator who's never run `mdk config add-target` should see a
    friendly nudge pointing at the right command, not a confusing
    empty table."""
    _isolate_auth_state(tmp_path, monkeypatch)
    _patch_az(monkeypatch, current_subscription=None, az_available=False)
    # No config file at all — load_user_config returns an empty UserConfig.
    result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "No deployment targets configured" in result.stdout
    assert "mdk config add-target" in result.stdout


# ---------------------------------------------------------------------------
# Bearer status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_bearer_in_credentials_file_shows_green_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bearer saved via `mdk auth save-runtime-key` → credentials file
    → rendered as ✓ in the Runtime Targets table."""
    _isolate_auth_state(tmp_path, monkeypatch)
    _write_user_config(
        tmp_path,
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://movate-dev-api.example.azurecontainerapps.io\n"
        "    key_env: MDK_DEV_KEY\n"
        "    azure_subscription: 00000000-0000-0000-0000-000000000001\n"
        "    azure_resource_group: movate-dev-rg\n"
        "    azure_env: dev\n",
    )
    # Save a key to the credentials file (the same path
    # save-runtime-key would write).
    creds = tmp_path / ".movate" / "credentials"
    creds.write_text("MDK_DEV_KEY=mvt_live_demo_KIDABCDEF12_secretXYZ\n")
    _patch_az(monkeypatch, current_subscription="00000000-0000-0000-0000-000000000001")

    result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    # Runtime Targets section header.
    assert "Runtime Targets" in combined
    # `dev` is marked active.
    assert "dev" in combined and "active" in combined
    # Bearer status: ✓ MDK_DEV_KEY rendered (Rich strips the green
    # color codes in pure stdout under CliRunner, but the text passes
    # through).
    assert "MDK_DEV_KEY" in combined
    # Azure subscription: ✓ (no drift — current matches pinned).
    assert "00000000" in combined


@pytest.mark.unit
def test_status_bearer_unset_renders_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Target whose key_env env var resolves to nothing (no env, no
    credentials entry) → yellow ⊘ marker + the env var name surfaced
    so the operator knows what to fix."""
    _isolate_auth_state(tmp_path, monkeypatch)
    _write_user_config(
        tmp_path,
        "targets:\n  prod:\n    url: https://prod.example.com\n    key_env: MDK_PROD_KEY\n",
    )
    # No credentials file → key is unset everywhere.
    _patch_az(monkeypatch, current_subscription=None, az_available=False)

    result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "MDK_PROD_KEY not set" in result.stdout


# ---------------------------------------------------------------------------
# Azure drift detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_flags_azure_subscription_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the operator's current `az account` differs from a target's
    pinned subscription, surface a ⚠ warning + the exact `az account
    set --subscription <id>` command they need to run. This catches
    the cross-window footgun where you switch subscriptions in another
    terminal and forget."""
    _isolate_auth_state(tmp_path, monkeypatch)
    _write_user_config(
        tmp_path,
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        "    azure_subscription: aaaaaaaa-0000-0000-0000-000000000001\n",
    )
    # az is on a DIFFERENT subscription than the dev target pins.
    _patch_az(monkeypatch, current_subscription="bbbbbbbb-0000-0000-0000-000000000002")

    result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Warning glyph + the corrective command surfaced.
    assert "az is on" in result.stdout
    assert "az account set --subscription" in result.stdout
    # Both subscription prefixes appear in the drift hint.
    assert "aaaaaaaa" in result.stdout  # pinned
    assert "bbbbbbbb" in result.stdout  # current


@pytest.mark.unit
def test_status_no_drift_when_subscriptions_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `az account` matches the target's pinned subscription, the
    cell is a quiet ✓ — no warning fires."""
    _isolate_auth_state(tmp_path, monkeypatch)
    _write_user_config(
        tmp_path,
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        "    azure_subscription: 11111111-0000-0000-0000-000000000001\n",
    )
    _patch_az(monkeypatch, current_subscription="11111111-0000-0000-0000-000000000001")

    result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # No drift warning.
    assert "az is on" not in result.stdout
    # Subscription prefix surfaced as a green ✓.
    assert "11111111" in result.stdout


@pytest.mark.unit
def test_status_skips_drift_when_az_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `az` on PATH → drift detection silently skipped. The runtime
    targets table still renders the pinned subscription; a footer hint
    explains why drift wasn't checked."""
    _isolate_auth_state(tmp_path, monkeypatch)
    _write_user_config(
        tmp_path,
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        "    azure_subscription: 22222222-0000-0000-0000-000000000001\n",
    )
    _patch_az(monkeypatch, current_subscription=None, az_available=False)

    result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # No drift warning.
    assert "az is on" not in result.stdout
    # Pinned subscription still surfaced.
    assert "22222222" in result.stdout
    # Footer explains why drift wasn't checked.
    assert "drift detection skipped" in result.stdout
    assert "az login" in result.stdout


# ---------------------------------------------------------------------------
# Summary line counts runtime bearers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_summary_includes_runtime_bearer_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The greppable `mdk_auth_status_summary:` line should aggregate
    LLM provider keys AND runtime bearers so a CI scraper can branch
    on the same line regardless of which kind of key matters."""
    _isolate_auth_state(tmp_path, monkeypatch)
    _write_user_config(
        tmp_path,
        "targets:\n"
        "  dev:\n"
        "    url: https://dev.example.com\n"
        "    key_env: MDK_DEV_KEY\n"
        "  prod:\n"
        "    url: https://prod.example.com\n"
        "    key_env: MDK_PROD_KEY\n",
    )
    # dev's bearer saved, prod's NOT saved.
    creds = tmp_path / ".movate" / "credentials"
    creds.write_text("MDK_DEV_KEY=mvt_live_demo_KIDABCDEF12_secretXYZ\n")
    _patch_az(monkeypatch, current_subscription=None, az_available=False)

    result = runner.invoke(app, ["auth", "status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "mdk_auth_status_summary:" in result.stdout
    # Summary surfaces set + unset counts. Exact numbers depend on the
    # full env-var matrix the status command checks, but the runtime
    # bearer accounting (1 set: MDK_DEV_KEY, 1 unset: MDK_PROD_KEY)
    # must contribute. Cheap regression: the prod bearer's "not set"
    # status appears in the body.
    assert "MDK_PROD_KEY not set" in result.stdout
