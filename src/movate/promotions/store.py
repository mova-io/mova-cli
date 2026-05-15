"""Project-local promotion log on the filesystem.

One file per project at ``<project_root>/.movate/promotions.yaml``,
append-only. The file format:

  api_version: movate/v1
  kind: Promotions
  promotions:
    - profile: staging
      snapshot_hash: sha256:abc123...
      promoted_at: 2026-05-15T14:00:00.000Z
      promoted_by: alice@laptop
      description: "post-release v0.7 bugfix"
      eval_score: 0.85         # optional
    - profile: prod
      snapshot_hash: sha256:def456...
      ...

Append-only: the CLI never edits or deletes existing entries.
Operators reverse mistakes by promoting a different snapshot,
which creates a *new* entry. This keeps the audit trail honest.
"""

from __future__ import annotations

import getpass
import os
import platform
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml


class PromotionsStoreError(Exception):
    """Raised on malformed promotions.yaml or filesystem errors.

    Same exit-2 mapping as the rest of the state cluster — operators
    see the underlying message and the offending profile / snapshot
    so they can ``ls -la .movate/`` to investigate.
    """


@dataclass(frozen=True)
class Promotion:
    """One recorded promotion event.

    All fields are captured at write time and never mutated. The
    ``eval_score`` field is optional — operators can promote
    without an observed score (recorded as ``None``), or supply
    one they got from ``mdk eval`` for the audit trail.
    """

    profile: str
    snapshot_hash: str
    promoted_at: str
    promoted_by: str = ""
    description: str = ""
    eval_score: float | None = None

    @property
    def short_hash(self) -> str:
        """First 8 hex chars of the snapshot hash — matches snapshot store."""
        return self.snapshot_hash.removeprefix("sha256:")[:8]


@dataclass
class PromotionsLog:
    """In-memory view of one project's promotions file.

    Mutable container holding an ordered list of :class:`Promotion`
    entries — order is insertion order (= chronological).
    """

    project_root: Path
    promotions: list[Promotion] = field(default_factory=list)

    def append(self, promotion: Promotion) -> None:
        """Append a new promotion to the log."""
        self.promotions.append(promotion)

    def for_profile(self, profile: str) -> list[Promotion]:
        """Return promotions targeting ``profile``, oldest first."""
        return [p for p in self.promotions if p.profile == profile]

    def current(self, profile: str) -> Promotion | None:
        """Return the most recent promotion to ``profile`` (or None)."""
        matching = self.for_profile(profile)
        return matching[-1] if matching else None


# ---------------------------------------------------------------------------
# Filesystem paths + persistence
# ---------------------------------------------------------------------------


def _log_path(project_root: Path) -> Path:
    """Resolve the on-disk path for a project's promotions log."""
    return project_root / ".movate" / "promotions.yaml"


def _now_iso8601() -> str:
    """UTC ISO-8601 with millisecond precision (matches snapshot timestamps)."""
    now = datetime.now(UTC)
    millis = now.microsecond // 1000
    return now.strftime(f"%Y-%m-%dT%H:%M:%S.{millis:03d}Z")


def _whoami() -> str:
    """Best-effort ``user@host`` string for the promoted_by field.

    Falls back to empty string if either piece can't be determined —
    we never want a missing username to block a promotion. Audit
    trail is best-effort; the CLI is the source of truth for who
    actually ran the command.
    """
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    host = platform.node() or ""
    if user and host:
        return f"{user}@{host}"
    return user or host or ""


def load_log(project_root: Path) -> PromotionsLog:
    """Read the promotions log. Returns empty log if the file is absent.

    Permissive — first-time-promote on a fresh project shouldn't error.
    Malformed YAML or schema raises :class:`PromotionsStoreError`.
    """
    path = _log_path(project_root)
    if not path.is_file():
        return PromotionsLog(project_root=project_root)
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PromotionsStoreError(f"promotions.yaml is not valid YAML: {exc}") from exc
    if raw is None:
        return PromotionsLog(project_root=project_root)
    if not isinstance(raw, dict):
        raise PromotionsStoreError(
            f"promotions.yaml root must be a mapping; got {type(raw).__name__}"
        )

    raw_entries = raw.get("promotions") or []
    if not isinstance(raw_entries, list):
        raise PromotionsStoreError("'promotions' must be a list")

    log = PromotionsLog(project_root=project_root)
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise PromotionsStoreError(
                f"each promotion entry must be a mapping; got {type(entry).__name__}"
            )
        for required in ("profile", "snapshot_hash", "promoted_at"):
            if required not in entry:
                raise PromotionsStoreError(f"promotion entry missing required field {required!r}")
        eval_score_raw = entry.get("eval_score")
        eval_score: float | None
        if eval_score_raw is None:
            eval_score = None
        else:
            try:
                eval_score = float(eval_score_raw)
            except (TypeError, ValueError) as exc:
                raise PromotionsStoreError(
                    f"eval_score must be a number; got {eval_score_raw!r}"
                ) from exc
        log.append(
            Promotion(
                profile=str(entry["profile"]),
                snapshot_hash=str(entry["snapshot_hash"]),
                promoted_at=str(entry["promoted_at"]),
                promoted_by=str(entry.get("promoted_by") or ""),
                description=str(entry.get("description") or ""),
                eval_score=eval_score,
            )
        )
    return log


def save_log(log: PromotionsLog) -> None:
    """Write the promotions file atomically (temp + rename).

    Like the snapshot store, we write to a temp file and rename so
    a crash mid-write never leaves a corrupt log. No file-mode
    tightening here (unlike secrets) — promotion data is not
    sensitive on its own, just an audit log.
    """
    movate_dir = log.project_root / ".movate"
    movate_dir.mkdir(parents=True, exist_ok=True)

    path = _log_path(log.project_root)
    tmp = path.with_suffix(".yaml.tmp")
    payload = {
        "api_version": "movate/v1",
        "kind": "Promotions",
        "promotions": [
            {
                "profile": p.profile,
                "snapshot_hash": p.snapshot_hash,
                "promoted_at": p.promoted_at,
                "promoted_by": p.promoted_by,
                "description": p.description,
                "eval_score": p.eval_score,
            }
            for p in log.promotions
        ],
    }
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False))
    tmp.replace(path)


def record_promotion(
    *,
    project_root: Path,
    profile: str,
    snapshot_hash: str,
    description: str = "",
    eval_score: float | None = None,
) -> Promotion:
    """Convenience: append a new promotion entry + save in one call.

    Captures the timestamp + best-effort user@host at call time.
    Idempotent in the sense that re-promoting the same snapshot
    to the same profile creates a new entry (not deduped) —
    a "no-op" promotion still records *when the operator decided
    it was OK to leave things as-is*, which is useful audit signal.
    """
    log = load_log(project_root)
    promotion = Promotion(
        profile=profile,
        snapshot_hash=snapshot_hash,
        promoted_at=_now_iso8601(),
        promoted_by=_whoami(),
        description=description,
        eval_score=eval_score,
    )
    log.append(promotion)
    save_log(log)
    return promotion


def current_promotion(project_root: Path, profile: str) -> Promotion | None:
    """Return the most recent promotion to ``profile`` (or None)."""
    log = load_log(project_root)
    return log.current(profile)
