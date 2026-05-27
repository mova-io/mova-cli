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
from typing import Literal

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

    # --- Optional OIDC auth (ADR 012 D4) ----------------------------------
    # Default ``"key"`` keeps the opaque-key path byte-for-byte unchanged for
    # every existing target. ``"oidc"`` opts a target into federated auth: the
    # CLI obtains a short-lived OIDC JWT from an OidcTokenProvider (default:
    # `az account get-access-token`) instead of reading ``key_env``.
    auth: Literal["key", "oidc"] = Field(
        default="key",
        description=(
            "Bearer source: 'key' (default) reads key_env; 'oidc' obtains an "
            "OIDC JWT via a token provider (default: the Azure CLI)."
        ),
    )
    oidc_resource: str | None = Field(
        default=None,
        description=(
            "Token audience/resource for OIDC auth (passed to "
            "`az account get-access-token --resource`). Required when auth='oidc' "
            "unless oidc_scope is set. Must match the runtime's MOVATE_OIDC_AUDIENCE."
        ),
    )
    oidc_scope: str | None = Field(
        default=None,
        description=(
            "OAuth2 scope for OIDC auth (passed to `az account get-access-token "
            "--scope`, and appended to the device-code login's requested scopes). "
            "Alternative to oidc_resource for IdPs/flows that scope by scope "
            "rather than resource."
        ),
    )
    # --- Device-code login config (ADR 013 L1) ----------------------------
    # The interactive `mdk auth login --target <t>` device-code flow (RFC 8628)
    # is plain OIDC HTTP — it needs the IdP issuer (for discovery) + the app
    # registration's client id. Both default None; only consulted when a target
    # opts into auth='oidc' AND uses the device-code provider (no `az` needed).
    oidc_issuer: str | None = Field(
        default=None,
        description=(
            "OIDC issuer URL for the device-code login flow (used for "
            "`{issuer}/.well-known/openid-configuration` discovery). Falls back "
            "to the MOVATE_OIDC_ISSUER env var when unset, so one value can "
            "drive both the runtime's acceptance and the CLI's login."
        ),
    )
    oidc_client_id: str | None = Field(
        default=None,
        description=(
            "IdP app-registration client id for the device-code login flow "
            "(RFC 8628). Required when using `mdk auth login` for an "
            "auth='oidc' target via the device-code provider."
        ),
    )
    oidc_provider: Literal["device-code", "azure-cli"] = Field(
        default="device-code",
        description=(
            "Which OIDC token provider to use for auth='oidc'. "
            "'device-code' (default) is the general, no-cloud-SDK human path: "
            "`mdk auth login` runs the device-authorization flow and caches a "
            "short-lived token. 'azure-cli' shells out to "
            "`az account get-access-token` instead (no `mdk auth login` step)."
        ),
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
    azure_keyvault: str | None = Field(
        default=None,
        description=(
            "Key Vault name (no FQDN, e.g. movate-dev-kv-mvt) holding the "
            "`bootstrap-api-key` secret the runtime seeds at startup. When set, "
            "`mdk deploy` recovers a missing/stale bearer by PULLING this "
            "guaranteed-trusted key rather than minting a fresh one in-pod. The "
            "vault name embeds an operator-chosen suffix so it can't be derived "
            "from azure_env alone — set it explicitly to enable auto-pull."
        ),
    )


class ScaffoldUserConfig(BaseModel):
    """User-level defaults for ``mdk init --llm`` scaffolding (ADR 026 D6).

    Lowest persistent layer of the scaffold-model precedence — settable via
    ``mdk config set scaffold.model <model>``. A project's
    ``project.yaml: scaffold.model`` overrides this; both override the
    built-in key-matched default. ``extra="allow"`` leaves room for future
    scaffold knobs without a breaking schema bump.
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = Field(
        default=None,
        description=(
            "LiteLLM-style model string that drives `mdk init --llm` "
            "generation when neither --llm-model, MDK_LLM_MODEL, nor a "
            "project-level scaffold.model is set."
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
    scaffold: ScaffoldUserConfig = Field(
        default_factory=ScaffoldUserConfig,
        description=(
            "User-level defaults for `mdk init --llm` scaffolding (ADR 026 D6). "
            "Set via `mdk config set scaffold.model <model>`."
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
    data = cfg.model_dump(exclude_none=True)
    # Don't emit an empty ``scaffold: {}`` block (ADR 026 D6) — the field
    # is opt-in, so a default-empty config stays byte-clean for the common
    # deploy-only case. A populated scaffold (model set) still round-trips.
    if not data.get("scaffold"):
        data.pop("scaffold", None)
    path.write_text(yaml.safe_dump(data, sort_keys=True))
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
    "ScaffoldUserConfig",
    "TargetConfig",
    "UserConfig",
    "UserConfigError",
    "config_path",
    "load_user_config",
    "resolve_bearer_token",
    "resolve_target",
    "save_user_config",
]
