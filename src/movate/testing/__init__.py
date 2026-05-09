"""Public test helpers for consumers writing movate agents.

This package is what an agent author should import when writing
``pytest`` tests for their own agent. It bundles:

  * test doubles — :class:`InMemoryStorage`, :class:`NullTracer`,
    :class:`JudgeStubProvider`, and a re-export of :class:`MockProvider`
  * scaffolding — :func:`scaffold_agent` (clones the packaged agent
    template into a directory) and :func:`build_test_executor` (wires
    test doubles into a ready-to-use executor)
  * pytest fixtures — auto-discovered when the consumer adds
    ``pytest_plugins = ["movate.testing.fixtures"]`` to their conftest.

Example usage in a consumer's ``conftest.py``::

    pytest_plugins = ["movate.testing.fixtures"]

Then a test file can simply::

    async def test_my_agent(temp_agent_dir, build_executor):
        from movate.core.loader import load_agent
        bundle = load_agent(temp_agent_dir)
        executor, *_ = build_executor(response='{"message": "ok"}')
        ...
"""

from movate.providers.mock import MockProvider
from movate.testing.doubles import InMemoryStorage, JudgeStubProvider, NullTracer
from movate.testing.scaffold import build_test_executor, scaffold_agent

__all__ = [
    "InMemoryStorage",
    "JudgeStubProvider",
    "MockProvider",
    "NullTracer",
    "build_test_executor",
    "scaffold_agent",
]
