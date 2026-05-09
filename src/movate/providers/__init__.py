"""Provider abstraction. Developers MUST NOT import LiteLLM directly.

The single :class:`BaseLLMProvider` Protocol is implemented today by
:class:`movate.providers.litellm.LiteLLMProvider` (LiteLLM-backed) and
:class:`movate.providers.mock.MockProvider` (deterministic, in-process).
"""

from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
)

__all__ = [
    "BaseLLMProvider",
    "CompletionRequest",
    "CompletionResponse",
    "Message",
    "provider_family",
]


# Map a LiteLLM provider prefix to the model *family* — the cluster of
# providers sharing weights, training data, and therefore likely to share
# blind spots. Used by the eval engine to enforce judge ≠ agent family.
_PROVIDER_FAMILY: dict[str, str] = {
    "openai": "openai",
    "azure": "openai",
    "azure_openai": "openai",
    "anthropic": "anthropic",
    "bedrock-anthropic": "anthropic",
    "vertex-anthropic": "anthropic",
    "gemini": "google",
    "vertex_ai": "google",
    "google": "google",
    "cohere": "cohere",
    "mistral": "mistral",
    "ollama": "ollama",
    "mock": "mock",
}


def provider_family(provider: str) -> str:
    """Return the model family for a LiteLLM provider string.

    ``openai/gpt-4o`` and ``azure/gpt-4o`` both → ``"openai"`` because they
    share weights; cross-family enforcement treats them as the same.
    Unknown prefixes default to themselves (one provider, one family).
    """
    head = provider.split("/", 1)[0]
    return _PROVIDER_FAMILY.get(head, head)
