"""``mdk playground`` — ChatGPT-like Chainlit UI for testing agents.

Boot with::

    mdk playground serve --runtime-url https://movate-prod-api.eastus2...

Operators get a browser UI that:

1. Lists agents on the configured runtime (``GET /api/v1/agents``) +
   detects the runtime's capabilities (``GET /api/v1/capabilities``).
2. Holds a multi-turn conversation with the picked agent — server-managed
   sessions when available, else client-managed (re-sent transcript) to
   the stateless run endpoint.
3. Persists conversation threads (Chainlit data layer) for a
   past-conversations sidebar + resume.
4. Accepts file uploads — text extracted via the shared KB parser into
   conversation context, optionally persisted to the agent's KB.
5. Streams tokens live when the runtime advertises it; else buffered.
6. Captures 👍/👎/comment feedback, routed to the feedback API when
   advertised, else the runtime's existing persistence path (Postgres
   + best-effort Langfuse mirror).

The module is laid out as:

* :mod:`movate.playground.client` — async HTTP client to the runtime.
* :mod:`movate.playground.capabilities` — capability discovery (pure).
* :mod:`movate.playground.conversation` — conversation backends + context
  assembly + feedback routing (pure).
* :mod:`movate.playground.uploads` — upload→context adapter reusing the
  shared KB text extractor (pure).
* :mod:`movate.playground.sse` — SSE frame parsing for streaming (pure).
* :mod:`movate.playground.state` — data-layer path resolution (pure).
* :mod:`movate.playground.app` — the Chainlit decorators that bind the
  pure logic to the UI. The file ``chainlit run`` loads.

The ``*.client`` module and every ``(pure)`` module import WITHOUT
Chainlit, so they're unit-testable on a no-extras install; only
:mod:`~movate.playground.app` requires it. Chainlit is an optional
dependency under the ``[playground]`` extra — the rest of MDK works
without it. The CLI command (:mod:`movate.cli.playground`) prints a
friendly error when the extra isn't installed.
"""

from __future__ import annotations

__all__: list[str] = []
