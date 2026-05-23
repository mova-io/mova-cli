"""Auth middleware — composes ``core/auth`` primitives over the wire.

The actual auth decision tree (parse → lookup → check) lives in
``core/auth``. This module is the *FastAPI dependency wrapper* that:

1. Pulls the bearer token from ``Authorization``.
2. Parses it via :func:`parse_api_key`.
3. Looks up the stored ``ApiKeyRecord`` via the storage Protocol.
4. Validates via :func:`check_record`.
5. Schedules a fire-and-forget ``touch_api_key`` so ``last_used_at``
   reflects this call without blocking the request.
6. Returns an :class:`AuthContext` for handlers — the **only** thing
   they should pull off auth, never the raw record.

Every failure mode returns the same ``401 AUTH_REQUIRED`` shape via
:func:`auth_required`; the discriminator is logged but never echoed
to the caller (timing-oracle defense).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Response

from movate.core.auth import (
    ApiKeyParseError,
    check_record,
    effective_scopes,
    parse_api_key,
)
from movate.core.rate_limit import NoOpRateLimiter, RateLimiter
from movate.runtime.errors import auth_required, forbidden, rate_limited
from movate.runtime.oidc import (
    OidcValidationError,
    looks_like_jwt,
    oidc_config,
    validate_oidc_token,
)
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    """What handlers receive after a successful auth.

    Carries only what handlers legitimately need — the tenant for
    scoping queries, the key id for audit attribution, and the resolved
    authorization scopes. Handlers MUST NOT reach back to the underlying
    ``ApiKeyRecord`` (no plaintext secret on the wire ever).
    """

    tenant_id: str
    api_key_id: str
    env: str
    scopes: frozenset[str] = frozenset()
    """Resolved least-privilege scopes (ADR 013 L2). On the opaque-key
    path this is :func:`movate.core.auth.effective_scopes` of the stored
    record (so a scopeless key resolves to the legacy ``{read, run,
    eval}`` default; a legacy ``fleet-admin`` key resolves to the full
    set). On the OIDC path it's mapped from ``MOVATE_OIDC_SCOPE_CLAIM``,
    falling back to the same legacy default when the claim is absent.
    Enforced per endpoint by :func:`require_scope`."""

    @property
    def scope(self) -> str | None:
        """Back-compat shim for the pre-ADR-013 single-scope field.

        Returns ``"fleet-admin"`` when that scope is present (preserving
        the historical boolean-ish check ``ctx.scope == "fleet-admin"``),
        else ``None``. New code should test membership in
        :attr:`scopes` / use :func:`require_scope` instead."""
        return "fleet-admin" if "fleet-admin" in self.scopes else None


# ----------------------------------------------------------------------
# Dependency factory
#
# FastAPI dependencies are functions; they can't directly take a
# storage backend at decoration time. We curry storage at app-build
# time (in app.py) and the dependency closes over it.
# ----------------------------------------------------------------------


def make_auth_dependency(
    storage: StorageProvider,
    rate_limiter: RateLimiter | None = None,
) -> Callable[..., Awaitable[AuthContext]]:
    """Build the FastAPI auth dependency bound to ``storage`` + an
    optional ``rate_limiter``.

    Called once in :func:`build_app`. Tests build a fresh app per case
    so each one closes over its own ``InMemoryStorage`` (and, when
    rate-limit testing, its own limiter with a low capacity).

    ``rate_limiter=None`` → uses :class:`NoOpRateLimiter` (always
    allow). Default behavior preserved for callers that haven't
    opted in. The headers ``X-RateLimit-*`` still attach with the
    sentinel zero limit so clients don't see them appear/disappear
    based on opt-in.
    """
    limiter: RateLimiter = rate_limiter or NoOpRateLimiter()

    async def auth_dependency(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthContext:
        if authorization is None:
            logger.info("auth_failure reason=missing_header")
            raise auth_required()

        # Expect `Authorization: Bearer <key>`. Tolerate case on the
        # scheme (RFC 7235) but require exactly one space.
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            logger.info("auth_failure reason=bad_scheme")
            raise auth_required()

        # Token-shape branch (ADR 012 D3). The portable opaque ``mvt_*``
        # path below is the default and is byte-for-byte unchanged. A JWT
        # (``eyJ…``) is only *attempted* as OIDC when an issuer is
        # configured (``MOVATE_OIDC_ISSUER``); a JWT presented with OIDC
        # off — or anything that isn't a JWT — falls through to the opaque
        # path, which fails parse and returns today's identical 401. The
        # OIDC path returns early with an AuthContext built from the JWT's
        # claims (no api_keys row, no touch, no rate-limit bucket — those
        # are tied to the opaque-key model).
        oidc_cfg = oidc_config()
        if oidc_cfg is not None and looks_like_jwt(token):
            try:
                return validate_oidc_token(token, oidc_cfg)
            except OidcValidationError as exc:
                logger.info("auth_failure reason=%s", exc.reason)
                raise auth_required() from None

        try:
            parsed = parse_api_key(token)
        except ApiKeyParseError:
            logger.info("auth_failure reason=parse_error")
            raise auth_required() from None

        record = await storage.get_api_key(parsed.key_id)
        failure = check_record(parsed, record)
        if failure is not None:
            logger.info("auth_failure reason=%s", failure.reason)
            raise auth_required()

        # Touch ``last_used_at`` inline. Originally this was a
        # fire-and-forget ``asyncio.create_task``, which races
        # against asyncpg's pool RESET path: the create_task can
        # acquire the same connection mid-reset and trigger
        # "another operation is in progress" 500s on the next
        # request. A single indexed UPDATE is sub-millisecond, so
        # the latency cost of awaiting is negligible vs the cost
        # of a flaky service.
        assert record is not None
        # ``check_record`` has already cross-checked the presented key's
        # tenant prefix matches the record's tenant_id, so we pass
        # ``record.tenant_id`` to touch_api_key with confidence — the
        # WHERE clause is defense in depth, not the primary check.
        await _safe_touch(storage, record.key_id, record.tenant_id)

        # Rate-limit AFTER auth succeeds — we use ``record.key_id`` as
        # the bucket key (not the presented token, which differs on
        # every refresh). Unauthenticated requests never reach here,
        # so the limiter is never asked about an anonymous identity.
        # If the bucket is empty, raise 429 with Retry-After + the
        # same X-RateLimit-* headers we set on the 200 path.
        decision = await limiter.check(record.key_id)
        # Attach the headers regardless — gives clients a way to see
        # their current budget on every successful response.
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        response.headers["X-RateLimit-Reset"] = str(decision.reset_at_unix)
        if not decision.allowed:
            logger.info(
                "rate_limited key_id=%s limit=%d retry_after=%s",
                record.key_id,
                decision.limit,
                decision.retry_after_seconds,
            )
            assert decision.retry_after_seconds is not None
            raise rate_limited(
                retry_after_seconds=decision.retry_after_seconds,
                limit=decision.limit,
                reset_at_unix=decision.reset_at_unix,
            )

        return AuthContext(
            tenant_id=record.tenant_id,
            api_key_id=record.key_id,
            env=record.env.value,
            scopes=frozenset(effective_scopes(record)),
        )

    return auth_dependency


def require_scope(
    auth_dependency: Callable[..., Awaitable[AuthContext]],
    *needed: str,
) -> Callable[..., Awaitable[AuthContext]]:
    """Build a FastAPI dependency that 403s unless the caller has every
    scope in ``needed`` (ADR 013 L2).

    ``auth_dependency`` is **the app's own** ``make_auth_dependency``
    closure (built once in ``build_app``). The returned scope-checker
    depends on *that exact callable*, so FastAPI's per-request dependency
    cache resolves the bearer parse + storage lookup + rate-limit charge
    **once** — shared with the handler's own ``ctx = Depends(auth_dep)``.
    (Passing the same object is what makes the cache hit; an indirection
    through ``app.dependency_overrides`` would create a second cache key
    and double-charge the limiter.)

    Layer it per endpoint group::

        @app.post(
            "/agents",
            dependencies=[Depends(require_scope(auth_dep, "admin"))],
        )

    All ``needed`` scopes must be present (AND semantics). The flat scope
    model has no hierarchy, so e.g. an ``admin`` key does **not** implicitly
    satisfy ``read`` — endpoints declare exactly the scope they need.

    A missing scope returns the standard ``403 FORBIDDEN`` envelope with a
    clear, non-sensitive message naming the required scope. (It is safe to
    name the scope: the caller is already authenticated, and the scope set
    is public contract.)
    """

    async def scope_dependency(
        ctx: AuthContext = Depends(auth_dependency),
    ) -> AuthContext:
        missing = [s for s in needed if s not in ctx.scopes]
        if missing:
            logger.info(
                "scope_denied key_id=%s needed=%s have=%s",
                ctx.api_key_id,
                sorted(needed),
                sorted(ctx.scopes),
            )
            raise forbidden(
                f"missing required scope(s): {', '.join(sorted(missing))}",
            )
        return ctx

    return scope_dependency


async def _safe_touch(storage: StorageProvider, key_id: str, tenant_id: str) -> None:
    """``touch_api_key`` wrapped so a write failure can't crash the loop.

    The fire-and-forget task is detached from the request lifecycle, so
    if it raises, asyncio logs it and moves on. Belt-and-suspenders:
    catch here too so we don't leak storage exceptions into the
    asyncio default handler.
    """
    try:
        await storage.touch_api_key(key_id, tenant_id=tenant_id)
    except Exception:
        # Really do want to swallow everything — fire-and-forget contract.
        logger.warning("touch_api_key failed for %s", key_id, exc_info=True)


__all__ = [
    "AuthContext",
    "make_auth_dependency",
    "require_scope",
]
