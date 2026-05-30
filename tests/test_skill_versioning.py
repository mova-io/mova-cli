"""Tests for two additive skill improvements.

Fix 1 — version constraints on agent.yaml ``skills:`` list:
  * Bare-string entries remain valid (backward-compat).
  * Inline ``{name, version}`` form accepted.
  * Installed version not satisfying a constraint raises AgentLoadError.
  * Skill with no version field (treated as 0.0.0) emits a warning.

Fix 2 — safe ``on_conflict`` default in ``persist_skill_bundle()``:
  * Default ``on_conflict='reject'`` rejects a duplicate.
  * Explicit ``on_conflict='replace'`` overwrites.
  * ``POST /api/v1/skills`` returns 409 without ``?force=true``.
  * ``POST /api/v1/skills?force=true`` overwrites cleanly.
  * ``mdk deploy --force`` passes ``force=True`` through to ``_upload_skills``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from movate.core.loader import AgentLoadError
from movate.core.models import AgentSpec, SkillRef
from movate.core.skill_loader import (
    SkillLoadError,
    load_skill_registry,
    resolve_agent_skills,
)
from movate.runtime.skill_creation import SkillCreationError, persist_skill_bundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_yaml(name: str = "my-skill", version: str = "1.2.0") -> bytes:
    return (
        f"api_version: movate/v1\n"
        f"kind: Skill\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: test skill\n"
        f"schema:\n"
        f"  input:\n"
        f"    x: integer\n"
        f"  output:\n"
        f"    y: integer\n"
        f"implementation:\n"
        f"  kind: python\n"
        f"  entry: tests.test_skills:_dummy_skill\n"
    ).encode()


def _write_skill_dir(skills_root: Path, name: str, version: str = "1.2.0") -> Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_bytes(_skill_yaml(name, version))
    return skill_dir


def _make_registry(tmp_path: Path, skills: dict[str, str]) -> dict[str, Any]:
    """Build a registry with ``{name: version}`` entries under tmp_path/skills/."""
    skills_root = tmp_path / "skills"
    for name, version in skills.items():
        _write_skill_dir(skills_root, name, version)
    return load_skill_registry(tmp_path)


# ---------------------------------------------------------------------------
# Fix 1a — SkillRef model
# ---------------------------------------------------------------------------


class TestSkillRef:
    def test_bare_string_coerce_from_raw(self) -> None:
        ref = SkillRef._from_raw("kb-lookup")
        assert ref.name == "kb-lookup"
        assert ref.version == "*"

    def test_dict_coerce_from_raw(self) -> None:
        ref = SkillRef._from_raw({"name": "kb-lookup", "version": "^1.2"})
        assert ref.name == "kb-lookup"
        assert ref.version == "^1.2"

    def test_dict_defaults_version_to_star(self) -> None:
        ref = SkillRef._from_raw({"name": "kb-lookup"})
        assert ref.version == "*"

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(ValueError, match="string or"):
            SkillRef._from_raw(42)

    def test_dict_without_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            SkillRef._from_raw({"version": "1.0.0"})

    def test_str_returns_name(self) -> None:
        ref = SkillRef(name="kb-lookup", version="^1.2")
        assert str(ref) == "kb-lookup"

    def test_eq_with_string(self) -> None:
        ref = SkillRef(name="kb-lookup")
        assert ref == "kb-lookup"
        assert ref != "web-search"

    def test_eq_with_another_ref(self) -> None:
        a = SkillRef(name="kb-lookup", version="^1.0")
        b = SkillRef(name="kb-lookup", version="^2.0")
        c = SkillRef(name="web-search")
        assert a != b  # different version
        assert a != c

    def test_in_operator_with_string_works(self) -> None:
        """Critical backward-compat: ``'kb-vector-lookup' in spec.skills``."""
        refs = [SkillRef(name="kb-vector-lookup"), SkillRef(name="web-search")]
        assert "kb-vector-lookup" in refs
        assert "nonexistent" not in refs

    def test_hash_equality_with_string(self) -> None:
        ref = SkillRef(name="kb-lookup")
        assert hash(ref) == hash("kb-lookup")


# ---------------------------------------------------------------------------
# Fix 1b — AgentSpec.skills field accepts both forms
# ---------------------------------------------------------------------------


def _agent_spec_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Agent",
        "name": "test-agent",
        "version": "0.1.0",
        "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
        "prompt": "./prompt.md",
        "schema": {"input": {"q": "string"}, "output": {"a": "string"}},
    }
    base.update(overrides)
    return base


class TestAgentSpecSkillsField:
    def test_bare_strings_still_accepted(self) -> None:
        spec = AgentSpec.model_validate(_agent_spec_dict(skills=["kb-lookup", "web-search"]))
        assert len(spec.skills) == 2
        assert spec.skills[0].name == "kb-lookup"
        assert spec.skills[0].version == "*"

    def test_inline_object_form_accepted(self) -> None:
        spec = AgentSpec.model_validate(
            _agent_spec_dict(
                skills=[
                    {"name": "kb-lookup", "version": "^1.2"},
                    {"name": "web-search", "version": ">=1.0,<2.0"},
                ]
            )
        )
        assert spec.skills[0].version == "^1.2"
        assert spec.skills[1].version == ">=1.0,<2.0"

    def test_mixed_forms_accepted(self) -> None:
        """Bare string + inline object in the same skills list."""
        spec = AgentSpec.model_validate(
            _agent_spec_dict(
                skills=[
                    "kb-lookup",
                    {"name": "web-search", "version": "^2.0"},
                ]
            )
        )
        assert spec.skills[0].version == "*"
        assert spec.skills[1].version == "^2.0"

    def test_empty_list_still_valid(self) -> None:
        spec = AgentSpec.model_validate(_agent_spec_dict(skills=[]))
        assert spec.skills == []

    def test_string_membership_still_works(self) -> None:
        """``'kb-lookup' in spec.skills`` must still evaluate True."""
        spec = AgentSpec.model_validate(_agent_spec_dict(skills=["kb-lookup"]))
        assert "kb-lookup" in spec.skills
        assert "nonexistent" not in spec.skills

    def test_invalid_skills_entry_raises(self) -> None:
        with pytest.raises(ValidationError):
            AgentSpec.model_validate(_agent_spec_dict(skills=[42]))


# ---------------------------------------------------------------------------
# Fix 1c — resolve_agent_skills version checking
# ---------------------------------------------------------------------------


class TestResolveAgentSkillsVersionCheck:
    def test_bare_string_resolves_any_version(self, tmp_path: Path) -> None:
        """Bare string = version '*' = any installed version accepted."""
        registry = _make_registry(tmp_path, {"kb-lookup": "9.9.9"})
        refs = [SkillRef(name="kb-lookup")]  # version="*" by default
        bundles = resolve_agent_skills(refs, registry)
        assert len(bundles) == 1
        assert bundles[0].spec.name == "kb-lookup"

    def test_exact_version_match_resolves(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, {"kb-lookup": "1.2.0"})
        refs = [SkillRef(name="kb-lookup", version="1.2.0")]
        bundles = resolve_agent_skills(refs, registry)
        assert bundles[0].spec.version == "1.2.0"

    def test_caret_range_satisfied(self, tmp_path: Path) -> None:
        """``^1.2`` means ``>=1.2,<2.0``."""
        registry = _make_registry(tmp_path, {"kb-lookup": "1.5.3"})
        refs = [SkillRef(name="kb-lookup", version="^1.2")]
        bundles = resolve_agent_skills(refs, registry)
        assert bundles[0].spec.version == "1.5.3"

    def test_caret_range_not_satisfied_raises(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, {"kb-lookup": "2.0.0"})
        refs = [SkillRef(name="kb-lookup", version="^1.2")]
        with pytest.raises(AgentLoadError, match="does not satisfy constraint"):
            resolve_agent_skills(refs, registry, agent_name="my-agent")

    def test_error_message_includes_skill_and_agent_names(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, {"kb-lookup": "1.0.0"})
        refs = [SkillRef(name="kb-lookup", version="^1.2")]
        with pytest.raises(AgentLoadError) as exc_info:
            resolve_agent_skills(refs, registry, agent_name="my-agent")
        msg = str(exc_info.value)
        assert "kb-lookup" in msg
        assert "my-agent" in msg
        assert "1.0.0" in msg
        assert "^1.2" in msg

    def test_gte_lt_range_not_satisfied_raises(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, {"kb-lookup": "2.5.0"})
        refs = [SkillRef(name="kb-lookup", version=">=1.0,<2.0")]
        with pytest.raises(AgentLoadError, match="does not satisfy constraint"):
            resolve_agent_skills(refs, registry)

    def test_unknown_name_still_raises_skill_load_error(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, {"kb-lookup": "1.0.0"})
        refs = [SkillRef(name="nonexistent")]
        with pytest.raises(SkillLoadError, match="no such skill is registered"):
            resolve_agent_skills(refs, registry)

    def test_invalid_constraint_raises_agent_load_error(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, {"kb-lookup": "1.0.0"})
        refs = [SkillRef(name="kb-lookup", version="NOT_VALID!!")]
        with pytest.raises(AgentLoadError, match="invalid version constraint"):
            resolve_agent_skills(refs, registry)

    def test_legacy_list_of_strings_still_works(self, tmp_path: Path) -> None:
        """Direct list[str] callers (legacy) continue to work."""
        registry = _make_registry(tmp_path, {"kb-lookup": "1.0.0"})
        bundles = resolve_agent_skills(["kb-lookup"], registry)  # type: ignore[arg-type]
        assert bundles[0].spec.name == "kb-lookup"

    def test_skill_with_no_version_emits_warning(self, tmp_path: Path) -> None:
        """A skill.yaml where version: 0.0.0 (the fallback) emits a UserWarning."""
        registry = _make_registry(tmp_path, {"kb-lookup": "0.0.0"})
        refs = [SkillRef(name="kb-lookup", version="^1.0")]
        # 0.0.0 doesn't satisfy ^1.0, but the warning fires regardless.
        # We only test the warning path — the constraint failure is separate.
        with (
            pytest.warns(UserWarning, match="no version field"),
            contextlib.suppress(AgentLoadError),
        ):
            resolve_agent_skills(refs, registry, agent_name="my-agent")


# ---------------------------------------------------------------------------
# Fix 2a — persist_skill_bundle default on_conflict='reject'
# ---------------------------------------------------------------------------


class TestPersistSkillBundleOnConflict:
    def test_first_upload_always_succeeds(self, tmp_path: Path) -> None:
        result = persist_skill_bundle(
            {"skill.yaml": _skill_yaml()},
            skills_path=tmp_path,
        )
        assert result.bundle.spec.name == "my-skill"

    def test_default_reject_raises_on_duplicate(self, tmp_path: Path) -> None:
        """Second upload without on_conflict='replace' must raise SkillCreationError."""
        persist_skill_bundle({"skill.yaml": _skill_yaml()}, skills_path=tmp_path)
        with pytest.raises(SkillCreationError) as exc_info:
            persist_skill_bundle({"skill.yaml": _skill_yaml()}, skills_path=tmp_path)
        assert exc_info.value.status_code == 409
        # Error message must include the actionable guidance.
        assert "--force" in str(exc_info.value)

    def test_error_message_includes_version(self, tmp_path: Path) -> None:
        persist_skill_bundle({"skill.yaml": _skill_yaml(version="1.2.0")}, skills_path=tmp_path)
        with pytest.raises(SkillCreationError) as exc_info:
            persist_skill_bundle({"skill.yaml": _skill_yaml(version="1.2.0")}, skills_path=tmp_path)
        assert "1.2.0" in str(exc_info.value)

    def test_explicit_replace_overwrites(self, tmp_path: Path) -> None:
        persist_skill_bundle({"skill.yaml": _skill_yaml(version="1.0.0")}, skills_path=tmp_path)
        result = persist_skill_bundle(
            {"skill.yaml": _skill_yaml(version="1.1.0")},
            skills_path=tmp_path,
            on_conflict="replace",
        )
        assert result.bundle.spec.version == "1.1.0"

    def test_update_alias_also_overwrites(self, tmp_path: Path) -> None:
        persist_skill_bundle({"skill.yaml": _skill_yaml()}, skills_path=tmp_path)
        result = persist_skill_bundle(
            {"skill.yaml": _skill_yaml(version="2.0.0")},
            skills_path=tmp_path,
            on_conflict="update",
        )
        assert result.bundle.spec.version == "2.0.0"


# ---------------------------------------------------------------------------
# Fix 2b — POST /api/v1/skills with and without ?force=true
# ---------------------------------------------------------------------------


@pytest.fixture
async def skills_storage():  # type: ignore[return]
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def skills_client(skills_storage: Any, tmp_path: Path):  # type: ignore[return]
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from movate.runtime import build_app  # noqa: PLC0415

    agents_path = tmp_path / "agents"
    agents_path.mkdir()
    return TestClient(build_app(skills_storage, agents_path=agents_path))


@pytest.fixture
async def skills_auth_header(skills_storage: Any) -> dict[str, str]:
    from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key  # noqa: PLC0415

    minted = mint_api_key(
        tenant_id=uuid4().hex,
        env=ApiKeyEnv.LIVE,
        label="skill-version-tests",
        scopes=list(ALL_SCOPES),
    )
    await skills_storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _post_skill(
    client: Any,
    auth: dict[str, str],
    *,
    name: str = "my-skill",
    version: str = "1.0.0",
    force: bool = False,
) -> Any:
    url = "/api/v1/skills"
    if force:
        url += "?force=true"
    return client.post(
        url,
        files=[("skill_yaml", ("skill.yaml", _skill_yaml(name, version), "application/x-yaml"))],
        headers=auth,
    )


class TestSkillsApiOnConflict:
    def test_first_upload_returns_201(
        self, skills_client: Any, skills_auth_header: dict[str, str]
    ) -> None:
        r = _post_skill(skills_client, skills_auth_header)
        assert r.status_code == 201, r.text

    def test_duplicate_without_force_returns_409(
        self, skills_client: Any, skills_auth_header: dict[str, str]
    ) -> None:
        _post_skill(skills_client, skills_auth_header)
        r = _post_skill(skills_client, skills_auth_header)
        assert r.status_code == 409, r.text

    def test_duplicate_with_force_returns_201(
        self, skills_client: Any, skills_auth_header: dict[str, str]
    ) -> None:
        _post_skill(skills_client, skills_auth_header)
        r = _post_skill(skills_client, skills_auth_header, force=True)
        assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Fix 2c — _upload_skills passes force kwarg
# ---------------------------------------------------------------------------


def test_upload_skills_force_appends_query_param(tmp_path: Path) -> None:
    """_upload_skills with force=True must pass ?force=true to the endpoint."""
    from movate.cli.deploy import _upload_skills  # noqa: PLC0415

    calls: list[str] = []

    class _FakeResp:
        status_code = 201
        text = ""

    class _FakeClient:
        def post(self, url: str, **_: Any) -> _FakeResp:
            calls.append(url)
            return _FakeResp()

    skills_dir = tmp_path / "skills" / "my-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "skill.yaml").write_bytes(_skill_yaml())

    _upload_skills(
        client=_FakeClient(),  # type: ignore[arg-type]
        base_url="http://runtime",
        headers={},
        project_root=tmp_path,
        force=True,
    )

    assert len(calls) == 1
    assert "?force=true" in calls[0]


def test_upload_skills_no_force_omits_query_param(tmp_path: Path) -> None:
    """_upload_skills without force must NOT append ?force=true."""
    from movate.cli.deploy import _upload_skills  # noqa: PLC0415

    calls: list[str] = []

    class _FakeResp:
        status_code = 201
        text = ""

    class _FakeClient:
        def post(self, url: str, **_: Any) -> _FakeResp:
            calls.append(url)
            return _FakeResp()

    skills_dir = tmp_path / "skills" / "my-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "skill.yaml").write_bytes(_skill_yaml())

    _upload_skills(
        client=_FakeClient(),  # type: ignore[arg-type]
        base_url="http://runtime",
        headers={},
        project_root=tmp_path,
        force=False,
    )

    assert len(calls) == 1
    assert "force" not in calls[0]
