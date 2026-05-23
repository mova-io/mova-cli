"""OIDC device-authorization (RFC 8628) login + token cache/refresh (ADR 013 L1).

The *human* half of OIDC, complementing :mod:`movate.core.oidc_provider` (the
machine ``az``-based provider) and :mod:`movate.runtime.oidc` (the runtime's
JWT acceptance). When a target sets ``auth: oidc``:

* ``mdk auth login --target <t>`` runs :func:`run_device_code_login`, which
  drives the device-authorization grant against the target's IdP and caches
  the resulting short-lived access token (+ refresh token + expiry) per-target
  via :class:`~movate.credentials.store.CredentialsStore` (OS-keychain capable,
  ADR 012b).
* every authenticated ``--target`` call resolves its bearer through
  :class:`CachedDeviceCodeTokenProvider`, which returns the cached access
  token, silently refreshing it via the refresh-token grant when it is near
  expiry, and raising an actionable :class:`OidcTokenError` (pointing at
  ``mdk auth login``) when no usable token / refresh exists.

Design invariants (CLAUDE.md + ADR 013):

* **Default-off / back-compat.** None of this runs unless a target opts into
  ``auth: oidc``. The opaque-key path is byte-for-byte unchanged.
* **No mandatory cloud SDK.** Everything is plain OIDC HTTP via ``httpx`` +
  OIDC discovery; ``msal``/``azure-identity`` stay optional (ADR 001 / 012 D4).
* **Tokens are never logged.** The cache stores them via ``CredentialsStore``
  (mode 0600 file or OS keychain); functions here return token strings to
  callers but never emit them to logs/stdout.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from movate.core.oidc_provider import OidcTokenError

if TYPE_CHECKING:
    from movate.core.user_config import TargetConfig
    from movate.credentials.store import CredentialsStore

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Network timeout for the one-shot discovery + device-auth + token POSTs.
_HTTP_TIMEOUT_SECONDS = 15.0

# OIDC discovery doc is cached per-issuer for the process lifetime; issuers
# rotate their endpoints essentially never.
_DISCOVERY_TTL_SECONDS = 3600

# Refresh the cached access token this many seconds *before* its real expiry,
# so a token that's about to lapse mid-request gets renewed proactively.
_REFRESH_SKEW_SECONDS = 60

# The RFC 8628 grant type for polling the token endpoint.
_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Bound the device-code poll loop independently of the IdP's expires_in, so a
# misbehaving IdP can never wedge the CLI forever.
_MAX_POLL_SECONDS = 900  # 15 minutes — generous for an interactive browser step.

# Default polling interval when the device-auth response omits ``interval``.
_DEFAULT_POLL_INTERVAL = 5

# Cache-key suffixes (per target) in the credentials store. Namespaced under
# the target name so multiple oidc targets don't collide.
_TOKEN_SUFFIX = "_OIDC_TOKEN"
_REFRESH_SUFFIX = "_OIDC_REFRESH"
_EXP_SUFFIX = "_OIDC_EXP"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Endpoints:
    """The two endpoints the device-code flow needs from OIDC discovery."""

    device_authorization_endpoint: str
    token_endpoint: str


# issuer -> (_Endpoints, fetched_at_monotonic)
_discovery_cache: dict[str, tuple[_Endpoints, float]] = {}


def reset_caches() -> None:
    """Clear the module-level discovery cache (test hook)."""
    _discovery_cache.clear()


def _discover(issuer: str, *, client: httpx.Client) -> _Endpoints:
    """Resolve the device-auth + token endpoints via OIDC discovery (TTL-cached).

    ``GET {issuer}/.well-known/openid-configuration``. Raises
    :class:`OidcTokenError` (operator-actionable) on any fetch/parse failure
    or a missing ``device_authorization_endpoint``.
    """
    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached is not None and (now - cached[1]) < _DISCOVERY_TTL_SECONDS:
        return cached[0]

    well_known = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        resp = client.get(well_known)
        resp.raise_for_status()
        doc = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcTokenError(
            f"could not fetch OIDC discovery from {well_known!r}: {exc}. "
            "Confirm 'oidc_issuer' (or MOVATE_OIDC_ISSUER) points at the IdP."
        ) from exc

    device_ep = doc.get("device_authorization_endpoint")
    token_ep = doc.get("token_endpoint")
    if not isinstance(device_ep, str) or not device_ep:
        raise OidcTokenError(
            "the IdP's OIDC discovery document has no "
            "'device_authorization_endpoint' — this IdP/app-registration does "
            "not support the device-code flow. Enable it on the app "
            "registration, or use auth via the Azure CLI provider instead."
        )
    if not isinstance(token_ep, str) or not token_ep:
        raise OidcTokenError("the IdP's OIDC discovery document has no 'token_endpoint'.")

    endpoints = _Endpoints(
        device_authorization_endpoint=device_ep,
        token_endpoint=token_ep,
    )
    _discovery_cache[issuer] = (endpoints, now)
    return endpoints


# ---------------------------------------------------------------------------
# Device-code login
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCodeStart:
    """The device-authorization response a human acts on (RFC 8628 §3.2)."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    interval: int
    expires_in: int


@dataclass(frozen=True)
class TokenResult:
    """A successfully-obtained token set (no secrets logged by callers)."""

    access_token: str
    refresh_token: str | None
    # Absolute expiry as a unix timestamp (wall-clock), or None if the IdP
    # didn't return ``expires_in``.
    expires_at: float | None


def _oidc_config(target_name: str, target: TargetConfig) -> tuple[str, str, str | None]:
    """Resolve ``(issuer, client_id, scope)`` for an oidc target.

    The issuer comes from the target's ``oidc_issuer`` or, failing that, the
    runtime-shared ``MOVATE_OIDC_ISSUER`` env var (so a single value can drive
    both the runtime's acceptance and the CLI's login). ``oidc_client_id`` is
    required for the device-code flow (it identifies the app registration).
    """
    import os  # noqa: PLC0415

    issuer = (getattr(target, "oidc_issuer", None) or "").strip() or os.environ.get(
        "MOVATE_OIDC_ISSUER", ""
    ).strip()
    if not issuer:
        raise OidcTokenError(
            f"target {target_name!r} uses auth='oidc' but no OIDC issuer is "
            "configured. Set 'oidc_issuer' on the target in "
            "~/.movate/config.yaml (or export MOVATE_OIDC_ISSUER)."
        )

    client_id = (getattr(target, "oidc_client_id", None) or "").strip()
    if not client_id:
        raise OidcTokenError(
            f"target {target_name!r} uses auth='oidc' but no 'oidc_client_id' "
            "is configured. Set it on the target in ~/.movate/config.yaml — "
            "it identifies the IdP app registration for the device-code flow."
        )

    scope = (getattr(target, "oidc_scope", None) or "").strip() or None
    return issuer, client_id, scope


def start_device_code(
    target_name: str,
    target: TargetConfig,
    *,
    client: httpx.Client | None = None,
) -> DeviceCodeStart:
    """POST to the device-authorization endpoint, returning the user-facing codes.

    The first leg of the device-code flow: the IdP mints a ``device_code`` (the
    CLI polls with it) and a short ``user_code`` the human types at
    ``verification_uri`` in any browser.
    """
    issuer, client_id, scope = _oidc_config(target_name, target)
    owns_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS))
    try:
        endpoints = _discover(issuer, client=client)
        # ``openid`` + ``offline_access`` request an id-token-shaped access
        # token and a refresh token; the target's configured scope is appended
        # so the access token carries the runtime's expected audience/scope.
        scopes = ["openid", "offline_access"]
        if scope:
            scopes.append(scope)
        data = {"client_id": client_id, "scope": " ".join(scopes)}
        try:
            resp = client.post(endpoints.device_authorization_endpoint, data=data)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcTokenError(
                f"device-authorization request failed for target {target_name!r}: "
                f"{_safe_http_detail(exc)}. Confirm 'oidc_client_id' is correct "
                "and the app registration allows the device-code flow."
            ) from exc

        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_uri = payload.get("verification_uri") or payload.get(
            "verification_url"  # some IdPs use the non-spec key
        )
        if not (device_code and user_code and verification_uri):
            raise OidcTokenError(
                f"the IdP returned an incomplete device-authorization response "
                f"for target {target_name!r} (missing device_code/user_code/"
                "verification_uri)."
            )
        return DeviceCodeStart(
            device_code=str(device_code),
            user_code=str(user_code),
            verification_uri=str(verification_uri),
            verification_uri_complete=(
                str(payload["verification_uri_complete"])
                if payload.get("verification_uri_complete")
                else None
            ),
            interval=int(payload.get("interval") or _DEFAULT_POLL_INTERVAL),
            expires_in=int(payload.get("expires_in") or _MAX_POLL_SECONDS),
        )
    finally:
        if owns_client:
            client.close()


def poll_for_token(
    target_name: str,
    target: TargetConfig,
    start: DeviceCodeStart,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> TokenResult:
    """Poll the token endpoint until the user approves, honoring RFC 8628.

    Honors the IdP's ``interval`` and the ``authorization_pending`` /
    ``slow_down`` back-pressure signals. Raises an actionable
    :class:`OidcTokenError` on ``access_denied``, ``expired_token``, the local
    deadline, or any unexpected error.

    ``sleep`` / ``now`` are injectable so tests drive the loop without real
    waiting.
    """
    issuer, client_id, _scope = _oidc_config(target_name, target)
    owns_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS))
    try:
        endpoints = _discover(issuer, client=client)
        interval = max(1, start.interval)
        deadline = now() + min(start.expires_in, _MAX_POLL_SECONDS)
        data = {
            "grant_type": _DEVICE_CODE_GRANT,
            "client_id": client_id,
            "device_code": start.device_code,
        }
        while True:
            if now() >= deadline:
                raise OidcTokenError(
                    f"device-code login for target {target_name!r} timed out "
                    "before approval. Run `mdk auth login --target "
                    f"{target_name}` again and approve promptly."
                )
            sleep(interval)
            try:
                resp = client.post(endpoints.token_endpoint, data=data)
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise OidcTokenError(
                    f"token-endpoint poll failed for target {target_name!r}: "
                    f"{_safe_http_detail(exc)}."
                ) from exc

            if resp.status_code == httpx.codes.OK and payload.get("access_token"):
                return _token_result_from_payload(payload, now=now)

            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                # RFC 8628 §3.5 — bump the interval and keep polling.
                interval += 5
                continue
            if error == "access_denied":
                raise OidcTokenError(
                    f"the IdP denied the device-code login for target "
                    f"{target_name!r} (access_denied). The user declined or "
                    "lacks access."
                )
            if error == "expired_token":
                raise OidcTokenError(
                    f"the device code for target {target_name!r} expired before "
                    "approval. Run `mdk auth login` again."
                )
            # Any other OAuth error → fail with the IdP's description (never a
            # token; error/description are not secrets).
            description = payload.get("error_description")
            detail = f" — {description}" if description else ""
            raise OidcTokenError(
                f"device-code login failed for target {target_name!r}: "
                f"{error or 'unknown_error'}{detail}."
            )
    finally:
        if owns_client:
            client.close()


def run_device_code_login(
    target_name: str,
    target: TargetConfig,
    *,
    on_prompt: Callable[[DeviceCodeStart], None],
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> TokenResult:
    """Run the full device-code flow: start, prompt the human, poll for a token.

    ``on_prompt`` is invoked with the :class:`DeviceCodeStart` so the caller
    (the CLI) can print the verification URL + user code. A shared
    :class:`httpx.Client` is used across the start + poll legs.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS))
    try:
        start = start_device_code(target_name, target, client=client)
        on_prompt(start)
        return poll_for_token(target_name, target, start, client=client, sleep=sleep, now=now)
    finally:
        if owns_client:
            client.close()


def refresh_access_token(
    target_name: str,
    target: TargetConfig,
    refresh_token: str,
    *,
    client: httpx.Client | None = None,
    now: Callable[[], float] = time.monotonic,
) -> TokenResult:
    """Exchange a refresh token for a fresh access token (refresh_token grant).

    Raises :class:`OidcTokenError` when the refresh is rejected (revoked /
    expired) so callers can fall back to a "run mdk auth login" prompt. Never
    logs the refresh or access token.
    """
    issuer, client_id, scope = _oidc_config(target_name, target)
    owns_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS))
    try:
        endpoints = _discover(issuer, client=client)
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
        if scope:
            data["scope"] = " ".join(["openid", "offline_access", scope])
        try:
            resp = client.post(endpoints.token_endpoint, data=data)
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcTokenError(
                f"token refresh failed for target {target_name!r}: "
                f"{_safe_http_detail(exc)}. Run `mdk auth login --target "
                f"{target_name}`."
            ) from exc

        if resp.status_code != httpx.codes.OK or not payload.get("access_token"):
            error = payload.get("error") or f"HTTP {resp.status_code}"
            raise OidcTokenError(
                f"refresh token for target {target_name!r} is no longer valid "
                f"({error}). Run `mdk auth login --target {target_name}` to "
                "sign in again."
            )
        return _token_result_from_payload(payload, now=now, prior_refresh=refresh_token)
    finally:
        if owns_client:
            client.close()


def _token_result_from_payload(
    payload: dict[str, object],
    *,
    now: Callable[[], float],
    prior_refresh: str | None = None,
) -> TokenResult:
    """Build a :class:`TokenResult` from a token-endpoint JSON payload.

    ``expires_at`` is computed against wall-clock ``time.time()`` (so the cache
    survives process restarts), but the relative ``expires_in`` is what the IdP
    returns. A refresh response may omit ``refresh_token`` (the IdP keeps the
    old one valid) — fall back to ``prior_refresh`` so we don't drop it.
    """
    access_token = str(payload["access_token"])
    refresh_token = payload.get("refresh_token")
    refresh_str = str(refresh_token) if refresh_token else prior_refresh
    expires_in = payload.get("expires_in")
    expires_at: float | None = None
    if isinstance(expires_in, (int, float)) or (
        isinstance(expires_in, str) and expires_in.isdigit()
    ):
        expires_at = time.time() + float(int(expires_in))
    return TokenResult(
        access_token=access_token,
        refresh_token=refresh_str,
        expires_at=expires_at,
    )


def _safe_http_detail(exc: Exception) -> str:
    """A short, secret-free description of an HTTP/JSON failure for messages.

    We deliberately do NOT include response bodies (a token grant body can
    echo back submitted parameters); only the exception class + status line.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Token cache (per target) via CredentialsStore
# ---------------------------------------------------------------------------


def _cache_keys(target_name: str) -> tuple[str, str, str]:
    """Return the ``(token, refresh, exp)`` credentials-store keys for a target.

    Uppercased + non-alphanumerics → ``_`` so the key is a valid ``.env`` name
    regardless of the operator's target naming (e.g. ``prod-eu`` → ``PROD_EU``).
    """
    safe = "".join(c if c.isalnum() else "_" for c in target_name).upper()
    prefix = f"MDK_{safe}"
    return (
        f"{prefix}{_TOKEN_SUFFIX}",
        f"{prefix}{_REFRESH_SUFFIX}",
        f"{prefix}{_EXP_SUFFIX}",
    )


class DeviceCodeTokenCache:
    """Persist/read the per-target OIDC token set via :class:`CredentialsStore`.

    Three entries per target — access token, refresh token, and absolute
    expiry. Stored in the same backend (file or OS keychain) as provider keys,
    so ``MOVATE_CRED_BACKEND=keychain`` covers them too. Token values never
    leave this class except as the returned strings callers need.
    """

    def __init__(self, store: CredentialsStore | None = None) -> None:
        if store is None:
            from movate.credentials.store import CredentialsStore  # noqa: PLC0415

            store = CredentialsStore()
        self._store = store

    def save(self, target_name: str, result: TokenResult) -> None:
        token_key, refresh_key, exp_key = _cache_keys(target_name)
        self._store.set(token_key, result.access_token)
        if result.refresh_token:
            self._store.set(refresh_key, result.refresh_token)
        if result.expires_at is not None:
            self._store.set(exp_key, str(int(result.expires_at)))

    def load(self, target_name: str) -> TokenResult | None:
        token_key, refresh_key, exp_key = _cache_keys(target_name)
        entries = self._store.read()
        access_token = entries.get(token_key)
        if not access_token:
            return None
        refresh_token = entries.get(refresh_key) or None
        exp_raw = entries.get(exp_key)
        expires_at: float | None = None
        if exp_raw and exp_raw.isdigit():
            expires_at = float(exp_raw)
        return TokenResult(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )

    def clear(self, target_name: str) -> bool:
        """Delete all cached entries for a target. True if anything was removed."""
        removed = False
        for key in _cache_keys(target_name):
            if self._store.delete(key):
                removed = True
        return removed


# ---------------------------------------------------------------------------
# Cached / device-code OidcTokenProvider
# ---------------------------------------------------------------------------


class CachedDeviceCodeTokenProvider:
    """An :class:`~movate.core.oidc_provider.OidcTokenProvider` over the cache.

    ``get_token`` returns the cached access token, silently refreshing it via
    the refresh-token grant when it is missing / near expiry. When there's no
    cached token at all, or the refresh is dead, it raises an actionable
    :class:`OidcTokenError` telling the operator to run ``mdk auth login`` —
    this is the general/default human path (no ``az`` needed).
    """

    def __init__(self, cache: DeviceCodeTokenCache | None = None) -> None:
        self._cache = cache or DeviceCodeTokenCache()

    def get_token(self, target_name: str, target: TargetConfig) -> str:
        cached = self._cache.load(target_name)
        if cached is None:
            raise OidcTokenError(
                f"no cached OIDC token for target {target_name!r}. "
                f"Run `mdk auth login --target {target_name}` to sign in."
            )

        if not _is_near_expiry(cached.expires_at):
            return cached.access_token

        # Near/at expiry — try a silent refresh.
        if not cached.refresh_token:
            raise OidcTokenError(
                f"the OIDC token for target {target_name!r} has expired and no "
                f"refresh token is cached. Run `mdk auth login --target "
                f"{target_name}` to sign in again."
            )
        refreshed = refresh_access_token(target_name, target, cached.refresh_token)
        self._cache.save(target_name, refreshed)
        return refreshed.access_token


def _is_near_expiry(expires_at: float | None) -> bool:
    """True when an access token is missing an expiry or within the skew window.

    A ``None`` expiry is treated as "refresh if possible" (we can't prove it's
    still valid) but only matters when a refresh token exists; the provider
    falls back to returning the token when no refresh is available.
    """
    if expires_at is None:
        return False
    return time.time() >= (expires_at - _REFRESH_SKEW_SECONDS)


__all__ = [
    "CachedDeviceCodeTokenProvider",
    "DeviceCodeStart",
    "DeviceCodeTokenCache",
    "TokenResult",
    "poll_for_token",
    "refresh_access_token",
    "reset_caches",
    "run_device_code_login",
    "start_device_code",
]
