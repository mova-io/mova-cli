"""``mdk playground`` — Chainlit-based UI for testing deployed agents.

Boot with::

    mdk playground serve --runtime-url https://movate-prod-api.eastus2... \\
        --api-key $MOVATE_API_KEY

Operators get a browser UI that:

1. Lists agents available on the configured runtime (calls ``GET /api/v1/agents``).
2. Renders an input form per agent from the agent's input JSON schema.
3. POSTs to ``/run`` and displays the structured output.
4. Captures 👍/👎/comment feedback via Chainlit's built-in widget.
5. Persists feedback to the runtime's Postgres via
   ``POST /runs/{run_id}/feedback``, which then (best-effort)
   mirrors the score to Langfuse for cross-linking traces.

The module is laid out as:

* :mod:`movate.playground.client` — async HTTP client to the runtime.
* :mod:`movate.playground.adapter` — read agent.yaml's input schema,
  generate a Chainlit form, dispatch ``mdk submit``-equivalent calls.
* :mod:`movate.playground.app` — the Chainlit decorators
  (``@cl.on_chat_start``, ``@cl.on_message``, etc.) that bind the
  adapter to the UI. This file is the one Chainlit's ``chainlit run``
  command loads as the app entry point.

Chainlit is an optional dependency under the ``[playground]`` extra —
the rest of MDK works without it. The CLI command
(:mod:`movate.cli.playground`) prints a friendly error when the
extra isn't installed.
"""

from __future__ import annotations

__all__: list[str] = []
