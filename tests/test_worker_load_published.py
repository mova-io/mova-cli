"""ADR 088 — the Temporal worker's storage discovery source.

``load_published_temporal_workflows`` lets ``mdk worker --backend temporal
--from-storage`` host workflows that were *published* (ADR 037) rather than
sitting on the agents volume. It materializes each published bundle's files and
reuses the one true ``scan_workflows`` loader, keeping only ``runtime: temporal``
graphs. Verified against the real in-repo ``refund-approval`` bundle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.models import WorkflowBundleRecord
from movate.runtime.registry import load_published_temporal_workflows
from movate.testing import InMemoryStorage

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFUND = _REPO_ROOT / "workflows" / "refund-approval"


def _bundle_files(root: Path) -> dict[str, str]:
    """Read a workflow dir into a POSIX-relative {path: text} map (ADR 037 shape)."""
    return {
        str(p.relative_to(root).as_posix()): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.unit
async def test_loads_published_temporal_workflow_from_storage() -> None:
    """A published runtime:temporal bundle is reconstructed + returned as a graph."""
    storage = InMemoryStorage()
    await storage.init()
    files = _bundle_files(_REFUND)
    assert "workflow.yaml" in files  # sanity: the fixture is intact
    await storage.save_workflow_bundle(
        WorkflowBundleRecord(
            name="refund-approval",
            tenant_id="t1",
            version="0.1.0",
            content_hash="sha256:test",
            files=files,
            published=True,
        )
    )

    graphs = await load_published_temporal_workflows(storage, tenant_id="t1")

    assert "refund-approval" in graphs
    assert getattr(graphs["refund-approval"], "runtime", "native") == "temporal"


@pytest.mark.unit
async def test_empty_when_no_published_workflows() -> None:
    """No published bundles → empty dict (worker just uses the filesystem scan)."""
    storage = InMemoryStorage()
    await storage.init()
    graphs = await load_published_temporal_workflows(storage, tenant_id="t1")
    assert graphs == {}


@pytest.mark.unit
async def test_other_tenant_bundles_are_not_loaded() -> None:
    """Tenant-scoped (ADR 088 D3): another tenant's published wf isn't returned."""
    storage = InMemoryStorage()
    await storage.init()
    await storage.save_workflow_bundle(
        WorkflowBundleRecord(
            name="refund-approval",
            tenant_id="other",
            version="0.1.0",
            content_hash="sha256:test",
            files=_bundle_files(_REFUND),
            published=True,
        )
    )
    graphs = await load_published_temporal_workflows(storage, tenant_id="t1")
    assert graphs == {}
