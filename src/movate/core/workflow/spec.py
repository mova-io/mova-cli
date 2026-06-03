"""WorkflowSpec — Pydantic contract for ``workflow.yaml``.

Spec is the *parsed YAML*. The IR (:class:`WorkflowGraph`) is what the
runner/compiler walks. Keeping them separate means we can evolve the
internal IR (e.g. add metadata for LangGraph routing) without breaking
the user-facing schema.

v0.3 surface intentionally narrow:

* one ``entrypoint`` node
* node types limited to ``"agent"`` and ``"intent-router"``
* edges have ``from`` and ``to`` only — no ``when:``, no parallel fan-out

ADR 017 D5 (PR 1) additionally admits a ``"human"`` node — a HITL gate the
runner pauses at (it persists a checkpoint and stops; PR 2 resumes on an
external signal). Other future variants (tool, sub-workflow) stay rejected
by :func:`validate_linear`.

Later phases relax these via separate validator passes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class WorkflowSpecLoadError(Exception):
    """Raised when ``workflow.yaml`` cannot be parsed or fails Pydantic validation."""


class IntentRouterConfig(BaseModel):
    """Configuration for an ``intent-router`` workflow node.

    The router calls a classifier agent with the text from ``input_field``
    and the list of intent labels drawn from ``routes``.  The classifier
    returns ``{"label": "<one-of-the-keys>"}``; the router maps that label
    to a downstream node id via ``routes``, falling back to ``fallback``
    when no entry matches.
    """

    model_config = ConfigDict(extra="forbid")

    routes: dict[str, str] = Field(
        ...,
        description="Map of intent label → target node id",
    )
    fallback: str = Field(
        ...,
        description="Node id to route to when no intent label matches",
    )
    classifier_agent: str = Field(
        ...,
        description="Name (or relative path) of the classifier agent to invoke",
    )
    input_field: str = Field(
        "text",
        description="Key from the workflow state to pass as the classifier's ``text`` input",
    )


class AgentNodeSpec(BaseModel):
    """One agent workflow node as written in YAML."""

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


class IntentRouterNodeSpec(BaseModel):
    """One intent-router workflow node as written in YAML."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    type: Literal["intent-router"]
    routes: dict[str, str] = Field(..., description="Intent label → target node id map")
    fallback: str = Field(..., description="Node id to route to when no label matches")
    classifier_agent: str = Field(..., description="Name of the classifier agent to use")
    input_field: str = Field("text", description="Workflow state key to classify")

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", v):
            raise ValueError(
                f"node id {v!r} must be lowercase alphanumeric with hyphens/underscores"
            )
        return v

    @property
    def intent_router_config(self) -> IntentRouterConfig:
        return IntentRouterConfig(
            routes=self.routes,
            fallback=self.fallback,
            classifier_agent=self.classifier_agent,
            input_field=self.input_field,
        )


class HumanNodeSpec(BaseModel):
    """One ``human`` (HITL gate) workflow node as written in YAML (ADR 017 D5).

    A human node carries no executable ``ref`` — when the runner reaches it,
    it does NOT execute anything. Instead it persists a durable checkpoint
    (current state + this spec) with status ``PAUSED`` and returns. PR 2's
    resume-on-signal path loads that checkpoint, shows ``prompt`` to the
    operator, validates the returned decision against ``output_contract``,
    merges it into state, and continues from this node's successor.

    The spec is intentionally minimal: a required human-readable ``prompt``
    plus an optional ``output_contract`` — the list of state keys the human's
    eventual response is expected to contribute. ``output_contract`` is the
    seam PR 2 uses to validate the decision before merging; PR 1 only persists
    it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    type: Literal["human"]
    prompt: str = Field(
        ...,
        min_length=1,
        description="Human-readable prompt/label shown to the operator at the gate",
    )
    output_contract: list[str] = Field(
        default_factory=list,
        description="State keys the human's response is expected to contribute (optional)",
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", v):
            raise ValueError(
                f"node id {v!r} must be lowercase alphanumeric with hyphens/underscores"
            )
        return v


# NodeSpec is a discriminated union of agent, intent-router, and human nodes.
NodeSpec = Annotated[
    AgentNodeSpec | IntentRouterNodeSpec | HumanNodeSpec,
    Field(discriminator="type"),
]


class EdgeSpec(BaseModel):
    """One workflow edge as written in YAML.

    The default edge is an unconditional sequential transition — a bare
    ``{from: a, to: b}`` is exactly what v0.3 shipped and is unchanged.

    ADR 030 D2 (additive, backward-compatible) adds two **optional** fields
    that the LangGraph *export* compiler lowers into the IR's existing
    ``EdgeKind`` variants. They are inert for the native linear runner /
    ``validate_linear`` phase gate (which still rejects non-sequential
    edges); they exist so ``mdk export langgraph`` can emit the complex
    graphs the IR already models:

    * ``when:`` — a condition expression. When present, the edge lowers to
      ``EdgeKind.CONDITIONAL`` (LangGraph ``add_conditional_edges``). The
      expression string is carried verbatim into ``WorkflowEdge.condition``;
      its grammar is intentionally not interpreted here (the generated
      router renders it as the branch predicate).
    * ``kind:`` — the explicit edge kind for parallel graphs:
      ``"fan_out"`` (concurrent siblings) / ``"fan_in"`` (merge), or
      ``"conditional"`` / ``"sequential"``. Omitted ⇒ ``sequential``
      (unless ``when:`` is set, which implies ``conditional``). The literal
      values are generic graph terms — no LangGraph specifics leak into the
      schema. They map 1:1 onto :class:`~movate.core.workflow.ir.EdgeKind`.

    These keep `workflow.yaml` portable: an old sequential workflow loads,
    compiles, and (per the native runner) behaves identically.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_id: str = Field(..., alias="from")
    to_id: str = Field(..., alias="to")
    when: str | None = Field(
        None,
        description="Condition expression; presence makes the edge conditional (ADR 030 D2)",
    )
    kind: Literal["sequential", "conditional", "fan_out", "fan_in"] | None = Field(
        None,
        description="Explicit edge kind for parallel/conditional graphs; omitted ⇒ sequential",
    )

    @field_validator("when")
    @classmethod
    def _validate_when(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("edge 'when' must be a non-empty condition expression")
        return v

    @model_validator(mode="after")
    def _reconcile_kind_when(self) -> EdgeSpec:
        # ``when:`` only makes sense on a conditional edge. If the author set
        # an explicit non-conditional kind alongside a condition, that's a
        # contradiction — fail loud rather than silently dropping the guard.
        if self.when is not None and self.kind not in (None, "conditional"):
            raise ValueError(
                f"edge {self.from_id!r}→{self.to_id!r}: 'when' is only valid on a "
                f"conditional edge, but kind={self.kind!r} was declared"
            )
        return self

    @property
    def resolved_kind(self) -> str:
        """The effective edge kind after reconciling ``when`` / ``kind``.

        ``when:`` set ⇒ ``"conditional"``; otherwise the explicit ``kind`` or
        ``"sequential"`` by default. Keeps the spec→IR lowering in one place.
        """
        if self.when is not None:
            return "conditional"
        return self.kind or "sequential"


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

    runtime: Literal["native", "langgraph", "temporal"] = Field(
        "native",
        description=(
            "Execution backend for this workflow (ADR 055 D1). 'native' (default) "
            "is the in-process WorkflowRunner — today's behavior, no extra. "
            "'temporal' compiles to a Temporal workflow (opt-in mdk[temporal], "
            "ADR 054). 'langgraph' is reserved (ADR 030; execution lands in ADR "
            "055 step 3). Additive + default-preserving: no existing workflow.yaml "
            "carries this key (extra='forbid'), so every current workflow stays "
            "'native' byte-for-byte."
        ),
    )

    state_schema: str = Field(
        ..., description="Path to a JSON Schema file, relative to workflow.yaml"
    )
    entrypoint: str = Field(..., description="ID of the starting node")
    evals: WorkflowEvalsSpec | None = None

    # Workflow runner — picks which backend executes the IR (ADR 054 D2).
    #
    # The patterns x runners matrix (default: ``native``):
    #
    # * ``native``    — the in-process linear runner that ships in core.
    #                   Default; preserves the v0.3 behavior for every existing
    #                   workflow (no flag = no change). Linear graphs only.
    # * ``langgraph`` — export target for the LangGraph runtime (ADR 030).
    #                   Selected when an operator runs ``mdk export langgraph``
    #                   or runs the workflow inside a LangGraph host. Admits
    #                   the wider graph shapes the IR already models
    #                   (conditional / fan-out / fan-in).
    # * ``temporal``  — durable backend (ADR 054). Per-workflow opt-in;
    #                   requires the ``[temporal]`` extra. The compiler
    #                   (Phase 1 Track B) lowers the IR to a Temporal workflow
    #                   + activities; ``mdk worker --backend temporal``
    #                   (Phase 1 Track C / D12) runs the worker pool. Default
    #                   stays ``native`` so existing workflows are wholly
    #                   unaffected (zero behavior change, ADR 054 D2).
    #
    # Adding a value here is the additive seam new backends extend; consumers
    # MUST match exhaustively (mypy + the compiler dispatch enforce it).
    runtime: Literal["native", "langgraph", "temporal"] = Field(
        "native",
        description=(
            "Workflow runner: 'native' (default, in-process linear), "
            "'langgraph' (ADR 030 export target), or 'temporal' (ADR 054 "
            "durable backend, opt-in via mdk[temporal])."
        ),
    )

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
