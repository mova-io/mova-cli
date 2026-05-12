"""Tool registry — operator-registered Python callables that agents
can invoke during a run.

```python
from movate.tools import tool

@tool(side_effects=False)
def kb_search(query: str, k: int = 5) -> list[dict]:
    \"\"\"Search the knowledge base. Returns top-k matches.\"\"\"
    return run_search(query, top_k=k)
```

The decorator inspects the function's signature + docstring to
synthesize a JSON Schema that LiteLLM passes to the model in
``tools=[...]``. When the model emits a tool call, the executor
looks up the registered callable, invokes it with the model's
arguments, and feeds the result back into the conversation.

The ``side_effects`` flag is load-bearing for the resume API
(Tier 2 #3): tools marked ``side_effects=True`` MUST NOT replay on
workflow resume — the side effect already happened. Idempotent tools
(``side_effects=False``) replay safely on resume. The checkpointer
consults this when resuming a workflow paused mid-tool-loop.

Public surface intentionally narrow:

* :func:`tool` — the decorator
* :func:`get_tool` / :func:`list_tools` — registry access
* :class:`Tool` — the dataclass each registered tool produces

Built-in tools (``kb_search``, ``http_get``, ``sql_query``) land as a
follow-up PR.
"""

from __future__ import annotations

from movate.tools.registry import (
    Tool,
    ToolError,
    get_tool,
    list_tools,
    register_tool,
    tool,
)

__all__ = [
    "Tool",
    "ToolError",
    "get_tool",
    "list_tools",
    "register_tool",
    "tool",
]
