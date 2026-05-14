"""Snapshot manifest — the metadata persisted alongside captured files.

Manifest is the authoritative description of what's in a snapshot:
when it was made, what files it captured (with SHA-256 hashes), what
agent / workflow / context names it covers, what the operator-supplied
description was.

The snapshot's own hash is the SHA-256 of the canonical (sorted,
JSON-serialised) manifest excluding the hash field itself — so two
snapshots over identical state collide deterministically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml


class SnapshotManifestError(Exception):
    """Raised on malformed manifest YAML.

    Always carries an operator-facing message; the CLI surfaces it
    directly with exit-2 status. Malformed manifests must fail loud,
    not silently corrupt the snapshot graph.
    """


@dataclass(frozen=True)
class FileEntry:
    """One captured file in a snapshot.

    ``path`` is relative to the project root (the directory the
    snapshot was taken from). ``sha256`` is the SHA-256 of the file's
    bytes — used by ``mdk diff`` to detect content drift and by the
    snapshot's own hash computation. ``size`` is bytes; cheap to
    include and surfaces in ``mdk snapshot show``.
    """

    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SnapshotManifest:
    """Full snapshot metadata.

    Persisted as ``<snapshot-dir>/manifest.yaml``. All fields are
    captured at create time and never mutated — re-running
    :func:`create_snapshot` on identical state produces an identical
    manifest (and therefore an identical hash).

    Future fields (TODO markers in :mod:`movate.snapshot`):

    * ``git_sha`` — capture the working tree's git SHA so an operator
      can correlate a snapshot to a commit. Skipped in MVP to avoid
      the gitpython dep + git-not-installed fail mode.
    * ``pricing_version`` / ``provider_versions`` — Sprint N Day 4-5
      adds these so ``mdk diff`` can surface cost drift.
    * ``eval_summary`` — Sprint S folds in baseline scores so
      ``mdk audit`` can flag regressions across snapshots.
    """

    api_version: str
    kind: str
    hash: str
    created_at: str
    description: str
    project_root: str
    files: tuple[FileEntry, ...]
    agent_count: int
    workflow_count: int = 0
    extras: dict[str, str] = field(default_factory=dict)

    def to_yaml(self) -> str:
        """Serialise to YAML for on-disk persistence."""
        return yaml.safe_dump(self._as_serialisable(), sort_keys=False)

    def _as_serialisable(self) -> dict:
        """Plain-dict form that yaml.safe_dump handles cleanly.

        ``files`` becomes a list of dicts (rather than a list of
        dataclass instances) so the YAML parser on the read side
        doesn't need a custom constructor. Same ordering convention
        as the canonical hash so re-reading + re-serialising is a
        no-op modulo whitespace.
        """
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "hash": self.hash,
            "created_at": self.created_at,
            "description": self.description,
            "project_root": self.project_root,
            "agent_count": self.agent_count,
            "workflow_count": self.workflow_count,
            "files": [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in self.files],
            "extras": dict(self.extras),
        }


def compute_snapshot_hash(
    *,
    description: str,
    project_root: str,
    files: tuple[FileEntry, ...],
    agent_count: int,
    workflow_count: int,
    extras: dict[str, str],
) -> str:
    """Compute the SHA-256 hash of a snapshot's canonical content.

    The hash function deliberately excludes ``created_at`` so a
    snapshot taken twice over identical state produces the same
    hash. This gives free deduplication on disk + idempotent
    behaviour for ``mdk snapshot create`` (re-running over
    unchanged state surfaces the existing snapshot).

    Canonical form: JSON, sort_keys=True, ensure_ascii=True. The
    only inputs to the hash are content + description; timestamps
    and the hash field itself are excluded.
    """
    canonical = {
        "description": description,
        "project_root": project_root,
        "agent_count": agent_count,
        "workflow_count": workflow_count,
        "files": [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in files],
        "extras": dict(sorted(extras.items())),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def now_iso8601() -> str:
    """Current UTC time as ISO 8601 — surfaced in ``mdk snapshot list``.

    Includes millisecond precision so two snapshots taken in the same
    second (a common case in tests + scripted use) get distinct
    ``created_at`` values and sort deterministically by recency.
    """
    now = datetime.now(UTC)
    millis = now.microsecond // 1000
    return now.strftime(f"%Y-%m-%dT%H:%M:%S.{millis:03d}Z")


def load_manifest(path: str | Path) -> SnapshotManifest:
    """Read + parse a manifest.yaml back into :class:`SnapshotManifest`.

    Raises :class:`SnapshotManifestError` on malformed YAML, missing
    fields, or kind / api_version mismatch.
    """
    resolved = Path(path)
    if not resolved.is_file():
        raise SnapshotManifestError(f"manifest not found at {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text())
    except yaml.YAMLError as exc:
        raise SnapshotManifestError(f"manifest is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SnapshotManifestError("manifest root must be a mapping")

    for required in (
        "api_version",
        "kind",
        "hash",
        "created_at",
        "description",
        "project_root",
        "files",
        "agent_count",
    ):
        if required not in raw:
            raise SnapshotManifestError(f"manifest missing required field {required!r}")

    if raw["api_version"] != "movate/v1":
        raise SnapshotManifestError(
            f"unsupported manifest api_version {raw['api_version']!r}; expected 'movate/v1'"
        )
    if raw["kind"] != "Snapshot":
        raise SnapshotManifestError(f"manifest kind must be 'Snapshot'; got {raw['kind']!r}")

    raw_files = raw["files"]
    if not isinstance(raw_files, list):
        raise SnapshotManifestError("'files' must be a list")

    files: list[FileEntry] = []
    for i, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise SnapshotManifestError(f"files[{i}] must be a mapping")
        for key in ("path", "sha256", "size"):
            if key not in entry:
                raise SnapshotManifestError(f"files[{i}] missing field {key!r}")
        files.append(
            FileEntry(
                path=str(entry["path"]),
                sha256=str(entry["sha256"]),
                size=int(entry["size"]),
            )
        )

    return SnapshotManifest(
        api_version=str(raw["api_version"]),
        kind=str(raw["kind"]),
        hash=str(raw["hash"]),
        created_at=str(raw["created_at"]),
        description=str(raw["description"]),
        project_root=str(raw["project_root"]),
        files=tuple(files),
        agent_count=int(raw["agent_count"]),
        workflow_count=int(raw.get("workflow_count") or 0),
        extras=dict(raw.get("extras") or {}),
    )
