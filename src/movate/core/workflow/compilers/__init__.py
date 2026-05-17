"""Workflow compilers — lower a :class:`WorkflowGraph` IR to a runtime.

Today: the native runner consumes the IR directly (see
:mod:`movate.core.workflow.runner`). Sprint U+ adds alternative
compilers behind the same IR so operators can opt into LangGraph /
LangChain runtimes without changing their workflow.yaml.

Compilers:

* :mod:`movate.core.workflow.compilers.langgraph` — emits a Python
  module declaring a LangGraph ``Graph`` over the same node set.
  [bold]Scaffold[/bold]: code generation works; the actual LangGraph
  runtime wiring (state schema, conditional edges, HITL nodes) lands
  in Sprint U+ as the engine gains those constructs.
"""

from __future__ import annotations

from movate.core.workflow.compilers.langgraph import compile_langgraph

__all__ = ["compile_langgraph"]
