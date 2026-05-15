"""Per-profile secrets store on the local filesystem.

Storage layout: ``~/.movate/secrets/<profile-name>.yaml``. One file
per profile, file mode 0600 (user-only readable + writable). The
convention matches ``~/.aws/credentials`` and ``~/.kube/config`` —
filesystem permissions are the only line of defense at rest.

File format:

  api_version: movate/v1
  kind: Secrets
  profile: dev
  secrets:
    OPENAI_API_KEY:
      value: sk-...
      description: "Primary OpenAI key for dev"
      created_at: 2026-05-14T20:30:00.123Z
    LANGFUSE_PUBLIC_KEY:
      value: lp-...
      description: "Langfuse public key"
      created_at: 2026-05-14T20:31:00.456Z

Future encryption / cloud-sync drops behind this interface — the
on-disk format will gain a ``encrypted`` flag + an opaque value
blob, but the :class:`SecretsStore` API stays the same.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml


class SecretsStoreError(Exception):
    """Raised on malformed secrets.yaml or filesystem errors."""


class SecretNotFoundError(SecretsStoreError):
    """Raised when a secret name doesn't resolve in the current profile."""


@dataclass(frozen=True)
class Secret:
    """One stored secret.

    ``value`` is the raw secret material — handle with care (the CLI
    never echoes it except in explicit ``get`` / ``export-shell``
    operations). ``description`` is operator-supplied metadata.
    ``created_at`` and ``last_rotated`` are ISO-8601 UTC timestamps;
    ``last_rotated`` tracks the most recent value change for the
    future rotation-tracking work.
    """

    name: str
    value: str
    description: str = ""
    created_at: str = ""
    last_rotated: str = ""


@dataclass
class SecretsStore:
    """In-memory view of one profile's secrets file.

    Mutable container; :class:`Secret` is frozen. Operators add /
    remove via this collection — saving writes the full file
    atomically (temp + rename).
    """

    profile: str
    secrets: dict[str, Secret] = field(default_factory=dict)

    def set(self, name: str, value: str, *, description: str = "") -> Secret:
        """Insert or update a secret. Returns the stored entry.

        Re-setting an existing name updates ``last_rotated`` (and
        keeps the original ``created_at``). Lets a future
        ``mdk secrets show`` distinguish "set yesterday" from
        "rotated this morning."
        """
        now = _now_iso8601()
        existing = self.secrets.get(name)
        created = existing.created_at if existing else now
        last_rotated = now if existing else ""
        self.secrets[name] = Secret(
            name=name,
            value=value,
            description=description or (existing.description if existing else ""),
            created_at=created,
            last_rotated=last_rotated,
        )
        return self.secrets[name]

    def get(self, name: str) -> Secret:
        """Look up a secret by name. Raises if absent."""
        if name not in self.secrets:
            raise SecretNotFoundError(f"secret {name!r} not found in profile {self.profile!r}")
        return self.secrets[name]

    def delete(self, name: str) -> Secret:
        """Remove a secret. Returns the deleted entry for confirmation."""
        if name not in self.secrets:
            raise SecretNotFoundError(f"secret {name!r} not found in profile {self.profile!r}")
        return self.secrets.pop(name)

    def names(self) -> list[str]:
        """Sorted list of secret names. Values NOT included — safe to print."""
        return sorted(self.secrets.keys())


# ---------------------------------------------------------------------------
# Filesystem paths + persistence
# ---------------------------------------------------------------------------


def _movate_home() -> Path:
    """``~/.movate`` — honors ``$HOME`` for tests."""
    return Path(os.path.expanduser("~")) / ".movate"


def _secrets_dir() -> Path:
    """``~/.movate/secrets/`` — one file per profile lives here."""
    return _movate_home() / "secrets"


def _store_path(profile: str) -> Path:
    return _secrets_dir() / f"{profile}.yaml"


def _now_iso8601() -> str:
    """UTC ISO-8601 with millisecond precision (matches snapshot/manifest)."""
    now = datetime.now(UTC)
    millis = now.microsecond // 1000
    return now.strftime(f"%Y-%m-%dT%H:%M:%S.{millis:03d}Z")


def load_store(profile: str) -> SecretsStore:
    """Read a profile's secrets file. Returns empty store if absent.

    Permissive — first-time-set on a fresh profile shouldn't error.
    Malformed YAML or schema raises :class:`SecretsStoreError`.
    """
    path = _store_path(profile)
    if not path.is_file():
        return SecretsStore(profile=profile)
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise SecretsStoreError(
            f"secrets file for profile {profile!r} is not valid YAML: {exc}"
        ) from exc
    if raw is None:
        return SecretsStore(profile=profile)
    if not isinstance(raw, dict):
        raise SecretsStoreError(f"secrets file root must be a mapping; got {type(raw).__name__}")

    raw_secrets = raw.get("secrets") or {}
    if not isinstance(raw_secrets, dict):
        raise SecretsStoreError("'secrets' must be a mapping")

    store = SecretsStore(profile=profile)
    for name, entry in raw_secrets.items():
        if not isinstance(entry, dict):
            raise SecretsStoreError(
                f"secret {name!r} entry must be a mapping; got {type(entry).__name__}"
            )
        if "value" not in entry:
            raise SecretsStoreError(f"secret {name!r} missing required field 'value'")
        store.secrets[str(name)] = Secret(
            name=str(name),
            value=str(entry["value"]),
            description=str(entry.get("description") or ""),
            created_at=str(entry.get("created_at") or ""),
            last_rotated=str(entry.get("last_rotated") or ""),
        )
    return store


def save_store(store: SecretsStore) -> None:
    """Write the secrets file atomically with 0600 perms.

    Atomic via temp + rename so a crashed `secrets set` can't leave
    a half-written file. The chmod 600 happens BEFORE the rename
    so the temp file is also user-only-readable during the brief
    window it exists.
    """
    secrets_dir = _secrets_dir()
    secrets_dir.mkdir(parents=True, exist_ok=True)
    # Ensure the secrets dir itself is also tight.
    os.chmod(secrets_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    path = _store_path(store.profile)
    tmp = path.with_suffix(".yaml.tmp")
    payload = {
        "api_version": "movate/v1",
        "kind": "Secrets",
        "profile": store.profile,
        "secrets": {
            name: {
                "value": s.value,
                "description": s.description,
                "created_at": s.created_at,
                "last_rotated": s.last_rotated,
            }
            for name, s in store.secrets.items()
        },
    }
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False))
    # Set tight perms BEFORE rename so the final file is never
    # group/world-readable, even for a moment.
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
