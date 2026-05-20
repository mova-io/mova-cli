"""Tests for the ``ORPHAN_RETRIEVAL_CONFIG`` lint rule (PR-J).

A retrieval: block on agent.yaml drives the kb-vector-lookup skill.
Without that skill declared, the config has nothing to operate on
at run time → silently ignored, which is a confusing failure mode
for the operator who tuned it. This rule flips the silent failure
into a visible warning at ``mdk validate`` time.

Covers:
* No retrieval: block → no warning (the common case)
* Default-all-off retrieval: block → no warning (operator scaffolded
  but didn't opt in)
* Non-default retrieval: block + kb-vector-lookup skill declared
  → no warning (the happy path PR-I intended)
* Non-default retrieval: block + NO matching skill → warning fires
  with a useful per-flag summary in the message
* Skill name with the canonical prefix (e.g. ``kb-vector-lookup-prod``
  for renamed copies) still suppresses the warning
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.loader import load_agent
from movate.core.prompt_linter import lint_prompt
from movate.testing import scaffold_agent


def _set_retrieval_block(agent_dir: Path, block_yaml: str) -> None:
    """Append a ``retrieval:`` block to the scaffolded agent.yaml.

    ``block_yaml`` is the literal YAML text including the
    ``retrieval:`` header. Pass an empty string to skip.
    """
    yaml_path = agent_dir / "agent.yaml"
    raw = yaml_path.read_text()
    yaml_path.write_text(raw.rstrip() + "\n" + block_yaml + "\n")


def _set_skills(agent_dir: Path, skills: list[str]) -> None:
    """Append a ``skills:`` block to agent.yaml. (The scaffolded
    template doesn't include one, so a plain append is fine.)"""
    yaml_path = agent_dir / "agent.yaml"
    raw = yaml_path.read_text()
    block = "skills:\n"
    for s in skills:
        block += f"  - {s}\n"
    yaml_path.write_text(raw.rstrip() + "\n" + block)


def _codes(issues: list) -> set[str]:
    return {i.code for i in issues}


# ---------------------------------------------------------------------------
# Negative cases — rule does NOT fire
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_retrieval_block_no_warning(tmp_path: Path) -> None:
    """Most agents have no retrieval: block. Rule stays silent."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "ORPHAN_RETRIEVAL_CONFIG" not in _codes(issues)


@pytest.mark.unit
def test_default_retrieval_block_no_warning(tmp_path: Path) -> None:
    """An operator who scaffolded a retrieval: block but kept all
    flags at their defaults (effectively pre-PR-I behavior) sees no
    warning — they haven't opted into anything to orphan."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_retrieval_block(
        agent_dir,
        "retrieval:\n  hybrid: false\n  rewrite: 0\n  rerank: false\n  multi_hop: 0\n",
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "ORPHAN_RETRIEVAL_CONFIG" not in _codes(issues)


@pytest.mark.unit
def test_retrieval_with_canonical_skill_no_warning(tmp_path: Path) -> None:
    """Happy path — retrieval: block tuned AND kb-vector-lookup
    declared on the skills list. Rule stays silent."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_retrieval_block(
        agent_dir,
        "retrieval:\n  hybrid: true\n  rewrite: 3\n",
    )
    _set_skills(agent_dir, ["kb-vector-lookup"])
    # We don't need the skill to actually exist on disk for the
    # linter — but the loader will reject if skills/<name>/skill.yaml
    # is missing. Bypass by removing the skills declaration we just
    # added then re-asserting via spec directly... easier path: mock
    # the bundle. For an integration-style test, we want load_agent
    # to succeed, so create a stub skill file.
    skills_dir = agent_dir.parent / "skills" / "kb-vector-lookup"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: kb-vector-lookup\n"
        "version: 0.1.0\n"
        "description: KB lookup skill\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: kb_vector_lookup.impl:run\n"
        "schema:\n"
        "  input:\n"
        "    question: string\n"
        "  output:\n"
        "    text: string\n"
    )
    (skills_dir / "impl.py").write_text(
        "async def run(inputs, ctx=None): return {'chunks': []}\n"
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "ORPHAN_RETRIEVAL_CONFIG" not in _codes(issues)


@pytest.mark.unit
def test_retrieval_with_renamed_skill_no_warning(tmp_path: Path) -> None:
    """A skill whose name STARTS with kb-vector-lookup (e.g. an
    operator who renamed their copy of the template to
    ``kb-vector-lookup-prod``) still suppresses the warning."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_retrieval_block(agent_dir, "retrieval:\n  hybrid: true\n")
    _set_skills(agent_dir, ["kb-vector-lookup-prod"])
    skills_dir = agent_dir.parent / "skills" / "kb-vector-lookup-prod"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: kb-vector-lookup-prod\n"
        "version: 0.1.0\n"
        "description: Renamed KB lookup\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: kb_vector_lookup_prod.impl:run\n"
        "schema:\n"
        "  input:\n    question: string\n"
        "  output:\n    text: string\n"
    )
    (skills_dir / "impl.py").write_text(
        "async def run(inputs, ctx=None): return {'chunks': []}\n"
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "ORPHAN_RETRIEVAL_CONFIG" not in _codes(issues)


# ---------------------------------------------------------------------------
# Positive case — rule fires
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_orphan_retrieval_config_fires(tmp_path: Path) -> None:
    """The operator tuned retrieval but forgot to declare the skill.
    Warning fires with a per-flag summary in the message."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_retrieval_block(
        agent_dir,
        "retrieval:\n  hybrid: true\n  rewrite: 3\n  rerank: true\n  multi_hop: 2\n",
    )
    # No skills: block at all. The retrieval config has nothing to
    # drive at run time.
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "ORPHAN_RETRIEVAL_CONFIG" in _codes(issues)
    issue = next(i for i in issues if i.code == "ORPHAN_RETRIEVAL_CONFIG")
    assert issue.severity == "warning"
    # The per-flag summary appears in the message so the operator
    # sees WHAT they configured, not just "you have a block".
    assert "hybrid=true" in issue.message
    assert "rewrite=3" in issue.message
    assert "rerank=true" in issue.message
    assert "multi_hop=2" in issue.message
    # Hint suggests both remediation paths.
    assert "kb-vector-lookup" in issue.hint
    assert "remove the retrieval" in issue.hint.lower()


@pytest.mark.unit
def test_orphan_retrieval_with_unrelated_skill_still_fires(
    tmp_path: Path,
) -> None:
    """Declaring some OTHER skill (e.g. http) doesn't satisfy the
    rule — the retrieval block is still orphaned w.r.t. KB lookup."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_retrieval_block(agent_dir, "retrieval:\n  hybrid: true\n")
    _set_skills(agent_dir, ["calculator"])
    # Stub the calculator skill so loader is happy.
    skills_dir = agent_dir.parent / "skills" / "calculator"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: calculator\n"
        "version: 0.1.0\n"
        "description: Calc\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: calculator.impl:run\n"
        "schema:\n"
        "  input:\n    expr: string\n"
        "  output:\n    result: number\n"
    )
    (skills_dir / "impl.py").write_text(
        "async def run(inputs, ctx=None): return {'result': 0}\n"
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "ORPHAN_RETRIEVAL_CONFIG" in _codes(issues)
