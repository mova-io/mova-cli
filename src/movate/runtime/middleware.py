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

from fastapi import Header, Response

from movate.core.auth import ApiKeyParseError, check_record, parse_api_key
from movate.core.rate_limit import NoOpRateLimiter, RateLimiter
from movate.runtime.errors import auth_required, rate_limited
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    """What handlers receive after a successful auth.

    Carries only what handlers legitimately need — the tenant for
    scoping queries, the key id for audit attribution. Handlers MUST
    NOT reach back to the underlying ``ApiKeyRecord`` (no plaintext
    secret on the wire ever).
    """

    tenant_id: str
    api_key_id: str
    env: str
    scope: str | None = None
    """Permission scope from the ApiKeyRecord. ``"fleet-admin"`` grants
    access to admin-only endpoints. ``None`` = standard tenant key."""


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
            scope=record.scope,
        )

    return auth_dependency


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


__all__ = ["AuthContext", "make_auth_dependency"]
