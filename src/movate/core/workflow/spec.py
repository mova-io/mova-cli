"""WorkflowSpec — Pydantic contract for ``workflow.yaml``.

Spec is the *parsed YAML*. The IR (:class:`WorkflowGraph`) is what the
runner/compiler walks. Keeping them separate means we can evolve the
internal IR (e.g. add metadata for LangGraph routing) without breaking
the user-facing schema.

v0.3 surface intentionally narrow:

* one ``entrypoint`` node
* node types limited to ``"agent"``
* edges have ``from`` and ``to`` only — no ``when:``, no parallel fan-out

Later phases relax these via separate validator passes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class WorkflowSpecLoadError(Exception):
    """Raised when ``workflow.yaml`` cannot be parsed or fails Pydantic validation."""


class NodeSpec(BaseModel):
    """One workflow node as written in YAML.

    v0.3 nodes are agents only. The Literal narrows the surface so a typo
    like ``type: ageent`` fails at parse time, not at run time.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    type: Literal["agent"] = "agent"
    ref: str = Field(..., description="Path to agent dir, relative to workflow.yaml")

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", v):
            raise ValueError(
                f"node id {v!r} must be lowercase alphanumeric with hyphens/underscores"
            )
        return v


class EdgeSpec(BaseModel):
    """One workflow edge as written in YAML.

    v0.3 edges are unconditional sequential transitions. ``when:`` and
    parallel-fan kinds are explicitly out of scope until v1.1.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_id: str = Field(..., alias="from")
    to_id: str = Field(..., alias="to")


class WorkflowEvalsSpec(BaseModel):
    """Optional ``evals:`` stanza in ``workflow.yaml``.

    Mirrors ``AgentSpec.evals`` so ``mdk eval <workflow-dir>`` can locate
    the dataset and default gate without extra CLI flags.
    """

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(
        "evals/dataset.jsonl",
        description="Path to the eval dataset, relative to workflow.yaml",
    )
    runs_per_case: int = Field(1, ge=1, description="How many times to run the workflow per case")
    gate: float = Field(0.7, ge=0.0, le=1.0, description="Default accuracy gate (0.0-1.0)")


class WorkflowSpec(BaseModel):
    """Top-level workflow.yaml contract."""

    model_config = ConfigDict(extra="forbid")

    api_version: Literal["movate/v1"]
    kind: Literal["Workflow"] = "Workflow"

    name: str = Field(..., min_length=1, max_length=128)
    version: str
    description: str = ""
    owner: str = ""

    state_schema: str = Field(
        ..., description="Path to a JSON Schema file, relative to workflow.yaml"
    )
    entrypoint: str = Field(..., description="ID of the starting node")
    evals: WorkflowEvalsSpec | None = None

    nodes: list[NodeSpec] = Field(..., min_length=1)
    edges: list[EdgeSpec] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", v):
            raise ValueError(f"workflow name {v!r} must be lowercase alphanumeric with hyphens")
        return v

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"workflow version {v!r} must be semver (MAJOR.MINOR.PATCH)")
        return v


def load_workflow_spec(path: str | Path) -> tuple[WorkflowSpec, Path]:
    """Load and validate a ``workflow.yaml`` file.

    Returns the spec plus the directory that contains it (so callers can
    resolve relative ``ref``s and ``state_schema`` paths).
    """
    p = Path(path).resolve()
    if p.is_dir():
        p = p / "workflow.yaml"
    if not p.is_file():
        raise WorkflowSpecLoadError(f"workflow.yaml not found at {p}")

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise WorkflowSpecLoadError(f"invalid YAML in {p}: {exc}") from exc

    try:
        spec = WorkflowSpec.model_validate(raw)
    except ValidationError as exc:
        raise WorkflowSpecLoadError(f"workflow.yaml validation failed:\n{exc}") from exc

    return spec, p.parent
