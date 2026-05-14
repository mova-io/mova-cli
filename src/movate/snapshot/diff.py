"""Snapshot diff (Sprint N Day 4-5).

Answers the most-asked operational question: *what changed between
two snapshots?* — the analog of ``git diff`` for the AI system's
state graph.

Three categories of change:

* **File-level** — files added / removed / modified between snapshot
  A and B. Modified = same path, different sha256. The most common
  signal an operator wants.
* **Metadata** — agent count, workflow count, description deltas.
* **Configuration** (future) — guardrails, policy, knowledge entries
  changed. Lands in Sprint S audit v2 once those configs are persisted
  inside snapshots in dedicated fields.

The diff is a pure function over two :class:`SnapshotManifest`
objects. The rendering surface (CLI tables, JSON) lives in
``movate.cli.diff_cmd``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from movate.snapshot.manifest import FileEntry, SnapshotManifest

ChangeKind = Literal["added", "removed", "modified"]


@dataclass(frozen=True)
class FileChange:
    """One file-level change between two snapshots.

    For ``modified``, both ``before`` and ``after`` are populated;
    for ``added``, only ``after`` is set; for ``removed``, only
    ``before``. ``size_delta`` is signed (after - before) and
    ``None`` for added/removed.
    """

    path: str
    kind: ChangeKind
    before: FileEntry | None = None
    after: FileEntry | None = None
    size_delta: int | None = None


@dataclass(frozen=True)
class SnapshotDiff:
    """Aggregated diff between two snapshots.

    ``files_added`` + ``files_removed`` + ``files_modified`` partition
    the set of changes (no path appears in more than one list). The
    counts make ``mdk diff`` summary-line rendering cheap.

    ``description_before`` / ``description_after`` surface the
    operator-supplied snapshot descriptions so the diff output reads
    contextually rather than as opaque hash-to-hash.
    """

    before_hash: str
    after_hash: str
    description_before: str = ""
    description_after: str = ""
    files_added: tuple[FileChange, ...] = field(default_factory=tuple)
    files_removed: tuple[FileChange, ...] = field(default_factory=tuple)
    files_modified: tuple[FileChange, ...] = field(default_factory=tuple)
    agent_count_delta: int = 0
    workflow_count_delta: int = 0

    @property
    def total_changes(self) -> int:
        """Total file-level changes — cheap proxy for "did anything change?"."""
        return len(self.files_added) + len(self.files_removed) + len(self.files_modified)

    @property
    def is_identical(self) -> bool:
        """True when nothing changed between the two snapshots.

        Useful for ``mdk diff`` to render a clean "no changes" line
        rather than an empty table. Identical content always implies
        identical hashes (content-addressed semantics), but we check
        the change lists explicitly in case the two snapshots are
        from different projects with coincidentally-identical files.
        """
        return self.total_changes == 0


def diff_snapshots(before: SnapshotManifest, after: SnapshotManifest) -> SnapshotDiff:
    """Compute the diff between two snapshots.

    Pure function — same inputs always produce the same output.
    No I/O, no side effects.

    Algorithm:
      1. Build path → :class:`FileEntry` dicts for each snapshot.
      2. Set-difference on paths: added = after - before;
         removed = before - after.
      3. Intersection of paths: modified = same path, different sha256.
      4. Tag each FileChange with kind + size delta.

    Returns the diff sorted by category, then by path within each
    category, for deterministic rendering.
    """
    before_files = {f.path: f for f in before.files}
    after_files = {f.path: f for f in after.files}

    added_paths = sorted(after_files.keys() - before_files.keys())
    removed_paths = sorted(before_files.keys() - after_files.keys())
    common_paths = sorted(before_files.keys() & after_files.keys())

    files_added = tuple(
        FileChange(
            path=p,
            kind="added",
            after=after_files[p],
        )
        for p in added_paths
    )
    files_removed = tuple(
        FileChange(
            path=p,
            kind="removed",
            before=before_files[p],
        )
        for p in removed_paths
    )

    modified: list[FileChange] = []
    for path in common_paths:
        b = before_files[path]
        a = after_files[path]
        if b.sha256 != a.sha256:
            modified.append(
                FileChange(
                    path=path,
                    kind="modified",
                    before=b,
                    after=a,
                    size_delta=a.size - b.size,
                )
            )

    return SnapshotDiff(
        before_hash=before.hash,
        after_hash=after.hash,
        description_before=before.description,
        description_after=after.description,
        files_added=files_added,
        files_removed=files_removed,
        files_modified=tuple(modified),
        agent_count_delta=after.agent_count - before.agent_count,
        workflow_count_delta=after.workflow_count - before.workflow_count,
    )
