"""Resolve a Teams user to a per-user :class:`MovateClient`.

The bot needs ONE :class:`MovateClient` per bound user — same warm
connection pool, same bearer token for the duration. Building a new
client on every Activity would lose the pool and add a TLS handshake
per request, which Teams' channel timeout doesn't tolerate.

This module owns the cache: an LRU of
``aad_object_id → MovateClient``. Cache is the inverse of the
storage layer:

* :class:`TeamsUsersStore` knows about *records on disk*.
* :class:`IdentityResolver` knows about *live clients in memory*.

A user binding flow looks like:

1. ``/movate connect`` → store.upsert_binding + resolver.invalidate
2. ``@movate run ...`` → resolver.client_for(aad_id) →
   cache hit → MovateClient.submit_job ...
3. ``/movate rotate-key`` → store.upsert_binding + resolver.invalidate
4. Bot shutdown → resolver.aclose() — closes every cached pool.
"""

from __future__ import annotations

import contextlib

from movate.core.client import MovateClient
from movate.teams_bot.storage import MissingBindingError, TeamsUsersStore

# Cap the per-user client cache so a long-lived bot doesn't accumulate
# clients for users who haven't talked to it in days. The eviction is
# LRU by insertion order via a regular dict — Python 3.7+ preserves
# insertion order, and we re-insert on every hit to maintain the LRU
# property. 256 is comfortable for an alpha pilot (a few dozen users);
# tune via constructor for higher-scale deployments.
_DEFAULT_CACHE_SIZE = 256


class IdentityResolver:
    """Per-user MovateClient resolver with a bounded LRU cache.

    Holds the :class:`TeamsUsersStore` + the runtime base URL +
    constructor args common to every per-user MovateClient. Tests
    inject a ``client_factory`` so they can substitute a fake
    MovateClient without spinning up HTTP.
    """

    def __init__(
        self,
        *,
        store: TeamsUsersStore,
        runtime_base_url: str,
        cache_size: int = _DEFAULT_CACHE_SIZE,
        client_factory: type[MovateClient] = MovateClient,
    ) -> None:
        self._store = store
        self._base_url = runtime_base_url.rstrip("/")
        self._cache: dict[str, MovateClient] = {}
        self._cache_size = cache_size
        self._client_factory = client_factory

    async def client_for(self, aad_object_id: str) -> MovateClient | None:
        """Resolve a Teams user to their per-user :class:`MovateClient`.

        Returns ``None`` when the user hasn't run ``/movate connect``
        yet — the handler dispatches this to a "not bound, please
        connect" reply.

        Raises whatever :class:`TeamsCryptoError` ``get_decrypted_key``
        raises — typically when the encryption key has rotated. The
        handler catches this and asks the user to rebind.
        """
        # Cache lookup. Pop + reinsert maintains LRU ordering on hit.
        cached = self._cache.pop(aad_object_id, None)
        if cached is not None:
            self._cache[aad_object_id] = cached
            return cached

        # Cache miss — go to the store.
        binding = await self._store.get_binding(aad_object_id)
        if binding is None:
            return None

        plaintext = await self._store.get_decrypted_key(aad_object_id)
        client = self._client_factory(
            base_url=self._base_url,
            api_key=plaintext,
        )

        # Evict the LRU entry if we're at capacity. ``next(iter(dict))``
        # is the oldest key (insertion-ordered).
        if len(self._cache) >= self._cache_size:
            oldest = next(iter(self._cache))
            evicted = self._cache.pop(oldest)
            await _safe_close(evicted)

        self._cache[aad_object_id] = client
        return client

    async def invalidate(self, aad_object_id: str) -> None:
        """Drop the cached client for one user.

        Call this after ``/movate connect`` (rebind), ``/movate
        rotate-key``, or ``/movate disconnect`` — anything that
        changes the key the user's binding resolves to.
        """
        client = self._cache.pop(aad_object_id, None)
        if client is not None:
            await _safe_close(client)

    async def aclose(self) -> None:
        """Close every cached client. Bot shutdown hook calls this so
        the underlying httpx pools don't leak event-loop handles."""
        for client in self._cache.values():
            await _safe_close(client)
        self._cache.clear()


async def _safe_close(client: MovateClient) -> None:
    """Close a MovateClient and swallow errors.

    The httpx pool's aclose() can raise if the loop is mid-tear-down
    (uvicorn shutdown race). We don't want that to take down the
    whole shutdown sequence — log + move on.
    """
    # Intentionally swallowing — close failures during shutdown are
    # noise, not signal. If this becomes a debugging concern, add
    # structlog.log inside the suppress block.
    with contextlib.suppress(Exception):
        await client.aclose()


__all__ = ["IdentityResolver", "MissingBindingError"]
