"""Workflow IR + compiler + runner.

Public surface kept minimal in v0.3. Most consumers want :func:`compile_workflow`
plus :class:`WorkflowRunner`; everything else is internal.

The IR is deliberately *richer* than v0.3 ships. ``NodeType`` and
``EdgeKind`` include future variants (HUMAN nodes, conditional edges,
parallel fan-out/in, sub-workflows) so v1.1's LangGraph compiler can
target the same model without a schema break. The :func:`validate_linear`
pass enforces "v0.3 = linear chain of agent nodes only" — strict-now,
permissive-later.
"""

from movate.core.workflow.compiler import (
    WorkflowCompileError,
    compile_workflow,
    validate_linear,
)
from movate.core.workflow.ir import (
    EdgeKind,
    NodeType,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)
from movate.core.workflow.runner import (
    WorkflowResult,
    WorkflowRunError,
    WorkflowRunner,
)
from movate.core.workflow.spec import WorkflowSpec, load_workflow_spec

__all__ = [
    "EdgeKind",
    "NodeType",
    "WorkflowCompileError",
    "WorkflowEdge",
    "WorkflowGraph",
    "WorkflowNode",
    "WorkflowResult",
    "WorkflowRunError",
    "WorkflowRunner",
    "WorkflowSpec",
    "compile_workflow",
    "load_workflow_spec",
    "validate_linear",
]
