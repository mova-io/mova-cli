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
    approvers: list[str] = Field(
        default_factory=list,
        description=(
            "Principals/roles allowed to respond (ADR 062 D3). Optional; "
            "enforced at the signal endpoint. Carried on the pause record so a "
            "transport can address the approval."
        ),
    )
    timeout: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Durable deadline in seconds (ADR 062 D4). Only the Temporal backend "
            "honors it (native waits indefinitely). On expiry the node takes "
            "'on_timeout'. Omit to wait forever."
        ),
    )
    on_timeout: str | None = Field(
        default=None,
        description=(
            "Node id to route to when 'timeout' elapses (ADR 062 D4). Required "
            "when 'timeout' is set; ignored otherwise."
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", v):
            raise ValueError(
                f"node id {v!r} must be lowercase alphanumeric with hyphens/underscores"
            )
        return v

    @model_validator(mode="after")
    def _validate_timeout_pair(self) -> HumanNodeSpec:
        # A durable timeout needs a route to take on expiry — fail loud at
        # compile time rather than emitting a node that raises at runtime.
        if self.timeout is not None and not self.on_timeout:
            raise ValueError(
                f"human node {self.id!r}: 'on_timeout' (a node id) is required "
                "when 'timeout' is set"
            )
        return self


class JudgeNodeSpec(BaseModel):
    """One ``judge`` workflow node as written in YAML (ADR 056 D1).

    A judge node runs an LLM judge over an artifact in workflow state and
    produces a *structured verdict* — ``{verdict, score, feedback,
    terminate}`` (ADR 056 D2, reusing :class:`movate.core.reflection.JudgeVerdict`)
    — that the workflow branches or loops on. It is the right primitive for
    the two highest-value agent patterns: eval-gated workflows and reflection
    loops. It is NOT an ``intent-router`` (label-only, no score, no feedback)
    and NOT a ``human`` gate (a model, not a person).

    Two forms, mirroring ADR 056 D1:

    * **eval-gate / branch** — set ``pass_threshold`` and/or ``on_accept`` /
      ``on_revise``. When ``pass_threshold`` is set, ``score >= threshold`` ⇒
      *accept* (``terminate=True``); otherwise the categorical
      ``accept``/``revise`` verdict drives the gate. On accept the runner
      routes to ``on_accept`` (or falls through to the sequential successor);
      on revise it routes to ``on_revise`` (or falls through to a bounded
      back-edge — the reflection form, D4).
    * **judge selection** — exactly one of ``judge_agent`` (a ref resolved
      like every other node ref) or ``criteria`` (inline rubric text that
      reuses ``reflection.py``'s default judge prompt) must be supplied. A
      judge that supplies neither cannot run; supplying both is ambiguous.

    Additive + ``extra="forbid"`` (CLAUDE.md rule 5): no existing workflow
    declares ``judge`` today, so every existing workflow is byte-for-byte
    unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    type: Literal["judge"]
    judge_agent: str | None = Field(
        None,
        description=(
            "Ref (path) to the judge agent, resolved like any other node ref. "
            "Mutually exclusive with ``criteria``."
        ),
    )
    criteria: str | None = Field(
        None,
        description=(
            "Inline rubric text — reuses reflection.py's default judge prompt "
            "when no dedicated judge agent is supplied. Mutually exclusive with "
            "``judge_agent``."
        ),
    )
    input_field: str = Field(
        "text",
        description="Workflow state key holding the artifact to judge",
    )
    pass_threshold: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description=(
            "When set, score >= threshold ⇒ accept (the eval-gate form). When "
            "unset, the categorical accept/revise verdict drives the gate."
        ),
    )
    on_accept: str | None = Field(
        None,
        description="Node id to route to when the judge accepts (else sequential successor)",
    )
    on_revise: str | None = Field(
        None,
        description="Node id to route to when the judge revises (else sequential successor)",
    )
    max_iterations: int = Field(
        1,
        ge=1,
        le=10,
        description=(
            "Mandatory cap (ADR 056 D4) on how many times this judge may drive a "
            "revise back-edge before the loop terminates regardless of verdict. "
            "1 (default) = no reflection loop (single judgement). >1 enables the "
            "produce→judge→revise→… reflection pattern, bounded so a judge that "
            "never accepts cannot loop forever."
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", v):
            raise ValueError(
                f"node id {v!r} must be lowercase alphanumeric with hyphens/underscores"
            )
        return v

    @model_validator(mode="after")
    def _validate_judge_selection(self) -> JudgeNodeSpec:
        has_agent = bool(self.judge_agent and self.judge_agent.strip())
        has_criteria = bool(self.criteria and self.criteria.strip())
        if has_agent and has_criteria:
            raise ValueError(
                f"judge node {self.id!r}: set exactly one of 'judge_agent' or 'criteria', not both"
            )
        if not has_agent and not has_criteria:
            raise ValueError(
                f"judge node {self.id!r}: one of 'judge_agent' (a ref) or "
                f"'criteria' (inline rubric) is required"
            )
        return self


# NodeSpec is a discriminated union of agent, intent-router, human, and judge nodes.
NodeSpec = Annotated[
    AgentNodeSpec | IntentRouterNodeSpec | HumanNodeSpec | JudgeNodeSpec,
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
    join: Literal["last_wins", "by_key", "collect"] | None = Field(
        None,
        description=(
            "Branch-state merge strategy for a fan-in edge (ADR 092 D2). Only valid "
            "on a ``kind: fan_in`` edge. ``last_wins`` (default) shallow-merges each "
            "branch's output into parent state in branch order; ``by_key`` namespaces "
            "each branch's output under its start-node id (no clobber); ``collect`` "
            "gathers each branch's value at ``join_key`` into a list. Omitted ⇒ "
            "``last_wins``."
        ),
    )
    join_key: str | None = Field(
        None,
        description=(
            "State key for the ``collect`` join strategy (ADR 092 D2): each branch's "
            "value at this key is gathered into a list under it. Required for "
            "``join: collect``; ignored otherwise."
        ),
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
        # ``join``/``join_key`` only make sense on a fan-in merge edge (ADR 092).
        if (self.join is not None or self.join_key is not None) and self.kind != "fan_in":
            raise ValueError(
                f"edge {self.from_id!r}→{self.to_id!r}: 'join'/'join_key' are only valid "
                f"on a 'fan_in' edge, but kind={self.kind!r} was declared"
            )
        if self.join == "collect" and not (self.join_key and self.join_key.strip()):
            raise ValueError(
                f"edge {self.from_id!r}→{self.to_id!r}: join='collect' requires a "
                f"non-empty 'join_key'"
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
