"""The post-run Temporal trace hint (``_print_temporal_trace_hints``).

After a ``runtime: temporal`` run, the CLI points the user at the durable
timeline — the per-activity trace view that the trace-context propagation now
renders as one connected trace. Always prints the ``mdk workflow web`` command;
adds the resolved deep-link URL when a UI base is configured.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from movate.cli import run as run_mod


def _capture(capsys: pytest.CaptureFixture[str]) -> str:
    out = capsys.readouterr()
    return out.out + out.err


@pytest.mark.unit
def test_hint_always_prints_workflow_web_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No UI base configured → still print the actionable command (no broken URL).
    monkeypatch.delenv("MDK_TEMPORAL_UI_URL", raising=False)
    monkeypatch.delenv("TEMPORAL_UI_URL", raising=False)
    run_mod._print_temporal_trace_hints(SimpleNamespace(workflow_run_id="wf-123"))
    combined = _capture(capsys)
    assert "mdk workflow web wf-123" in combined
    # No deep-link URL when unconfigured.
    assert "/namespaces/" not in combined


@pytest.mark.unit
def test_hint_includes_deep_link_when_ui_base_configured(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDK_TEMPORAL_UI_URL", "https://temporal.example.com")
    monkeypatch.setenv("TEMPORAL_HOST", "localhost:7233")  # so namespace resolves
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "prod-ns")
    run_mod._print_temporal_trace_hints(SimpleNamespace(workflow_run_id="wf-xyz"))
    combined = _capture(capsys)
    assert "https://temporal.example.com/namespaces/prod-ns/workflows/wf-xyz" in combined


@pytest.mark.unit
def test_hint_never_raises_on_bad_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # A hint must never break a run — even if URL resolution blows up.
    monkeypatch.setenv("MDK_TEMPORAL_UI_URL", "https://ui")
    monkeypatch.setenv("TEMPORAL_HOST", "localhost:7233")
    # Missing workflow_run_id attr would raise inside — assert it's swallowed
    # by the outer guard (the command print needs it, so use a blank one).
    run_mod._print_temporal_trace_hints(SimpleNamespace(workflow_run_id=""))
