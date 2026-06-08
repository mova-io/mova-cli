"""ADR 091 — `runtime: auto` resolution (Temporal as the graceful default).

The default runtime is now ``auto``: it resolves to Temporal when Temporal can
actually run the workflow (extra + TEMPORAL_HOST configured AND the graph
compiles on Temporal), else native. Explicit ``temporal`` / ``native`` are
returned verbatim (explicit ``temporal`` still fails loud on unavailable —
covered in test_temporal_execution). These tests pin the resolution table.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from movate.runtime import workflow_backend as wb


def _graph(runtime: str = "auto", node_types: list[str] | None = None) -> SimpleNamespace:
    """A minimal duck-typed WorkflowGraph: .runtime + .nodes[*].type.value."""
    nodes = {
        f"n{i}": SimpleNamespace(type=SimpleNamespace(value=t))
        for i, t in enumerate(node_types or ["agent"])
    }
    return SimpleNamespace(runtime=runtime, nodes=nodes)


@pytest.mark.unit
def test_auto_resolves_temporal_when_available_and_compilable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wb, "_temporal_available", lambda: True)
    assert wb.resolve_effective_runtime(_graph("auto", ["agent", "judge"]), None) == "temporal"


@pytest.mark.unit
def test_auto_falls_back_to_native_when_temporal_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wb, "_temporal_available", lambda: False)  # no extra / no host
    assert wb.resolve_effective_runtime(_graph("auto"), None) == "native"


@pytest.mark.unit
def test_auto_falls_back_to_native_for_uncompilable_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FUNCTION / SUB_WORKFLOW node can't compile on Temporal → stay native
    even when Temporal is available (don't break the workflow)."""
    monkeypatch.setattr(wb, "_temporal_available", lambda: True)
    assert wb.resolve_effective_runtime(_graph("auto", ["agent", "function"]), None) == "native"
    assert wb.resolve_effective_runtime(_graph("auto", ["agent", "sub_workflow"]), None) == "native"


@pytest.mark.unit
def test_explicit_runtime_is_returned_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit values never auto-resolve — temporal stays temporal even with a
    FUNCTION node (fail-loud is the caller's job), native stays native."""
    monkeypatch.setattr(wb, "_temporal_available", lambda: False)
    assert wb.resolve_effective_runtime(_graph("temporal"), None) == "temporal"
    assert wb.resolve_effective_runtime(_graph("native"), None) == "native"


@pytest.mark.unit
def test_override_beats_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "_temporal_available", lambda: True)
    # auto would resolve temporal, but an explicit override wins.
    assert wb.resolve_effective_runtime(_graph("auto"), "native") == "native"


@pytest.mark.unit
def test_temporal_available_is_nonthrowing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe must return False, never raise, when the extra/host are absent."""

    def _boom() -> None:
        raise RuntimeError("no temporal")

    monkeypatch.setattr(wb, "_require_temporal_extra", _boom)
    assert wb._temporal_available() is False


@pytest.mark.unit
def test_auto_is_a_valid_runtime() -> None:
    assert "auto" in wb.VALID_RUNTIMES
