"""``mdk doctor`` runtime-bearer-key section.

Diagnoses the runtime keys (``MDK_<TARGET>_KEY``) that pair with the
``mdk fix unshadow-runtime-keys`` remediation:

* set from ~/.movate/credentials → ✓ ok (source reported)
* set from shell AND differs from the saved file value → ⚠ shadow
  finding that names ``mdk fix unshadow-runtime-keys --apply``
* set from shell with NO saved file value → ✓ ok (not a shadow)
* target configured but key unset → ⚠ with the
  ``mdk auth save-runtime-key`` hint
* no configured targets → section is a clean no-op (no header)

Unit tests call :func:`_render_runtime_keys_section` directly with a
fake ``_add`` collector (same pattern the bundle-B agent-doctor tests
use), monkeypatching ``os.environ`` + the credentials store + the
target config. A final CliRunner test drives the full ``mdk doctor``
table end-to-end. None of these touch a real ``~/.movate``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli import doctor as doctor_mod
from movate.cli.doctor import _is_runtime_key_shadowed, _render_runtime_keys_section
from movate.cli.main import app
from movate.core.user_config import TargetConfig, UserConfig

runner = CliRunner(mix_stderr=False)


def _collect() -> tuple[list[tuple[str, str]], Any]:
    """Return ``(rows, _add)`` — a fake ``_add`` that records rows."""
    rows: list[tuple[str, str]] = []

    def _add(check: str, result: str, *extra: str) -> None:
        rows.append((check, result))

    return rows, _add


def _patch_targets(monkeypatch: pytest.MonkeyPatch, targets: dict[str, TargetConfig]) -> None:
    """Make ``load_user_config`` (as imported inside the section) return
    a config carrying ``targets`` — without touching disk."""
    cfg = UserConfig(targets=targets)
    monkeypatch.setattr(
        "movate.core.user_config.load_user_config",
        lambda: cfg,
    )


def _patch_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: str,
    env_value: str,
    file_value: str | None,
) -> None:
    """Stub the two seams the section reads: ``key_source`` (source
    attribution) and ``CredentialsStore.get`` (the saved file value).
    Also set the live env value so ``_is_runtime_key_shadowed`` can
    compare shell-vs-file. ``var``-agnostic — all keys map the same way.
    """
    monkeypatch.setattr("movate.credentials.key_source", lambda _var: source)
    monkeypatch.setattr(
        "movate.credentials.store.CredentialsStore.get",
        lambda _self, _key: file_value,
    )
    if env_value:
        monkeypatch.setenv("MDK_DEV_KEY", env_value)
    else:
        monkeypatch.delenv("MDK_DEV_KEY", raising=False)


def _dev_key_target() -> dict[str, TargetConfig]:
    return {"dev": TargetConfig(url="http://127.0.0.1:8000", key_env="MDK_DEV_KEY", auth="key")}


# ---------------------------------------------------------------------------
# _is_runtime_key_shadowed predicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShadowPredicate:
    def test_shell_value_differs_from_file_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sources(monkeypatch, source="shell", env_value="shellkey", file_value="filekey")
        assert _is_runtime_key_shadowed("MDK_DEV_KEY") is True

    def test_shell_value_with_no_file_value_is_not_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_sources(monkeypatch, source="shell", env_value="shellkey", file_value=None)
        assert _is_runtime_key_shadowed("MDK_DEV_KEY") is False

    def test_shell_value_equal_to_file_value_is_not_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same value in both places — nothing is being hidden.
        _patch_sources(monkeypatch, source="shell", env_value="same", file_value="same")
        assert _is_runtime_key_shadowed("MDK_DEV_KEY") is False

    def test_credentials_file_source_is_not_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with a differing file value, a non-shell source means the
        # file path won — there's nothing to unshadow.
        _patch_sources(
            monkeypatch, source="credentials_file", env_value="filekey", file_value="filekey"
        )
        assert _is_runtime_key_shadowed("MDK_DEV_KEY") is False


# ---------------------------------------------------------------------------
# _render_runtime_keys_section rows
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuntimeKeysSection:
    def test_set_from_file_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_targets(monkeypatch, _dev_key_target())
        _patch_sources(
            monkeypatch, source="credentials_file", env_value="filekey", file_value="filekey"
        )
        rows, _add = _collect()
        _render_runtime_keys_section(_add)
        row = next(r for r in rows if r[0] == "MDK_DEV_KEY")
        assert "[green]ok" in row[1]
        assert "credentials_file" in row[1]
        # The secret value must NEVER appear.
        assert "filekey" not in row[1]

    def test_shell_differs_from_file_is_shadow_naming_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_targets(monkeypatch, _dev_key_target())
        _patch_sources(monkeypatch, source="shell", env_value="shellkey", file_value="filekey")
        rows, _add = _collect()
        _render_runtime_keys_section(_add)
        row = next(r for r in rows if r[0] == "MDK_DEV_KEY")
        assert "[yellow]shadowed[/yellow]" in row[1]
        # Names BOTH the auto-fix and the manual remediation.
        assert "mdk fix unshadow-runtime-keys --apply" in row[1]
        assert "unset MDK_DEV_KEY" in row[1]
        # Neither secret leaks.
        assert "shellkey" not in row[1]
        assert "filekey" not in row[1]

    def test_shell_with_no_file_value_is_not_flagged_as_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_targets(monkeypatch, _dev_key_target())
        _patch_sources(monkeypatch, source="shell", env_value="shellkey", file_value=None)
        rows, _add = _collect()
        _render_runtime_keys_section(_add)
        row = next(r for r in rows if r[0] == "MDK_DEV_KEY")
        assert "[green]ok" in row[1]
        assert "shadowed" not in row[1]
        assert "shellkey" not in row[1]

    def test_unset_key_gets_save_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_targets(monkeypatch, _dev_key_target())
        _patch_sources(monkeypatch, source="unset", env_value="", file_value=None)
        rows, _add = _collect()
        _render_runtime_keys_section(_add)
        row = next(r for r in rows if r[0] == "MDK_DEV_KEY")
        assert "[yellow]missing[/yellow]" in row[1]
        assert "mdk auth save-runtime-key dev" in row[1]

    def test_no_targets_is_clean_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_targets(monkeypatch, {})
        rows, _add = _collect()
        _render_runtime_keys_section(_add)
        # No rows at all — not even a section separator.
        assert rows == []

    def test_oidc_target_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_targets(
            monkeypatch,
            {
                "prod": TargetConfig(
                    url="http://x", key_env="MDK_PROD_KEY", auth="oidc", oidc_resource="api://x"
                )
            },
        )
        rows, _add = _collect()
        _render_runtime_keys_section(_add)
        # oidc targets have no MDK_<T>_KEY to diagnose → nothing emitted.
        assert rows == []


# ---------------------------------------------------------------------------
# Full-table integration — drive `mdk doctor` end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_full_doctor_table_shows_shadow_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live ``mdk doctor`` run surfaces the shadow finding (and its fix
    pointer) when a shell export shadows the saved credentials file."""
    # Hermetic home + config — never touch the real ~/.movate.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {"targets": {"dev": {"url": "http://127.0.0.1:8000", "key_env": "MDK_DEV_KEY"}}}
        )
    )
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    # Live shell value differs from what the (stubbed) credentials store holds.
    monkeypatch.setenv("MDK_DEV_KEY", "shell-stale")
    monkeypatch.setattr(doctor_mod, "_is_runtime_key_shadowed", lambda _var: True)
    monkeypatch.setattr("movate.credentials.key_source", lambda _var: "shell")

    result = runner.invoke(app, ["doctor", "--no-fix-prompt"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "shadowed" in result.stdout
    assert "unshadow-runtime-keys" in result.stdout
    # The secret never prints.
    assert "shell-stale" not in result.stdout
    # Shadow counts as a "missing" signal in the greppable summary.
    assert "mdk_doctor_summary:" in result.stdout
