"""Provider registry: maps :class:`AgentRuntime` → :class:`BaseLLMProvider`.

Every agent declares a ``runtime`` field in ``agent.yaml``
(:class:`movate.core.models.AgentRuntime`). The executor uses this
registry to look up the right provider for each agent. v0.5 only
registers :data:`AgentRuntime.LITELLM`; the native-SDK and LangChain
adapters land in v0.6 and register themselves the same way.

Wire it once at app startup:

    registry = ProviderRegistry(default_litellm=LiteLLMProvider())
    # Future: registry.register(AgentRuntime.NATIVE_ANTHROPIC, AnthropicProvider(...))
    executor = Executor(registry=registry, ...)

Calling :meth:`get` for an unregistered runtime raises
:class:`UnregisteredRuntimeError` with a clear message — used by
``movate validate`` to fail loud at parse time rather than after a
worker picks up a job it can't run.
"""

from __future__ import annotations

from movate.core.models import AgentRuntime
from movate.providers.base import BaseLLMProvider


class UnregisteredRuntimeError(LookupError):
    """Raised when an :class:`AgentRuntime` has no provider in the registry.

    Subclass of ``LookupError`` so a caller that wants generic dict-miss
    handling still catches it; subclass-specific catches let
    ``movate validate`` produce a tailored error message."""

    def __init__(self, runtime: AgentRuntime, registered: list[AgentRuntime]) -> None:
        super().__init__(
            f"runtime {runtime.value!r} is not registered "
            f"(available: {sorted(r.value for r in registered)}). "
            f"Native-SDK + LangChain runtimes land in v0.6 — see the "
            f"high-priority list."
        )
        self.runtime = runtime
        self.registered = registered


class ProviderRegistry:
    """Mutable mapping from :class:`AgentRuntime` to a configured provider.

    Construction takes the LiteLLM provider as the default since every
    movate install can talk to it; other runtimes are opt-in via
    :meth:`register`."""

    def __init__(self, default_litellm: BaseLLMProvider) -> None:
        self._providers: dict[AgentRuntime, BaseLLMProvider] = {
            AgentRuntime.LITELLM: default_litellm,
        }

    def register(self, runtime: AgentRuntime, provider: BaseLLMProvider) -> None:
        """Register ``provider`` for ``runtime``. Replaces any previous
        registration silently — last writer wins. Tests use this to
        swap in mock providers per-runtime."""
        self._providers[runtime] = provider

    def get(self, runtime: AgentRuntime) -> BaseLLMProvider:
        """Look up the provider for ``runtime``.

        Raises:
            UnregisteredRuntimeError: if no provider is registered for
                this runtime — typically because the user declared
                ``runtime: native_anthropic`` (etc.) and we haven't
                shipped that adapter yet.
        """
        if runtime not in self._providers:
            raise UnregisteredRuntimeError(runtime, list(self._providers.keys()))
        return self._providers[runtime]

    def is_registered(self, runtime: AgentRuntime) -> bool:
        """Cheap "would .get() work?" check — used by ``movate validate``
        to surface unwired-runtime errors at static check time rather
        than at execute time."""
        return runtime in self._providers

    def registered_runtimes(self) -> list[AgentRuntime]:
        """Snapshot of registered runtimes; for diagnostics."""
        return list(self._providers.keys())


__all__ = ["ProviderRegistry", "UnregisteredRuntimeError"]
