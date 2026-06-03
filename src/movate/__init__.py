"""movate — declarative platform for building, evaluating, and deploying AI agents."""

from __future__ import annotations

import logging

__version__ = "2026.6.2.12"


class _LiteLLMBotocoreNoiseFilter(logging.Filter):
    """Drop LiteLLM's import-time botocore-probe warnings.

    LiteLLM emits two WARNING records when ``boto3`` / ``botocore``
    aren't installed — one for bedrock-runtime, one for
    sagemaker-runtime — claiming those decoders are "unavailable".
    Every movate provider call routes through native HTTP clients,
    so AWS decoding isn't on the path. Installing the ~10MB AWS SDK
    just to silence these would be wrong; filtering the two specific
    log records is the proportionate response.

    Installed at package-import time (in ``movate/__init__.py``) so
    the filter is already on the ``LiteLLM`` logger before any
    movate code path can trigger ``import litellm``. This is
    defense-in-depth: ``movate.providers.litellm`` would normally
    install it too, but any future code path that imports litellm
    before going through that module would otherwise leak the
    warnings.
    """

    _SILENT_NEEDLES = (
        "bedrock-runtime response stream shape",
        "sagemaker-runtime response stream shape",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(needle in msg for needle in self._SILENT_NEEDLES)


# Install the filter on the ``LiteLLM`` logger at the earliest
# possible point — package import. Idempotent: re-importing movate
# (e.g. inside tests) won't stack duplicates of the same filter.
_litellm_logger = logging.getLogger("LiteLLM")
if not any(isinstance(f, _LiteLLMBotocoreNoiseFilter) for f in _litellm_logger.filters):
    _litellm_logger.addFilter(_LiteLLMBotocoreNoiseFilter())

# Raise the LiteLLM logger's threshold to WARNING so per-completion
# ``LiteLLM:INFO: utils.py:4053 - LiteLLM completion() model=…``
# lines don't flood stderr during ``mdk eval`` / ``mdk run`` — the
# progress bar repaints get interleaved with these lines and become
# unreadable. WARNING+ records (rate limits, model-not-found, etc.)
# still surface; the botocore-probe WARNINGs above are filtered too.
# Operators who want the verbose log can re-enable per-call via
# ``logging.getLogger("LiteLLM").setLevel(logging.INFO)``.
_litellm_logger.setLevel(logging.WARNING)
