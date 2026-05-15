"""Profile registry — read / write ``~/.movate/profiles.yaml``.

Each :class:`Profile` is a named environment context: a bundle of
``target`` (reference to an mdk-config target), ``tenant_id``
(multi-tenant scoping), and operator-supplied ``description``.

The active profile lives at ``~/.movate/active-profile`` as a
one-line text file — same lightweight convention kubectl uses for
its active context. Reading it is O(1) on every command that needs
to know "what context am I in?".

Two-file split (registry + active marker) instead of one combined
file because the active selection changes often (every `profiles
use`) while the registry rarely does. Splitting avoids YAML
rewrite churn on `use`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ProfileStoreError(Exception):
    """Raised on malformed profiles.yaml or filesystem errors.

    Distinct from :class:`ProfileNotFoundError` so the CLI can map
    "registry corrupted" (exit 2) vs "you asked for a profile that
    doesn't exist" (exit 1) cleanly.
    """


class ProfileNotFoundError(ProfileStoreError):
    """Raised when a profile name doesn't resolve."""


@dataclass(frozen=True)
class Profile:
    """One named environment context.

    Fields:
        name: Operator-facing handle, e.g. ``"dev"`` / ``"staging"`` /
            ``"prod"``. Used as the secrets namespace + the
            ``MDK_TARGET`` value when active.
        target: Reference to an ``mdk config`` target. May be empty
            for local-only profiles that don't deploy.
        tenant_id: Multi-tenant scoping value. Defaults to the
            profile name when the operator doesn't override.
        description: One-line human-friendly note. Surfaces in
            ``mdk profiles list``.

    Frozen because operators shouldn't mutate profiles in place —
    every change goes through the registry's add/remove API.
    """

    name: str
    target: str = ""
    tenant_id: str = ""
    description: str = ""

    @property
    def effective_tenant_id(self) -> str:
        """Returns the explicit ``tenant_id`` or falls back to ``name``.

        Pattern lets profile authors omit ``tenant_id`` for the
        common case where profile name + tenant id match (e.g. a
        "dev" profile naturally has tenant_id "dev"). Override
        explicitly when names diverge from tenant labels.
        """
        return self.tenant_id or self.name


@dataclass
class ProfileRegistry:
    """In-memory model of ``~/.movate/profiles.yaml``.

    Mutable because :class:`Profile` is frozen — operators add /
    remove via this collection, not by re-binding inner profile
    fields. The registry itself round-trips through YAML
    deterministically.
    """

    profiles: dict[str, Profile] = field(default_factory=dict)

    def add(self, profile: Profile) -> None:
        """Insert or replace a profile by name.

        Replacement (rather than rejection on duplicate name) lets
        ``mdk profiles create`` double as an update operation.
        Operators get one verb, the CLI handles the upsert.
        """
        self.profiles[profile.name] = profile

    def remove(self, name: str) -> Profile:
        """Drop a profile. Returns the deleted entry for confirmation."""
        if name not in self.profiles:
            raise ProfileNotFoundError(f"profile {name!r} not found")
        return self.profiles.pop(name)

    def get(self, name: str) -> Profile:
        """Look up a profile or raise :class:`ProfileNotFoundError`."""
        if name not in self.profiles:
            raise ProfileNotFoundError(
                f"profile {name!r} not found; available: {sorted(self.profiles.keys())}"
            )
        return self.profiles[name]

    def list_names(self) -> list[str]:
        """Sorted list of registered profile names."""
        return sorted(self.profiles.keys())


# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------


def _movate_home() -> Path:
    """``~/.movate``, the user-level config dir.

    Mirrors the pattern other commands use (auth, snapshot, etc.).
    Honors ``$HOME`` so tests can override via ``monkeypatch.setenv``.
    """
    return Path(os.path.expanduser("~")) / ".movate"


def _registry_path() -> Path:
    return _movate_home() / "profiles.yaml"


def _active_path() -> Path:
    return _movate_home() / "active-profile"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_registry() -> ProfileRegistry:
    """Read the registry from disk. Returns empty registry if absent.

    Permissive — a missing file is the natural state for a fresh
    install, not an error. Malformed YAML or schema raises
    :class:`ProfileStoreError`.
    """
    path = _registry_path()
    if not path.is_file():
        return ProfileRegistry()
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ProfileStoreError(f"profiles.yaml is not valid YAML: {exc}") from exc
    if raw is None:
        return ProfileRegistry()
    if not isinstance(raw, dict):
        raise ProfileStoreError(f"profiles.yaml root must be a mapping; got {type(raw).__name__}")

    raw_profiles = raw.get("profiles") or []
    if not isinstance(raw_profiles, list):
        raise ProfileStoreError("'profiles' must be a list")

    registry = ProfileRegistry()
    for i, entry in enumerate(raw_profiles):
        if not isinstance(entry, dict):
            raise ProfileStoreError(f"profiles[{i}] must be a mapping")
        if "name" not in entry:
            raise ProfileStoreError(f"profiles[{i}] missing required field 'name'")
        registry.add(
            Profile(
                name=str(entry["name"]),
                target=str(entry.get("target") or ""),
                tenant_id=str(entry.get("tenant_id") or ""),
                description=str(entry.get("description") or ""),
            )
        )
    return registry


def save_registry(registry: ProfileRegistry) -> None:
    """Write the registry to disk atomically (via temp + rename).

    Atomic write protects against half-written YAML on a crashed
    `profiles create` — concurrent reads either see the old version
    or the new, never a corrupted middle state.
    """
    home = _movate_home()
    home.mkdir(parents=True, exist_ok=True)
    path = _registry_path()
    tmp = path.with_suffix(".yaml.tmp")
    payload = {
        "api_version": "movate/v1",
        "kind": "Profiles",
        "profiles": [
            {
                "name": p.name,
                "target": p.target,
                "tenant_id": p.tenant_id,
                "description": p.description,
            }
            for p in registry.profiles.values()
        ],
    }
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False))
    tmp.replace(path)


def get_active_profile() -> str | None:
    """Read the active-profile name, or None if none set."""
    path = _active_path()
    if not path.is_file():
        return None
    content = path.read_text().strip()
    return content or None


def set_active_profile(name: str | None) -> None:
    """Set or clear the active profile.

    ``None`` clears (deletes the marker file). Anything else writes
    the name to the marker; doesn't validate against the registry —
    that's the CLI's job before calling this. Lets the store stay
    primitive.
    """
    path = _active_path()
    if name is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name + "\n")
