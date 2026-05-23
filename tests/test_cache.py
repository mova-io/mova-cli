"""LLM response cache — backend behavior, key helper, executor wiring.

Three layers, mirroring ``test_rate_limit.py``'s structure:

1. **Backend behavior** — :class:`InProcessCache` set→get hit, TTL
   expiry → miss, LRU eviction; :class:`NoOpCache` always-miss.
2. **Key helper** — :func:`compute_cache_key` determinism + tenant
   isolation; :func:`is_cacheable` temperature rule;
   :func:`build_cache` env selection.
3. **Executor integration** — with cache ON a repeated temp==0 call
   hits the cache (provider called once, hit is $0); a temp>0 call is
   never cached; with cache OFF (default) the provider is always
   called.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from movate.core.cache import (
    DEFAULT_TTL_S,
    CachedResponse,
    InProcessCache,
    NoOpCache,
    build_cache,
    cache_ttl_s,
    compute_cache_key,
    is_cacheable,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest, TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

# ---------------------------------------------------------------------------
# 1. Backend behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_clock(monkeypatch) -> Iterator[list[float]]:
    """Pin ``time.monotonic`` in the cache module to a mutable holder
    so TTL tests can fast-forward deterministically."""
    holder = [1000.0]
    monkeypatch.setattr("movate.core.cache.time.monotonic", lambda: holder[0])
    yield holder


def _resp(text: str) -> CachedResponse:
    return CachedResponse(text=text, tokens=TokenUsage(input=3, output=5), raw={"k": "v"})


@pytest.mark.unit
def test_inprocess_set_then_get_hit() -> None:
    cache = InProcessCache()
    cache.set("k1", _resp("hello"), ttl_s=60)
    got = cache.get("k1")
    assert got is not None
    assert got.text == "hello"
    assert got.tokens == TokenUsage(input=3, output=5)


@pytest.mark.unit
def test_inprocess_miss_on_absent_key() -> None:
    cache = InProcessCache()
    assert cache.get("never-set") is None


@pytest.mark.unit
def test_inprocess_ttl_expiry_is_a_miss(fixed_clock) -> None:
    cache = InProcessCache()
    cache.set("k", _resp("v"), ttl_s=30)
    # Still fresh at +29s.
    fixed_clock[0] += 29
    assert cache.get("k") is not None
    # Expired at +31s total → miss.
    fixed_clock[0] += 2
    assert cache.get("k") is None


@pytest.mark.unit
def test_inprocess_nonpositive_ttl_does_not_store() -> None:
    cache = InProcessCache()
    cache.set("k", _resp("v"), ttl_s=0)
    assert cache.get("k") is None
    cache.set("k2", _resp("v"), ttl_s=-5)
    assert cache.get("k2") is None


@pytest.mark.unit
def test_inprocess_lru_bound_evicts_oldest() -> None:
    cache = InProcessCache(max_entries=2)
    cache.set("a", _resp("A"), ttl_s=60)
    cache.set("b", _resp("B"), ttl_s=60)
    # Touch "a" so "b" becomes the least-recently-used.
    assert cache.get("a") is not None
    # Insert "c" → over capacity → evict LRU ("b").
    cache.set("c", _resp("C"), ttl_s=60)
    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert cache.get("b") is None  # evicted


@pytest.mark.unit
def test_inprocess_rejects_zero_max_entries() -> None:
    with pytest.raises(ValueError):
        InProcessCache(max_entries=0)


@pytest.mark.unit
def test_noop_always_misses_and_never_stores() -> None:
    cache = NoOpCache()
    cache.set("k", _resp("v"), ttl_s=60)
    assert cache.get("k") is None


# ---------------------------------------------------------------------------
# 2. Key helper + cacheability + factory
# ---------------------------------------------------------------------------


def _key(**overrides) -> str:
    base = dict(
        provider="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "hello"}],
        params={"temperature": 0},
        tools=None,
        tenant_id="tenant-a",
    )
    base.update(overrides)
    return compute_cache_key(**base)


@pytest.mark.unit
def test_key_same_request_same_key() -> None:
    assert _key() == _key()


@pytest.mark.unit
def test_key_differs_on_model() -> None:
    assert _key() != _key(provider="anthropic/claude-haiku-4-5")


@pytest.mark.unit
def test_key_differs_on_prompt() -> None:
    assert _key() != _key(messages=[{"role": "user", "content": "different"}])


@pytest.mark.unit
def test_key_differs_on_params() -> None:
    assert _key() != _key(params={"temperature": 0, "max_tokens": 99})


@pytest.mark.unit
def test_key_differs_on_tools() -> None:
    tool = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    assert _key() != _key(tools=tool)


@pytest.mark.unit
def test_key_tenant_isolation() -> None:
    """Same prompt, different tenant → different key. No cross-tenant leak."""
    assert _key(tenant_id="tenant-a") != _key(tenant_id="tenant-b")


@pytest.mark.unit
def test_key_accepts_pydantic_messages() -> None:
    """The helper normalizes pydantic Message objects identically to dicts."""
    from movate.providers.base import Message  # noqa: PLC0415

    msg_key = compute_cache_key(
        provider="openai/x",
        messages=[Message(role="user", content="hi")],
        params={"temperature": 0},
        tools=None,
        tenant_id="t",
    )
    dict_key = compute_cache_key(
        provider="openai/x",
        messages=[{"role": "user", "content": "hi"}],
        params={"temperature": 0},
        tools=None,
        tenant_id="t",
    )
    assert msg_key == dict_key


@pytest.mark.unit
def test_is_cacheable_temperature_rule() -> None:
    assert is_cacheable({"temperature": 0}) is True
    assert is_cacheable({"temperature": 0.0}) is True
    assert is_cacheable({"temperature": 0.7}) is False
    assert is_cacheable({"temperature": 1}) is False
    # Missing temperature is NOT cacheable — explicit temp==0 is the
    # documented opt-in.
    assert is_cacheable({}) is False
    assert is_cacheable({"max_tokens": 256}) is False


@pytest.mark.unit
def test_build_cache_default_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("MOVATE_LLM_CACHE", raising=False)
    assert isinstance(build_cache(), NoOpCache)


@pytest.mark.unit
def test_build_cache_memory_selector(monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_LLM_CACHE", "memory")
    assert isinstance(build_cache(), InProcessCache)


@pytest.mark.unit
def test_build_cache_unknown_selector_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_LLM_CACHE", "redis")  # not implemented yet
    assert isinstance(build_cache(), NoOpCache)
    monkeypatch.setenv("MOVATE_LLM_CACHE", "none")
    assert isinstance(build_cache(), NoOpCache)


@pytest.mark.unit
def test_cache_ttl_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("MOVATE_LLM_CACHE_TTL_S", raising=False)
    assert cache_ttl_s() == DEFAULT_TTL_S
    monkeypatch.setenv("MOVATE_LLM_CACHE_TTL_S", "120")
    assert cache_ttl_s() == 120
    # Malformed / non-positive falls back to the default.
    monkeypatch.setenv("MOVATE_LLM_CACHE_TTL_S", "nope")
    assert cache_ttl_s() == DEFAULT_TTL_S
    monkeypatch.setenv("MOVATE_LLM_CACHE_TTL_S", "0")
    assert cache_ttl_s() == DEFAULT_TTL_S


# ---------------------------------------------------------------------------
# 3. Executor integration
# ---------------------------------------------------------------------------


class CountingProvider(BaseLLMProvider):
    """Fake provider with a call counter. Returns a fixed JSON reply
    that satisfies the scaffolded agent's output schema."""

    name = "counting"
    version = "0.0.1"

    def __init__(self, response: str = '{"message": "hi"}') -> None:
        self._response = response
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        return CompletionResponse(
            text=self._response,
            tokens=TokenUsage(input=10, output=10),
            raw={},
        )

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError

    def pricing_key(self, provider: str) -> str | None:
        # Route to a real pricing-table key so cost > 0 on a miss
        # (proves the hit's $0 is the cache, not a missing price).
        return "openai/gpt-4o-mini-2024-07-18"


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _set_temperature(agent_dir: Path, temp: float) -> None:
    """Rewrite the scaffolded agent.yaml's model temperature."""
    yaml_path = agent_dir / "agent.yaml"
    text = yaml_path.read_text()
    text = text.replace("temperature: 0.0", f"temperature: {temp}")
    yaml_path.write_text(text)


@pytest.mark.unit
async def test_cache_on_repeated_temp0_call_hits(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """Cache ON + temp==0: second identical run does NOT call the
    provider again, and the hit run is $0 cost."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo"))  # temp 0.0 by default
    provider = CountingProvider()
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
        cache=InProcessCache(),
    )
    req = RunRequest(agent="demo", input={"text": "hi"})

    first = await executor.execute(bundle, req)
    assert first.status == "success"
    assert provider.calls == 1
    assert first.metrics.cost_usd > 0  # miss → real (priced) call

    second = await executor.execute(bundle, req)
    assert second.status == "success"
    assert provider.calls == 1  # NOT called again → served from cache
    assert second.data == first.data
    assert second.metrics.cost_usd == 0.0  # hit → $0


@pytest.mark.unit
async def test_cache_off_by_default_always_calls_provider(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """Default (NoOpCache): provider is called every time → unchanged
    behavior, backward compatible."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo"))
    provider = CountingProvider()
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
        # no cache= → NoOpCache default
    )
    req = RunRequest(agent="demo", input={"text": "hi"})

    await executor.execute(bundle, req)
    await executor.execute(bundle, req)
    assert provider.calls == 2  # always hits the provider


@pytest.mark.unit
async def test_temp_above_zero_is_never_cached(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """Cache ON but temperature > 0: sampled responses are never
    cached, so the provider is called on every run."""
    agent_dir = scaffold_agent(tmp_path / "demo")
    _set_temperature(agent_dir, 0.7)
    bundle = load_agent(agent_dir)
    provider = CountingProvider()
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
        cache=InProcessCache(),
    )
    req = RunRequest(agent="demo", input={"text": "hi"})

    await executor.execute(bundle, req)
    await executor.execute(bundle, req)
    assert provider.calls == 2  # sampled → never cached


@pytest.mark.unit
async def test_cache_isolates_by_tenant(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage
) -> None:
    """Same prompt under two tenants → two provider calls (no
    cross-tenant cache hit)."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo"))
    provider = CountingProvider()
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
        cache=InProcessCache(),
    )
    req = RunRequest(agent="demo", input={"text": "hi"})

    await executor.execute(bundle, req, tenant_id_override="tenant-a")
    await executor.execute(bundle, req, tenant_id_override="tenant-b")
    assert provider.calls == 2  # different tenant → different key → miss
