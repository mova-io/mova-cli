"""The natural-language → catalog-action planner (ADR 025 PR3 / S1, D6).

This is the LLM layer the conversational ``mdk dev`` copilot drives. It maps a
user's free-text request ("add a returns-policy context", "make the tone
formal", "ingest https://…", "add a calculator skill") to one or more **typed
catalog actions** — it never edits files itself. The chosen actions are then run
through the existing :class:`movate.authoring.AuthoringDriver`
(plan → preview → confirm → apply → verify), so every D8 boundary the catalog
enforces (no raw fs/shell, validate/registry/versioning, reversibility,
confirmation gates) holds transitively.

The planner is **provider-pluggable** (D6):

* :class:`LLMPlanner` wraps any :class:`~movate.providers.base.BaseLLMProvider`
  (the existing model seam) — no new dependency.
* :class:`MockPlanner` is a deterministic, scripted intent→action mapper so the
  whole copilot is hermetically testable with **no API keys** — it is how PR3 is
  tested in CI, and it backs ``mdk dev --mock``.

The LLM's instructions are **generated from the catalog** (each action
self-describes via :func:`movate.authoring.catalog.describe_catalog` — there is
no hand-maintained tool list to drift) plus the **current project state** (the
tree, ``agent.yaml``, existing contexts/skills) so it is grounded in *this*
project. The prompt is split into a **cacheable static prefix** (catalog +
instructions, per #109) and a **per-turn suffix** (project state + the user
message) so the static half can be prompt-cached.

A request the planner cannot confidently map yields a single
:class:`PlannerOutcome` with ``needs_clarification`` set (a structured
question) rather than a silent guess (D6).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from movate.authoring._yaml_edit import load_agent_yaml
from movate.authoring.catalog import action_names, describe_catalog

if TYPE_CHECKING:
    from movate.authoring.budget import SessionCostTracker
    from movate.providers.base import BaseLLMProvider
    from movate.providers.pricing import PricingTable


class PlannerError(Exception):
    """Raised when the planner backend returns an unusable response.

    The copilot catches this and reports it without mutating anything — a
    planner failure must never leave the project half-edited.
    """


@dataclass(frozen=True)
class ProposedAction:
    """One catalog action the planner chose, with its args (D6).

    ``name`` is a catalog action name (see
    :func:`movate.authoring.catalog.action_names`); ``args`` is the dict the
    driver validates against the action's ``args_model`` before planning. The
    planner produces these; it never touches the filesystem — the driver does,
    behind the confirmation gate.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True)
class PlannerOutcome:
    """The planner's structured decision for one user turn (D6).

    Exactly one of two shapes:

    * **actionable** — :attr:`actions` is non-empty: the ordered catalog
      action(s) to drive. :attr:`needs_clarification` is ``None``.
    * **needs clarification** — :attr:`needs_clarification` carries a single
      question to ask the user; :attr:`actions` is empty. The copilot asks it
      and mutates **nothing** (D6).

    :attr:`message` is an optional human-facing note (e.g. the planner's
    summary of what it intends), surfaced before the plan preview.
    """

    actions: list[ProposedAction] = field(default_factory=list)
    needs_clarification: str | None = None
    message: str = ""

    @property
    def is_clarification(self) -> bool:
        """True when the planner asked a question instead of proposing actions."""
        return self.needs_clarification is not None and not self.actions


@runtime_checkable
class Planner(Protocol):
    """The NL→catalog mapping seam the copilot drives.

    Implementations map a user's natural-language request — given the current
    agent in scope — to a :class:`PlannerOutcome`. They MUST NOT mutate the
    project; the driver owns all writes.
    """

    def plan(self, request: str, *, agent: str) -> PlannerOutcome:
        """Map ``request`` to catalog action(s) or a clarifying question."""
        ...


# ---------------------------------------------------------------------------
# Grounding — the current-project state fed to the planner (D6)
# ---------------------------------------------------------------------------


def project_state_summary(project: Path, *, agent: str) -> dict[str, Any]:
    """Describe *this* project so the planner is grounded, not generic (D6).

    Returns a small JSON-serializable snapshot: the agent in scope, its
    ``agent.yaml`` highlights (model, prompt ref, attached contexts/skills,
    retrieval), the agent-local context files on disk, and the project's other
    agents. Best-effort — a missing/partial tree yields a partial summary
    rather than raising, so the copilot still works on a fresh scaffold.
    """
    summary: dict[str, Any] = {"agent": agent}
    agent_dir = (project / "agents" / agent).resolve()
    agent_yaml = agent_dir / "agent.yaml"
    try:
        data = load_agent_yaml(agent_yaml)
    except (OSError, ValueError):
        data = {}
    if data:
        model = data.get("model") or {}
        summary["model"] = model.get("provider") if isinstance(model, dict) else None
        summary["description"] = data.get("description", "")
        summary["attached_contexts"] = list(data.get("contexts") or [])
        summary["skills"] = list(data.get("skills") or [])
        summary["retrieval"] = data.get("retrieval") or {}

    # Context files physically present on disk (the planner uses this to avoid
    # proposing add-context for a name that already exists, and to target
    # edit-context for one that does).
    ctx_dir = agent_dir / "contexts"
    if ctx_dir.is_dir():
        summary["context_files"] = sorted(p.stem for p in ctx_dir.glob("*.md"))
    else:
        summary["context_files"] = []

    agents_dir = project / "agents"
    if agents_dir.is_dir():
        summary["project_agents"] = sorted(
            p.name for p in agents_dir.iterdir() if (p / "agent.yaml").is_file()
        )
    else:
        summary["project_agents"] = [agent]
    return summary


# ---------------------------------------------------------------------------
# System prompt — generated from the catalog (D6), cacheable prefix (#109)
# ---------------------------------------------------------------------------

_INSTRUCTIONS = """\
You are the mdk authoring copilot. You help a developer evolve an AI agent by \
mapping their natural-language request to one or more typed authoring actions \
from the catalog below. You DO NOT edit files yourself — you only choose \
catalog actions and their arguments; a safe driver previews, confirms, applies, \
and verifies each one.

Rules:
- Choose ONLY actions from the catalog. Use each action's `args_schema` to build \
valid arguments. The agent currently in scope is provided in the project state; \
fill any `agent` argument with it unless the user clearly names another.
- Prefer the smallest set of actions that satisfies the request. Most requests \
map to ONE action.
- If the request is ambiguous, under-specified, or you are not confident which \
action fits, DO NOT guess. Instead return a single clarifying question.
- Ground your choice in the provided project state (existing contexts, skills, \
model). E.g. to refine an existing context use `edit-context`, not \
`add-context`.

Respond with a SINGLE JSON object, no prose, in one of these two shapes:

  {"actions": [{"name": "<catalog-action>", "args": {...}, "rationale": "..."}]}

or, when you need more information:

  {"needs_clarification": "<one specific question>"}
"""


def build_static_prefix() -> str:
    """The cacheable, project-independent half of the system prompt (#109).

    Contains the fixed instructions + the self-describing catalog manifest
    (:func:`describe_catalog`). It depends only on the installed catalog, so it
    is identical across turns and projects and can be prompt-cached. The
    per-turn project state + user message are appended separately (see
    :func:`build_messages`).
    """
    catalog_json = json.dumps(describe_catalog(), indent=2)
    return f"{_INSTRUCTIONS}\n\n# Action catalog\n\n{catalog_json}\n"


def build_messages(request: str, *, project_state: dict[str, Any]) -> list[dict[str, str]]:
    """Build the planner's chat messages: cacheable system prefix + per-turn user.

    The system message is the static catalog/instructions prefix
    (:func:`build_static_prefix`); the user message carries the *per-turn*
    project state then the request — static-first so the prefix stays cacheable
    (#109). Returned as plain dicts so the caller wraps them in whatever
    ``Message`` shape its provider seam wants.
    """
    state_json = json.dumps(project_state, indent=2, default=str)
    user = f"# Current project state\n\n{state_json}\n\n# Request\n\n{request.strip()}\n"
    return [
        {"role": "system", "content": build_static_prefix()},
        {"role": "user", "content": user},
    ]


def parse_planner_response(raw: str) -> PlannerOutcome:
    """Parse a planner backend's raw text into a :class:`PlannerOutcome`.

    Accepts the two documented shapes (``actions`` or ``needs_clarification``),
    tolerating a leading ```` ```json ```` code fence. Validates that chosen
    action names exist in the catalog. Raises :class:`PlannerError` on
    unparseable / malformed / unknown-action output so the copilot can report
    it without mutating anything.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.removesuffix("```").strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerError(f"planner returned non-JSON: {exc}; got: {raw[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise PlannerError("planner response must be a JSON object")

    clarify = payload.get("needs_clarification")
    if clarify:
        return PlannerOutcome(needs_clarification=str(clarify))

    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise PlannerError(
            "planner response must contain a non-empty `actions` list "
            "or a `needs_clarification` question"
        )
    known = set(action_names())
    actions: list[ProposedAction] = []
    for entry in raw_actions:
        if not isinstance(entry, dict) or "name" not in entry:
            raise PlannerError(f"malformed action entry: {entry!r}")
        name = str(entry["name"])
        if name not in known:
            raise PlannerError(
                f"planner chose unknown action {name!r}; known: {', '.join(sorted(known))}"
            )
        args = entry.get("args") or {}
        if not isinstance(args, dict):
            raise PlannerError(f"action {name!r} args must be an object, got {args!r}")
        actions.append(
            ProposedAction(name=name, args=args, rationale=str(entry.get("rationale", "")))
        )
    return PlannerOutcome(actions=actions, message=str(payload.get("message", "")))


# ---------------------------------------------------------------------------
# LLMPlanner — the provider-pluggable planner (D6)
# ---------------------------------------------------------------------------


class LLMPlanner:
    """Map NL → catalog actions via a :class:`BaseLLMProvider` (D6).

    Provider-pluggable: pass any adapter behind the existing model seam (the
    same one ``mdk init --llm`` uses). The system prompt is generated from the
    catalog + grounded in the project state; the model returns the structured
    plan parsed by :func:`parse_planner_response`.

    Cost budgeting (D7e, #136): pass an optional :class:`SessionCostTracker`.
    Before each model call the planner asks the tracker to gate the budget
    (:meth:`SessionCostTracker.check_before_call`) — raising
    :class:`~movate.authoring.budget.BudgetExceededError` *before* the call when
    the cap is spent, so a session stops at a call boundary with no half-work.
    After a successful call it derives the call's cost from the response's token
    usage against the canonical pricing table (ADR 024) and records it. With no
    tracker the path is byte-for-byte the prior behavior.
    """

    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        project: Path,
        model: str,
        tracker: SessionCostTracker | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        self._provider = provider
        self._project = project.resolve()
        self._model = model
        self._tracker = tracker
        self._pricing = pricing

    def plan(self, request: str, *, agent: str) -> PlannerOutcome:
        # Lazy imports keep the module importable (and the mock path hermetic)
        # without pulling the provider request types at module import time.
        import asyncio  # noqa: PLC0415

        from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415

        # Budget gate (D7e): refuse the call BEFORE it happens once the cap is
        # spent. This raises so no propose/apply work begins past the cap.
        if self._tracker is not None:
            self._tracker.check_before_call()

        state = project_state_summary(self._project, agent=agent)
        messages = [
            Message(role=m["role"], content=m["content"])  # type: ignore[arg-type]
            for m in build_messages(request, project_state=state)
        ]
        req = CompletionRequest(
            provider=self._model,
            messages=messages,
            params={
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
                "max_tokens": 1024,
            },
        )
        try:
            response = asyncio.run(self._provider.complete(req))
        except Exception as exc:  # provider wire / auth / timeout
            raise PlannerError(f"planner provider call failed: {exc}") from exc

        # Record the call's cost against the session budget (ADR 024 pricing).
        if self._tracker is not None:
            self._tracker.record(self._call_cost(response.tokens))
        return parse_planner_response(response.text)

    def _call_cost(self, tokens: object) -> float:
        """Derive this call's cost from token usage via the pricing table.

        Reuses the canonical packaged pricing table — never the provider's own
        cost field. A missing table / unknown model degrades to 0.0 (the
        :func:`cost_of_tokens` helper handles the lookup miss) so an unpriced
        model is simply not counted against the budget rather than crashing.
        """
        from movate.authoring.budget import cost_of_tokens  # noqa: PLC0415
        from movate.core.models import TokenUsage  # noqa: PLC0415

        if not isinstance(tokens, TokenUsage):
            return 0.0
        pricing = self._pricing
        if pricing is None:
            from movate.providers.pricing import load_pricing  # noqa: PLC0415

            pricing = load_pricing()
            self._pricing = pricing
        return cost_of_tokens(pricing, provider=self._model, tokens=tokens)


# ---------------------------------------------------------------------------
# MockPlanner — deterministic, scripted, no-keys planner (D6, the test path)
# ---------------------------------------------------------------------------


# A quoted token (e.g. add a "returns-policy" context) splits a request into at
# least three parts around the quote char: [before, token, after].
_QUOTED_TOKEN_PARTS = 3


@dataclass
class _Rule:
    """One keyword→action rule for :class:`MockPlanner`.

    ``build`` maps ``(request, agent)`` → a :class:`PlannerOutcome`.
    """

    keywords: tuple[str, ...]
    build: Callable[[str, str], PlannerOutcome]


def _quoted_name(request: str, *, default: str) -> str:
    """Best-effort: pull a name out of a request like add a "returns-policy" X.

    Looks for a single- or double-quoted token, then for a token following the
    word "named"/"called". Falls back to ``default``. Deterministic — this is a
    scripted mock, not a parser.
    """
    for quote in ('"', "'"):
        # A quoted token splits into at least [before, token, after].
        if quote in request:
            parts = request.split(quote)
            if len(parts) >= _QUOTED_TOKEN_PARTS and parts[1].strip():
                return parts[1].strip()
    lowered = request.lower()
    for marker in (" named ", " called "):
        if marker in lowered:
            tail = request[lowered.index(marker) + len(marker) :].strip()
            token = tail.split()[0].strip("\"'.,") if tail else ""
            if token:
                return token
    return default


class MockPlanner:
    """A deterministic, scripted NL→catalog planner — the hermetic test path (D6).

    Maps a small set of intent keywords to catalog actions with NO model and NO
    API keys, so the entire copilot (menu → planner → driver → verify) is
    CI-runnable offline. It backs ``mdk dev --mock`` and every copilot test.

    Two modes:

    * **scripted** — pass ``script`` (an ordered list of
      :class:`PlannerOutcome`) and each :meth:`plan` call returns the next one
      (the last repeats once exhausted). Lets a test drive an exact
      intent→outcome sequence.
    * **rule-based** (default) — a few keyword rules cover the canonical
      requests ("add a context named X", "make the tone …", "ingest <path>",
      "add a … skill", "set model …"). Anything unmatched returns a
      ``needs_clarification`` outcome — the ambiguous-intent behavior (D6).

    The mock never guesses past its rules: an unrecognized request is a
    clarifying question, never a silent (possibly destructive) action.
    """

    def __init__(self, *, script: list[PlannerOutcome] | None = None) -> None:
        self._script = list(script or [])
        self._idx = 0
        self._rules = self._default_rules()

    def plan(self, request: str, *, agent: str) -> PlannerOutcome:
        if self._script:
            outcome = self._script[min(self._idx, len(self._script) - 1)]
            self._idx += 1
            return outcome
        lowered = request.lower()
        for rule in self._rules:
            if any(kw in lowered for kw in rule.keywords):
                return rule.build(request, agent)
        return PlannerOutcome(
            needs_clarification=(
                "I'm not sure which authoring action that maps to. Could you say what "
                "you'd like to change — e.g. add a context, edit the instructions, "
                "ingest a knowledge source, add a skill, or change the model?"
            )
        )

    # -- rule builders --------------------------------------------------------

    def _default_rules(self) -> list[_Rule]:
        return [
            # "improve my agent" autopilot (D7) — the failure-grounded request
            # built by movate.authoring.autopilot.build_improve_request. Checked
            # FIRST so its distinctive phrasing wins over the generic
            # "instruction" keyword in the request body. Proposes a single
            # additive+reversible+free fix (a missing-fact context) so the
            # hermetic autopilot test applies + verifies cleanly with no keys.
            _Rule(("failing the cases below", "propose targeted authoring"), self._improve),
            # KB ingest — checked before "context" so "ingest the docs" doesn't
            # fall into add-context. Networked + cost → the driver confirm-gates.
            _Rule(("ingest", "crawl", "knowledge base", "kb "), self._ingest_kb),
            _Rule(
                ("add a context", "add context", "returns-policy", "policy context"),
                self._add_context,
            ),
            _Rule(
                ("tone", "formal", "instruction", "prompt", "rewrite the"),
                self._edit_instructions,
            ),
            _Rule(("add a skill", "add skill", "calculator skill"), self._add_skill),
            _Rule(
                ("set the model", "set model", "switch the model", "use model"),
                self._set_model,
            ),
            _Rule(("eval case", "test case", "add a test"), self._add_eval_case),
        ]

    def _improve(self, request: str, agent: str) -> PlannerOutcome:
        """Scripted fix for the D7 autopilot's failure-grounded request.

        Proposes one additive+reversible+free action — a "missing-fact" context
        the agent can lean on for the failing cases. Deterministic so the
        hermetic autopilot test (mock eval → propose → apply → verify) is
        repeatable with no API keys. A real LLM planner would pick from the full
        catalog (edit-instructions / add-eval-case / …) per the failure detail.
        """
        return PlannerOutcome(
            actions=[
                ProposedAction(
                    name="add-context",
                    args={
                        "agent": agent,
                        "name": "eval-fixes",
                        "body": (
                            "# Eval fixes\n\n"
                            "Guidance added by the improve autopilot to address "
                            "failing eval cases.\n"
                        ),
                    },
                    rationale="add a missing-fact context to address the failing cases",
                )
            ],
            message="I'll add a context capturing the facts the failing cases need.",
        )

    def _add_context(self, request: str, agent: str) -> PlannerOutcome:
        name = _quoted_name(request, default="returns-policy")
        return PlannerOutcome(
            actions=[
                ProposedAction(
                    name="add-context",
                    args={"agent": agent, "name": name},
                    rationale=f"create and attach a new context {name!r}",
                )
            ],
            message=f"I'll add a context named {name!r}.",
        )

    def _edit_instructions(self, request: str, agent: str) -> PlannerOutcome:
        return PlannerOutcome(
            actions=[
                ProposedAction(
                    name="edit-instructions",
                    args={
                        "agent": agent,
                        "body": (
                            "You are a formal, professional assistant. Respond "
                            "concisely and in a courteous, businesslike tone.\n"
                        ),
                    },
                    rationale="rewrite prompt.md to set a formal tone",
                )
            ],
            message="I'll rewrite the instructions to set a more formal tone.",
        )

    def _ingest_kb(self, request: str, agent: str) -> PlannerOutcome:
        # Pull a path/URL token if the user gave one; else a sensible default
        # (the agent-local kb/ dir, the convention `kb ingest` uses).
        path = "kb"
        for token in request.split():
            if token.startswith(("http://", "https://", "/", "./", "~")) or token.endswith(
                (".md", ".txt", ".pdf")
            ):
                path = token.strip("\"'.,")
                break
        return PlannerOutcome(
            actions=[
                ProposedAction(
                    name="ingest-kb",
                    args={"agent": agent, "path": path},
                    rationale=f"ingest documents from {path!r} into the KB",
                )
            ],
            message=f"I'll ingest documents from {path!r} (this is networked + costs money).",
        )

    def _add_skill(self, request: str, agent: str) -> PlannerOutcome:
        name = _quoted_name(request, default="calculator")
        return PlannerOutcome(
            actions=[
                ProposedAction(
                    name="add-skill",
                    args={"name": name, "agent": agent},
                    rationale=f"scaffold and wire a {name!r} skill",
                )
            ],
            message=f"I'll scaffold a {name!r} skill and wire it into the agent.",
        )

    def _set_model(self, request: str, agent: str) -> PlannerOutcome:
        provider = "anthropic/claude-sonnet-4-6"
        for token in request.split():
            if "/" in token and not token.startswith(("http", "/")):
                provider = token.strip("\"'.,")
                break
        return PlannerOutcome(
            actions=[
                ProposedAction(
                    name="set-model",
                    args={"agent": agent, "provider": provider},
                    rationale=f"swap the primary model to {provider!r}",
                )
            ],
            message=f"I'll set the model to {provider!r} (this changes cost/behavior).",
        )

    def _add_eval_case(self, request: str, agent: str) -> PlannerOutcome:
        return PlannerOutcome(
            actions=[
                ProposedAction(
                    name="add-eval-case",
                    args={
                        "agent": agent,
                        "input": {"text": "example input"},
                        "expected": {"message": "example output"},
                    },
                    rationale="append a starter eval case",
                )
            ],
            message="I'll add a starter eval case to the dataset.",
        )


__all__ = [
    "LLMPlanner",
    "MockPlanner",
    "Planner",
    "PlannerError",
    "PlannerOutcome",
    "ProposedAction",
    "build_messages",
    "build_static_prefix",
    "parse_planner_response",
    "project_state_summary",
]
