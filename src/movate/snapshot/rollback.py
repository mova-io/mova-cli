"""Snapshot rollback (Sprint N Day 6-7).

Restore a project's state to match a prior snapshot. Pairs with
:mod:`movate.snapshot.store` (create) + :mod:`movate.snapshot.diff`
(compare) to complete the local state-cluster surface.

Crucial design call: **rollback is itself a snapshot operation, not
a mutation in place.** The idiomatic flow is:

  1. Take a "before rollback" snapshot of current state (audit trail).
  2. Restore files from the target snapshot back into the project.
  3. Optionally take a "after rollback" snapshot to confirm.

Step 1 is **automatic** in the MVP — every rollback creates a
fresh snapshot of the pre-rollback state. That snapshot's
description encodes the rollback origin so the audit trail reads:

  $ mdk snapshot list
  abc12345  rolled back FROM def67890 (model swap reverted)
  def67890  red-bar after model swap
  9a8b7c6d  green deploy

Rollback is **never** destructive: the prior state is always
recoverable from its newly-created snapshot.

For the cluster integration (PRs #20-#22), rollback only restores
files captured by the target snapshot's manifest. Files added
since the snapshot but absent from it remain in place — they're
not "uncreated." That preserves the snapshot-as-state semantics
without surprising the operator who has, say, a half-edited
prompt that's not under any captured root.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from movate.snapshot.manifest import SnapshotManifest
from movate.snapshot.store import (
    SnapshotStoreError,
    create_snapshot,
    resolve_snapshot,
    snapshot_path,
)


class SnapshotRollbackError(Exception):
    """Raised when a rollback can't proceed.

    Typical causes: target snapshot missing on disk (deleted between
    list + rollback), or IO failure writing back captured files.
    """


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of one rollback operation.

    The CLI uses this to render a confirmation panel. ``pre_snapshot``
    is the auto-captured snapshot of state BEFORE the rollback —
    operators can re-roll forward by passing its hash to a future
    ``mdk rollback`` call.
    """

    target: SnapshotManifest
    pre_snapshot: SnapshotManifest
    restored_count: int


def rollback_to(
    *,
    project_root: Path,
    target_hash: str,
) -> RollbackResult:
    """Restore the project to the state captured by ``target_hash``.

    Steps:
      1. Resolve the target snapshot from its hash / prefix.
      2. Auto-capture a "pre-rollback" snapshot of the current state
         (description encodes the rollback origin for the audit trail).
      3. Copy every file from the target snapshot's ``files/`` mirror
         back into the project, overwriting whatever's at each path.

    Returns :class:`RollbackResult` so callers can surface both the
    pre-rollback hash (for re-roll-forward) and the restored count.

    Raises:
      :class:`movate.snapshot.SnapshotNotFoundError` if ``target_hash``
        doesn't resolve. The CLI maps this to exit-1.
      :class:`SnapshotRollbackError` on IO failure during restore.
    """
    target = resolve_snapshot(project_root, target_hash)
    target_short = target.hash.removeprefix("sha256:")[:8]

    pre_snapshot = create_snapshot(
        project_root=project_root,
        description=f"rolled back FROM {target_short}",
        extras={"rollback_target": target.hash},
    )

    target_files_dir = snapshot_path(project_root, target_short) / "files"
    if not target_files_dir.is_dir():
        raise SnapshotRollbackError(
            f"target snapshot {target_short} has no files/ directory at "
            f"{target_files_dir} — snapshot store corruption?"
        )

    restored = 0
    try:
        for entry in target.files:
            src = target_files_dir / entry.path
            dst = project_root / entry.path
            if not src.is_file():
                raise SnapshotRollbackError(
                    f"captured file {entry.path} missing from snapshot store at {src}"
                )
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
    except (OSError, shutil.Error) as exc:
        raise SnapshotRollbackError(
            f"failed during file restore (restored {restored} of "
            f"{len(target.files)} before failure): {exc}"
        ) from exc

    return RollbackResult(
        target=target,
        pre_snapshot=pre_snapshot,
        restored_count=restored,
    )


# Surface to the package __init__
__all__ = ["RollbackResult", "SnapshotRollbackError", "rollback_to"]


# Re-export from movate.snapshot.store for convenience
def _ensure_store_compatible() -> None:
    """Sanity: SnapshotStoreError import must remain valid at runtime."""
    _ = SnapshotStoreError  # pragma: no cover — type-import only
