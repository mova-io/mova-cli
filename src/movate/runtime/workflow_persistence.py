"""Workflow bundle persistence + registry helpers (ADR 037 D1).

The workflow analogue of :mod:`movate.runtime.agent_creation` +
:mod:`movate.runtime.agent_resolver`. Persistence keeps the same shape as
agents — small text files keyed by relative path — but the workflow layout
is intentionally narrower: a ``workflow.yaml`` plus any sibling files the
spec references (typically a JSON-schema file under ``schema/`` and,
optionally, an ``evals/dataset.jsonl``). Agent refs inside a workflow are
NOT bundled here; the agent registry already owns those.

Two surfaces, mirrored from agents:

1. :func:`persist_workflow_bundle` — validate + write a freshly-uploaded
   bundle to ``<workflows_path>/<name>/``. Atomic (stages to a temp dir,
   then renames). Used by ``POST /api/v1/workflows`` (multipart or JSON).
2. :func:`publish_workflow_bundle` — content-aware dual-write into the
   durable registry (ADR 037 D1). Mirrors :func:`publish_agent_bundle`:
   computes the content hash, returns early on a no-op re-publish,
   derives a ``<version>+<hash8>`` local version on a content-vs-version
   collision so the immutable ``(name, version)`` PK holds.

Errors raise :class:`WorkflowPersistenceError` with a typed ``status_code``
the route handler maps to an HTTP code (409 for conflict, 422 for
malformed bundles, 503 for missing config).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from movate.core.models import WorkflowBundleRecord
from movate.core.workflow.spec import WorkflowSpec, WorkflowSpecLoadError, load_workflow_spec
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


# Files the bundle must always carry.
_REQUIRED_FILES: frozenset[str] = frozenset({"workflow.yaml"})

# Prefixes whose entire subtree is permitted. Workflow bundles are deliberately
# narrower than agent bundles — no skills/, no contexts/. The state schema
# typically lives at ``schema/state.json`` but workflow.yaml may also name an
# arbitrary path (``./state.json``), so we permit the schema/ subtree AND
# accept any sibling .json the spec references via state_schema.
_ALLOWED_PREFIXES: tuple[str, ...] = (
    "schema/",
    "evals/",
)

# Exact paths allowed at the workflow root, in addition to workflow.yaml.
_ALLOWED_ROOT_FILES: frozenset[str] = frozenset(
    {
        "workflow.yaml",
        # Common state-schema location for workflows whose YAML uses
        # ``state_schema: ./state.json`` at the root.
        "state.json",
    }
)


class WorkflowPersistenceError(Exception):
    """Raised on any failure that should surface as a non-2xx HTTP response.

    Mirrors :class:`movate.runtime.agent_creation.AgentCreationError`: the
    ``status_code`` attribute maps to the HTTP code the route returns.
    """

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PersistResult:
    """What :func:`persist_workflow_bundle` returns on success.

    ``spec`` is the parsed :class:`WorkflowSpec` so the route can pluck
    ``name`` / ``version`` / ``description`` for the response without a
    second load. ``files_persisted`` is the sorted list of canonical paths
    that landed under ``workflow_dir``.
    """

    spec: WorkflowSpec
    workflow_dir: Path
    files_persisted: list[str]


@dataclass(frozen=True)
class PublishResult:
    """Outcome of :func:`publish_workflow_bundle` — mirrors
    :class:`movate.runtime.agent_resolver.PublishResult` row-for-row.
    """

    published: bool
    version: str
    content_hash: str
    previous_version: str | None = None


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def content_hash(files: dict[str, str]) -> str:
    """Content-addressed hash over a bundle's ``files`` dict.

    Same convention as :func:`movate.runtime.agent_resolver.content_hash` —
    sha256 over the JSON of ``files`` with sorted keys.
    """
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Layout validation
# ---------------------------------------------------------------------------


def _validate_layout(files: dict[str, bytes]) -> None:
    """Reject bundles whose paths aren't in the allow-listed workflow layout."""
    missing = _REQUIRED_FILES - set(files.keys())
    if missing:
        raise WorkflowPersistenceError(
            f"bundle missing required files: {sorted(missing)}",
            status_code=422,
        )

    for path in files:
        # No path escapes / absolute paths / dot segments.
        if path.startswith("/") or ".." in Path(path).parts:
            raise WorkflowPersistenceError(
                f"illegal path in bundle: {path!r}",
                status_code=422,
            )
        # Root-level files: must be on the explicit allow-list.
        if "/" not in path:
            if path in _ALLOWED_ROOT_FILES:
                continue
            raise WorkflowPersistenceError(
                f"file {path!r} is not allowed at the workflow root "
                f"(allowed: {sorted(_ALLOWED_ROOT_FILES)} or "
                f"{list(_ALLOWED_PREFIXES)})",
                status_code=422,
            )
        # Subtree files: must live under one of the allowed prefixes.
        if not path.startswith(_ALLOWED_PREFIXES):
            raise WorkflowPersistenceError(
                f"file {path!r} is not under an allowed prefix {list(_ALLOWED_PREFIXES)}",
                status_code=422,
            )


def _extract_workflow_name(workflow_yaml: bytes) -> str:
    """Pull the workflow ``name`` from a ``workflow.yaml`` byte stream.

    Parsed lightly here — full Pydantic validation runs at
    :func:`load_workflow_spec` time once the bundle is staged on disk. We
    only need the name up front so we can pick the target directory and
    emit a clean 409 BEFORE writing a single byte.
    """
    try:
        raw = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError as exc:
        raise WorkflowPersistenceError(
            f"workflow.yaml is not valid YAML: {exc}", status_code=422
        ) from exc
    if not isinstance(raw, dict):
        raise WorkflowPersistenceError("workflow.yaml must be a YAML mapping", status_code=422)
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise WorkflowPersistenceError(
            "workflow.yaml is missing a string ``name`` field", status_code=422
        )
    return name


# ---------------------------------------------------------------------------
# Multipart unzip helper (mirrors agent_creation.unzip_bundle)
# ---------------------------------------------------------------------------


def unzip_bundle(bundle_bytes: bytes) -> dict[str, bytes]:
    """Inflate a uploaded ``bundle`` field into ``{rel_path: bytes}``.

    Tolerates a single top-level dir prefix (e.g. ``my-workflow/...``) by
    stripping it, mirroring :func:`movate.runtime.agent_creation.unzip_bundle`
    so the wire shape matches between the agent and workflow endpoints.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    except zipfile.BadZipFile as exc:
        raise WorkflowPersistenceError(
            f"bundle is not a valid zip file: {exc}", status_code=422
        ) from exc

    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        raise WorkflowPersistenceError("bundle zip is empty", status_code=422)

    # If every entry shares a top-level dir, strip it.
    prefixes = {m.split("/", 1)[0] for m in members if "/" in m}
    strip = ""
    if len(prefixes) == 1 and all("/" in m for m in members):
        strip = next(iter(prefixes)) + "/"

    files: dict[str, bytes] = {}
    for m in members:
        rel = m[len(strip) :] if m.startswith(strip) else m
        if not rel:
            continue
        files[rel] = zf.read(m)
    return files


# ---------------------------------------------------------------------------
# Persistence — write a bundle to disk, atomically
# ---------------------------------------------------------------------------


def persist_workflow_bundle(
    files: dict[str, bytes],
    *,
    workflows_path: Path,
    on_conflict: str = "reject",
) -> PersistResult:
    """Validate + persist a workflow bundle to ``<workflows_path>/<name>/``.

    ``files`` is a mapping of canonical relative path (e.g. ``workflow.yaml``,
    ``schema/state.json``) → file bytes. The route handler is responsible
    for assembling this dict from either individual multipart fields or
    an inflated zip via :func:`unzip_bundle`.

    ``on_conflict``:

    * ``"reject"`` (default) — raise with ``status_code=409`` if the target
      dir already exists. Used by ``POST /api/v1/workflows``.
    * ``"replace"`` — atomically swap an existing dir aside. Used by
      ``PUT /api/v1/workflows/{name}``.

    Mirrors :func:`movate.runtime.agent_creation.persist_bundle` —
    same staging-tmpdir + atomic-rename pattern, same conflict semantics.
    """
    _validate_layout(files)

    name = _extract_workflow_name(files["workflow.yaml"])
    target_dir = workflows_path / name

    if target_dir.exists() and on_conflict == "reject":
        raise WorkflowPersistenceError(
            f"workflow {name!r} already exists at {target_dir}; "
            f"use PUT /api/v1/workflows/{name} to update",
            status_code=409,
        )

    workflows_path.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{name}-", dir=workflows_path))
    try:
        _write_files(staging, files)
        # Pydantic + YAML validation. Catches duplicate node ids, bad
        # entrypoint, missing state schema file, etc. — same code path
        # the CLI's `mdk workflow validate` will use.
        try:
            spec, _ = load_workflow_spec(staging)
        except WorkflowSpecLoadError as exc:
            raise WorkflowPersistenceError(
                f"bundle failed validation: {exc}",
                status_code=422,
            ) from exc

        files_persisted = sorted(files.keys())

        if target_dir.exists():
            stale = target_dir.with_name(f".stale-{name}-{staging.name[-8:]}")
            target_dir.rename(stale)
            try:
                staging.rename(target_dir)
            except Exception:
                stale.rename(target_dir)
                raise
            try:
                shutil.rmtree(stale)
            except OSError:
                # Best-effort cleanup; the new bundle is already live.
                logger.warning("stale_workflow_dir_cleanup_failed path=%s", stale)
        else:
            staging.rename(target_dir)

        return PersistResult(
            spec=spec,
            workflow_dir=target_dir,
            files_persisted=files_persisted,
        )
    finally:
        # Final safety net — if staging still exists after a raised exception
        # we never published, rm it.
        if staging.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(staging)


def _write_files(target: Path, files: dict[str, bytes]) -> None:
    """Write every file under ``target`` honoring its relative path."""
    for rel, data in files.items():
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)


def bundle_files_from_dir(workflow_dir: Path) -> dict[str, str]:
    """Read every file under ``workflow_dir`` into a ``{rel_path: text}`` dict.

    The inverse of :func:`persist_workflow_bundle`. Used by
    :func:`publish_workflow_bundle` to capture what just landed on disk so it
    can be written into the durable registry. POSIX paths so it round-trips
    on any OS. Non-UTF-8 files are skipped with a warning — the canonical
    workflow layout is small text files.
    """
    files: dict[str, str] = {}
    root = workflow_dir.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            logger.warning("workflow_bundle_file_skipped path=%s reason=%s", path, exc)
    return files


# ---------------------------------------------------------------------------
# Soft delete (mirrors soft_delete_agent)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeleteResult:
    name: str
    deleted_dir: Path


def soft_delete_workflow(name: str, *, workflows_path: Path) -> DeleteResult:
    """Move a workflow's bundle to ``.deleted-<name>-<timestamp>/``.

    Soft delete so a botched delete is recoverable out-of-band, matching
    :func:`movate.runtime.agent_creation.soft_delete_agent`. Raises with
    ``status_code=404`` when the bundle isn't on disk.
    """
    import time  # noqa: PLC0415

    target = workflows_path / name
    if not target.exists() or not target.is_dir():
        raise WorkflowPersistenceError(
            f"workflow {name!r} not found at {target}",
            status_code=404,
        )
    timestamp = int(time.time())
    stale = target.with_name(f".deleted-{name}-{timestamp}")
    try:
        target.rename(stale)
    except Exception as exc:
        raise WorkflowPersistenceError(
            f"could not soft-delete {name!r}: {exc}",
            status_code=500,
        ) from exc
    return DeleteResult(name=name, deleted_dir=stale)


# ---------------------------------------------------------------------------
# Durable-registry publish (content-aware, mirrors publish_agent_bundle)
# ---------------------------------------------------------------------------


async def publish_workflow_bundle(
    storage: StorageProvider,
    *,
    name: str,
    tenant_id: str,
    version: str,
    files: dict[str, str],
    created_by: str | None = None,
) -> PublishResult:
    """Publish a workflow bundle to the durable registry IFF content changed.

    Mirrors :func:`movate.runtime.agent_resolver.publish_agent_bundle`:

    1. Look up the latest published bundle for ``(name, tenant_id)``.
    2. If it exists and its ``content_hash`` matches → no-op (no duplicate
       history row, no swallowed PK error).
    3. Otherwise persist a new immutable :class:`WorkflowBundleRecord`. If
       the declared ``version`` collides with a different-content history
       entry, derive ``<version>+<hash8>`` so the ``(name, version)``
       constraint holds and ``latest`` is the new content.

    Uses only the existing ``StorageProvider`` workflow surface; tenant-
    scoped throughout. New rows are NOT auto-published — promotion is
    explicit via :meth:`StorageProvider.publish_workflow_version` (the
    ``POST /api/v1/workflows/{name}/publish`` endpoint).
    """
    new_hash = content_hash(files)
    latest = await storage.get_workflow_bundle(name, tenant_id=tenant_id)
    if latest is not None and latest.content_hash == new_hash:
        return PublishResult(
            published=False,
            version=latest.version,
            content_hash=new_hash,
            previous_version=latest.version,
        )

    previous_version = latest.version if latest is not None else None
    resolved_version = await _resolve_publish_version(
        storage,
        name,
        tenant_id=tenant_id,
        declared=version,
        content_hash_=new_hash,
    )
    record = WorkflowBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=resolved_version,
        created_by=created_by,
        content_hash=new_hash,
        files=files,
        published=False,
    )
    await storage.save_workflow_bundle(record)
    logger.info(
        "workflow_published name=%s tenant_id=%s version=%s changed_from=%s",
        name,
        tenant_id,
        resolved_version,
        previous_version or "<new>",
    )
    return PublishResult(
        published=True,
        version=resolved_version,
        content_hash=new_hash,
        previous_version=previous_version,
    )


async def _resolve_publish_version(
    storage: StorageProvider,
    name: str,
    *,
    tenant_id: str,
    declared: str,
    content_hash_: str,
) -> str:
    """Pick a registry version that doesn't collide with the history PK.

    Returns ``declared`` when the operator bumped the version (typical
    publish), else ``<declared>+<hash8>`` PEP-440 local version. Mirrors
    the agent helper.
    """
    history = await storage.list_workflow_versions(name, tenant_id=tenant_id, limit=1000)
    existing = {r.version for r in history}
    if declared not in existing:
        return declared
    suffix = content_hash_[:8]
    candidate = f"{declared}+{suffix}"
    # Extremely unlikely 64-bit collision, but stay safe.
    n = 1
    while candidate in existing:
        candidate = f"{declared}+{suffix}.{n}"
        n += 1
    return candidate


def mint_revert_version(to_version: str, existing: set[str]) -> str:
    """Collision-free ``<base>+revert.N`` version for a workflow revert.

    Mirrors the agent helper so local + deployed reverts use the same
    provenance suffix.
    """
    base = to_version.split("+revert.", 1)[0]
    n = 1
    candidate = f"{base}+revert.{n}"
    while candidate in existing:
        n += 1
        candidate = f"{base}+revert.{n}"
    return candidate


__all__ = [
    "DeleteResult",
    "PersistResult",
    "PublishResult",
    "WorkflowPersistenceError",
    "bundle_files_from_dir",
    "content_hash",
    "mint_revert_version",
    "persist_workflow_bundle",
    "publish_workflow_bundle",
    "soft_delete_workflow",
    "unzip_bundle",
]
