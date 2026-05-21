"""Snapshot — the central operational primitive (Sprint N, BACKLOG K-state).

Per the Group K North Star (Terraform/dbt for AI), snapshots are the
load-bearing primitive. Every other operational command in the
state cluster (``mdk diff``, ``rollback``, ``promote``, ``audit``,
``migrate``) operates on a snapshot.

Design lock-ins:

* **Immutable** — once written, a snapshot's manifest + files are
  never modified. Re-snapshotting identical state produces the same
  hash, so you get free deduplication.
* **Content-addressed** — the snapshot's hash IS the SHA-256 of its
  canonical manifest (sorted file list + per-file SHA-256s).
  Two snapshots of identical state collide deterministically;
  any drift in any file changes the hash.
* **Lives in git** — by default snapshots write to
  ``.movate/snapshots/<hash>/`` (gitignored by convention so the
  repo doesn't bloat, but operators can opt in via ``.gitignore``
  surgery). Avoids Terraform-state-file pathologies (locking,
  external state-backend coordination, "I edited the state file
  manually").

What ships in the MVP:

* :class:`SnapshotManifest` data class — metadata that's persisted
  alongside the captured files.
* :func:`create_snapshot` — captures the current project state.
* :func:`list_snapshots`, :func:`load_manifest`, :func:`delete_snapshot`.

What does NOT ship (future):

* Pricing / provider version capture (~30 min add when Sprint N
  Day 4-5 `mdk diff` needs it for cost-drift detection)
* Cross-snapshot eval-score persistence (lands with Sprint S audit v2)
* OCI artifact export (Sprint R)
"""

from __future__ import annotations

from movate.snapshot.diff import (
    FileChange,
    SnapshotDiff,
    diff_snapshots,
)
from movate.snapshot.manifest import (
    FileEntry,
    SnapshotManifest,
    SnapshotManifestError,
    load_manifest,
)
from movate.snapshot.rollback import (
    RollbackResult,
    SnapshotRollbackError,
    rollback_to,
)
from movate.snapshot.store import (
    SnapshotNotFoundError,
    SnapshotStoreError,
    create_snapshot,
    delete_snapshot,
    list_snapshots,
    resolve_snapshot,
    snapshot_path,
)

__all__ = [
    "FileChange",
    "FileEntry",
    "RollbackResult",
    "SnapshotDiff",
    "SnapshotManifest",
    "SnapshotManifestError",
    "SnapshotNotFoundError",
    "SnapshotRollbackError",
    "SnapshotStoreError",
    "create_snapshot",
    "delete_snapshot",
    "diff_snapshots",
    "list_snapshots",
    "load_manifest",
    "resolve_snapshot",
    "rollback_to",
    "snapshot_path",
]
