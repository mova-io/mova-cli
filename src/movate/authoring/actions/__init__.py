"""The initial authoring action catalog (ADR 025 D1).

Importing this package registers every shipped action into the catalog
(:mod:`movate.authoring.catalog`) as an import-time side effect. Each action
composes an existing mdk primitive — the import here is the single place the
catalog's membership is declared.

Mapping (action → primitive it reuses):

* add/edit/remove-context → ``movate.cli.contexts_cmd`` attach/detach + the
  ``contexts create`` file write.
* edit-instructions       → the agent.yaml-declared ``prompt.md`` (load_agent's ref).
* set-model / add-fallback → the canonical agent.yaml round-trip + ``ModelConfig`` validation.
* set-retrieval           → the canonical round-trip + ``RetrievalConfig`` (ADR 023).
* describe-agent          → the canonical round-trip (name/description fields).
* add-eval-case           → the agent.yaml-declared ``evals/dataset.jsonl``.
* add-skill               → the ``skills scaffold`` template-copy primitive.
* add-agent               → the ``mdk add`` role-template scaffold.
* compose-workflow        → ``mdk compose``'s ``_scaffold_workflow_yaml``.
* ingest-kb               → ``movate.kb.ingest.ingest_path``.
"""

from __future__ import annotations

from movate.authoring.actions.agents import AddAgentAction, ComposeWorkflowAction
from movate.authoring.actions.contexts import (
    AddContextAction,
    EditContextAction,
    RemoveContextAction,
)
from movate.authoring.actions.evals import AddEvalCaseAction
from movate.authoring.actions.instructions import EditInstructionsAction
from movate.authoring.actions.kb import IngestKbAction
from movate.authoring.actions.metadata import DescribeAgentAction
from movate.authoring.actions.model import AddFallbackAction, SetModelAction
from movate.authoring.actions.retrieval import SetRetrievalAction
from movate.authoring.actions.skills import AddSkillAction
from movate.authoring.catalog import register

# Register every action exactly once, at import time.
register(AddContextAction())
register(EditContextAction())
register(RemoveContextAction())
register(EditInstructionsAction())
register(SetModelAction())
register(AddFallbackAction())
register(SetRetrievalAction())
register(DescribeAgentAction())
register(AddEvalCaseAction())
register(AddSkillAction())
register(AddAgentAction())
register(ComposeWorkflowAction())
register(IngestKbAction())

__all__ = [
    "AddAgentAction",
    "AddContextAction",
    "AddEvalCaseAction",
    "AddFallbackAction",
    "AddSkillAction",
    "ComposeWorkflowAction",
    "DescribeAgentAction",
    "EditContextAction",
    "EditInstructionsAction",
    "IngestKbAction",
    "RemoveContextAction",
    "SetModelAction",
    "SetRetrievalAction",
]
