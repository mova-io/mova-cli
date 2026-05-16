"""Skill bundle persistence for ``POST /api/v1/skills``.

Sibling to :mod:`movate.runtime.agent_creation`. Accepts a multipart
form carrying:

* ``skill_yaml`` (required) — the spec.
* ``impl`` (optional) — Python implementation file.
* ``corpus`` (optional) — JSON corpus shipped alongside the skill.

Persists to ``<skills_path>/<name>/`` so the agent loader's
``load_skill_registry(<project_root>)`` call picks it up the next time
an agent declares ``skills: [<name>]``. ``skills_path`` is whatever
the runtime was built with — by convention ``<agents_path>/skills/``
so the loader's project-root fallback (``agent_dir.parent``) resolves
to the same dir.

The endpoint deliberately rejects nothing larger than the canonical
file set; multi-file impls and richer corpus layouts land in a
follow-up if a customer asks. Keeping the surface tight here keeps
the bug-fix scope of "let agents with skills: deploy" honest.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from movate.core.skill_loader import SkillBundle, SkillLoadError, load_skill


class SkillCreationError(Exception):
    """Raised on any failure that should surface as a non-2xx HTTP
    response. ``status_code`` maps to the HTTP code; same convention
    as :class:`movate.runtime.agent_creation.AgentCreationError` so the
    app's existing exception handler can be reused.
    """

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


# Canonical files a skill bundle may carry. ``skill.yaml`` is required;
# the rest are optional and only persisted when the multipart form sends
# them. Anything outside this set is rejected at the upload boundary —
# mirrors the agent endpoint's "strict canonical layout" stance.
_REQUIRED_FILES: frozenset[str] = frozenset({"skill.yaml"})
_OPTIONAL_FILES: frozenset[str] = frozenset(
    {
        "impl.py",
        "corpus.json",
        "README.md",
    }
)
_ALLOWED_FILES: frozenset[str] = _REQUIRED_FILES | _OPTIONAL_FILES


@dataclass(frozen=True)
class SkillPersistResult:
    """What :func:`persist_skill_bundle` returns on success."""

    bundle: SkillBundle
    skill_dir: Path
    files_persisted: list[str]


def persist_skill_bundle(
    files: dict[str, bytes],
    *,
    skills_path: Path,
    on_conflict: str = "replace",
) -> SkillPersistResult:
    """Validate + persist a skill bundle to ``<skills_path>/<name>/``.

    ``files`` is a mapping of canonical path → bytes. Keys MUST be in
    :data:`_ALLOWED_FILES`; the route handler extracts individual
    multipart fields into this shape.

    Default ``on_conflict="replace"`` is intentional — skills are
    referenced by name from agent bundles, so a deploy that re-uploads
    the same skill must overwrite cleanly without forcing the operator
    to DELETE first. (The agent endpoint defaults to ``reject`` because
    customers care about agent-identity uniqueness — different model
    for different resources.)

    Raises :class:`SkillCreationError` on any failure; never leaves
    partial state on disk.
    """
    _validate_layout(files)
    name = _extract_skill_name(files["skill.yaml"])
    target_dir = skills_path / name

    if target_dir.exists() and on_conflict == "reject":
        raise SkillCreationError(
            f"skill {name!r} already exists at {target_dir}; "
            f"PUT-style replace is the default — set on_conflict='replace'",
            status_code=409,
        )

    skills_path.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-skill-{name}-", dir=skills_path))
    try:
        _write_files(staging, files)
        # Validate via the same loader the agent loader will use at
        # registry-load time. Catching SkillLoadError here surfaces a
        # 422 instead of waiting for the NEXT agent upload to trip it.
        try:
            load_skill(staging)
        except SkillLoadError as exc:
            raise SkillCreationError(
                f"skill bundle failed validation: {exc}",
                status_code=422,
            ) from exc

        files_persisted = sorted(files.keys())

        if target_dir.exists():
            stale = target_dir.with_name(f".stale-skill-{name}-{staging.name[-8:]}")
            target_dir.rename(stale)
            try:
                staging.rename(target_dir)
            except Exception:
                stale.rename(target_dir)
                raise
            shutil.rmtree(stale, ignore_errors=True)
        else:
            staging.rename(target_dir)

        final_bundle = load_skill(target_dir)
        return SkillPersistResult(
            bundle=final_bundle,
            skill_dir=target_dir,
            files_persisted=files_persisted,
        )
    except SkillCreationError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise SkillCreationError(
            f"persist failed: {exc}",
            status_code=500,
        ) from exc


def _validate_layout(files: dict[str, bytes]) -> None:
    keys = set(files.keys())
    missing = _REQUIRED_FILES - keys
    if missing:
        raise SkillCreationError(
            f"skill bundle is missing required files: {sorted(missing)}. "
            f"Required: {sorted(_REQUIRED_FILES)}",
            status_code=422,
        )
    extras = keys - _ALLOWED_FILES
    if extras:
        raise SkillCreationError(
            f"skill bundle contains files outside the canonical layout: "
            f"{sorted(extras)}. Allowed: {sorted(_ALLOWED_FILES)}",
            status_code=422,
        )


def _extract_skill_name(skill_yaml_bytes: bytes) -> str:
    import yaml  # noqa: PLC0415

    try:
        spec = yaml.safe_load(skill_yaml_bytes.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SkillCreationError(
            f"skill.yaml is not valid YAML: {exc}",
            status_code=422,
        ) from exc
    name = spec.get("name") if isinstance(spec, dict) else None
    if not isinstance(name, str) or not name:
        raise SkillCreationError(
            "skill.yaml is missing the required 'name' field",
            status_code=422,
        )
    return name


def _write_files(staging: Path, files: dict[str, bytes]) -> None:
    for canonical_path, content in files.items():
        dest = staging / canonical_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


__all__ = [
    "SkillCreationError",
    "SkillPersistResult",
    "persist_skill_bundle",
]
