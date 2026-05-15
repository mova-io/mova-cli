"""User-level CLI config — ``~/.movate/config.yaml``.

Distinct from the *project* config in ``movate/core/config.py`` (which
reads ``movate.yaml`` at the repo root for project-wide defaults like
bench models). User config is per-machine: the list of deployment
targets a developer talks to, the active one, and where each one's
bearer token comes from.

Layout::

    targets:
      local:
        url: http://127.0.0.1:8000
        key_env: MOVATE_LOCAL_KEY
      prod:
        url: https://movate-prod-api.eastus2.azurecontainerapps.io
        key_env: MOVATE_PROD_KEY
    active: local

**Bearer tokens never live in the config file.** Each target points at
an env var via ``key_env``; the CLI resolves it at call time. Operators
keep tokens in their shell init / password manager / 1Password CLI
plugin; the config file can safely be committed to a dotfiles repo.

Config path is configurable via ``MOVATE_CONFIG_PATH`` for tests +
CI hermetic runs; defaults to ``~/.movate/config.yaml``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class TargetConfig(BaseModel):
    """One deployment movate-cli can talk to.

    Beyond the runtime URL + bearer-token env var, optional Azure
    fields tie a target to its deploy infrastructure so ``movate deploy
    --target <name>`` knows where to push images + which Container
    Apps to update. All Azure fields are optional — pure read-only
    targets (e.g. a customer's runtime you can submit jobs to but
    can't deploy) leave them None.

    ``extra="allow"`` so operators can annotate targets with custom
    fields (links to dashboards, runbook URLs, IT-ops references)
    without the loader rejecting them. The CLI ignores unknown fields
    at read time and preserves them on rewrite via Pydantic v2's
    ``model_extra`` mechanism.
    """

    model_config = ConfigDict(extra="allow")

    url: str = Field(
        ...,
        description="Base URL of the runtime (no trailing slash; /healthz is appended).",
    )
    key_env: str = Field(
        ...,
        description="Name of the env var that holds the bearer token (e.g. MOVATE_PROD_KEY).",
    )

    # --- Optional deploy config (used by `movate deploy`) -----------------
    azure_subscription: str | None = Field(
        default=None,
        description="Azure subscription id. Passed to `az --subscription`.",
    )
    azure_resource_group: str | None = Field(
        default=None,
        description="Resource group containing the ACA env + ACR (e.g. movate-dev-rg).",
    )
    azure_acr_name: str | None = Field(
        default=None,
        description="ACR registry name without the .azurecr.io suffix (e.g. movatedevacr).",
    )
    azure_env: str | None = Field(
        default=None,
        description=(
            "Environment label baked into resource names (dev / staging / prod). "
            "Used to derive Container App names: movate-{env}-api, movate-{env}-worker."
        ),
    )


class UserConfig(BaseModel):
    """The contents of ``~/.movate/config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    targets: dict[str, TargetConfig] = Field(default_factory=dict)
    active: str | None = Field(
        default=None,
        description=(
            "Name of the default target. CLI commands default to this when --target is omitted."
        ),
    )


class UserConfigError(Exception):
    """Raised for unrecoverable config issues (malformed YAML, missing target, etc.)."""


def config_path() -> Path:
    """Resolve the config file location.

    ``MOVATE_CONFIG_PATH`` overrides for tests / CI; otherwise
    defaults to ``~/.movate/config.yaml``.
    """
    override = os.environ.get("MOVATE_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path("~/.movate/config.yaml").expanduser()


def load_user_config() -> UserConfig:
    """Read the config file, or return an empty :class:`UserConfig`.

    Missing file is fine (first-run UX). Malformed YAML is NOT fine —
    surfaces a clean error so the user can fix it.
    """
    path = config_path()
    if not path.exists():
        return UserConfig()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise UserConfigError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return UserConfig.model_validate(raw)
    except Exception as exc:
        raise UserConfigError(f"config validation failed at {path}:\n{exc}") from exc


def save_user_config(cfg: UserConfig) -> Path:
    """Write the config file, creating ``~/.movate/`` if needed."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg.model_dump(exclude_none=True), sort_keys=True))
    return path


def resolve_target(name: str | None = None) -> tuple[str, TargetConfig]:
    """Pick a target by explicit name, or fall back to ``config.active``.

    Returns ``(name, target)``. Raises :class:`UserConfigError` if no
    target matches.
    """
    cfg = load_user_config()
    if name is None:
        if cfg.active is None:
            raise UserConfigError(
                "no --target specified and no active target set. "
                "Run `movate config add-target <name> ...` first."
            )
        name = cfg.active
    if name not in cfg.targets:
        available = ", ".join(sorted(cfg.targets)) or "(none)"
        raise UserConfigError(f"target {name!r} not found in config. Available: {available}")
    return name, cfg.targets[name]


def resolve_bearer_token(target: TargetConfig) -> str:
    """Read the bearer token from the env var named in ``target.key_env``.

    Errors loudly if the env var is unset or empty — silent fall-throughs
    to 401 from the server are a worse UX than a clear "set $FOO".
    """
    token = os.environ.get(target.key_env, "")
    if not token:
        raise UserConfigError(
            f"env var {target.key_env!r} is unset or empty. "
            f"Set it to the bearer token for this target."
        )
    return token


__all__ = [
    "TargetConfig",
    "UserConfig",
    "UserConfigError",
    "config_path",
    "load_user_config",
    "resolve_bearer_token",
    "resolve_target",
    "save_user_config",
]
