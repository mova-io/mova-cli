"""Optional OIDC JWT validation for the runtime (ADR 012 D3).

The runtime's default and portable auth path is the opaque ``mvt_*`` key
(see :mod:`movate.core.auth`). This module adds an **additive, default-off**
second path: when ``MOVATE_OIDC_ISSUER`` is set, the auth middleware will
*also* accept an OIDC JWT bearer, validate it against the issuer's JWKS, and
map its claims onto the same :class:`~movate.runtime.middleware.AuthContext`
that the opaque path returns.

Design notes / invariants:

* **Off unless configured.** :func:`oidc_config` returns ``None`` when
  ``MOVATE_OIDC_ISSUER`` is unset, and the middleware never calls into this
  module in that case. Existing deployments are byte-for-byte unaffected.
* **Generic, not Azure-bound.** Entra/Azure AD is *one* value of the issuer
  URL; Okta / Google / Keycloak work identically. Tenant mapping is an
  explicit configurable claim, never hardcoded.
* **Security (critical).** Validation passes an explicit **asymmetric-only**
  algorithm allowlist (RS*/ES*/PS*). ``HS*`` is never accepted — accepting it
  enables the public-key-as-HMAC-secret downgrade attack — and ``none`` (the
  unsigned-token bypass) is likewise impossible. Signature, ``aud``, ``iss``
  and ``exp`` (with a small clock-skew leeway) are all verified.
* **No token logging.** Failures log a short ``reason`` only — never the token
  or its claims.

JWKS handling: OIDC discovery (``GET {issuer}/.well-known/openid-configuration``)
resolves ``jwks_uri``; a :class:`jwt.PyJWKClient` then fetches + caches signing
keys and resolves the right key per token via its ``kid`` header (so key
rotation is handled transparently). Both the discovery result and the JWK
client are cached module-level, keyed by issuer, so we don't refetch per
request. Tests monkeypatch the discovery + signing-key resolution so no real
network is touched.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt
from jwt import PyJWKClient

if TYPE_CHECKING:
    from movate.runtime.middleware import AuthContext

logger = logging.getLogger(__name__)

# Asymmetric-only algorithm allowlist. NEVER add an HS* algorithm here
# (public-key-as-HMAC-secret downgrade attack) and NEVER ``none``
# (unsigned-token bypass). These are the standard OIDC asymmetric signing
# algorithms; an IdP that signs with anything outside this set is rejected.
ALLOWED_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
)

# Number of dot-separated segments in a JWS compact serialization
# (header.payload.signature).
_JWS_SEGMENTS = 2

# Clock-skew tolerance for ``exp`` (and ``nbf``/``iat``) so a few seconds of
# drift between the IdP and the runtime doesn't spuriously 401 a fresh token.
LEEWAY_SECONDS = 60

# How long a cached OIDC discovery document is reused before re-fetching.
# Issuers rotate ``jwks_uri`` essentially never, and PyJWKClient handles
# *key* rotation itself by ``kid``, so a generous TTL is fine.
_DISCOVERY_TTL_SECONDS = 3600

# Network timeout for the one-shot discovery fetch (PyJWKClient has its own).
_DISCOVERY_TIMEOUT_SECONDS = 5


class OidcValidationError(Exception):
    """Raised when a presented JWT fails OIDC validation.

    Carries a short ``reason`` for *internal* logging only — the middleware
    maps every variant to the same opaque 401, exactly like the opaque-key
    path, so the discriminator is never echoed to the caller.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OidcConfig:
    """OIDC settings, read from the environment at request/dependency time.

    ``issuer`` being present is the master switch; the middleware only reaches
    OIDC validation when :func:`oidc_config` returns a non-``None`` value.
    """

    issuer: str
    audience: str | None
    tenant_claim: str
    env_claim: str | None
    default_env: str
    scope_claim: str | None
    """Claim carrying the token's least-privilege scopes (ADR 013 L2). Read
    from ``MOVATE_OIDC_SCOPE_CLAIM`` (e.g. ``scp`` for Azure AD delegated
    scopes, or ``roles`` for app roles). The claim value may be a
    space-delimited string (the OAuth ``scope`` convention) or a JSON
    list. When unset, or the claim is absent on the token, the OIDC
    identity falls back to the legacy default ``{read, run, eval}`` — same
    rule as a scopeless opaque key."""


def oidc_config() -> OidcConfig | None:
    """Build the :class:`OidcConfig` from env, or ``None`` if OIDC is off.

    ``MOVATE_OIDC_ISSUER`` is the master switch: unset/empty → ``None`` →
    the middleware never attempts OIDC and behavior is unchanged.

    Env vars (all read fresh per call, so tests + redeploys see changes
    without process restart):

    * ``MOVATE_OIDC_ISSUER`` — IdP issuer URL (master switch). e.g.
      ``https://login.microsoftonline.com/<tenant>/v2.0``.
    * ``MOVATE_OIDC_AUDIENCE`` — the ``aud`` to accept. **Required when OIDC
      is enabled.** If the issuer is set but this is unset, every OIDC token is
      rejected (fail closed) — a shared issuer (e.g. Azure AD's per-tenant
      issuer) mints tokens for many apps, so accepting any audience would let a
      token for a different app authenticate here.
    * ``MOVATE_OIDC_TENANT_CLAIM`` — which claim carries the movate tenant id
      (default ``tid``, the Entra tenant-id claim; override for other IdPs).
    * ``MOVATE_OIDC_ENV_CLAIM`` — optional claim carrying the movate env
      (``live``/``test``). When unset or absent on the token,
      ``MOVATE_OIDC_DEFAULT_ENV`` is used.
    * ``MOVATE_OIDC_DEFAULT_ENV`` — env value when no env claim resolves
      (default ``live``).
    * ``MOVATE_OIDC_SCOPE_CLAIM`` — optional claim carrying the token's
      authorization scopes (ADR 013 L2). Space-delimited string or JSON
      list. When unset or absent on the token, the identity falls back to
      the legacy default ``{read, run, eval}``.
    """
    issuer = os.environ.get("MOVATE_OIDC_ISSUER", "").strip()
    if not issuer:
        return None
    audience = os.environ.get("MOVATE_OIDC_AUDIENCE", "").strip() or None
    tenant_claim = os.environ.get("MOVATE_OIDC_TENANT_CLAIM", "").strip() or "tid"
    env_claim = os.environ.get("MOVATE_OIDC_ENV_CLAIM", "").strip() or None
    default_env = os.environ.get("MOVATE_OIDC_DEFAULT_ENV", "").strip() or "live"
    scope_claim = os.environ.get("MOVATE_OIDC_SCOPE_CLAIM", "").strip() or None
    return OidcConfig(
        issuer=issuer,
        audience=audience,
        tenant_claim=tenant_claim,
        env_claim=env_claim,
        default_env=default_env,
        scope_claim=scope_claim,
    )


def looks_like_jwt(token: str) -> bool:
    """Cheap shape check: a JWS compact serialization is three base64url
    segments separated by dots, and OIDC JWTs start with ``eyJ`` (the
    base64url of ``{"``). This is a *routing* heuristic only — it decides
    which validator to try, not whether the token is valid. A malformed
    ``eyJ…`` string still fails real validation downstream and 401s.
    """
    return token.startswith("eyJ") and token.count(".") == _JWS_SEGMENTS


# --- Discovery + JWKS caching ------------------------------------------------
#
# Keyed by issuer so a process talking to multiple issuers (rare, but the
# config is per-request) keeps a client per issuer. PyJWKClient caches the
# fetched keys internally and re-fetches on an unknown ``kid`` (rotation),
# so we only cache the *client* + the discovered ``jwks_uri`` here.

_jwks_clients: dict[str, PyJWKClient] = {}
# issuer -> (jwks_uri, fetched_at_monotonic)
_discovery_cache: dict[str, tuple[str, float]] = {}


def _fetch_discovery(issuer: str) -> str:
    """Resolve ``jwks_uri`` via OIDC discovery, with a TTL cache.

    Separated out so tests can monkeypatch it (or the higher-level
    :func:`_jwks_client`) and avoid any network. Raises
    :class:`OidcValidationError` on any fetch/parse failure.
    """
    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached is not None and (now - cached[1]) < _DISCOVERY_TTL_SECONDS:
        return cached[0]

    well_known = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(well_known, timeout=_DISCOVERY_TIMEOUT_SECONDS) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network, JSON, anything
        raise OidcValidationError("discovery_failed") from exc

    jwks_uri = doc.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise OidcValidationError("discovery_no_jwks_uri")

    _discovery_cache[issuer] = (jwks_uri, now)
    return jwks_uri


def _jwks_client(issuer: str) -> PyJWKClient:
    """Return a cached :class:`PyJWKClient` for ``issuer``.

    Builds (and caches) the client from the discovered ``jwks_uri`` on first
    use. The client itself caches signing keys and resolves the right one by
    the token's ``kid``, transparently re-fetching on rotation.
    """
    client = _jwks_clients.get(issuer)
    if client is not None:
        return client
    jwks_uri = _fetch_discovery(issuer)
    client = PyJWKClient(jwks_uri, cache_keys=True)
    _jwks_clients[issuer] = client
    return client


def reset_caches() -> None:
    """Clear the module-level discovery + JWKS caches (test hook)."""
    _jwks_clients.clear()
    _discovery_cache.clear()


def validate_oidc_token(token: str, config: OidcConfig) -> AuthContext:
    """Validate an OIDC JWT and map its claims to an :class:`AuthContext`.

    Raises :class:`OidcValidationError` (mapped to 401 by the caller) on any
    failure: bad signature, wrong ``aud``/``iss``, expired, disallowed alg,
    or a missing tenant claim. Never logs the token or its claims.
    """
    from movate.runtime.middleware import AuthContext  # noqa: PLC0415 - avoid import cycle

    if config.audience is None:
        # Fail closed: an issuer with no configured ``aud`` would accept any
        # token that issuer minted — including tokens for *other* apps under a
        # shared issuer (Azure AD's per-tenant issuer is shared across every
        # app registration, so a token for a different app would otherwise
        # authenticate here). Require an explicit MOVATE_OIDC_AUDIENCE.
        raise OidcValidationError("oidc_audience_not_configured")

    try:
        client = _jwks_client(config.issuer)
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=config.audience,
            issuer=config.issuer,
            leeway=LEEWAY_SECONDS,
            options={
                # Audience is guaranteed configured (guarded above) — always verify.
                "verify_aud": True,
                "require": ["exp", "iss"],
            },
        )
    except OidcValidationError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise OidcValidationError("oidc_expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise OidcValidationError("oidc_bad_audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise OidcValidationError("oidc_bad_issuer") from exc
    except jwt.InvalidAlgorithmError as exc:
        raise OidcValidationError("oidc_bad_algorithm") from exc
    except jwt.PyJWTError as exc:
        # Catch-all for signature / decode / key-resolution failures.
        raise OidcValidationError("oidc_invalid") from exc

    tenant_id = claims.get(config.tenant_claim)
    if not isinstance(tenant_id, str) or not tenant_id:
        raise OidcValidationError("oidc_missing_tenant_claim")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise OidcValidationError("oidc_missing_sub")

    env = config.default_env
    if config.env_claim:
        env_value = claims.get(config.env_claim)
        if isinstance(env_value, str) and env_value:
            env = env_value

    scopes = _map_scopes_from_claims(claims, config.scope_claim)

    # ``api_key_id`` is a stable, audit-friendly identity derived from the
    # subject; it deliberately is NOT a real ``mvt_*`` key id (OIDC tokens
    # have no row in the api_keys table).
    return AuthContext(
        tenant_id=tenant_id,
        api_key_id=f"oidc:{sub}",
        env=env,
        scopes=frozenset(scopes),
    )


def _map_scopes_from_claims(claims: dict[str, object], scope_claim: str | None) -> set[str]:
    """Resolve an OIDC token's authorization scopes (ADR 013 L2).

    Mirrors the opaque-key back-compat rule
    (:func:`movate.core.auth.effective_scopes`): when no scope claim is
    configured, or the claim is absent / empty on the token, fall back to
    the legacy default ``{read, run, eval}``. Otherwise read the claim,
    which may be a **space-delimited string** (the OAuth ``scope``
    convention, e.g. Azure AD's ``scp``) or a **JSON list** (e.g. Entra
    app ``roles``).
    """
    # Imported lazily to keep this module importable without core.auth at
    # module load (and to avoid any import-order surprises).
    from movate.core.auth import (  # noqa: PLC0415
        LEGACY_DEFAULT_SCOPES,
        normalize_scopes,
    )

    if not scope_claim:
        return set(LEGACY_DEFAULT_SCOPES)
    raw = claims.get(scope_claim)
    if isinstance(raw, str):
        parts = raw.split()
    elif isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = []
    normalized = normalize_scopes(parts)
    if not normalized:
        # Claim absent / empty / wrong type → legacy default, same as a
        # scopeless key. (Fail-open to read/run/eval, never to admin.)
        return set(LEGACY_DEFAULT_SCOPES)
    return set(normalized)


__all__ = [
    "ALLOWED_ALGORITHMS",
    "LEEWAY_SECONDS",
    "OidcConfig",
    "OidcValidationError",
    "looks_like_jwt",
    "oidc_config",
    "reset_caches",
    "validate_oidc_token",
]
