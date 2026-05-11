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

from fastapi import Header

from movate.core.auth import ApiKeyParseError, check_record, parse_api_key
from movate.runtime.errors import auth_required
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


# ----------------------------------------------------------------------
# Dependency factory
#
# FastAPI dependencies are functions; they can't directly take a
# storage backend at decoration time. We curry storage at app-build
# time (in app.py) and the dependency closes over it.
# ----------------------------------------------------------------------


def make_auth_dependency(
    storage: StorageProvider,
) -> Callable[..., Awaitable[AuthContext]]:
    """Build the FastAPI auth dependency bound to ``storage``.

    Called once in :func:`build_app`. Tests build a fresh app per case
    so each one closes over its own ``InMemoryStorage``.
    """

    async def auth_dependency(
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

        return AuthContext(
            tenant_id=record.tenant_id,
            api_key_id=record.key_id,
            env=record.env.value,
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
