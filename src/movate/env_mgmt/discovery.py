"""Env-var discovery — walk a project + collect every required name.

Three sources combine into a deduped :class:`EnvVarRef` list:

1. **``.env.example``** — operator-curated. If present, the format
   ``KEY=`` (value-less) means *required*; ``KEY=default`` means
   *optional with default*. ``KEY=value`` from the operator side is
   informational (the value should not be checked in; the example
   form just communicates that a value is expected).
2. **``agent.yaml``** scans — regex match ``${VAR}`` and ``$VAR``
   references in every agent's YAML. Lexical, not Jinja-aware:
   catches the common pattern but won't trace complex template
   expressions.
3. **Skill ``impl.py``** scans — ``os.environ["VAR"]`` and
   ``os.environ.get("VAR", ...)`` references. Same lexical strategy.

Discovery is **best-effort**. The operator-supplied ``.env.example``
is the override mechanism — if discovery misses something, the
operator adds it to ``.env.example`` and the check sees it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class EnvSource(StrEnum):
    """Where an env-var reference was discovered."""

    EXAMPLE = "env.example"
    AGENT_YAML = "agent.yaml"
    SKILL_IMPL = "skill impl.py"


@dataclass(frozen=True)
class EnvVarRef:
    """One env-var requirement.

    ``required`` is True for every reference except ``KEY=default``
    entries in ``.env.example``, which are explicitly optional.
    ``default`` carries the example value when set.

    ``sources`` lists every place this var was found — useful for
    operators chasing "why does my project need FOO?".
    """

    name: str
    required: bool = True
    default: str = ""
    sources: tuple[EnvSource, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# .env.example line shape: KEY[=value]
# Comments (#) and blank lines are skipped.
# We accept all-caps + underscores + digits — the universal env-var
# convention. Lowercase is allowed for non-Unix-shell-compatible
# names (rare but Windows tooling sometimes does it).
_ENV_EXAMPLE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")

# YAML ``${VAR}`` / ``$VAR`` references. The brace form is the more
# common one (escapes underscores cleanly). The bare form is
# accepted but only matches alphanumeric + underscore.
_YAML_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]+)")

# Python ``os.environ["VAR"]`` or ``os.environ.get("VAR")``.
# Single-quote and double-quote variants both match.
_PY_ENVIRON_RE = re.compile(
    r"""os\.environ
        (?:\[                            # subscript form
            ['\"]([A-Z_][A-Z0-9_]*)['\"]
        \]
        |
        \.get\(                          # .get(...) form
            ['\"]([A-Z_][A-Z0-9_]*)['\"]
        )""",
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_env_example(path: Path) -> list[EnvVarRef]:
    """Parse a ``.env.example`` file.

    Comments (lines starting with ``#``) and blank lines are skipped.
    ``KEY=`` (value-less) → required. ``KEY=default`` (with value) →
    optional, with the default captured. The example's default value
    is informational — the operator can override per-shell.
    """
    if not path.is_file():
        return []
    refs: list[EnvVarRef] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_EXAMPLE_RE.match(stripped)
        if not match:
            continue
        name = match.group(1)
        value = match.group(2).strip()
        # Strip surrounding quotes from example values for cleaner display.
        # Needs at least 2 chars (the opening + closing quote) for the
        # check to make sense; single-quote-only strings stay as-is.
        min_quoted_len = 2
        if (
            len(value) >= min_quoted_len
            and value[0] in ('"', "'")
            and value[-1] == value[0]
        ):
            value = value[1:-1]
        refs.append(
            EnvVarRef(
                name=name,
                required=not value,
                default=value,
                sources=(EnvSource.EXAMPLE,),
            )
        )
    return refs


def _scan_yaml_for_vars(path: Path) -> set[str]:
    """Return every ``${VAR}`` / ``$VAR`` name in a YAML file."""
    if not path.is_file():
        return set()
    try:
        content = path.read_text()
    except OSError:
        return set()
    names: set[str] = set()
    for match in _YAML_VAR_RE.finditer(content):
        # Either group 1 (braced) or group 2 (bare) matches.
        name = match.group(1) or match.group(2)
        if name:
            names.add(name)
    return names


def _scan_python_for_vars(path: Path) -> set[str]:
    """Return every ``os.environ[...]`` / ``os.environ.get(...)`` name."""
    if not path.is_file():
        return set()
    try:
        content = path.read_text()
    except OSError:
        return set()
    names: set[str] = set()
    for match in _PY_ENVIRON_RE.finditer(content):
        # Either group 1 (subscript) or group 2 (.get) matches.
        name = match.group(1) or match.group(2)
        if name:
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def discover_env_vars(project_root: Path) -> list[EnvVarRef]:
    """Walk ``project_root`` + return deduped :class:`EnvVarRef` list.

    Merge strategy:

    * Names from ``.env.example`` take precedence on ``required`` /
      ``default`` (operator's explicit intent wins over discovery
      heuristics).
    * Names discovered ONLY in agent.yaml / impl.py default to
      ``required=True`` (no way to know they have defaults).
    * ``sources`` accumulates — a name found in all three places
      lists all three.

    Sorted by name for deterministic output.
    """
    by_name: dict[str, EnvVarRef] = {}

    # 1. .env.example (canonical)
    example_path = project_root / ".env.example"
    for ref in parse_env_example(example_path):
        by_name[ref.name] = ref

    # 2. agent.yaml scans across agents/
    agents_dir = project_root / "agents"
    if agents_dir.is_dir():
        for yaml_path in sorted(agents_dir.rglob("agent.yaml")):
            names = _scan_yaml_for_vars(yaml_path)
            for name in names:
                _merge(by_name, name, EnvSource.AGENT_YAML)

    # 3. impl.py scans across skills/
    skills_dir = project_root / "skills"
    if skills_dir.is_dir():
        for py_path in sorted(skills_dir.rglob("impl.py")):
            names = _scan_python_for_vars(py_path)
            for name in names:
                _merge(by_name, name, EnvSource.SKILL_IMPL)

    return sorted(by_name.values(), key=lambda r: r.name)


def _merge(by_name: dict[str, EnvVarRef], name: str, source: EnvSource) -> None:
    """Add a source to an existing entry, or create one with default required=True."""
    existing = by_name.get(name)
    if existing is None:
        by_name[name] = EnvVarRef(
            name=name,
            required=True,
            default="",
            sources=(source,),
        )
        return
    if source in existing.sources:
        return  # already counted
    by_name[name] = EnvVarRef(
        name=existing.name,
        required=existing.required,
        default=existing.default,
        sources=(*existing.sources, source),
    )


def check_presence(
    refs: Iterable[EnvVarRef],
    env: dict[str, str],
    *,
    strict: bool = False,
) -> tuple[list[EnvVarRef], list[EnvVarRef]]:
    """Split refs into (missing, present) lists against the live env.

    ``strict=True`` treats optional vars (those with defaults) as
    failures when unset. The CI gate use-case wants strict; the
    interactive use-case wants the default (lax) so a missing
    optional doesn't drown out a missing required.
    """
    missing: list[EnvVarRef] = []
    present: list[EnvVarRef] = []
    for ref in refs:
        if env.get(ref.name):
            present.append(ref)
        elif ref.required or strict:
            missing.append(ref)
        # else: optional + unset + non-strict → silent skip
    return missing, present
