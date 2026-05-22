"""Action builder for ``mdk menu``.

Given a :class:`movate.menu.WorkspaceStatus`, produce 5-10 contextual
suggestions ordered by how blocking they are. The highest-priority
action goes first (it's the suggested default when the operator just
hits Enter).

Priority order, roughly:

  1. Project not initialized at all          → ``init --project``
  2. No agents yet                            → ``init <name>``
  3. Critical env var missing                 → ``secrets set <var>``
  4. Agents exist but never validated         → ``validate``
  5. Validated but never run                  → ``run``
  6. Runs exist but no eval baseline          → ``eval --save-baseline``
  7. Always-available housekeeping            → ``snapshot create``,
                                                 ``doctor``, ``--help``

Each action exposes the literal ``argv`` that ``subprocess.run`` will
execute, so callers can either run it (the menu command) or just
print it (a future ``mdk menu --dry-run`` that lists suggestions
without prompting).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING


def _first_agent_dataset_input(agent_dir: Path) -> str:
    """Return a copy-pasteable example payload from the agent's first
    eval-dataset row, or literal ``"{}"`` if no dataset ships.

    Mirrors the helper :mod:`movate.cli.add_cmd` uses for its success
    Panel — kept as a sibling here (rather than imported) so the menu
    module stays free of CLI-layer dependencies. Same shape, same
    fallback. Best-effort: any read / JSON error silently falls back.
    """
    dataset = agent_dir / "evals" / "dataset.jsonl"
    if not dataset.is_file():
        return "{}"
    try:
        first_line = dataset.read_text().splitlines()[0]
        row = json.loads(first_line)
        payload = row.get("input")
        if payload is None:
            return "{}"
        return json.dumps(payload, separators=(",", ":"))
    except (OSError, IndexError, json.JSONDecodeError):
        return "{}"


if TYPE_CHECKING:
    from movate.menu.status import WorkspaceStatus


@dataclass(frozen=True)
class Action:
    """One menu suggestion.

    ``label`` is the human description ("Validate agents", "Set
    OPENAI_API_KEY"). ``command`` is the exact CLI string to display
    AND the argv that gets executed if the operator picks this entry.
    ``priority`` is just for sorting — never shown to the user; lower
    numbers float to the top of the menu.
    """

    label: str
    command: str
    argv: tuple[str, ...]
    priority: int = 100
    # Some actions need extra context the operator must fill in
    # (e.g. "Run an agent" → which agent? what input?). The menu
    # prints the command but skips auto-execution for these.
    needs_user_input: bool = False


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_actions(status: WorkspaceStatus) -> list[Action]:
    """Build a sorted action list from the workspace status.

    Ordered as the agent-authoring lifecycle so the menu reads as a
    guided walkthrough: develop (``mdk dev``) → validate → run → grounding
    check → deploy → test-deployed, with project/agent bootstrap ahead of
    it and housekeeping (doctor / --help) behind. Operators who want the
    full surface area run ``mdk --help``.
    """
    actions: list[Action] = []

    if not status.has_movate_yaml:
        actions.append(
            Action(
                label="Initialize this directory as an mdk project",
                command="mdk init --project",
                argv=("init", "--project"),
                priority=1,
            )
        )

    if not status.has_agents:
        actions.append(
            Action(
                label="Scaffold your first agent (guided dev loop)",
                command="mdk dev <agent-name>",
                argv=("dev",),
                priority=2,
                needs_user_input=True,
            )
        )
    else:
        # Lifecycle step 1 — the front door: a scaffold-aware loop to edit
        # the prompt, add a context, and live-test it.
        first_agent = status.agents[0]
        actions.append(
            Action(
                label=f"Develop {first_agent.name!r} — edit prompt, add context, live-test",
                command=f"mdk dev {first_agent.name}",
                argv=("dev", first_agent.name),
                priority=10,
            )
        )
        # Validate is cheap + safe; surface it whenever there are agents.
        actions.append(
            Action(
                label=f"Validate {len(status.agents)} agent(s)",
                command="mdk validate",
                argv=("validate",),
                priority=20,
            )
        )

    # Missing env vars — one entry per declared-required key.
    for var in status.missing_env_vars:
        actions.append(
            Action(
                label=f"Set {var.name}",
                command=f"mdk secrets set {var.name}",
                argv=("secrets", "set", var.name),
                priority=5,
                needs_user_input=True,  # prompts for the value
            )
        )

    if status.has_agents:
        # Suggest running the first agent with a real example payload
        # from its evals/dataset.jsonl[0].input (same helper that
        # `mdk add`'s success Panel uses). Falls back to literal '{}'
        # when the agent ships without a dataset — the operator's still
        # prompted to fill the input, but at least the command parses.
        first = status.agents[0]
        example = _first_agent_dataset_input(first.path)
        actions.append(
            Action(
                label=f"Run {first.name!r} locally",
                command=f"mdk run {first.name} '{example}'",
                argv=("run", first.name, example),
                priority=30,
                needs_user_input=example == "{}",  # only prompt if no real example
            )
        )
        # Lifecycle: grounding check — does it obey context + not hallucinate?
        actions.append(
            Action(
                label=f"Grounding check {first.name!r} (hallucination / context obedience)",
                command=f"mdk eval-scorecard {first.name}",
                argv=("eval-scorecard", first.name),
                priority=35,
            )
        )
        # Lifecycle: ship to Azure. Needs a deploy target only the operator
        # knows, so we print the template rather than auto-running (also
        # avoids auto-firing a destructive deploy from the menu).
        actions.append(
            Action(
                label="Deploy agents to Azure",
                command="mdk deploy --target <env> --mode agents",
                argv=("deploy", "--target", "<env>", "--mode", "agents"),
                priority=50,
                needs_user_input=True,
            )
        )
        # Lifecycle: confirm the deployed agent behaves like local.
        actions.append(
            Action(
                label=f"Test {first.name!r} on the deployed runtime",
                command=f"mdk run {first.name} --target <env> '{example}'",
                argv=("run", first.name, "--target", "<env>", example),
                priority=55,
                needs_user_input=True,
            )
        )

    # Eval baseline gap.
    for agent in status.agents_without_baseline[:2]:  # cap at 2 to keep menu tight
        actions.append(
            Action(
                label=f"Record eval baseline for {agent.name!r}",
                command=f"mdk eval {agent.name} --save-baseline",
                argv=("eval", agent.name, "--save-baseline"),
                priority=40,
            )
        )

    # Always-available housekeeping (run last in the menu).
    if status.has_movate_yaml:
        actions.append(
            Action(
                label="Snapshot the current state",
                command="mdk snapshot create",
                argv=("snapshot", "create"),
                priority=80,
            )
        )

    actions.append(
        Action(
            label="Diagnose environment with doctor",
            command="mdk doctor",
            argv=("doctor",),
            priority=90,
        )
    )

    actions.append(
        Action(
            label="View all commands (--help)",
            command="mdk --help",
            argv=("--help",),
            priority=95,
        )
    )

    actions.sort(key=lambda a: a.priority)
    return actions
