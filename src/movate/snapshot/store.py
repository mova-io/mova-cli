"""Snapshot store — create / list / load / delete on the filesystem.

Snapshots live at ``<project>/.movate/snapshots/<short-hash>/`` by
default. Each directory is self-contained:

  .movate/snapshots/abc12345/
    manifest.yaml            # the metadata
    files/                   # captured project files, mirroring layout
      movate.yaml
      agents/triage/agent.yaml
      agents/triage/prompt.md
      ...

The directory's name is the first 8 hex chars of the snapshot's
sha256 hash — collision-free for any reasonable project history
(~4 billion before paradox math kicks in). The full hash lives in
the manifest's ``hash`` field for unambiguous identification.

What's captured (MVP):

* ``movate.yaml`` / ``policy.yaml`` / ``knowledge.yaml`` (if present)
* All files under ``agents/``
* All files under ``contexts/`` / ``workflows/`` / ``skills/`` (if present)
* All files under ``evals/`` matching ``*.json`` (baseline files)

What's NOT captured (deliberate scope):

* ``.movate/local.db`` (runtime state, not config)
* ``.movate/snapshots/`` itself (recursion!)
* ``.git/``, ``__pycache__``, ``.venv``, ``node_modules`` (junk)
* Anything outside the captured-roots whitelist

A future ``mdk snapshot create --include <glob>`` could open this
up; today the whitelist keeps snapshots small + predictable.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable
from pathlib import Path

from movate.snapshot.manifest import (
    FileEntry,
    SnapshotManifest,
    SnapshotManifestError,
    _git_short_sha,
    compute_snapshot_hash,
    load_manifest,
    now_iso8601,
)


class SnapshotStoreError(Exception):
    """Raised when a snapshot operation can't proceed.

    Surfaces operator-facing detail with the offending path / hash.
    """


class SnapshotNotFoundError(SnapshotStoreError):
    """Raised when a hash (or hash prefix) doesn't resolve to a snapshot.

    Distinct subclass so the CLI can exit-1 on "not found" vs exit-2
    on other store errors (permission, IO).
    """


# ---------------------------------------------------------------------------
# Capture-root whitelist
# ---------------------------------------------------------------------------

# Roots scanned for snapshotting. Each entry is a relative directory
# (or file) under the project root. Directories are walked
# recursively; files are captured as-is. Missing entries are
# silently skipped (a project without a ``workflows/`` dir still
# produces a valid snapshot).
_CAPTURE_ROOTS: tuple[str, ...] = (
    "movate.yaml",
    "policy.yaml",
    "knowledge.yaml",
    "agents",
    "contexts",
    "workflows",
    "skills",
)

# Subdirectories under captured roots that we skip — junk that
# shouldn't end up in a reproducible snapshot.
_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".git",
        ".venv",
    }
)

# Files we skip even if they live under a captured root.
_SKIP_FILE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def snapshot_path(project_root: Path, short_hash: str) -> Path:
    """Resolve the on-disk dir for a snapshot's short hash."""
    return project_root / ".movate" / "snapshots" / short_hash


def create_snapshot(
    *,
    project_root: Path,
    description: str = "",
    extras: dict[str, str] | None = None,
) -> SnapshotManifest:
    """Capture the current project state as an immutable snapshot.

    Walks the capture-root whitelist, hashes every file, builds the
    manifest, and writes both the manifest and a copy of each file
    to ``.movate/snapshots/<short-hash>/``.

    Idempotent: re-running over identical state produces the same
    hash and short-hash directory, so the second call is a no-op
    (returns the existing manifest unchanged).

    Raises :class:`SnapshotStoreError` on IO failure (permissions,
    disk full, etc.) — never silently produces a partial snapshot.
    """
    if not project_root.is_dir():
        raise SnapshotStoreError(f"project root is not a directory: {project_root}")

    extras_dict = dict(extras or {})
    files = tuple(_collect_files(project_root))
    agent_count = _count_agents_in_files(files)
    workflow_count = _count_workflows_in_files(files)

    full_hash = compute_snapshot_hash(
        description=description,
        project_root=str(project_root.resolve()),
        files=files,
        agent_count=agent_count,
        workflow_count=workflow_count,
        extras=extras_dict,
    )
    short = full_hash.split(":", 1)[1][:8]

    dest = snapshot_path(project_root, short)
    if dest.is_dir():
        # Idempotent path: identical state, identical hash. Return
        # the existing manifest unchanged.
        return load_manifest(dest / "manifest.yaml")

    manifest = SnapshotManifest(
        api_version="movate/v1",
        kind="Snapshot",
        hash=full_hash,
        created_at=now_iso8601(),
        description=description,
        project_root=str(project_root.resolve()),
        files=files,
        agent_count=agent_count,
        workflow_count=workflow_count,
        git_sha=_git_short_sha(cwd=project_root),
        extras=extras_dict,
    )

    _persist_snapshot(dest, manifest=manifest, project_root=project_root)
    return manifest


def list_snapshots(project_root: Path) -> list[SnapshotManifest]:
    """Return every snapshot's manifest, newest first."""
    snapshots_root = project_root / ".movate" / "snapshots"
    if not snapshots_root.is_dir():
        return []
    manifests: list[SnapshotManifest] = []
    for child in snapshots_root.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifests.append(load_manifest(manifest_path))
        except SnapshotManifestError:
            # Skip corrupted snapshots rather than failing the whole list —
            # operators with a half-written snapshot from an interrupted
            # ``create`` shouldn't lose visibility into the good ones.
            continue
    manifests.sort(key=lambda m: m.created_at, reverse=True)
    return manifests


def resolve_snapshot(project_root: Path, hash_or_prefix: str) -> SnapshotManifest:
    """Look up a snapshot by full hash, short hash, or short-hash prefix.

    Path resolution:

    1. Strip ``sha256:`` if present (operators paste either form).
    2. Match against the 8-char short-hash directory name first.
    3. Fall back to prefix match (≥ 4 chars to avoid first-byte
       collisions on a busy project).
    4. Ambiguous prefix raises :class:`SnapshotStoreError`; no match
       raises :class:`SnapshotNotFoundError`.
    """
    bare = hash_or_prefix.removeprefix("sha256:")
    snapshots_root = project_root / ".movate" / "snapshots"
    if not snapshots_root.is_dir():
        raise SnapshotNotFoundError(
            f"no snapshots in {snapshots_root} — run `mdk snapshot create` first"
        )

    # Exact short-hash match first.
    exact = snapshots_root / bare[:8]
    if exact.is_dir() and (exact / "manifest.yaml").is_file():
        manifest = load_manifest(exact / "manifest.yaml")
        # If a longer prefix was given, require the full hash to match too.
        if bare and not manifest.hash.removeprefix("sha256:").startswith(bare):
            raise SnapshotNotFoundError(
                f"prefix {hash_or_prefix!r} matches short-hash {bare[:8]} but full "
                f"hash differs ({manifest.hash})"
            )
        return manifest

    # Prefix match.
    min_prefix_for_match = 4
    if len(bare) < min_prefix_for_match:
        raise SnapshotNotFoundError(f"hash prefix {hash_or_prefix!r} too short (need ≥ 4 chars)")
    candidates = [
        child
        for child in snapshots_root.iterdir()
        if child.is_dir() and child.name.startswith(bare)
    ]
    if not candidates:
        raise SnapshotNotFoundError(f"no snapshot found for {hash_or_prefix!r}")
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise SnapshotStoreError(
            f"prefix {hash_or_prefix!r} is ambiguous — matches {len(candidates)} "
            f"snapshots: {names}. Use more characters."
        )
    return load_manifest(candidates[0] / "manifest.yaml")


def delete_snapshot(project_root: Path, hash_or_prefix: str) -> SnapshotManifest:
    """Remove a snapshot from disk + return its manifest (for confirmation).

    Resolution semantics match :func:`resolve_snapshot`. The
    operation is destructive — there's no soft-delete in the MVP.
    A future ``--retain N`` policy + ``mdk snapshot purge`` cron
    could land in Sprint S audit v2.
    """
    manifest = resolve_snapshot(project_root, hash_or_prefix)
    short = manifest.hash.removeprefix("sha256:")[:8]
    target = snapshot_path(project_root, short)
    if target.is_dir():
        shutil.rmtree(target)
    return manifest


# ---------------------------------------------------------------------------
# File collection (internal)
# ---------------------------------------------------------------------------


def _collect_files(project_root: Path) -> Iterable[FileEntry]:
    """Walk the capture-root whitelist + emit hashed FileEntry per file.

    Order is sorted-deterministic across runs: by relative path,
    lexicographic. The canonical hash function depends on this
    ordering for idempotency.
    """
    entries: list[FileEntry] = []
    for root_name in _CAPTURE_ROOTS:
        root_path = project_root / root_name
        if not root_path.exists():
            continue
        if root_path.is_file():
            entries.append(_file_entry(root_path, project_root))
            continue
        for path in _walk_capturing(root_path):
            entries.append(_file_entry(path, project_root))
    # Sort by relative path for deterministic ordering.
    entries.sort(key=lambda e: e.path)
    return entries


def _walk_capturing(root: Path) -> Iterable[Path]:
    """Recursive walk that skips junk directories + binary suffixes.

    Manual implementation (not ``Path.rglob('*')``) so we can prune
    the walk at directory boundaries — skipping ``__pycache__``
    saves ~hundreds of irrelevant entries per snapshot.
    """
    for child in sorted(root.iterdir()):
        if child.is_dir():
            if child.name in _SKIP_DIR_NAMES:
                continue
            yield from _walk_capturing(child)
        elif child.is_file() and child.suffix not in _SKIP_FILE_SUFFIXES:
            yield child


def _file_entry(path: Path, project_root: Path) -> FileEntry:
    """Hash a single file + build a :class:`FileEntry`."""
    rel = path.resolve().relative_to(project_root.resolve())
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return FileEntry(
        path=str(rel),
        sha256=f"sha256:{hasher.hexdigest()}",
        size=path.stat().st_size,
    )


def _count_agents_in_files(files: tuple[FileEntry, ...]) -> int:
    """Distinct agent directories in the captured set.

    Agents live under ``agents/<name>/agent.yaml``; we count
    unique ``<name>`` values rather than file count.
    """
    names = {
        f.path.split("/", 2)[1]
        for f in files
        if f.path.startswith("agents/") and f.path.endswith("/agent.yaml")
    }
    return len(names)


def _count_workflows_in_files(files: tuple[FileEntry, ...]) -> int:
    """Distinct workflow directories — same shape as agents."""
    names = {
        f.path.split("/", 2)[1]
        for f in files
        if f.path.startswith("workflows/") and f.path.endswith("/workflow.yaml")
    }
    return len(names)


def _persist_snapshot(dest: Path, *, manifest: SnapshotManifest, project_root: Path) -> None:
    """Write the manifest + copy every captured file to ``dest``.

    ``dest`` must not exist (caller checks). Files are copied with
    their relative paths preserved under ``dest/files/`` so the
    mirror reads like the original project root.

    Errors mid-write surface as :class:`SnapshotStoreError` — and
    the partial directory is removed before re-raising, so the
    snapshot store never carries a half-written entry.
    """
    try:
        dest.mkdir(parents=True)
        files_dir = dest / "files"
        files_dir.mkdir()
        for entry in manifest.files:
            src = project_root / entry.path
            dst = files_dir / entry.path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        (dest / "manifest.yaml").write_text(manifest.to_yaml())
    except (OSError, shutil.Error) as exc:
        # Best-effort cleanup of the partial snapshot.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise SnapshotStoreError(f"failed to persist snapshot: {exc}") from exc
