"""DR backup/restore — a portable logical snapshot of operator-critical
control-plane state (item 26).

This is the **escape hatch**, not the primary disaster-recovery story. The
primary DR for a deployed runtime is **Azure Database for PostgreSQL Flexible
Server point-in-time-restore (PITR)** — automated, transactionally consistent,
covering *every* table (see ``docs/runbooks/dr-backup.md``). This logical
export/import exists for the cases PITR can't serve:

* migrating control-plane state between environments / clouds (a Postgres PITR
  restores *into the same server family*; a JSON snapshot is portable to a
  fresh sqlite/postgres of any version, on any cloud — the ADR-001 portability
  contract);
* seeding a brand-new deployment from a known-good baseline;
* a belt-and-suspenders off-Azure copy of the few rows an operator genuinely
  cannot reconstruct after a total loss.

**Scope — operator-critical, non-reconstructible control-plane state only:**

* **agent registry** — every published :class:`AgentBundleRecord` version
  (the registry table doubles as the version history, so a backup needs *all*
  versions, not just latest);
* **api keys** — :class:`ApiKeyRecord` rows (hash + salt only — see secrets
  note below);
* **canary configs** — :class:`CanaryConfig` champion/challenger rollout state;
* **eval + job schedules** — :class:`EvalSchedule` / :class:`JobSchedule` cron
  cadences;
* **per-tenant provider keys** — :class:`TenantProviderKey` BYOK ciphertext
  (ADR 018 — see Fernet note below).

**Out of scope by design** — high-volume, reconstructible, or
operationally-ephemeral history that PITR (not this escape hatch) owns: runs,
jobs, eval/bench records, KB chunks + knowledge-graph entities/relations,
conversation threads, feedback, agent memory, trigger-delivery / run-submission
dedup ledgers, and tenant budgets/usage. Including them would balloon the
snapshot and duplicate what PITR already protects. (No ``--include-history``
flag ships: there is no partial-history story that's both cheap and correct —
runs reference jobs reference api-keys, and a half-restored history is worse
than none. Use PITR for history.)

**Secrets posture (both already safe to export as-is):**

* **api keys** persist only ``secret_hash`` + ``salt`` (never the plaintext
  key — see :class:`ApiKeyRecord`). Exporting those hashes is fine: a restored
  row keeps the SAME hash + salt, so every existing key string an operator
  issued keeps authenticating after a restore. The plaintext was never
  recoverable and is not needed.
* **provider keys** persist a Fernet ``ciphertext`` + masked ``fingerprint``
  (ADR 018), decryptable ONLY with ``MOVATE_PROVIDER_KEY_SECRET`` — which is
  **NOT** in the export. We export the ciphertext verbatim. The restore
  environment MUST have the *same* ``MOVATE_PROVIDER_KEY_SECRET`` or the
  restored provider keys won't decrypt at run time (the rows restore, but the
  resolver can't read them — re-set them with ``mdk keys set`` if the secret
  differs).

**Idempotency:** ``import_state`` defaults to ``skip-existing`` — a row whose
unique key already exists in the target is left untouched (re-running an import
imports 0 new rows). ``overwrite`` re-saves every row (the upsert-keyed
entities last-write-win; immutable agent bundles are replaced version-by-version
after a delete). Both modes are safe to re-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from movate.core.models import (
    AgentBundleRecord,
    ApiKeyRecord,
    CanaryConfig,
    EvalSchedule,
    JobSchedule,
    TenantProviderKey,
)

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

__all__ = [
    "SCHEMA_VERSION",
    "ImportResult",
    "SnapshotError",
    "export_state",
    "import_state",
]

# Bump when the snapshot shape changes incompatibly. ``import_state`` accepts
# the current version (and is liberal about additive future-entity keys it
# doesn't know — it skips them with a note rather than crashing); a snapshot
# from a newer MAJOR is refused with a clear error so an operator never
# silently half-restores.
SCHEMA_VERSION = 1

# Generous per-entity caps for the cross-tenant list reads. Control-plane
# tables are small (one row per agent-version / key / schedule / canary /
# provider-key), so these are headroom, not a real limit — but they keep an
# accidental unbounded scan bounded.
_LIST_LIMIT = 100_000

# Order matters on import: nothing here has a hard FK, but restoring agents
# before the configs that reference their versions keeps a partially-applied
# restore (interrupted mid-run) internally sensible.
_ENTITY_ORDER = (
    "agent_bundles",
    "api_keys",
    "canary_configs",
    "eval_schedules",
    "job_schedules",
    "tenant_provider_keys",
)


class SnapshotError(ValueError):
    """A snapshot is malformed, or its ``schema_version`` is unsupported.

    Raised by :func:`import_state` so a CLI/operator gets a clean,
    actionable message instead of a deep ``KeyError`` / ``ValidationError``
    traceback on a truncated or wrong-version file.
    """


@dataclass
class ImportResult:
    """Per-entity import counts (imported vs skipped), with a running total.

    ``imported`` = rows written to the target; ``skipped`` = rows that already
    existed (``skip-existing`` mode) and were left untouched. ``unknown`` lists
    any snapshot entity keys this build doesn't recognise (forward-compat: a
    snapshot from a newer minor that added an entity is imported best-effort,
    noting what was skipped rather than crashing).
    """

    imported: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)

    def _bump(self, entity: str, *, was_imported: bool) -> None:
        bucket = self.imported if was_imported else self.skipped
        bucket[entity] = bucket.get(entity, 0) + 1

    @property
    def total_imported(self) -> int:
        return sum(self.imported.values())

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped.values())

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable summary for ``--format json`` CLI output."""
        return {
            "imported": dict(self.imported),
            "skipped": dict(self.skipped),
            "unknown": list(self.unknown),
            "total_imported": self.total_imported,
            "total_skipped": self.total_skipped,
        }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def export_state(storage: StorageProvider) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of the in-scope control-plane state.

    Backend-agnostic: reads only through the ``StorageProvider`` Protocol
    (cross-tenant list accessors + the existing per-entity lists), so the same
    function serves sqlite / postgres / in-memory. Every backend's
    ``export_state`` delegates here.

    The snapshot is ``{schema_version, exported_at, entities: {<name>: [rows]}}``
    where each row is the entity's ``model_dump(mode="json")`` (datetimes →
    ISO-8601 strings), so it round-trips byte-stably through ``json.dumps`` and
    back into the Pydantic models on import.
    """
    agent_bundles = await storage.list_all_agent_bundles(limit=_LIST_LIMIT)
    api_keys = await storage.list_api_keys(tenant_id=None, include_revoked=True)
    canary_configs = await storage.list_canary_configs(tenant_id=None, limit=_LIST_LIMIT)
    eval_schedules = await storage.list_eval_schedules(tenant_id=None, limit=_LIST_LIMIT)
    job_schedules = await storage.list_job_schedules(tenant_id=None, limit=_LIST_LIMIT)
    provider_keys = await storage.list_all_tenant_provider_keys(limit=_LIST_LIMIT)

    entities: dict[str, list[dict[str, Any]]] = {
        "agent_bundles": [b.model_dump(mode="json") for b in agent_bundles],
        "api_keys": [k.model_dump(mode="json") for k in api_keys],
        "canary_configs": [c.model_dump(mode="json") for c in canary_configs],
        "eval_schedules": [s.model_dump(mode="json") for s in eval_schedules],
        "job_schedules": [s.model_dump(mode="json") for s in job_schedules],
        "tenant_provider_keys": [k.model_dump(mode="json") for k in provider_keys],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "entities": entities,
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _validate_snapshot(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Shape + version check; return the ``entities`` map or raise SnapshotError."""
    if not isinstance(snapshot, dict):
        raise SnapshotError(f"snapshot must be a JSON object, got {type(snapshot).__name__}")
    version = snapshot.get("schema_version")
    if version is None:
        raise SnapshotError(
            "snapshot missing 'schema_version' — not a movate backup "
            "(or a pre-versioned/corrupt file)"
        )
    if not isinstance(version, int):
        raise SnapshotError(
            f"snapshot 'schema_version' must be an int, got {type(version).__name__}"
        )
    if version > SCHEMA_VERSION:
        raise SnapshotError(
            f"snapshot schema_version {version} is newer than this build supports "
            f"(max {SCHEMA_VERSION}) — upgrade mdk before restoring this backup"
        )
    entities = snapshot.get("entities")
    if not isinstance(entities, dict):
        raise SnapshotError("snapshot missing an 'entities' object")
    return entities


async def import_state(
    storage: StorageProvider,
    snapshot: dict[str, Any],
    *,
    mode: str = "skip-existing",
) -> ImportResult:
    """Load a snapshot (from :func:`export_state`) back into ``storage``.

    ``mode``:

    * ``"skip-existing"`` (default, safe) — a row whose unique key already
      exists in the target is left untouched. Re-running an import imports 0
      new rows: idempotent.
    * ``"overwrite"`` — every row is re-saved. The upsert-keyed entities
      (api keys are insert-keyed; canary / schedules / provider keys upsert
      on their unique key) last-write-win; immutable agent-bundle versions are
      replaced (delete-then-insert) so a re-import refreshes their content.

    Backend-agnostic — uses only Protocol methods (the existing per-entity
    ``get_*`` for existence checks + ``save_*`` for writes). Every backend's
    ``import_state`` delegates here.

    Raises :class:`SnapshotError` on a malformed or unsupported-version
    snapshot (never a raw ``KeyError`` / ``ValidationError``).
    """
    if mode not in ("skip-existing", "overwrite"):
        raise SnapshotError(f"unknown import mode {mode!r}; use 'skip-existing' or 'overwrite'")

    entities = _validate_snapshot(snapshot)
    result = ImportResult()
    overwrite = mode == "overwrite"

    # Note any entity keys we don't recognise (forward-compat) so a snapshot
    # from a newer minor that added an entity imports best-effort.
    for key in entities:
        if key not in _ENTITY_ORDER:
            result.unknown.append(key)

    for entity in _ENTITY_ORDER:
        rows = entities.get(entity, [])
        if not isinstance(rows, list):
            raise SnapshotError(f"snapshot entity {entity!r} must be a list of rows")
        try:
            await _import_entity(storage, entity, rows, result=result, overwrite=overwrite)
        except SnapshotError:
            raise
        except Exception as exc:
            raise SnapshotError(f"failed to import {entity!r}: {exc}") from exc

    return result


async def _import_entity(
    storage: StorageProvider,
    entity: str,
    rows: list[dict[str, Any]],
    *,
    result: ImportResult,
    overwrite: bool,
) -> None:
    """Import one entity's rows via its registered per-row handler.

    Each handler is ``(storage, row, overwrite) -> bool`` (True = a row was
    written, False = skipped because it already existed). The skip-existing vs
    overwrite decision lives inside each handler because the upsert semantics
    differ per entity (immutable agent-bundle versions delete-then-insert;
    api keys are insert-only on the key_id; the rest upsert in place).
    """
    handler = _ROW_HANDLERS[entity]
    for row in rows:
        was_imported = await handler(storage, row, overwrite=overwrite)
        result._bump(entity, was_imported=was_imported)


async def _import_agent_bundle(
    storage: StorageProvider, row: dict[str, Any], *, overwrite: bool
) -> bool:
    bundle = AgentBundleRecord.model_validate(row)
    existing = await storage.get_agent_bundle(
        bundle.name, tenant_id=bundle.tenant_id, version=bundle.version
    )
    if existing is not None:
        if not overwrite:
            return False
        # Immutable rows: replace this exact version in place.
        await storage.delete_agent_bundle(
            bundle.name, tenant_id=bundle.tenant_id, version=bundle.version
        )
    await storage.save_agent_bundle(bundle)
    return True


async def _import_api_key(
    storage: StorageProvider, row: dict[str, Any], *, overwrite: bool
) -> bool:
    key = ApiKeyRecord.model_validate(row)
    existing = await storage.get_api_key(key.key_id)
    # save_api_key is insert-only (errors on a duplicate key_id). An existing
    # key_id IS the same row (hash/salt and all), so there's nothing to update:
    # in overwrite mode we count it imported (no-op); in skip mode we skip it.
    if existing is not None:
        return overwrite
    await storage.save_api_key(key)
    return True


async def _import_canary(storage: StorageProvider, row: dict[str, Any], *, overwrite: bool) -> bool:
    config = CanaryConfig.model_validate(row)
    existing = await storage.get_canary_config(config.agent, tenant_id=config.tenant_id)
    if existing is not None and not overwrite:
        return False
    await storage.save_canary_config(config)
    return True


async def _import_eval_schedule(
    storage: StorageProvider, row: dict[str, Any], *, overwrite: bool
) -> bool:
    sched = EvalSchedule.model_validate(row)
    existing = await storage.get_eval_schedule(sched.agent, tenant_id=sched.tenant_id)
    if existing is not None and not overwrite:
        return False
    await storage.save_eval_schedule(sched)
    return True


async def _import_job_schedule(
    storage: StorageProvider, row: dict[str, Any], *, overwrite: bool
) -> bool:
    sched = JobSchedule.model_validate(row)
    existing = await storage.get_job_schedule(sched.name, tenant_id=sched.tenant_id)
    if existing is not None and not overwrite:
        return False
    await storage.save_job_schedule(sched)
    return True


async def _import_provider_key(
    storage: StorageProvider, row: dict[str, Any], *, overwrite: bool
) -> bool:
    pkey = TenantProviderKey.model_validate(row)
    existing = await storage.get_tenant_provider_key(pkey.provider, tenant_id=pkey.tenant_id)
    if existing is not None and not overwrite:
        return False
    await storage.save_tenant_provider_key(pkey)
    return True


# entity name → per-row import handler. Keys MUST match _ENTITY_ORDER.
_ROW_HANDLERS = {
    "agent_bundles": _import_agent_bundle,
    "api_keys": _import_api_key,
    "canary_configs": _import_canary,
    "eval_schedules": _import_eval_schedule,
    "job_schedules": _import_job_schedule,
    "tenant_provider_keys": _import_provider_key,
}
