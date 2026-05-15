"""Workspace status inspection for ``mdk menu``.

Light-touch checks — file existence, profile marker, env-var presence,
agent enumeration. **No I/O against external services** (LLM providers,
Azure, etc.) — that's :mod:`movate.cli.doctor`'s job. The menu fires
on every Tab-complete-style invocation, so it must be cheap.

Everything is a dataclass for easy testing — the inspector returns
the snapshot, the renderer is separate, the action-builder consumes
the same snapshot. No tangled rendering inside the inspector.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentInfo:
    """One agent found in the workspace.

    ``has_eval_dataset`` lights up the "record baseline" suggestion;
    ``has_baseline`` lights up the "compare against baseline" one.
    Both are best-effort — a missing file means "no signal," never
    a hard error.
    """

    name: str
    path: Path
    has_eval_dataset: bool = False
    has_baseline: bool = False


@dataclass(frozen=True)
class EnvVarStatus:
    """One env var the workspace expects + whether it's set.

    ``required`` matches the convention from ``mdk env``: vars
    referenced in ``.env.example`` are considered required; vars
    referenced only in agent ``${VAR}`` templates are "soft" required.
    """

    name: str
    set_in_env: bool
    required: bool = True


@dataclass(frozen=True)
class WorkspaceStatus:
    """Snapshot of the workspace's current state.

    Designed so the action-builder (:func:`movate.menu.build_actions`)
    can derive every suggestion from this object alone — no second
    pass over the filesystem.
    """

    project_root: Path
    has_movate_yaml: bool
    movate_yaml_version: str | None
    active_profile: str | None
    agents: tuple[AgentInfo, ...] = field(default_factory=tuple)
    env_vars: tuple[EnvVarStatus, ...] = field(default_factory=tuple)
    has_local_db: bool = False
    snapshot_count: int = 0
    has_dotenv_file: bool = False

    @property
    def has_agents(self) -> bool:
        return len(self.agents) > 0

    @property
    def missing_env_vars(self) -> tuple[EnvVarStatus, ...]:
        """Required env vars that aren't currently set."""
        return tuple(v for v in self.env_vars if v.required and not v.set_in_env)

    @property
    def agents_without_baseline(self) -> tuple[AgentInfo, ...]:
        """Agents that have eval datasets but no recorded baseline yet."""
        return tuple(a for a in self.agents if a.has_eval_dataset and not a.has_baseline)


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def inspect_workspace(project_root: Path | str = ".") -> WorkspaceStatus:
    """Walk ``project_root`` and return a :class:`WorkspaceStatus`.

    Designed to run in well under 100ms on a typical project — purely
    filesystem checks, no parsing beyond reading the first line of
    ``movate.yaml`` to surface the api_version.

    Always returns a status — never raises. Missing files, bad
    permissions, and unparseable manifests degrade to ``None`` /
    empty tuples so the menu can still render and suggest "run
    doctor" rather than crashing.
    """
    root = Path(project_root).resolve()

    return WorkspaceStatus(
        project_root=root,
        has_movate_yaml=_has_movate_yaml(root),
        movate_yaml_version=_movate_yaml_version(root),
        active_profile=_active_profile(),
        agents=_find_agents(root),
        env_vars=_check_env_vars(root),
        has_local_db=(root / ".movate" / "local.db").is_file(),
        snapshot_count=_count_snapshots(root),
        has_dotenv_file=(root / ".env").is_file(),
    )


def _has_movate_yaml(root: Path) -> bool:
    return (root / "movate.yaml").is_file() or (root / "mdk.yaml").is_file()


def _movate_yaml_version(root: Path) -> str | None:
    """Read the ``api_version`` line from ``movate.yaml`` (best-effort)."""
    for candidate in ("movate.yaml", "mdk.yaml"):
        path = root / candidate
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines()[:20]:
                stripped = line.strip()
                if stripped.startswith("api_version:"):
                    return stripped.split(":", 1)[1].strip().strip('"').strip("'")
        except OSError:
            return None
    return None


def _active_profile() -> str | None:
    """Read the active profile marker via the profiles module if available."""
    try:
        from movate.profiles import get_active_profile  # noqa: PLC0415

        return get_active_profile()
    except (ImportError, OSError):
        return None


def _find_agents(root: Path) -> tuple[AgentInfo, ...]:
    """Walk ``agents/*/agent.yaml`` and collect basic agent info.

    Best-effort: a broken ``agents/`` symlink or a non-readable
    subdir is silently skipped rather than aborting the inspection.
    """
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return ()

    results: list[AgentInfo] = []
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
        # Look for eval dataset under evals/<agent-name>/*.{jsonl,yaml}
        # or alongside the agent at evals/dataset.{jsonl,yaml}
        eval_dir = root / "evals" / child.name
        has_dataset = eval_dir.is_dir() and any(eval_dir.iterdir())
        # Baseline: evals/<agent>/baseline.json by convention
        baseline = eval_dir / "baseline.json"
        results.append(
            AgentInfo(
                name=child.name,
                path=child,
                has_eval_dataset=has_dataset,
                has_baseline=baseline.is_file(),
            )
        )
    return tuple(results)


# Env vars the menu treats as "expected" even when not declared anywhere.
# Operators almost always need one of these to actually run an agent.
_COMMON_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_API_KEY")


def _check_env_vars(root: Path) -> tuple[EnvVarStatus, ...]:
    """Build the env-var status list.

    Pulls vars from two sources:
      1. ``.env.example`` (treated as required — they're in the
         template for a reason)
      2. The hardcoded :data:`_COMMON_PROVIDER_KEYS` (soft required
         — at least one needs to be set for runs to work)

    Falls back to the common keys only if no ``.env.example`` exists.
    """
    declared = _parse_env_example(root)

    if declared:
        return tuple(
            EnvVarStatus(name=name, set_in_env=bool(os.environ.get(name)), required=True)
            for name in declared
        )
    # Fall back: at least surface that *some* provider key is needed.
    return tuple(
        EnvVarStatus(
            name=name,
            set_in_env=bool(os.environ.get(name)),
            required=False,  # any-one-of, not all-of
        )
        for name in _COMMON_PROVIDER_KEYS
    )


def _parse_env_example(root: Path) -> list[str]:
    """Extract var names from ``.env.example`` (skip blank lines + comments)."""
    path = root / ".env.example"
    if not path.is_file():
        return []
    names: list[str] = []
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            name = stripped.split("=", 1)[0].strip()
            if name and name.isidentifier():
                names.append(name)
    except OSError:
        return []
    return names


def _count_snapshots(root: Path) -> int:
    """Count subdirectories in ``.movate/snapshots/``."""
    snapshots_dir = root / ".movate" / "snapshots"
    if not snapshots_dir.is_dir():
        return 0
    try:
        return sum(1 for c in snapshots_dir.iterdir() if c.is_dir())
    except OSError:
        return 0
