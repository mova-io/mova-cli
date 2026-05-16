"""Agent loader: parse an agent directory into a validated AgentBundle.

Resolves relative paths, validates JSON schemas, and computes a stable hash
of the prompt template body for run-record traceability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, select_autoescape
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from movate.core.canonical_schema import (
    CanonicalSchemaError,
    compile_canonical,
    is_canonical_format,
)
from movate.core.config import (
    PROJECT_MARKER_FILES,
    AgentDefaults,
    load_project_config,
)
from movate.core.layered_defaults import apply_defaults_to_raw
from movate.core.models import AgentSpec
from movate.core.schema_shorthand import SchemaShorthandError, compile_shorthand


class AgentLoadError(Exception):
    """Raised when an agent directory fails to load or validate."""


# Markers used by `_resolve_project_root` to identify a project root.
# Imported from :data:`movate.core.config.PROJECT_MARKER_FILES` so the
# loader stays in sync with `mdk add` / `mdk validate` / `mdk snapshot`
# walk-up conventions — adding a new project marker filename is one
# edit, not seven. Today's set:
#
# * `project.yaml` — canonical (post-MVP rename, May 2026)
# * `policy.yaml`  — legacy v1.x canonical
# * `movate.yaml`  — original v0.x name
#
# All three resolve equally for ROOT detection; deprecation warnings on
# the legacy names fire from `load_project_config` when they're read.
_PROJECT_MARKERS: tuple[str, ...] = PROJECT_MARKER_FILES


def _resolve_project_root(agent_dir: Path) -> Path:
    """Walk up from ``agent_dir`` looking for the project root marker.

    Returns the first parent dir containing ``movate.yaml`` or
    ``policy.yaml``. Falls back to ``agent_dir.parent`` if no marker
    is found — the legacy "agent dropped flat alongside skills/"
    layout keeps working (used by the executor's tool-use tests).

    Why this matters: the canonical project layout is
    ``<project>/agents/<name>/``, so ``agent_dir.parent`` =
    ``<project>/agents/``. The skills/ + contexts/ folders live at
    ``<project>/skills/`` + ``<project>/contexts/`` — one level
    UP from ``agent_dir.parent``. Without this walk, skill /
    context resolution silently picks the wrong directory.
    """
    current = agent_dir.resolve()
    for parent in current.parents:
        if any((parent / marker).is_file() for marker in _PROJECT_MARKERS):
            return parent
    # No marker found anywhere up the tree → assume the agent lives
    # at the project root level (legacy layout). Falls back to the
    # agent's immediate parent.
    return agent_dir.parent


@dataclass
class AgentBundle:
    """Fully-resolved agent: spec, prompt template, validated schemas, hash.

    ``skills`` holds the resolved :class:`SkillBundle` list — one entry
    per name in ``spec.skills``. Empty list means single-shot mode (the
    executor skips the tool-use loop entirely). Field is declared with
    ``Any`` because :class:`SkillBundle` is in a sibling module to
    avoid a circular import at module-load time; the loader populates
    it via :func:`resolve_agent_skills` (also a lazy import).

    ``contexts`` holds ``(name, body)`` pairs in declaration order from
    ``spec.contexts``. The bodies are prepended to the rendered prompt
    at execution time, joined with a standard markdown separator. Empty
    list = the rendered prompt is exactly the agent's own ``prompt.md``
    (v0.5 behavior, bit-for-bit). See ADR 002.
    """

    spec: AgentSpec
    agent_dir: Path
    prompt_template: str
    prompt_hash: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator
    skills: list[Any] = field(default_factory=list)
    contexts: list[tuple[str, str]] = field(default_factory=list)

    def render_prompt(self, input_data: dict[str, Any]) -> str:
        """Render the prompt template with the ``input.*`` namespace,
        prepending shared contexts.

        Contexts are pure markdown — no Jinja, no Python — so they're
        concatenated with the standard ``\\n\\n---\\n\\n`` separator
        before the prompt template renders. The template itself can
        still use Jinja against ``input.*``; contexts are static
        prose that lives "above" the templated body.

        No filesystem, network, or other globals are exposed to templates.
        """
        # Local import to avoid module-load coupling; context_loader is
        # otherwise a leaf module.
        from movate.core.context_loader import build_context_prefix  # noqa: PLC0415

        env = Environment(
            autoescape=select_autoescape(disabled_extensions=("md",)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        template = env.from_string(self.prompt_template)
        rendered = template.render(input=input_data)
        prefix = build_context_prefix(self.contexts)
        return prefix + rendered


def load_agent(  # noqa: PLR0912 — orchestrator; branch count is inherent
    path: str | Path,
    *,
    defaults: AgentDefaults | None = None,
) -> AgentBundle:
    """Load an agent directory. Raises AgentLoadError on any validation failure.

    ``defaults`` is the project-wide layered-defaults block (from
    ``policy.yaml: defaults:``). When omitted, the loader reads it
    via :func:`load_project_config` so most CLI callers get the
    expected merge behavior for free. Pass an explicit
    ``AgentDefaults()`` (empty) to bypass the project config — tests
    and library callers that want a pristine agent.yaml use that
    escape hatch. See :mod:`movate.core.layered_defaults` for the
    merge rules.
    """
    agent_dir = Path(path).resolve()
    if not agent_dir.is_dir():
        raise AgentLoadError(f"agent path is not a directory: {agent_dir}")

    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.exists():
        raise AgentLoadError(f"agent.yaml not found in {agent_dir}")

    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError as exc:
        # PyYAML's MarkedYAMLError carries a `problem_mark` with line +
        # column for the offending byte. Surface it as `path:line:col`
        # so editors (VS Code, vim quickfix) jump straight to the right
        # spot. Falls back gracefully for unmarked errors (rare).
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            location = f"{yaml_path}:{mark.line + 1}:{mark.column + 1}"
        else:
            location = str(yaml_path)
        raise AgentLoadError(f"invalid YAML in {location}: {exc}") from exc

    # Apply project defaults at the raw-dict level — before Pydantic
    # validation — so we can distinguish "operator wrote this value"
    # from "Pydantic filled in its default". See layered_defaults.py.
    if defaults is None:
        defaults = load_project_config().defaults
    if isinstance(raw, dict):
        raw = apply_defaults_to_raw(raw, defaults)

    try:
        spec = AgentSpec.model_validate(raw)
    except ValidationError as exc:
        # Include the file path so the error reads like a compiler
        # diagnostic ("file: reason") rather than a bare stack-style
        # message. Pydantic's exc carries field-level loc info; we
        # prefix it with the file so editors can at least open the
        # right document.
        raise AgentLoadError(f"agent.yaml validation failed in {yaml_path}:\n{exc}") from exc

    prompt_path = (agent_dir / spec.prompt).resolve()
    if not prompt_path.exists():
        raise AgentLoadError(f"prompt file not found: {prompt_path}")
    prompt_text = prompt_path.read_text()
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

    input_schema = _resolve_schema(spec.schemas.input, agent_dir=agent_dir, label="input")
    output_schema = _resolve_schema(spec.schemas.output, agent_dir=agent_dir, label="output")

    try:
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
    except Exception as exc:
        raise AgentLoadError(f"invalid JSON schema: {exc}") from exc

    # Resolve declared skills against the project's skills/ registry.
    # Lazy import keeps this loader free of a circular dep with
    # skill_loader (which imports _resolve_schema from here).
    #
    # Project-root resolution: walk up from the agent directory
    # looking for ``movate.yaml`` / ``policy.yaml``. That's the
    # canonical project root marker (set by ``mdk init --project``),
    # and ``<project>/skills/`` + ``<project>/contexts/`` live as its
    # siblings.
    #
    # Fallback: if no marker is found (agent loaded outside a project,
    # e.g. test fixtures or single-agent ``mdk init <name>`` scaffolds
    # without project mode), use ``agent_dir.parent``. That keeps the
    # legacy "agent dropped into a flat dir alongside skills/" layout
    # working — the executor tool-use tests rely on this fallback.
    project_root = _resolve_project_root(agent_dir)
    skills_resolved: list[Any] = []
    if spec.skills:
        # Local import to avoid module-load-time cycle.
        from movate.core.skill_loader import (  # noqa: PLC0415
            SkillLoadError,
            load_skill_registry,
            resolve_agent_skills,
        )

        try:
            registry = load_skill_registry(project_root)
            skills_resolved = list(resolve_agent_skills(spec.skills, registry))
        except SkillLoadError as exc:
            raise AgentLoadError(f"skills resolution failed: {exc}") from exc

    # Resolve declared contexts. Two-tier registry: project-level
    # (`<project_root>/contexts/<name>.md`) is the shared base; agent-
    # local (`<agent_dir>/contexts/<name>.md`) overrides on name
    # collision. Same permissive-empty-registry default.
    contexts_resolved: list[tuple[str, str]] = []
    if spec.contexts:
        from movate.core.context_loader import (  # noqa: PLC0415
            ContextLoadError,
            load_context_registry,
            resolve_agent_contexts,
        )

        try:
            ctx_registry = load_context_registry(project_root, agent_dir=agent_dir)
            contexts_resolved = resolve_agent_contexts(spec.contexts, ctx_registry)
        except ContextLoadError as exc:
            raise AgentLoadError(f"contexts resolution failed: {exc}") from exc

    return AgentBundle(
        spec=spec,
        agent_dir=agent_dir,
        prompt_template=prompt_text,
        prompt_hash=prompt_hash,
        input_schema=input_schema,
        output_schema=output_schema,
        input_validator=Draft202012Validator(input_schema),
        output_validator=Draft202012Validator(output_schema),
        skills=skills_resolved,
        contexts=contexts_resolved,
    )


def _resolve_schema(
    raw: str | dict[str, Any],
    *,
    agent_dir: Path,
    label: str,
) -> dict[str, Any]:
    """Resolve one of the two ``schema:`` forms into a JSON Schema dict.

    * **path string** → read the file from disk. Three file types:
      ``.json`` is parsed as JSON Schema (canonical, unchanged from
      v0.x). ``.yaml`` / ``.yml`` are parsed as YAML — if the YAML
      looks like a JSON Schema (top-level ``type``/``properties`` or
      ``$schema``) it's used verbatim; otherwise it's treated as
      :func:`compile_shorthand` shorthand and compiled to JSON Schema.
    * **inline shorthand dict** → compile via :func:`compile_shorthand`.
      Strict-by-default object schema, same downstream API.

    Validation errors from any path are normalized to
    :class:`AgentLoadError` so the CLI surfaces one consistent
    error surface to operators.
    """
    if isinstance(raw, dict):
        try:
            return compile_shorthand(raw, root_label=label)
        except SchemaShorthandError as exc:
            raise AgentLoadError(f"inline schema shorthand error: {exc}") from exc
    # Path string — resolve relative to the agent dir + parse by extension.
    return _load_schema_doc(agent_dir / raw, label=label)


def _load_schema_doc(path: Path, *, label: str) -> dict[str, Any]:
    """Load a schema file from disk; dispatch on extension + shape.

    Supports:

    * ``.json`` — parsed as JSON; assumed to be a full JSON Schema
      (the v0.x canonical path; unchanged for backwards-compat).
    * ``.yaml`` / ``.yml`` — parsed as YAML, then shape-sniffed:
      a top-level ``$schema`` field or a ``type=object`` paired with
      ``properties`` marks it as a hand-written JSON Schema (used
      verbatim); anything else is treated as the readable shorthand
      and run through :func:`compile_shorthand`.
    """
    if not path.exists():
        raise AgentLoadError(f"schema file not found: {path}")
    text = path.read_text()
    suffix = path.suffix.lower()
    # `.json` is unambiguous: always a hand-written JSON Schema (the
    # v0.x canonical path). Operators don't write shorthand in JSON
    # — they write it inline in agent.yaml, or in a .yaml file. Routing
    # .json through the shorthand compiler would mis-classify legitimate
    # JSON Schemas with non-`object` roots or `additionalProperties: true`.
    if suffix == ".json":
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentLoadError(f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AgentLoadError(
                f"schema {path} must be a top-level object, got {type(data).__name__}"
            )
        return data
    if suffix not in (".yaml", ".yml"):
        raise AgentLoadError(
            f"schema file extension {suffix!r} not supported (use .json, .yaml, .yml): {path}"
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AgentLoadError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentLoadError(f"schema {path} must be a top-level object, got {type(data).__name__}")
    # Three-way shape-sniff to pick the right compiler:
    #
    # 1. Canonical format (PR #103, May 2026 — the business-readable
    #    DSL). Unambiguous marker: top-level `version: 1`.
    # 2. Hand-written JSON Schema. Marker: top-level `$schema` URL
    #    OR `type: object` + `properties` (matches what the loader
    #    itself produces, so this captures hand-compiled exports).
    # 3. Shorthand (the engineer's terse form). Default fall-through
    #    for everything else — `compile_shorthand` raises with a
    #    clear field-path error if the shape doesn't parse.
    if is_canonical_format(data):
        try:
            return compile_canonical(data)
        except CanonicalSchemaError as exc:
            raise AgentLoadError(f"canonical schema error in {path}: {exc}") from exc
    is_json_schema = "$schema" in data or (data.get("type") == "object" and "properties" in data)
    if is_json_schema:
        return data
    # Shorthand path — compile to JSON Schema. Use the file path as the
    # error-message label so operators see the exact file, not just
    # "input"/"output".
    try:
        return compile_shorthand(data, root_label=f"{label} ({path.name})")
    except SchemaShorthandError as exc:
        raise AgentLoadError(f"schema shorthand error in {path}: {exc}") from exc
