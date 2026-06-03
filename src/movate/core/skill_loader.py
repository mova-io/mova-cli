"""Skill loader: parse a ``skills/<name>/`` directory into a validated SkillBundle.

Mirrors :mod:`movate.core.loader` for skills. The two stay distinct rather
than sharing code because their domains diverge — skills have an
implementation backend pointer where agents have a prompt template, and
the two have different file conventions. Folding them together would
make either harder to evolve.

Output is a :class:`SkillBundle` carrying the parsed :class:`SkillSpec`,
the compiled JSON Schema dicts for input/output, and a reference to the
agent directory (used to resolve relative paths in the implementation
entry).

See ``docs/adr/002-skills-and-contexts.md`` for the full design.
"""

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from movate.core.loader import AgentLoadError, _resolve_schema, format_agent_validation_error
from movate.core.models import SkillSpec

if TYPE_CHECKING:
    from movate.core.models import SkillRef

_log = logging.getLogger(__name__)


class SkillLoadError(Exception):
    """Raised when a skill directory fails to load or validate."""


@dataclass
class SkillBundle:
    """Fully-resolved skill: spec + compiled schemas + dir for relative paths.

    Counterpart to :class:`movate.core.loader.AgentBundle`. Held in the
    skill registry built at agent-load time; the executor's tool-use
    loop reads ``input_schema`` / ``output_schema`` / ``spec`` to
    dispatch + validate.
    """

    spec: SkillSpec
    skill_dir: Path
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator


def load_skill(path: str | Path) -> SkillBundle:
    """Load a skill directory. Raises SkillLoadError on any validation failure.

    Expects ``<path>/skill.yaml`` to exist; schemas resolve the same
    way they do for agents (path string → load file; inline dict →
    compile via the shorthand compiler).
    """
    skill_dir = Path(path).resolve()
    if not skill_dir.is_dir():
        raise SkillLoadError(f"skill path is not a directory: {skill_dir}")

    yaml_path = skill_dir / "skill.yaml"
    if not yaml_path.exists():
        raise SkillLoadError(f"skill.yaml not found in {skill_dir}")

    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"invalid YAML in {yaml_path}: {exc}") from exc

    try:
        spec = SkillSpec.model_validate(raw)
    except ValidationError as exc:
        # Reuse the agent loader's friendly formatter (drops pydantic's raw
        # ``errors.pydantic.dev`` trailer + ``input_type=dict`` noise, lists the
        # offending fields with a did-you-mean) so the customer-facing
        # ``invalid_bundle`` message is readable instead of a raw pydantic dump.
        friendly = format_agent_validation_error(exc, SkillSpec, filename="skill.yaml")
        raise SkillLoadError(f"{skill_dir.name}/skill.yaml validation failed:\n{friendly}") from exc

    # Reuse the agent loader's schema-resolution helper — same
    # path-or-inline-dict semantics, just labelled with the field name
    # for error messages.
    try:
        input_schema = _resolve_schema(spec.schemas.input, agent_dir=skill_dir, label="skill.input")
        output_schema = _resolve_schema(
            spec.schemas.output, agent_dir=skill_dir, label="skill.output"
        )
    except AgentLoadError as exc:
        # Normalize to SkillLoadError so callers don't have to catch both.
        raise SkillLoadError(str(exc)) from exc

    try:
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
    except Exception as exc:
        raise SkillLoadError(f"invalid JSON schema in {skill_dir}: {exc}") from exc

    return SkillBundle(
        spec=spec,
        skill_dir=skill_dir,
        input_schema=input_schema,
        output_schema=output_schema,
        input_validator=Draft202012Validator(input_schema),
        output_validator=Draft202012Validator(output_schema),
    )


def load_skill_registry(project_root: str | Path) -> dict[str, SkillBundle]:
    """Discover every skill under ``<project_root>/skills/<name>/``.

    Returns a name → :class:`SkillBundle` map. Skills that fail to
    parse surface as :class:`SkillLoadError` — we don't silently skip
    them, because a typo in one skill.yaml would otherwise look like a
    "skill not found" error at agent-load time and waste operator
    debug time.

    Empty registry (no ``skills/`` folder, or it's empty) is the
    permissive default — agents whose ``skills:`` list is empty
    don't care; agents that reference a missing skill fail later
    at name resolution.
    """
    project_dir = Path(project_root).resolve()
    skills_root = project_dir / "skills"
    if not skills_root.is_dir():
        return {}

    registry: dict[str, SkillBundle] = {}
    for skill_dir in sorted(skills_root.iterdir()):
        # Ignore non-directory entries (README.md at the skills/ root,
        # stray .DS_Store, etc.) and dotfiles.
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        # Tolerate dirs that don't have a skill.yaml — they may be
        # work-in-progress or shared utility folders next to skills.
        if not (skill_dir / "skill.yaml").exists():
            continue
        bundle = load_skill(skill_dir)
        if bundle.spec.name in registry:
            raise SkillLoadError(
                f"duplicate skill name {bundle.spec.name!r} — declared in "
                f"both {registry[bundle.spec.name].skill_dir} and {skill_dir}"
            )
        registry[bundle.spec.name] = bundle
    return registry


def _check_version_constraint(
    skill_name: str,
    installed_version: str,
    constraint: str,
    agent_name: str,
) -> None:
    """Check that ``installed_version`` satisfies ``constraint``.

    ``constraint`` is a pip-style specifier string (``"^1.2"``,
    ``">=1.0,<2.0"``, ``"1.2.3"``) or ``"*"`` (any version accepted).
    ``"^1.2"`` is a caret-range shorthand: ``>=1.2,<2.0``.

    Raises :class:`AgentLoadError` with a descriptive message when the
    constraint is not met. Emits a :mod:`warnings` warning when the
    installed skill has no version field (treated as ``"0.0.0"``).
    """
    if constraint == "*":
        return  # bare-string shorthand — any version accepted

    # Normalize caret ranges + bare version strings before handing to
    # packaging.specifiers (which only understands pip-style specifiers).
    normalized = _normalize_constraint(constraint)

    from packaging.specifiers import InvalidSpecifier, SpecifierSet  # noqa: PLC0415
    from packaging.version import Version  # noqa: PLC0415

    try:
        spec_set = SpecifierSet(normalized)
    except InvalidSpecifier as exc:
        raise AgentLoadError(
            f"skill {skill_name!r}: invalid version constraint {constraint!r} — {exc}"
        ) from exc

    # Treat bare skill.yaml "version: 0.0.0" from no-version field as unknown.
    installed = installed_version.strip()
    is_fallback_version = installed == "0.0.0"
    if is_fallback_version:
        warnings.warn(
            f"skill {skill_name!r} has no version field (defaulting to '0.0.0'); "
            f"agent {agent_name!r} requires {constraint!r}. Pin a version in "
            f"skills/{skill_name}/skill.yaml to silence this warning.",
            UserWarning,
            stacklevel=4,
        )

    try:
        parsed = Version(installed)
    except Exception as exc:
        raise AgentLoadError(
            f"skill {skill_name!r}: installed version {installed!r} is not valid semver: {exc}"
        ) from exc

    if parsed not in spec_set:
        raise AgentLoadError(
            f"skill {skill_name!r} version {installed!r} does not satisfy "
            f"constraint {constraint!r} required by agent {agent_name!r}"
        )


_BARE_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def _normalize_constraint(constraint: str) -> str:
    """Normalize a constraint string to a form ``packaging.specifiers`` understands.

    Two normalizations:

    * ``^MAJOR.MINOR[.PATCH]`` (caret range, e.g. ``^1.2``) → ``>=1.2,<2.0.0``
    * Bare version string (e.g. ``1.2.3``) → ``==1.2.3`` (exact match)

    All other specifiers (``>=1.0,<2.0``, ``~=1.2``, etc.) pass through
    unchanged — ``packaging.specifiers.SpecifierSet`` handles them natively.
    """
    stripped = constraint.strip()

    # Bare version → exact equality specifier.
    if _BARE_VERSION_RE.match(stripped):
        return f"=={stripped}"

    # Caret range → >=lo,<(major+1).0.0
    def _replace(m: re.Match[str]) -> str:
        parts = m.group(1).split(".")
        major = int(parts[0])
        lo = m.group(1)
        # Next-breaking: bump MAJOR; minor+patch become 0.
        hi = f"{major + 1}.0.0"
        return f">={lo},<{hi}"

    return re.sub(r"\^(\d+(?:\.\d+)*)", _replace, stripped)


def resolve_agent_skills(
    skill_refs: list[SkillRef] | list[str],
    registry: dict[str, SkillBundle],
    *,
    agent_name: str = "<unknown>",
) -> list[SkillBundle]:
    """Resolve an agent's ``skills: [...]`` list against the project registry.

    Accepts both the legacy ``list[str]`` form and the new
    ``list[SkillRef]`` form (see :class:`movate.core.models.SkillRef`).

    Returns the matching :class:`SkillBundle` list in declaration order.

    Raises:
        :class:`SkillLoadError` — unknown skill name (typo / missing registration).
        :class:`AgentLoadError` — version constraint not satisfied.
    """
    resolved: list[SkillBundle] = []
    for ref in skill_refs:
        # Accept both bare strings (legacy callers) and SkillRef objects.
        name: str = ref if isinstance(ref, str) else ref.name
        constraint: str = "*" if isinstance(ref, str) else ref.version

        if name not in registry:
            available = sorted(registry.keys())
            hint = str(available) if available else "(empty registry; add skills/<name>/skill.yaml)"
            raise SkillLoadError(
                f"agent references skill {name!r} but no such skill is "
                f"registered. Available: {hint}"
            )

        bundle = registry[name]
        installed_version = bundle.spec.version if bundle.spec.version else "0.0.0"
        _check_version_constraint(name, installed_version, constraint, agent_name)

        resolved.append(bundle)
    return resolved
