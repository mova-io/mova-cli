"""Scaffolding helpers for agent tests.

* :func:`scaffold_agent` — clone the packaged ``agent_init`` template into a
  directory and substitute the agent name.
* :func:`build_test_executor` — wire :class:`InMemoryStorage` +
  :class:`NullTracer` + a provider into a ready-to-use :class:`Executor`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from movate.core.executor import Executor
from movate.providers.base import BaseLLMProvider
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.templates import get_template_path
from movate.testing.doubles import InMemoryStorage, NullTracer


def scaffold_agent(dst: Path, *, name: str = "demo", template: str = "default") -> Path:
    """Clone a packaged agent template into ``dst`` and stamp ``name``.

    ``template`` is one of the names in :mod:`movate.templates.TEMPLATES`.
    Returns ``dst`` for chaining. The destination must not already exist.

    For templates that declare ``skills:`` or ``contexts:``, this helper
    also synthesizes the minimum project context needed for
    :func:`load_agent` to succeed:

    * Marks ``dst.parent`` as a project root by writing a tiny
      ``movate.yaml`` (the loader's project-root walk-up looks for
      this file).
    * Copies any ``contexts/*.md`` shipped inside the template's
      ``contexts/`` subdir to ``dst.parent / contexts/``.
    * Auto-scaffolds each declared skill at
      ``dst.parent / skills/<name>/`` using the canonical skill
      template registry (mirrors what ``mdk add`` does at runtime).

    Pre-bundle this helper did a flat copy and assumed all templates
    were schema-only. Role templates that declare skills/contexts
    (rag-qa, ticket-triager, code-reviewer as of May 2026) need the
    project context or ``load_agent`` errors on missing skill refs.
    """
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    src = get_template_path(template)
    shutil.copytree(src, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))

    # Synthesize the minimum project context needed for the loader's
    # project-root walk-up to find skills/ + contexts/. dst.parent
    # plays the role of the project root.
    project_root = dst.parent
    movate_yaml = project_root / "movate.yaml"
    if not movate_yaml.is_file():
        # Minimal valid ProjectConfig — `agents_dir: ./agents` is the
        # one field tests need. Adding more would risk drift with
        # `_PROJECT_MOVATE_YAML`.
        movate_yaml.write_text("agents_dir: ./agents\n")

    # Copy template-shipped contexts (rubrics, style guides).
    src_contexts = src / "contexts"
    if src_contexts.is_dir():
        dest_contexts = project_root / "contexts"
        dest_contexts.mkdir(exist_ok=True)
        for src_ctx in sorted(src_contexts.glob("*.md")):
            dest_ctx = dest_contexts / src_ctx.name
            if not dest_ctx.exists():
                dest_ctx.write_text(src_ctx.read_text())

    # Auto-scaffold declared skills. Mirrors `mdk add`'s behavior.
    import yaml as _yaml  # noqa: PLC0415

    try:
        data = _yaml.safe_load(yaml_path.read_text()) or {}
    except _yaml.YAMLError:
        data = {}
    declared_skills = data.get("skills") or []
    if declared_skills:
        from movate.cli.add_cmd import _scaffold_one_skill  # noqa: PLC0415

        for skill_name in declared_skills:
            skill_path = project_root / "skills" / skill_name
            if not skill_path.exists():
                _scaffold_one_skill(name=skill_name, project_root=project_root)

    return dst


def build_test_executor(
    *,
    provider: BaseLLMProvider | None = None,
    response: str | None = None,
    pricing: PricingTable | None = None,
    tenant_id: str = "test",
) -> tuple[Executor, BaseLLMProvider, InMemoryStorage, NullTracer]:
    """Construct (executor, provider, storage, tracer) for an agent test.

    * If ``provider`` is given, it's used as-is. Otherwise a ``MockProvider``
      is constructed; ``response`` (when given) configures the agent reply.
    * Storage and tracer are always test doubles.
    * Pricing defaults to the packaged production table so cost calculations
      match real runs.
    """
    chosen: BaseLLMProvider = provider or MockProvider(response=response)
    storage = InMemoryStorage()
    tracer = NullTracer()
    executor = Executor(
        provider=chosen,
        pricing=pricing or load_pricing(),
        storage=storage,
        tracer=tracer,
        tenant_id=tenant_id,
    )
    return executor, chosen, storage, tracer
