"""Tool handler: __TOOL_NAME__.

This module is invoked by the agent runtime when an agent decides to
call this tool. The runtime:

1. Validates the model's tool-call payload against ``schema/input.json``.
2. Awaits :func:`handler` with that validated dict.
3. Validates your return value against ``schema/output.json``.
4. Threads the result back into the agent's conversation.

So your job in this module is one async function. No imports from
``movate.*`` are needed — keep handlers thin so they're easy to test
in isolation. If you need shared state, lift it to a singleton at
module scope; the runtime imports this file once per worker process.
"""

from __future__ import annotations

from typing import Any


async def handler(input: dict[str, Any]) -> dict[str, Any]:
    """Implement __TOOL_NAME__.

    Args:
        input: The model's tool-call payload, already validated against
            ``schema/input.json``. Mutating it has no side effect — the
            runtime passes a fresh dict per call.

    Returns:
        A dict matching ``schema/output.json``. Anything else gets
        rejected by the runtime before it reaches the model.

    Raises:
        Any exception propagates to the runtime, which surfaces it
        back to the model as a tool error. The conversation continues
        — the model can retry or apologize. Don't ``sys.exit`` here.
    """
    # TODO: replace this stub.
    raise NotImplementedError("handler not yet implemented for __TOOL_NAME__")
