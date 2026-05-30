"""Demo telemetry generation — pure, deterministic, dashboard-shaped.

This package holds the *generation logic* for ``mdk demo seed`` (the CLI
wrapper lives in :mod:`movate.cli.demo_cmd`, keeping the control plane and
the generation logic separate per CLAUDE.md boundary rule 6 — ``cli`` wraps,
``core`` generates).

Everything here is normal synchronous Python over stdlib ``random`` +
``datetime`` — no storage, no async, no I/O. The CLI takes the generated
records and writes them through the :class:`~movate.storage.base.StorageProvider`
Protocol. That split keeps the generator unit-testable without a database and
keeps the storage seam the only place that touches a backend.

**Safety invariant (do not weaken).** Every record this module produces is
tagged so it is unambiguously synthetic and fully purgeable:

* the ``tenant_id`` carries the :data:`DEMO_TENANT_PREFIX` (``demo-``) prefix,
* the run/eval ``input`` (or, for evals, the dataset hash namespace) carries
  the :data:`DEMO_MARKER_KEY` (``__mdk_demo__``) sentinel set to ``True``.

``mdk demo clear`` deletes exactly the rows whose ``tenant_id`` starts with the
prefix, so seeded data never co-mingles with real telemetry.
"""

from __future__ import annotations

from movate.core.demo.scenario import (
    DEMO_GRAPH_AGENT,
    DEMO_PROJECT_ID,
    DEMO_TENANT_ID,
    ScenarioBundle,
    generate_scenario,
)
from movate.core.demo.seeder import (
    DEMO_MARKER_KEY,
    DEMO_TENANT_PREFIX,
    DemoBundle,
    SeedConfig,
    VoiceTurnRecord,
    generate_bundle,
    is_demo_tenant,
)

__all__ = [
    "DEMO_GRAPH_AGENT",
    "DEMO_MARKER_KEY",
    "DEMO_PROJECT_ID",
    "DEMO_TENANT_ID",
    "DEMO_TENANT_PREFIX",
    "DemoBundle",
    "ScenarioBundle",
    "SeedConfig",
    "VoiceTurnRecord",
    "generate_bundle",
    "generate_scenario",
    "is_demo_tenant",
]
