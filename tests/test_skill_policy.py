"""Tests for the SkillPolicy gate — Skills PR 7 / ADR 002 PR 7 of N.

Three layers:

* **Model unit tests** — SkillPolicy.is_permissive / check_skill /
  check_agent_skills. No I/O, no Pydantic round-trip.
* **CLI integration** — ``mdk validate`` rejects an agent whose
  skills exceed the project's allowed_side_effects.
* **Executor integration** — Executor.execute() enforces at runtime
  (belt-and-braces, for bundles that bypass validate).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.config import SkillPolicy
from movate.core.models import (
    SkillImplementation,
    SkillImplementationKind,
    SkillSideEffects,
    SkillSpec,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers — synth a SkillBundle without going through the loader
# ---------------------------------------------------------------------------


@dataclass
class _FakeSkillBundle:
    """SkillPolicy.check_agent_skills only reads ``bundle.spec.name``
    and ``bundle.spec.side_effects``. A real SkillBundle would also
    carry validators and schemas; we only need the spec slice."""

    spec: SkillSpec


def _make_skill_spec(
    *,
    name: str = "demo",
    side_effects: SkillSideEffects = SkillSideEffects.READ_ONLY,
) -> SkillSpec:
    """Construct a minimal valid SkillSpec for policy testing."""
    return SkillSpec(
        api_version="movate/v1",
        kind="Skill",
        name=name,
        version="0.1.0",
        schema={"input": {"x": "string"}, "output": {"y": "string"}},  # type: ignore[arg-type]
        implementation=SkillImplementation(
            kind=SkillImplementationKind.PYTHON,
            entry=f"tests.test_skill_policy:_{name}_fn",
        ),
        side_effects=side_effects,
    )


def _bundle(name: str, side_effects: SkillSideEffects) -> _FakeSkillBundle:
    return _FakeSkillBundle(spec=_make_skill_spec(name=name, side_effects=side_effects))


# ---------------------------------------------------------------------------
# SkillPolicy.is_permissive / check_skill — pure-function unit tests
# ---------------------------------------------------------------------------


def test_is_permissive_default() -> None:
    """Empty SkillPolicy is permissive — no allowlist, accepts everything."""
    assert SkillPolicy().is_permissive() is True


def test_is_permissive_with_empty_list_is_false() -> None:
    """An empty allowlist ([]) is NOT permissive — it's the strictest
    config (no skills at all). Operators use this for default-deny."""
    assert SkillPolicy(allowed_side_effects=[]).is_permissive() is False


def test_check_skill_returns_none_when_permissive() -> None:
    """Permissive policy never returns a violation regardless of skill."""
    p = SkillPolicy()
    assert p.check_skill("any", SkillSideEffects.MUTATES_STATE) is None


def test_check_skill_returns_none_when_side_effects_allowed() -> None:
    p = SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY, SkillSideEffects.NETWORK])
    assert p.check_skill("x", SkillSideEffects.READ_ONLY) is None
    assert p.check_skill("y", SkillSideEffects.NETWORK) is None


def test_check_skill_returns_message_on_violation() -> None:
    p = SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY])
    msg = p.check_skill("danger", SkillSideEffects.MUTATES_STATE)
    assert msg is not None
    assert "danger" in msg
    assert "mutates-state" in msg
    assert "read-only" in msg  # the allowed list surfaces


def test_check_skill_empty_allowlist_has_distinct_message() -> None:
    """Empty allowlist gets its own error wording so operators
    understand the policy is default-deny, not just narrow."""
    p = SkillPolicy(allowed_side_effects=[])
    msg = p.check_skill("any", SkillSideEffects.READ_ONLY)
    assert msg is not None
    assert "empty allowlist" in msg


# ---------------------------------------------------------------------------
# SkillPolicy.check_agent_skills — aggregate violations
# ---------------------------------------------------------------------------


def test_check_agent_skills_permissive_returns_empty() -> None:
    p = SkillPolicy()
    bundles = [_bundle("alpha-skill", SkillSideEffects.MUTATES_STATE)]
    assert p.check_agent_skills(bundles) == []  # type: ignore[arg-type]


def test_check_agent_skills_all_compliant() -> None:
    p = SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY])
    bundles = [
        _bundle("alpha-skill", SkillSideEffects.READ_ONLY),
        _bundle("beta-skill", SkillSideEffects.READ_ONLY),
    ]
    assert p.check_agent_skills(bundles) == []  # type: ignore[arg-type]


def test_check_agent_skills_reports_every_violation() -> None:
    """One agent with multiple bad skills surfaces every offender, not
    just the first — operator fixes everything in one pass."""
    p = SkillPolicy(allowed_side_effects=[SkillSideEffects.READ_ONLY])
    bundles = [
        _bundle("safe", SkillSideEffects.READ_ONLY),
        _bundle("evil", SkillSideEffects.MUTATES_STATE),
        _bundle("noisy", SkillSideEffects.NETWORK),
    ]
    violations = p.check_agent_skills(bundles)  # type: ignore[arg-type]
    assert len(violations) == 2
    assert any("evil" in v for v in violations)
    assert any("noisy" in v for v in violations)


# ---------------------------------------------------------------------------
# CLI integration — `mdk validate` enforces the policy
# ---------------------------------------------------------------------------


def _write_agent_with_skill(
    project_root: Path,
    *,
    agent_name: str = "demo-agent",
    skill_name: str = "demo-skill",
    skill_side_effects: SkillSideEffects = SkillSideEffects.READ_ONLY,
) -> Path:
    """Drop a minimal agent + one skill referenced by it. Returns the
    agent dir."""
    skill_dir = project_root / "skills" / skill_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {skill_name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {x: string}\n"
        "  output: {y: string}\n"
        "implementation:\n"
        "  kind: python\n"
        f"  entry: {skill_name}.impl:run\n"
        f"side_effects: {skill_side_effects.value}\n"
    )

    agent_dir = project_root / agent_name
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        f"name: {agent_name}\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: {q: string}\n"
        "  output: {a: string}\n"
        "skills:\n"
        f"  - {skill_name}\n"
    )
    (agent_dir / "prompt.md").write_text("Q: {{ input.q }}")
    return agent_dir


def test_validate_passes_when_policy_permissive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No policy.yaml = permissive default. Agent with a mutates-state
    skill still validates."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent_with_skill(tmp_path, skill_side_effects=SkillSideEffects.MUTATES_STATE)
    result = runner.invoke(app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


def test_validate_passes_when_skill_in_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text("skills:\n  allowed_side_effects: [read-only]\n")
    agent_dir = _write_agent_with_skill(tmp_path, skill_side_effects=SkillSideEffects.READ_ONLY)
    result = runner.invoke(app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


def test_validate_rejects_skill_outside_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent using a mutates-state skill in a read-only-only project
    fails ``mdk validate`` with a clear policy-violation message."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text("skills:\n  allowed_side_effects: [read-only]\n")
    agent_dir = _write_agent_with_skill(
        tmp_path,
        skill_name="dangerous-skill",
        skill_side_effects=SkillSideEffects.MUTATES_STATE,
    )
    result = runner.invoke(app, ["validate", str(agent_dir)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "skill policy violation" in combined.lower()
    assert "dangerous-skill" in combined
    assert "mutates-state" in combined
    # The fix hint surfaces.
    assert "allowed_side_effects" in combined


def test_validate_default_deny_rejects_any_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allowed_side_effects: [] means no skills at all. Even a
    read-only skill fails."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text("skills:\n  allowed_side_effects: []\n")
    agent_dir = _write_agent_with_skill(tmp_path, skill_side_effects=SkillSideEffects.READ_ONLY)
    result = runner.invoke(app, ["validate", str(agent_dir)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "empty allowlist" in combined


def test_validate_agent_without_skills_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent with no skills bypasses the policy entirely — empty
    skill list = nothing to check."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text("skills:\n  allowed_side_effects: []\n")
    # Agent with skills: [] (no skills section) — write directly.
    agent_dir = tmp_path / "vanilla"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: vanilla\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: {q: string}\n"
        "  output: {a: string}\n"
    )
    (agent_dir / "prompt.md").write_text("Q: {{ input.q }}")
    result = runner.invoke(app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


# ---------------------------------------------------------------------------
# Executor integration — runtime enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_raises_on_skill_policy_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle whose skills violate the policy must fail at the top
    of Executor.execute — even when ``mdk validate`` was skipped (e.g.
    a worker loading the bundle over HTTP)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text("skills:\n  allowed_side_effects: [read-only]\n")
    # Drop a skill + agent
    skill_dir = tmp_path / "skills" / "danger"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: danger\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {x: string}\n"
        "  output: {y: string}\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: danger.impl:run\n"
        "side_effects: mutates-state\n"
    )
    agent_dir = tmp_path / "risky-agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: risky-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: {q: string}\n"
        "  output: {a: string}\n"
        "skills:\n"
        "  - danger\n"
    )
    (agent_dir / "prompt.md").write_text("Q: {{ input.q }}")

    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.core.models import RunRequest  # noqa: PLC0415

    bundle = load_agent(agent_dir)
    rt = await build_local_runtime(mock=True)
    try:
        # Executor catches MovateError subclasses and returns a
        # failure RunResponse — that's its standard pattern (so a
        # policy violation doesn't blow up the worker, it just gets
        # recorded as a failed run). Assert the failure shape.
        response = await rt.executor.execute(
            bundle, RunRequest(agent="risky-agent", input={"q": "hi"})
        )
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)
    assert response.status == "error"
    assert response.error is not None
    assert "mutates-state" in response.error.message
    assert "danger" in response.error.message


@pytest.mark.asyncio
async def test_executor_permissive_default_allows_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No policy.yaml → permissive default → mutates-state skills run
    fine. Confirms zero regression for projects without a SkillPolicy."""
    monkeypatch.chdir(tmp_path)
    # Drop a python skill + agent that uses it.
    skill_dir = tmp_path / "skills" / "mutator"
    skill_dir.mkdir(parents=True)
    (skill_dir / "__init__.py").write_text("")
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: mutator\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {x: string}\n"
        "  output: {y: string}\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: mutator.impl:run\n"
        "side_effects: mutates-state\n"
    )
    (skill_dir / "impl.py").write_text("def run(input, ctx):\n    return {'y': 'ok'}\n")
    agent_dir = tmp_path / "tolerant"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: tolerant\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: {q: string}\n"
        "  output: {a: string}\n"
        "skills:\n"
        "  - mutator\n"
    )
    (agent_dir / "prompt.md").write_text("Q: {{ input.q }}")

    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    bundle = load_agent(agent_dir)
    # Agent has skills.allowed_side_effects unset → permissive.
    rt = await build_local_runtime(mock=True)
    try:
        # Just confirm the policy check doesn't raise. We don't run
        # execute() end-to-end because that would invoke the LLM mock
        # + the skill dispatch loop — out of scope for this test.
        violations = rt.executor._skill_policy.check_agent_skills(bundle.skills)  # type: ignore[attr-defined]
        assert violations == []
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)


# ---------------------------------------------------------------------------
# Module-level fixtures for skill `entry` resolution
# ---------------------------------------------------------------------------


def _demo_fn(input: Any, ctx: Any) -> dict[str, str]:
    """Referenced by some test fixtures' entry strings — present so
    load_skill doesn't fail at import-resolution time."""
    return {"y": str(input)}


# Aliases for the entry strings in _make_skill_spec. The SkillSpec
# validator doesn't actually import the entry — it just checks the
# shape — so these aliases exist for completeness rather than
# functional necessity.
_safe_fn = _demo_fn
_evil_fn = _demo_fn
_noisy_fn = _demo_fn
_alpha_skill_fn = _demo_fn
_beta_skill_fn = _demo_fn
