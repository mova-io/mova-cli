"""Tests for ``mdk workflow web <run_id>`` — the Temporal Web deep-link.

A ``runtime: temporal`` run's id IS its Temporal workflow id (ADR 054 D6), so
its durable timeline lives at ``<ui>/namespaces/<ns>/workflows/<run_id>``. The
command resolves the UI base from ``--ui-url`` > ``MDK_TEMPORAL_UI_URL`` >
``TEMPORAL_UI_URL`` and prints (or opens) the deep-link.
"""

from __future__ import annotations

import pytest
import typer

from movate.cli import workflow_cmd


@pytest.mark.unit
def test_temporal_web_url_builds_deep_link() -> None:
    url = workflow_cmd._temporal_web_url("https://ui.example.com/", "default", "abc-123")
    assert url == "https://ui.example.com/namespaces/default/workflows/abc-123"


@pytest.mark.unit
def test_temporal_web_url_encodes_components() -> None:
    url = workflow_cmd._temporal_web_url("https://ui/", "my ns", "id/with space")
    assert "/namespaces/my%20ns/" in url
    assert url.endswith("workflows/id%2Fwith%20space")


@pytest.mark.unit
def test_web_prints_url_from_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MDK_TEMPORAL_UI_URL", "https://temporal.example.com")
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    workflow_cmd.web(run_id="run-7", namespace=None, ui_url=None, open_browser=False)
    assert (
        "https://temporal.example.com/namespaces/default/workflows/run-7" in capsys.readouterr().out
    )


@pytest.mark.unit
def test_web_ui_url_override_wins(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MDK_TEMPORAL_UI_URL", raising=False)
    monkeypatch.delenv("TEMPORAL_UI_URL", raising=False)
    workflow_cmd.web(run_id="r1", namespace="prod", ui_url="https://override", open_browser=False)
    assert "https://override/namespaces/prod/workflows/r1" in capsys.readouterr().out


@pytest.mark.unit
def test_web_falls_back_to_temporal_ui_url_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MDK_TEMPORAL_UI_URL", raising=False)
    monkeypatch.setenv("TEMPORAL_UI_URL", "https://legacy")
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    workflow_cmd.web(run_id="r9", namespace=None, ui_url=None, open_browser=False)
    assert "https://legacy/namespaces/default/workflows/r9" in capsys.readouterr().out


@pytest.mark.unit
def test_web_no_url_configured_exits_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MDK_TEMPORAL_UI_URL", raising=False)
    monkeypatch.delenv("TEMPORAL_UI_URL", raising=False)
    with pytest.raises(typer.Exit) as ei:
        workflow_cmd.web(run_id="r1", namespace=None, ui_url=None, open_browser=False)
    assert ei.value.exit_code == 2
    assert "MDK_TEMPORAL_UI_URL" in " ".join(capsys.readouterr().err.split())
