"""Pytest fixtures for movate consumers.

Activate by adding to your ``conftest.py``::

    pytest_plugins = ["movate.testing.fixtures"]

Then your tests can request any of these fixtures by name:

    * ``mock_provider`` — fresh :class:`MockProvider` per test
    * ``in_memory_storage`` — :class:`InMemoryStorage` already ``init()``-ed
    * ``null_tracer`` — :class:`NullTracer`
    * ``pricing`` — packaged :class:`PricingTable`
    * ``temp_agent_dir`` — scaffolded agent in a tmp dir; returns the path
    * ``build_executor`` — factory; call to get
      ``(executor, provider, storage, tracer)``
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from movate.core.executor import Executor
from movate.providers.base import BaseLLMProvider
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing.doubles import InMemoryStorage, NullTracer
from movate.testing.scaffold import build_test_executor, scaffold_agent

ExecutorFactory = Callable[..., tuple[Executor, BaseLLMProvider, InMemoryStorage, NullTracer]]


@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
async def in_memory_storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def null_tracer() -> NullTracer:
    return NullTracer()


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
def temp_agent_dir(tmp_path: Path) -> Path:
    """Scaffold an agent in ``tmp_path / 'demo'`` and return its path."""
    return scaffold_agent(tmp_path / "demo")


@pytest.fixture
def build_executor() -> ExecutorFactory:
    """Factory fixture: call to construct (executor, provider, storage, tracer).

    Accepts the same kwargs as :func:`movate.testing.build_test_executor`.
    """
    return build_test_executor
