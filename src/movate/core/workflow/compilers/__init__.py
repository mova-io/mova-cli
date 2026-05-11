"""Alternative compilers for :class:`movate.core.workflow.ir.WorkflowGraph`.

The default execution path walks the IR directly via
:class:`movate.core.workflow.runner.WorkflowRunner`. This package holds
*alternative* compilers — currently just :mod:`langgraph` — that
``WorkflowRunner.run`` dispatches to based on
:attr:`WorkflowGraph.runtime`.

Each compiler module must expose:

* A capability check (``can_compile(graph) -> bool``) so the runner can
  refuse pre-walk instead of crashing mid-execution.
* An async run entry point that takes the IR + initial state + executor
  and returns a :class:`movate.core.workflow.runner.WorkflowResult` with
  the same shape the homegrown runner produces.

Compiler internals are free to differ — for LangGraph that means
materialising a ``StateGraph``, wrapping AGENT nodes around
``Executor.execute``, and calling ``CompiledStateGraph.ainvoke``. But
the return shape (``WorkflowResult`` with ``runs: list[RunRecord]``,
status, error_node_id, etc.) is the IR's contract with the rest of
movate.
"""

from __future__ import annotations
