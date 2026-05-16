"""Runbook generator — pure function from project state to markdown.

The runbook is the operator-facing document for an on-call engineer:
"what's in this project, how do I run it, what do I do when it
breaks?" We auto-generate as much as possible so the runbook stays
in sync with the code (no stale wiki page rotting in a Confluence
space the team forgot about).

What gets captured:

* **Project header** — name, version, description from movate.yaml.
* **Agents** — one section per agent, with model, prompt path, eval
  dataset path. Sourced from ``agents/*/agent.yaml``.
* **Environment** — required + optional env vars, discovered via the
  same logic as :mod:`movate.menu.status` (``.env.example``
  primarily, common provider keys as fallback).
* **Operations** — canned recipes for common day-2 tasks:
  ``mdk run`` / ``eval`` / ``snapshot`` / ``rollback`` / ``trace``.
* **Troubleshooting** — exit-code legend, common gotchas, where to
  look for state files.

Future fields:

* Workflows — one section per workflow, step count, entry/exit agents
* Profiles + promotions snapshot (when this project has them)
* Last N runs from SQLite (when ``mdk explain`` matures)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentEntry:
    """One agent's worth of runbook data.

    Captured at context-build time so the generator function is pure —
    given a :class:`RunbookContext`, the output is deterministic.
    """

    name: str
    description: str = ""
    model_provider: str = ""
    has_prompt: bool = False
    has_eval_dataset: bool = False


@dataclass(frozen=True)
class RunbookContext:
    """Everything the runbook generator needs to do its job.

    Frozen so a caller can build it once and pass it to multiple
    generators (markdown today, HTML tomorrow). All fields default
    to empty so the generator handles partially-populated projects
    gracefully (a fresh ``mdk init --project`` should still render
    a useful runbook).
    """

    project_name: str = "movate project"
    project_description: str = ""
    project_version: str = ""
    project_root: Path = field(default_factory=Path)
    agents: tuple[AgentEntry, ...] = ()
    required_env_vars: tuple[str, ...] = ()
    optional_env_vars: tuple[str, ...] = ()
    has_movate_yaml: bool = False
    has_snapshots: bool = False


# ---------------------------------------------------------------------------
# Context builder — reads the project + populates the context
# ---------------------------------------------------------------------------


def build_context(project_root: Path) -> RunbookContext:
    """Walk ``project_root`` and assemble the :class:`RunbookContext`.

    Best-effort: missing files / unparseable YAML degrade to empty
    fields. The generator handles partial data gracefully. We never
    want a malformed agent.yaml to break the runbook generation —
    operators need the runbook MOST when something's broken.
    """
    root = project_root.resolve()

    project = _read_project(root)
    agents = _find_agents(root)
    required, optional = _discover_env_vars(root)

    return RunbookContext(
        project_name=project.get("name") or root.name,
        project_description=project.get("description") or "",
        project_version=project.get("version") or "",
        project_root=root,
        agents=agents,
        required_env_vars=required,
        optional_env_vars=optional,
        has_movate_yaml=(root / "movate.yaml").is_file(),
        has_snapshots=(root / ".movate" / "snapshots").is_dir()
        and any((root / ".movate" / "snapshots").iterdir()),
    )


def _read_project(root: Path) -> dict[str, Any]:
    """Pull name/description/version out of movate.yaml. Permissive."""
    for candidate in ("movate.yaml", "mdk.yaml"):
        path = root / candidate
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _find_agents(root: Path) -> tuple[AgentEntry, ...]:
    """Walk ``agents/*/agent.yaml`` and build :class:`AgentEntry`s."""
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return ()
    entries: list[AgentEntry] = []
    try:
        children = sorted(agents_dir.iterdir())
    except OSError:
        return ()
    for child in children:
        if not child.is_dir():
            continue
        agent_yaml = child / "agent.yaml"
        if not agent_yaml.is_file():
            continue
        try:
            data = yaml.safe_load(agent_yaml.read_text()) or {}
        except yaml.YAMLError:
            data = {}
        model_block = data.get("model") or {}
        provider = ""
        if isinstance(model_block, dict):
            provider = str(model_block.get("provider") or "")

        # Eval dataset: either evals/<agent>/dataset.* under project, or
        # evals/dataset.* alongside the agent (older convention).
        has_dataset = (
            (root / "evals" / child.name).is_dir() and any((root / "evals" / child.name).iterdir())
        ) or (child / "evals").is_dir()

        entries.append(
            AgentEntry(
                name=child.name,
                description=str(data.get("description") or ""),
                model_provider=provider,
                has_prompt=(child / "prompt.md").is_file(),
                has_eval_dataset=has_dataset,
            )
        )
    return tuple(entries)


def _discover_env_vars(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split env vars into (required, optional) from .env.example.

    Convention: a commented-out line in .env.example is "optional"
    (e.g. ``# ANTHROPIC_API_KEY=`` means "you don't NEED this"). An
    uncommented declaration is required.
    """
    path = root / ".env.example"
    if not path.is_file():
        return ((), ())

    required: list[str] = []
    optional: list[str] = []
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            is_commented = line.startswith("#")
            # Strip the leading comment marker to inspect the inner content.
            stripped = line.lstrip("#").strip()
            if "=" not in stripped:
                continue
            name = stripped.split("=", 1)[0].strip()
            if not name or not name.isidentifier():
                continue
            if is_commented:
                optional.append(name)
            else:
                required.append(name)
    except OSError:
        return ((), ())
    return (tuple(required), tuple(optional))


# ---------------------------------------------------------------------------
# Generator — pure function from context to markdown
# ---------------------------------------------------------------------------


def generate_runbook(ctx: RunbookContext) -> str:
    """Render the runbook as markdown.

    Pure: same context → same string. The structure is fixed for MVP;
    operators who want a custom layout can post-process this output or
    propose changes to the canonical template (one PR moves the needle
    for the whole org).
    """
    sections: list[str] = [
        _header(ctx),
        _project_overview(ctx),
        _agents_section(ctx),
        _environment_section(ctx),
        _operations_section(ctx),
        _state_cluster_section(ctx),
        _troubleshooting_section(),
        _footer(),
    ]
    return "\n\n".join(s.strip() for s in sections if s.strip()) + "\n"


def _header(ctx: RunbookContext) -> str:
    return f"# Runbook — {ctx.project_name}"


def _project_overview(ctx: RunbookContext) -> str:
    lines = ["## Overview", ""]
    if ctx.project_description:
        lines.append(ctx.project_description)
        lines.append("")
    lines.append(f"- **Project root**: `{ctx.project_root}`")
    if ctx.project_version:
        lines.append(f"- **Version**: `{ctx.project_version}`")
    lines.append(f"- **Has movate.yaml**: {'yes' if ctx.has_movate_yaml else 'no'}")
    lines.append(f"- **Agents**: {len(ctx.agents)}")
    return "\n".join(lines)


def _agents_section(ctx: RunbookContext) -> str:
    if not ctx.agents:
        return (
            "## Agents\n\n"
            "_No agents yet._ Scaffold one with "
            "`mdk init <name>` or grab the FAQ template via `mdk demo`."
        )
    lines = ["## Agents", ""]
    for agent in ctx.agents:
        lines.append(f"### `{agent.name}`")
        if agent.description:
            lines.append("")
            lines.append(agent.description)
        lines.append("")
        if agent.model_provider:
            lines.append(f"- **Model**: `{agent.model_provider}`")
        lines.append(f"- **Prompt file**: {'yes' if agent.has_prompt else 'missing'}")
        lines.append(f"- **Eval dataset**: {'yes' if agent.has_eval_dataset else 'none recorded'}")
        lines.append("")
        lines.append("**Run it:**")
        lines.append("")
        lines.append("```bash")
        lines.append(f"mdk run {agent.name} '{{}}'   # provide JSON matching input schema")
        if agent.has_eval_dataset:
            lines.append(f"mdk eval {agent.name}")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _environment_section(ctx: RunbookContext) -> str:
    lines = ["## Environment", ""]
    if not ctx.required_env_vars and not ctx.optional_env_vars:
        lines.append(
            "_No `.env.example` found._ The project may inherit env "
            "from the parent shell. Check `mdk doctor` for required keys."
        )
        return "\n".join(lines)

    if ctx.required_env_vars:
        lines.append("### Required")
        lines.append("")
        for name in ctx.required_env_vars:
            lines.append(f"- `{name}`")
        lines.append("")

    if ctx.optional_env_vars:
        lines.append("### Optional")
        lines.append("")
        for name in ctx.optional_env_vars:
            lines.append(f"- `{name}`")
        lines.append("")

    lines.append("**Verify with:**")
    lines.append("")
    lines.append("```bash")
    lines.append("mdk env check       # all required vars set?")
    lines.append("mdk secrets list    # what's in the active profile?")
    lines.append("```")
    return "\n".join(lines)


def _operations_section(ctx: RunbookContext) -> str:
    # Pick the first agent (if any) for example commands so the runbook
    # shows the operator something runnable instead of a placeholder.
    example_agent = ctx.agents[0].name if ctx.agents else "<agent-name>"
    lines = [
        "## Common operations",
        "",
        "### Run an agent",
        "",
        "```bash",
        f"mdk run {example_agent} '{{}}'      # one-shot",
        f"mdk chat {example_agent}            # REPL",
        "```",
        "",
        "### Evaluate",
        "",
        "```bash",
        f"mdk eval {example_agent}                  # full dataset",
        f"mdk eval {example_agent} --gate 0.7       # CI gate",
        f"mdk bench {example_agent}                 # multi-model comparison",
        "```",
        "",
        "### Deploy / serve",
        "",
        "```bash",
        "mdk serve            # FastAPI runtime",
        "mdk worker           # job worker",
        "mdk deploy           # build + ship to ACR + ACA",
        "```",
        "",
        "### Diagnose",
        "",
        "```bash",
        "mdk doctor           # check env + tooling + connectivity",
        "mdk menu             # workspace status + suggested next step",
        "mdk logs             # tail recent runs",
        "```",
    ]
    return "\n".join(lines)


def _state_cluster_section(ctx: RunbookContext) -> str:
    lines = [
        "## State cluster (Terraform-for-AI primitives)",
        "",
        "```bash",
        'mdk snapshot create -d "<note>"   # capture current state',
        "mdk snapshot list                  # browse history",
        "mdk diff <a> <b>                   # compare two snapshots",
        "mdk rollback <hash>                # undo to a prior state",
        "mdk migrate <hash> --apply         # selective file restore",
        "mdk promote <hash> --to prod       # mark canonical for a profile",
        "mdk audit                          # scan for state issues",
        "```",
        "",
    ]
    if ctx.has_snapshots:
        lines.append(
            "_This project has snapshots stored locally._ Run `mdk snapshot list` to see them."
        )
    else:
        lines.append(
            '_No snapshots yet._ Take your first one with `mdk snapshot create -d "baseline"`.'
        )
    return "\n".join(lines)


def _troubleshooting_section() -> str:
    return (
        "## Troubleshooting\n"
        "\n"
        "### Exit codes\n"
        "\n"
        "- `0` — success\n"
        "- `1` — expected failure (eval gate missed, snapshot not found, etc.)\n"
        "- `2` — operator error (bad args, malformed YAML, missing required env var)\n"
        "\n"
        "### Where state lives\n"
        "\n"
        "- `./.movate/local.db` — run history (SQLite)\n"
        "- `./.movate/snapshots/` — content-addressed snapshots\n"
        "- `./.movate/promotions.yaml` — promotion audit log\n"
        "- `~/.movate/profiles/` — operator-level profiles (cross-project)\n"
        "- `~/.movate/secrets/<profile>.yaml` — per-profile secrets (chmod 0600)\n"
        "\n"
        "### Common gotchas\n"
        "\n"
        "- **Missing API key** → `mdk secrets set OPENAI_API_KEY` or set it in `.env`.\n"
        "- **Eval flakiness** → run with `--runs 3 --gate-mode mean` for stability.\n"
        "- **YAML drift between PRs** → `mdk fmt --check` in CI.\n"
        '- **"Where is X currently deployed?"** → `mdk promote --current <profile>`.\n'
    )


def _footer() -> str:
    return (
        "---\n"
        "\n"
        "_This runbook was generated by `mdk docs runbook`. Re-run after "
        "significant changes to keep it in sync._"
    )
