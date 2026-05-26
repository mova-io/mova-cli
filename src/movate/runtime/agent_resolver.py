"""Resolve agents from the durable registry (ADR 014 D2).

The deployed runtime stores published agents as versioned, tenant-scoped
rows behind the :class:`~movate.storage.base.StorageProvider` Protocol
(ADR 014 D1 — the storage layer shipped in step 1). This module is the
*resolution* half: turn a ``(name, tenant_id, version)`` registry row
back into a runnable :class:`~movate.core.loader.AgentBundle` so that
**every pod** — the API and the async worker — sees the same agents,
surviving recycles and multi-replica scale-out (closes BACKLOG #109).

Design (low-risk, backward-compatible — registry-first, FS-fallback):

* :func:`resolve_agent_bundle` tries ``storage.get_agent_bundle`` first.
  On a hit it **materializes** the bundle's ``files`` dict to a
  version-keyed per-pod cache dir (written once per ``(tenant, name,
  version)``, reused after) and calls the **unchanged**
  :func:`~movate.core.loader.load_agent`. On a miss it falls back to the
  caller-supplied in-memory list (today's filesystem-scanned
  ``app.state.agents`` / the worker's ``self._agents``). The fallback
  keeps local ``mdk serve --agents ./dir`` and the entire existing test
  suite working byte-for-byte when the registry is empty.

* :func:`bundle_files_from_dir` reads a persisted agent directory into
  the ``{relative_path: contents}`` dict the registry stores — used by
  the API publish endpoints to **dual-write** to the registry alongside
  the existing filesystem ``persist_bundle``.

* :func:`import_filesystem_agents` does the one-time, idempotent
  filesystem → registry seed so agents that pre-date the registry become
  resolvable without manual work.

The materialization cache key includes the version, so a freshly
published version is a natural cache miss → every pod picks it up
without any cross-pod invalidation machinery (ADR 014 risks section).
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from movate.core.config import AgentDefaults
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import AgentBundleRecord
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)

# Per-pod materialization cache root. A single dir under the system
# tempdir keyed by ``<tenant_id>/<name>/<version>/`` — written once per
# version, reused afterwards. Not the repo, not the deployed agents_path;
# a scratch dir the pod can lose on recycle (the durable copy lives in
# storage). The leading ``mdk-agents`` namespaces it so the operator can
# eyeball / clear it.
_CACHE_ROOT = Path(tempfile.gettempdir()) / "mdk-agents"

# Bundle entries that constitute an agent-local skills/contexts layout —
# their presence means the materialized dir should itself be the project
# root so the loader's ``<project_root>/skills`` + ``<project_root>/
# contexts`` resolution finds the self-contained bundle copies.
_PROJECT_LOCAL_PREFIXES: tuple[str, ...] = ("skills/", "contexts/")


def content_hash(files: dict[str, str]) -> str:
    """Content-addressed hash over a bundle's ``files`` dict.

    Matches the convention the storage conformance tests use
    (``sha256`` over the JSON of ``files`` with sorted keys) so a
    re-publish of identical bytes yields a stable, comparable hash.
    """
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def bundle_files_from_dir(agent_dir: Path) -> dict[str, str]:
    """Read every file under ``agent_dir`` into a ``{rel_path: text}`` dict.

    The inverse of :func:`materialize_bundle`. Used by the publish
    endpoints to capture what ``persist_bundle`` just wrote to disk so it
    can be dual-written into the durable registry. Paths are stored
    POSIX-style (``schema/input.json``) so they round-trip identically on
    any OS. Binary / non-UTF-8 files are skipped with a warning — the KB
    is excluded from the bundle by design (ADR 014 D1; it lives in
    pgvector), and the canonical agent layout is small text files.
    """
    files: dict[str, str] = {}
    root = agent_dir.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            # A non-text file in the bundle dir (shouldn't happen for the
            # canonical layout) — skip rather than corrupt the row.
            logger.warning("bundle_file_skipped path=%s reason=%s", path, exc)
    return files


def materialize_bundle(record: AgentBundleRecord) -> Path:
    """Write a bundle's ``files`` to its version-keyed cache dir; return it.

    Idempotent + cheap on the warm path: if the dir already exists with a
    matching ``.content_hash`` marker, the existing dir is reused without
    rewriting a single file. A version is immutable (ADR 014 D3), so a
    cache hit can be trusted. The cache key is
    ``<tenant_id>/<name>/<version>/`` — version inclusion means a new
    publish is a natural miss that every pod re-materializes on first use.
    """
    target = _CACHE_ROOT / record.tenant_id / record.name / record.version
    marker = target / ".content_hash"

    # Warm path — already materialized this exact version. Trust the
    # immutable-version invariant and reuse it.
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() == record.content_hash:
                return target
        except OSError:
            pass  # fall through to a fresh write

    # Cold path — (re)materialize. Write to a sibling staging dir then
    # atomic-rename so a concurrent reader never sees a half-written
    # bundle (two pods, or two requests in one pod, can race here).
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{record.version}-", dir=target.parent))
    try:
        _write_files(staging, record.files)
        # If the bundle carries an agent-local skills/ or contexts/ tree,
        # drop an (empty) project marker so load_agent's project-root
        # walk resolves to THIS dir — making the self-contained bundle
        # copies resolvable. Agents without those trees don't need it.
        if (
            any(key.startswith(_PROJECT_LOCAL_PREFIXES) for key in record.files)
            and "project.yaml" not in record.files
        ):
            (staging / "project.yaml").write_text("{}\n", encoding="utf-8")
        (staging / ".content_hash").write_text(record.content_hash, encoding="utf-8")

        if target.exists():
            # Another writer won the race (or a stale version with a
            # different hash). Replace atomically: swap the old dir aside,
            # move ours in, best-effort clean up the old one.
            import shutil  # noqa: PLC0415

            stale = target.with_name(f".stale-{record.version}-{staging.name[-8:]}")
            target.rename(stale)
            try:
                staging.rename(target)
            except OSError:
                stale.rename(target)
                raise
            shutil.rmtree(stale, ignore_errors=True)
        else:
            staging.rename(target)
    except Exception:
        import shutil  # noqa: PLC0415

        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


async def resolve_agent_bundle(
    storage: StorageProvider,
    name: str,
    *,
    tenant_id: str,
    version: str | None = None,
    fallback: list[AgentBundle] | None = None,
) -> AgentBundle | None:
    """Resolve an agent to a runnable :class:`AgentBundle`.

    Registry-first, filesystem-fallback:

    1. Look up ``storage.get_agent_bundle(name, tenant_id=..., version=...)``
       (``version=None`` → latest). On a hit, materialize the bundle to a
       version-keyed per-pod cache dir and ``load_agent`` it.
    2. On a miss (no registry row for this tenant), fall back to the
       ``fallback`` list — today's filesystem-scanned in-memory registry
       (``app.state.agents`` for the API, ``self._agents`` for the
       worker). This is what keeps local ``mdk serve --agents ./dir`` and
       the existing tests green when the registry is empty.

    Returns ``None`` when neither the registry nor the fallback has the
    agent — the caller raises its own 404 / ``unknown_agent`` outcome.

    Tenant scoping: the registry lookup is tenant-scoped (a different
    tenant's agent returns ``None`` from storage). The filesystem
    fallback is the existing (non-tenant) behavior and is only consulted
    on a registry miss.
    """
    record = await storage.get_agent_bundle(name, tenant_id=tenant_id, version=version)
    if record is not None:
        try:
            agent_dir = materialize_bundle(record)
            # Empty defaults: the materialized bundle is self-contained
            # and must NOT inherit the worker/API process's CWD project
            # config. Mirrors how persist_bundle validates a staged bundle.
            return load_agent(agent_dir, defaults=AgentDefaults())
        except (AgentLoadError, OSError) as exc:
            # A registry row that won't materialize/load is a real
            # problem, but it shouldn't mask a usable filesystem copy.
            # Log loud, then fall through to the FS fallback.
            logger.warning(
                "registry_bundle_load_failed name=%s tenant_id=%s version=%s reason=%s",
                name,
                tenant_id,
                version or "<latest>",
                exc,
            )

    if fallback is not None:
        # Version-aware fallback: if an explicit version was requested,
        # only match a fallback bundle of that version; otherwise any
        # name match (the FS registry holds one bundle per name).
        for bundle in fallback:
            if bundle.spec.name != name:
                continue
            if version is not None and bundle.spec.version != version:
                continue
            return bundle
    return None


async def import_filesystem_agents(
    storage: StorageProvider,
    agents: list[AgentBundle],
    *,
    tenant_id: str,
    created_by: str | None = None,
) -> int:
    """One-time, idempotent filesystem → registry seed (ADR 014 D5).

    For each filesystem-scanned ``AgentBundle`` not yet in the registry
    (no ``(name, version)`` row for ``tenant_id``), read its dir into a
    ``files`` dict and ``save_agent_bundle`` it. Returns the number of
    bundles imported. Idempotent: an already-present ``(name, version)``
    is skipped, so re-running on every boot is safe and cheap.

    Best-effort + non-destructive: a single bundle that fails to read or
    save is logged and skipped — it never aborts the import or drops the
    filesystem copy (which still serves via the resolver's fallback).
    Callers must guard this so a local ``mdk serve`` without durable
    storage doesn't trip over it.
    """
    imported = 0
    for bundle in agents:
        name = bundle.spec.name
        version = bundle.spec.version
        try:
            existing = await storage.get_agent_bundle(name, tenant_id=tenant_id, version=version)
            if existing is not None:
                continue  # already seeded — idempotent skip
            files = bundle_files_from_dir(bundle.agent_dir)
            record = AgentBundleRecord(
                name=name,
                tenant_id=tenant_id,
                version=version,
                created_by=created_by,
                content_hash=content_hash(files),
                files=files,
            )
            await storage.save_agent_bundle(record)
            imported += 1
            logger.info(
                "agent_imported_to_registry name=%s version=%s tenant_id=%s",
                name,
                version,
                tenant_id,
            )
        except Exception:
            # Never let one bad bundle abort the seed (or take the boot
            # down). The FS copy still resolves via the fallback.
            logger.warning(
                "agent_import_skipped name=%s version=%s tenant_id=%s",
                name,
                version,
                tenant_id,
                exc_info=True,
            )
    return imported


@dataclass(frozen=True)
class PublishResult:
    """Outcome of :func:`publish_agent_bundle` (ADR 021 D2).

    ``published`` is ``True`` when a NEW registry row was written (the
    bundle's content changed vs. the latest published version), ``False``
    for a no-op (content unchanged — nothing was written, immutability +
    audit stay clean). ``version`` is the version of the row that now
    serves as ``latest`` — either the bundle's declared ``agent.yaml``
    version, or a derived ``<version>+<hash8>`` PEP-440 local version when
    the declared version collided with a different-content history entry.
    ``content_hash`` is the bundle's content hash (the same value whether
    published or skipped). ``previous_version`` is the version that WAS
    latest before this publish (``None`` for a first publish).
    """

    published: bool
    version: str
    content_hash: str
    previous_version: str | None = None


async def publish_agent_bundle(
    storage: StorageProvider,
    *,
    name: str,
    tenant_id: str,
    version: str,
    files: dict[str, str],
    created_by: str | None = None,
) -> PublishResult:
    """Publish a bundle to the durable registry IFF its content changed (ADR 021 D2).

    The compare-and-publish that makes a re-deploy actually update what
    runs, while preserving ADR 014's immutable ``(name, tenant, version)``
    rows + audit history:

    1. Look up the **latest** published bundle for ``(name, tenant_id)``.
    2. If it exists and its ``content_hash`` equals this bundle's hash →
       **no-op** (``published=False``). Nothing is written — an unchanged
       re-deploy never adds a duplicate history row.
    3. Otherwise the content changed (or it's a first publish): persist a
       **new** :class:`AgentBundleRecord` so ``get_agent_bundle(version=
       None)`` (the run-resolution source, ADR 014 D1) serves the new
       content. Version selection:
       * If ``version`` (the declared ``agent.yaml`` version) is **not**
         already in this agent's history → use it verbatim.
       * If it collides (the operator edited content without bumping the
         version) → derive a distinct PEP-440 *local version*
         ``<version>+<hash8>`` so the immutable ``(name, version)``
         constraint holds and ``latest`` (newest ``created_at``) is the
         new content. The derived label lives ONLY on the registry row;
         the on-disk ``agent.yaml`` is untouched.

    Uses only the existing ``StorageProvider`` surface
    (``get_agent_bundle`` / ``list_agent_versions`` / ``save_agent_bundle``)
    — no new backend method, no schema change. Tenant-scoped throughout.
    """
    new_hash = content_hash(files)
    latest = await storage.get_agent_bundle(name, tenant_id=tenant_id)
    if latest is not None and latest.content_hash == new_hash:
        # Byte-identical to what's already serving — no-op. Don't write a
        # duplicate history row (keeps the audit trail meaningful).
        return PublishResult(
            published=False,
            version=latest.version,
            content_hash=new_hash,
            previous_version=latest.version,
        )

    previous_version = latest.version if latest is not None else None
    resolved_version = await _resolve_publish_version(
        storage, name, tenant_id=tenant_id, declared=version, content_hash_=new_hash
    )
    record = AgentBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=resolved_version,
        created_by=created_by,
        content_hash=new_hash,
        files=files,
    )
    await storage.save_agent_bundle(record)
    logger.info(
        "agent_published name=%s tenant_id=%s version=%s changed_from=%s",
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
    """Pick a registry version for a changed bundle (ADR 021 D2).

    Returns ``declared`` verbatim when it isn't already in the agent's
    version history. When it collides (content changed but the
    ``agent.yaml`` version stayed the same), returns a PEP-440 local
    version ``<declared>+<hash8>`` derived from the content hash so the
    immutable ``(name, version)`` constraint holds. A second
    ``+<hash8>`` is never nested (the hash is stable per content), and an
    already-stripped declared value is used as the base.
    """
    existing = {r.version for r in await storage.list_agent_versions(name, tenant_id=tenant_id)}
    if declared not in existing:
        return declared
    # Strip any prior ``+<hash8>`` build tag so we don't nest local
    # versions if a derived label is ever re-fed as the declared version.
    base = declared.split("+", 1)[0]
    candidate = f"{base}+{content_hash_[:8]}"
    if candidate not in existing:
        return candidate
    # Extremely unlikely (same content already published under this
    # derived label) — fall through to a counter so we still write a
    # distinct, legible row rather than colliding on the PK.
    n = 2
    while f"{candidate}.{n}" in existing:
        n += 1
    return f"{candidate}.{n}"


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Write a ``{rel_path: contents}`` dict under ``root``.

    Defends against path traversal — a ``..`` segment or an absolute key
    is rejected (the registry stores trusted, validated bundles, but the
    materializer is the trust boundary on the read-back path)."""
    for rel, contents in files.items():
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise AgentLoadError(f"unsafe bundle path: {rel!r}")
        dest = root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(contents, encoding="utf-8")


__all__ = [
    "PublishResult",
    "bundle_files_from_dir",
    "content_hash",
    "import_filesystem_agents",
    "materialize_bundle",
    "publish_agent_bundle",
    "resolve_agent_bundle",
]
