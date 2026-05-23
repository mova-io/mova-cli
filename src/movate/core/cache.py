"""LLM response cache — exact-match, in-process state, opt-in.

Architecture
------------

A read-through cache at the executor↔provider boundary. Identical
deterministic model calls (same model, same rendered messages, same
params, same tenant) return a stored :class:`CachedResponse` instead
of round-tripping to the provider — a cost (``$0``) and latency
(``~0``) win for repeated calls (eval reruns, idempotent retries,
duplicated workflow steps).

State is **in-process** in v1.x — exactly the same posture as
:mod:`movate.core.rate_limit`. ACA may run multiple replicas of the
API container, and each has its own :class:`InProcessCache`, so a
cache entry written on replica A is invisible to replica B. Acceptable
for v1.x: the cache is a best-effort accelerator, never a correctness
dependency (a miss just calls the provider). A shared, cross-replica
backend (``RedisCache`` / ``PostgresCache``) lands later behind the
same :class:`CacheProvider` Protocol — the executor wiring does not
change. **Semantic (embedding-similarity) matching** is likewise a
documented future behind this Protocol; v1.x is **exact-match only**.

The :class:`CacheProvider` Protocol is the seam: the executor calls
``get(key)`` / ``set(key, value, ttl_s=...)`` and never knows whether
the backend is a dict, Redis, or Postgres.

Determinism rule
----------------

**Only deterministic calls are cached.** A response sampled at
``temperature > 0`` is one draw from a distribution — replaying a
stored draw for the next identical request would be silently wrong
(it changes the statistical behavior the caller asked for). So the
executor only consults/populates the cache when ``temperature == 0``
(or temperature is unset, which every provider treats as its default
*non*-sampling-for-our-purposes only when 0 — see :func:`is_cacheable`).
The keying helper and the cacheability predicate are pure functions so
the rule is trivially testable.

Tenant isolation
-----------------

The cache key folds in ``tenant_id`` so two tenants issuing the
byte-identical prompt get **different** keys — no cross-tenant cache
leakage. This matters even in-process: a single ``mdk worker`` drains
a multi-tenant queue through one shared executor + cache.

Default OFF
-----------

``build_cache()`` returns a :class:`NoOpCache` (always-miss, never
stores) unless ``MOVATE_LLM_CACHE`` selects a real backend. With the
env var unset, behavior is byte-for-byte identical to no cache at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from movate.core.models import TokenUsage

# Default time-to-live for a cached entry, in seconds. One hour is a
# conservative balance: long enough to absorb eval reruns / retry
# storms / duplicated workflow steps, short enough that a model or
# pricing change isn't served stale all day. Override with
# ``MOVATE_LLM_CACHE_TTL_S``.
DEFAULT_TTL_S = 3600

# Default LRU bound on the in-process backend so a long-lived API
# process can't grow the dict without limit. 1024 distinct
# deterministic requests is generous for the in-process tier; the
# shared backends (later) carry their own eviction policy.
DEFAULT_MAX_ENTRIES = 1024

_CACHE_ENV = "MOVATE_LLM_CACHE"
_TTL_ENV = "MOVATE_LLM_CACHE_TTL_S"


@dataclass(frozen=True)
class CachedResponse:
    """A stored LLM completion, replayed verbatim on a cache hit.

    Deliberately a small, provider-agnostic value type (not the full
    :class:`movate.providers.base.CompletionResponse`) so the cache
    module stays free of a providers→core import edge. The executor
    reconstructs a ``CompletionResponse`` from these fields on a hit.

    ``tokens`` is preserved so observability still reports the
    *original* token usage; **cost is recomputed as $0 on a hit** by
    the executor (the win is that we pay no provider call), so this
    type carries no cost field.
    """

    text: str
    tokens: TokenUsage
    raw: dict[str, Any]


def is_cacheable(params: dict[str, Any]) -> bool:
    """Return ``True`` only when the call is deterministic enough to cache.

    The rule is **temperature == 0** (or an explicit ``temperature``
    absent AND no other sampling knob set). Caching a ``temperature >
    0`` response is wrong: it's one sample from a distribution, and
    replaying it defeats the sampling the caller asked for.

    We treat a *missing* temperature as **not cacheable** rather than
    guess the provider default — explicit ``temperature: 0`` is the
    documented opt-in for caching. This keeps the predicate honest:
    the only way an entry is cached is if the agent author pinned
    determinism.

    ``top_p < 1`` is also a sampling knob, but ``temperature == 0``
    already collapses the distribution to the argmax for every
    mainstream provider, so we don't second-guess it here.
    """
    temp = params.get("temperature")
    return temp == 0


def compute_cache_key(
    *,
    provider: str,
    messages: list[dict[str, Any]] | list[Any],
    params: dict[str, Any],
    tools: list[dict[str, Any]] | None,
    tenant_id: str,
) -> str:
    """Deterministic sha256 of the full request signature (pure helper).

    The signature folds in everything that affects the response:

    * ``provider`` — the LiteLLM-style model string (model identity).
    * ``messages`` — the rendered conversation actually sent (system +
      user + any history/tool turns). Each entry is normalized to its
      role/content/tool fields so two structurally-identical
      conversations hash the same.
    * ``params`` — temperature, max_tokens, top_p, etc. — any sampling
      / generation knob that changes the output.
    * ``tools`` — the tool specs offered to the model (a different tool
      set can yield a different answer).
    * ``tenant_id`` — folded in so two tenants with the byte-identical
      request get **different** keys. No cross-tenant leakage.

    Same inputs → same key; any differing field → a different key.
    The hash is over a canonical JSON encoding (sorted keys) so dict
    ordering can't produce spurious misses.
    """
    # Normalize messages to plain dicts. Accept either pydantic
    # ``Message`` objects (have ``.model_dump``) or already-plain
    # dicts, so callers don't have to pre-serialize.
    norm_messages: list[dict[str, Any]] = []
    for m in messages:
        if hasattr(m, "model_dump"):
            norm_messages.append(m.model_dump(exclude_none=True))
        else:
            norm_messages.append(dict(m))

    signature = {
        "provider": provider,
        "messages": norm_messages,
        "params": params,
        "tools": tools,
        "tenant_id": tenant_id,
    }
    # ``sort_keys`` + ``default=str`` makes the encoding canonical and
    # total (no surprise TypeError on an odd value type). The leading
    # version tag lets a future signature change invalidate old keys
    # cleanly rather than silently colliding.
    encoded = json.dumps(signature, sort_keys=True, default=str, separators=(",", ":"))
    return "llmcache:v1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CacheProvider(Protocol):
    """Anything that can store and replay an LLM completion by key.

    Implementations:

    * :class:`InProcessCache` — dict-backed, TTL + LRU bound,
      single-process. Selected by ``MOVATE_LLM_CACHE=memory``.
    * :class:`NoOpCache` — always-miss, never stores. The default
      (``MOVATE_LLM_CACHE`` unset / ``none``).
    * Future ``RedisCache`` / ``PostgresCache`` — same interface,
      shared across replicas. Lands when cross-replica hit rate
      becomes load-bearing. Semantic (embedding) matching is a
      separate future impl behind this same Protocol.

    ``get`` / ``set`` MUST never raise on a malfunctioning backend —
    the cache is an accelerator, not a correctness dependency. A
    failing cache must degrade to "always miss" so the provider call
    still happens.
    """

    def get(self, key: str) -> CachedResponse | None:
        """Return the stored response for ``key``, or ``None`` on miss
        (absent, expired, or backend error)."""
        ...

    def set(self, key: str, value: CachedResponse, *, ttl_s: int) -> None:
        """Store ``value`` under ``key`` for ``ttl_s`` seconds. A
        ``ttl_s <= 0`` means "do not store" (treated as a no-op)."""
        ...


@dataclass
class _Entry:
    """Internal stored value + its absolute monotonic expiry."""

    value: CachedResponse
    expires_at: float  # time.monotonic() deadline


class InProcessCache:
    """Exact-match LLM response cache, dict-backed, TTL + LRU bound.

    Single-process state. Memory is bounded two ways: a per-entry TTL
    (lazily evicted on access, so an idle key can linger past expiry
    until next touched — acceptable, it's never *served* stale) and a
    hard ``max_entries`` LRU cap (oldest-touched evicted when full).

    For dev / staging / single-replica prod this is plenty. A shared
    Redis/Postgres backend (post-v1.x) slots in behind
    :class:`CacheProvider` for cross-replica hit rate — the executor
    wiring is unchanged.
    """

    name = "in-process"

    def __init__(
        self, *, max_entries: int = DEFAULT_MAX_ENTRIES, default_ttl_s: int = DEFAULT_TTL_S
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._max_entries = max_entries
        self._default_ttl_s = default_ttl_s
        # OrderedDict gives O(1) move-to-end for LRU recency tracking.
        self._store: OrderedDict[str, _Entry] = OrderedDict()

    def get(self, key: str) -> CachedResponse | None:
        # ``time.monotonic`` so TTL doesn't break on NTP / clock jumps.
        now = time.monotonic()
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            # Expired — lazily evict and report a miss.
            del self._store[key]
            return None
        # Hit: mark most-recently-used.
        self._store.move_to_end(key)
        return entry.value

    def set(self, key: str, value: CachedResponse, *, ttl_s: int) -> None:
        if ttl_s <= 0:
            # Non-positive TTL = "don't cache". Don't store, and drop
            # any stale entry under this key.
            self._store.pop(key, None)
            return
        now = time.monotonic()
        self._store[key] = _Entry(value=value, expires_at=now + ttl_s)
        self._store.move_to_end(key)
        # LRU bound: evict oldest-touched until within capacity.
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)


@dataclass
class NoOpCache:
    """Always-miss, never-store cache. The default when caching is
    disabled (``MOVATE_LLM_CACHE`` unset / ``none``). The executor's
    cache path still calls ``get``/``set``; this just makes every
    ``get`` a miss and every ``set`` a no-op, so behavior is identical
    to having no cache at all."""

    name: str = "noop"

    def get(self, key: str) -> CachedResponse | None:
        return None

    def set(self, key: str, value: CachedResponse, *, ttl_s: int) -> None:
        return None


def cache_ttl_s() -> int:
    """Resolve the configured entry TTL from ``MOVATE_LLM_CACHE_TTL_S``
    (seconds), falling back to :data:`DEFAULT_TTL_S`. A malformed or
    non-positive value falls back to the default — a typo shouldn't
    silently disable caching."""
    raw = os.environ.get(_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_TTL_S
    try:
        ttl = int(raw)
    except ValueError:
        return DEFAULT_TTL_S
    return ttl if ttl > 0 else DEFAULT_TTL_S


def build_cache() -> CacheProvider:
    """Construct the cache backend from ``MOVATE_LLM_CACHE``.

    * unset / ``none`` / ``off`` / ``0`` → :class:`NoOpCache` (the
      default — caching disabled, zero behavior change).
    * ``memory`` / ``inprocess`` → :class:`InProcessCache` with the
      TTL from :func:`cache_ttl_s` and the default LRU bound.

    Future selectors (``redis``, ``postgres``) slot in here, each
    returning a shared-state :class:`CacheProvider` implementation —
    **not implemented in v1.x**. An unrecognized value falls back to
    :class:`NoOpCache` (fail-safe: a typo never silently enables a
    backend that doesn't exist).
    """
    selector = os.environ.get(_CACHE_ENV, "").strip().lower()
    if selector in ("memory", "inprocess", "in-process"):
        return InProcessCache(default_ttl_s=cache_ttl_s())
    # "none" / "off" / "0" / "" / anything-unknown → disabled.
    return NoOpCache()


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_S",
    "CacheProvider",
    "CachedResponse",
    "InProcessCache",
    "NoOpCache",
    "build_cache",
    "cache_ttl_s",
    "compute_cache_key",
    "is_cacheable",
]
