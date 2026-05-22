"""Config-free commands (`mdk --version` / `--help`) must not load project
config — so they don't emit the legacy-yaml deprecation warning.

Regression: running `mdk --version` from inside a project dir (one with a
``movate.yaml``) printed "movate.yaml is deprecated" on every call, because the
import-time eager config load fired regardless of the command.
"""

from __future__ import annotations

import sys

import pytest

import movate.cli.main as cli_main
import movate.core.config as cfg


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["--version", "-V", "--help", "-h"])
def test_config_free_flag_skips_project_config_load(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg, "is_project_root", lambda _p: True)
    calls: list[int] = []
    monkeypatch.setattr(cfg, "load_project_config", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(sys, "argv", ["mdk", flag])

    cli_main._eager_load_project_config()

    assert calls == [], f"{flag} must not eager-load project config"


@pytest.mark.unit
def test_real_command_in_project_root_eager_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "is_project_root", lambda _p: True)
    calls: list[int] = []
    monkeypatch.setattr(cfg, "load_project_config", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(sys, "argv", ["mdk", "eval", "demo"])

    cli_main._eager_load_project_config()

    assert calls == [1], "a real command in a project root should eager-load config"
