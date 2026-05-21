"""Static checks on an :class:`AgentBundle` prompt + schemas.

Each rule is a pure function that takes a bundle and returns a list
of :class:`LintIssue` (zero or many). The orchestrator
:func:`lint_prompt` runs every rule and concatenates the results;
``movate validate`` consumes this and prints / exits accordingly.

Severity model
--------------

* ``error`` — definitely breaks the agent at runtime. ``movate
  validate`` exits 2.
* ``warning`` — heuristic that *usually* indicates a bug. Printed
  always; only fails the build under ``movate validate --strict``.

Rules
-----

* ``UNDECLARED_INPUT_REF`` (error) — the template references
  ``{{ input.X }}`` but ``X`` is not in the input schema's
  ``properties``. Renders to ``StrictUndefined`` at runtime, which
  raises in ``render_prompt``. Catch it before deploy.
* ``MISSING_JSON_INSTRUCTION`` (warning) — the output schema is a
  JSON object but the prompt doesn't mention "json" anywhere. Models
  often emit prose around the JSON; ``_parse_json_output`` is
  markdown-fence-tolerant but not prose-tolerant.
* ``NO_OUTPUT_SCHEMA_REFERENCE`` (warning) — the prompt doesn't
  mention any of the output schema's field names. Models tend to
  hallucinate field names when the prompt doesn't surface the
  expected shape.
* ``EMPTY_PROMPT`` (error) — prompt is empty or just whitespace.
  Almost certainly a scaffolding leftover.
* ``TINY_PROMPT`` (warning) — prompt is under 40 chars of
  non-whitespace content. Usually a scaffolding leftover that
  somehow shipped.

* ``SKILL_OUTPUT_REF_MISMATCH`` (warning) — the prompt references
  ``{{ <skill_name>_output.X }}`` but field ``X`` is not in the
  skill's declared output schema. Operators who inject skill output
  via the ``<name>_output`` naming convention get early feedback
  instead of a silent ``undefined`` at render time.
* ``ORPHAN_RETRIEVAL_CONFIG`` (warning) — the agent declares a
  non-default ``retrieval:`` block but doesn't list the
  ``kb-vector-lookup`` skill, so the config has nothing to drive.
  Either the skill name is wrong / missing, or the operator forgot
  to remove leftover retrieval tuning from a template.

Future rules (not in this pass — add when they catch a real bug):

* Floating temperature on a deterministic-eval agent
* Prompt contains hard-coded API keys (regex check)
* Output schema references nullable fields the prompt doesn't
  acknowledge as optional

The rules deliberately bias toward false-negative rather than
false-positive — we'd rather miss a real bug than spam warnings
that operators learn to ignore.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from jinja2 import Environment

from movate.core.loader import AgentBundle

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class LintIssue:
    """One linter finding. The ``code`` is a stable enum so CI annotations
    can filter / suppress specific rules; the ``message`` is
    human-readable and may change between versions."""

    code: str
    severity: Severity
    message: str
    hint: str = ""
    """Optional pointer to the fix — printed as a dim line under the
    main message. Keep short."""


def lint_prompt(bundle: AgentBundle) -> list[LintIssue]:
    """Run every lint rule against ``bundle``. Returns all issues in
    a stable order (rule-name first, then occurrence order within a
    rule). Empty list = clean bill of health.

    Never raises — a lint rule failure is itself a bug and would be
    silently dropped here; the CI gate would still pass. (We're not
    in the business of breaking validate on a linter bug.)
    """
    issues: list[LintIssue] = []
    issues.extend(_check_empty_prompt(bundle))
    issues.extend(_check_undeclared_input_refs(bundle))
    issues.extend(_check_skill_output_refs(bundle))
    issues.extend(_check_json_instruction(bundle))
    issues.extend(_check_output_schema_reference(bundle))
    issues.extend(_check_orphan_retrieval_config(bundle))
    return issues


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


_TINY_PROMPT_THRESHOLD = 40


def _check_empty_prompt(bundle: AgentBundle) -> list[LintIssue]:
    """``EMPTY_PROMPT`` (error) — no actual content.
    ``TINY_PROMPT`` (warning) — almost no content."""
    stripped = bundle.prompt_template.strip()
    if not stripped:
        return [
            LintIssue(
                code="EMPTY_PROMPT",
                severity="error",
                message="prompt is empty or whitespace-only",
                hint=(
                    "write the system instruction in the file referenced by `prompt:` in agent.yaml"
                ),
            )
        ]
    if len(stripped) < _TINY_PROMPT_THRESHOLD:
        return [
            LintIssue(
                code="TINY_PROMPT",
                severity="warning",
                message=(
                    f"prompt is {len(stripped)} chars; likely a scaffolding stub. "
                    "Real agent prompts are usually >100 chars."
                ),
                hint="replace the scaffolded prompt with the real system instruction",
            )
        ]
    return []


def _check_undeclared_input_refs(bundle: AgentBundle) -> list[LintIssue]:
    """``UNDECLARED_INPUT_REF`` (error) — template references
    ``{{ input.X }}`` but ``X`` is not in input_schema.properties.

    Uses Jinja2's AST analyzer (``meta.find_undeclared_variables``
    + attribute extraction) so we don't false-positive on
    string-literal occurrences of ``input.foo`` in plain text.
    """
    env = Environment()
    try:
        ast = env.parse(bundle.prompt_template)
    except Exception:
        # If the template doesn't even parse, the schema-validation
        # step in load_agent has already failed; nothing useful
        # for us to report here. The render_prompt call at
        # execute() time will surface the real error.
        return []

    # We want every ``input.X`` reference, where ``input`` is the
    # only var bundle.render_prompt injects. Walk the AST: any
    # ``Getattr`` node whose target is the ``Name("input")`` carries
    # the attribute name we care about.
    referenced: set[str] = set()
    # ``find_all`` returns generic ``jinja2.nodes.Node`` objects;
    # ``getattr`` lookups below tolerate the missing attrs on
    # non-Getattr instances (the cls filter limits to Getattr, but
    # the type-checker doesn't know that).
    getattr_cls = _jinja_getattr_class()
    _getattr_iter: Iterator[Any] = ast.find_all((getattr_cls,))
    for node in _getattr_iter:
        target = getattr(node, "node", None)
        attr = getattr(node, "attr", None)
        if (
            target is not None
            and getattr(target, "name", None) == "input"
            and isinstance(attr, str)
        ):
            referenced.add(attr)

    declared = set(_input_property_names(bundle))
    if not declared:
        # No input schema → we don't know what's declared; skip.
        return []

    issues: list[LintIssue] = []
    for ref in sorted(referenced - declared):
        issues.append(
            LintIssue(
                code="UNDECLARED_INPUT_REF",
                severity="error",
                message=(
                    f"prompt references `{{{{ input.{ref} }}}}` but "
                    f"{ref!r} is not in the input schema's properties"
                ),
                hint=f"add {ref!r} to the input schema or rename the reference in the prompt",
            )
        )
    return issues


def _jinja_getattr_class() -> type:
    """Locate the Jinja2 ``Getattr`` AST node class.

    Lives in different modules across Jinja versions; this lookup
    keeps the linter resilient to upstream movements. Falls back to
    a sentinel that never matches so the rule degrades gracefully.
    """
    try:
        from jinja2.nodes import Getattr  # noqa: PLC0415

        return Getattr
    except ImportError:  # pragma: no cover - defensive
        # Sentinel class that matches no real AST nodes.
        return type("Unreachable", (), {})


def _input_property_names(bundle: AgentBundle) -> list[str]:
    """Top-level keys of the input schema's ``properties``. Empty list
    if the schema is shapeless (``additionalProperties`` open / no
    declared shape)."""
    props = bundle.input_schema.get("properties")
    if not isinstance(props, dict):
        return []
    return [k for k in props if isinstance(k, str)]


def _output_property_names(bundle: AgentBundle) -> list[str]:
    """Same for output schema. Used by the schema-reference check."""
    props = bundle.output_schema.get("properties")
    if not isinstance(props, dict):
        return []
    return [k for k in props if isinstance(k, str)]


def _skill_output_var_name(skill_name: str) -> str:
    """Canonical Jinja2 variable name for a skill's output.

    Convention: replace non-identifier chars (``-``) with ``_`` and
    append ``_output``. E.g. ``web-search`` → ``web_search_output``.
    """
    return re.sub(r"[^a-z0-9_]", "_", skill_name.lower()) + "_output"


def _skill_output_property_names(skill: object) -> list[str]:
    """Top-level keys of the skill's output schema ``properties``."""
    props = getattr(skill, "output_schema", {}).get("properties")
    if not isinstance(props, dict):
        return []
    return [k for k in props if isinstance(k, str)]


def _check_skill_output_refs(bundle: AgentBundle) -> list[LintIssue]:
    """``SKILL_OUTPUT_REF_MISMATCH`` (warning) — template references
    ``{{ <skill_name>_output.X }}`` but field ``X`` is absent from the
    skill's declared output schema.

    Convention: a skill named ``web-search`` exposes its result under
    ``{{ web_search_output.<field> }}`` in the Jinja2 template.
    This rule catches typos and stale references before they produce
    silent ``Undefined`` at render time.

    The rule is a no-op when:
    * the agent has no skills
    * the skill has no declared output schema properties (open schema)
    * the template doesn't reference the skill's output variable at all
    """
    if not bundle.skills:
        return []

    env = Environment()
    try:
        ast = env.parse(bundle.prompt_template)
    except Exception:
        return []

    getattr_cls = _jinja_getattr_class()

    # Build a map: var_name → (skill, declared_output_fields)
    skill_vars: dict[str, tuple[Any, set[str]]] = {}
    for skill in bundle.skills:
        var_name = _skill_output_var_name(skill.spec.name)
        props = _skill_output_property_names(skill)
        if props:  # skip skills with open / empty output schemas
            skill_vars[var_name] = (skill, set(props))

    if not skill_vars:
        return []

    # Walk AST: collect all Getattr nodes of the form `<var_name>.X`.
    issues: list[LintIssue] = []
    seen: set[tuple[str, str]] = set()
    _skill_getattr_iter: Iterator[Any] = ast.find_all((getattr_cls,))
    for node in _skill_getattr_iter:
        target = getattr(node, "node", None)
        attr = getattr(node, "attr", None)
        if target is None or not isinstance(attr, str):
            continue
        node_var: str | None = getattr(target, "name", None)
        if not isinstance(node_var, str) or node_var not in skill_vars:
            continue
        _, declared = skill_vars[node_var]
        if attr in declared:
            continue
        key = (node_var, attr)
        if key in seen:
            continue
        seen.add(key)
        # Recover the original skill name for the message.
        skill = skill_vars[node_var][0]
        skill_name = skill.spec.name
        issues.append(
            LintIssue(
                code="SKILL_OUTPUT_REF_MISMATCH",
                severity="warning",
                message=(
                    f"prompt references `{{{{ {node_var}.{attr} }}}}` "
                    f"but {attr!r} is not in skill {skill_name!r}'s output schema "
                    f"(declared: {sorted(declared)})"
                ),
                hint=(
                    f"check the output schema in skills/{skill_name}/skill.yaml "
                    f"or fix the field name in the prompt"
                ),
            )
        )
    return issues


def _check_json_instruction(bundle: AgentBundle) -> list[LintIssue]:
    """``MISSING_JSON_INSTRUCTION`` (warning) — output schema is a JSON
    object but the prompt doesn't mention "json" anywhere.

    LLMs trained on prose default to prose. Without an explicit "JSON
    only" / "respond with JSON" cue, they wrap the JSON in markdown
    headers, apologies, or commentary. ``_parse_json_output``
    handles markdown fences but not free-form prose.
    """
    if bundle.output_schema.get("type") != "object":
        return []
    # Word boundary so we don't false-positive on "JS Online" or
    # whatever. Case-insensitive because "JSON" / "Json" / "json"
    # are all equally good.
    if re.search(r"\bjson\b", bundle.prompt_template, re.IGNORECASE):
        return []
    return [
        LintIssue(
            code="MISSING_JSON_INSTRUCTION",
            severity="warning",
            message=(
                "prompt does not mention 'JSON' but the output schema is a JSON object. "
                "Models often wrap JSON in prose without an explicit instruction."
            ),
            hint=(
                "add a line like `You are a JSON-only assistant. Respond with a single "
                "JSON object that matches the output schema.`"
            ),
        )
    ]


def _check_output_schema_reference(bundle: AgentBundle) -> list[LintIssue]:
    """``NO_OUTPUT_SCHEMA_REFERENCE`` (warning) — prompt mentions
    *none* of the output schema's field names.

    Models tend to hallucinate field names when the prompt doesn't
    surface the expected shape. Catching this at validate time is
    cheaper than discovering it at eval time.
    """
    output_fields = _output_property_names(bundle)
    if not output_fields:
        return []
    prompt_lower = bundle.prompt_template.lower()
    # ``\b`` so we don't false-positive on "message" matching
    # inside "messages" (different field name). For our typical
    # single-word field names this is robust enough.
    found_any = any(re.search(rf"\b{re.escape(f.lower())}\b", prompt_lower) for f in output_fields)
    if found_any:
        return []
    return [
        LintIssue(
            code="NO_OUTPUT_SCHEMA_REFERENCE",
            severity="warning",
            message=(
                f"prompt does not mention any of the output schema's field names "
                f"({', '.join(repr(f) for f in output_fields)}). "
                "Models tend to hallucinate field names without a sample."
            ),
            hint="include a sample JSON object in the prompt showing the expected keys",
        )
    ]


# Names the skill might use to surface KB retrieval. Today only
# the canonical ``kb-vector-lookup`` skill consumes the retrieval
# config (via SkillExecutionContext.retrieval); operators sometimes
# rename their copy of the template, so we also accept anything that
# STARTS with the canonical prefix (e.g. ``kb-vector-lookup-prod``).
# A skill with a totally different name still passes the lint —
# false-negative, by design: better to miss a rename than to spam
# warnings the operator learns to ignore.
_RETRIEVAL_SKILL_PREFIX = "kb-vector-lookup"


def _check_orphan_retrieval_config(bundle: AgentBundle) -> list[LintIssue]:
    """``ORPHAN_RETRIEVAL_CONFIG`` (warning) — non-default
    ``retrieval:`` block on an agent that doesn't declare the
    ``kb-vector-lookup`` skill.

    The retrieval block (PR-I) drives the four optional stages
    (hybrid / rewrite / rerank / multi_hop) inside the
    ``kb-vector-lookup`` skill. Without that skill on the agent's
    declared skill list, the config has nothing to operate on at
    run time — silently ignored, which is a confusing failure mode
    for the operator who tuned it.

    Two common causes:

    1. Operator typo'd the skill name in ``skills:`` (or removed
       it accidentally while editing).
    2. Operator copy-pasted a ``retrieval:`` block from a template
       and forgot to also add the skill.

    Both produce identical run-time behavior (vector-only). This
    rule flips that silent failure into a visible warning.

    Skips entirely when the retrieval block is at its default
    (all-off) — no signal to warn about.
    """
    # Two-stage getattr so stub bundles (used in unit tests for OTHER
    # rules) without a real ``spec`` attribute don't crash this rule.
    # AgentBundle always has ``spec``; test SimpleNamespace fixtures
    # sometimes don't.
    spec = getattr(bundle, "spec", None)
    if spec is None:
        return []
    retrieval = getattr(spec, "retrieval", None)
    if retrieval is None:
        return []
    is_default_fn = getattr(retrieval, "is_default", None)
    if callable(is_default_fn) and is_default_fn():
        return []

    # Any declared skill whose name starts with the canonical
    # prefix counts (handles renamed copies of the template).
    declared_skills = list(getattr(spec, "skills", []) or [])
    if any(isinstance(s, str) and s.startswith(_RETRIEVAL_SKILL_PREFIX) for s in declared_skills):
        return []

    # Build a short summary of what the operator configured, so the
    # warning surfaces the SPECIFIC drift (not just "you have a block").
    parts: list[str] = []
    if getattr(retrieval, "hybrid", False):
        parts.append("hybrid=true")
    rewrite_n = int(getattr(retrieval, "rewrite", 0))
    if rewrite_n > 0:
        parts.append(f"rewrite={rewrite_n}")
    if getattr(retrieval, "rerank", False):
        parts.append("rerank=true")
    multi_hop_n = int(getattr(retrieval, "multi_hop", 0))
    if multi_hop_n > 0:
        parts.append(f"multi_hop={multi_hop_n}")
    config_summary = ", ".join(parts) or "non-default"

    return [
        LintIssue(
            code="ORPHAN_RETRIEVAL_CONFIG",
            severity="warning",
            message=(
                f"agent.yaml declares retrieval: ({config_summary}) but the "
                f"agent's skills: list has no entry starting with "
                f"{_RETRIEVAL_SKILL_PREFIX!r}. The retrieval config will be "
                "silently ignored at run time."
            ),
            hint=(
                "either add 'kb-vector-lookup' to skills: in agent.yaml, "
                "or remove the retrieval: block if you don't need KB lookup."
            ),
        )
    ]


__all__ = ["LintIssue", "lint_prompt"]
