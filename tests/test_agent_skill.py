"""Tests for the ``kind: agent`` skill backend (backlog item 32).

Coverage:

* SkillSpec model — ``kind: agent`` with ``target_agent`` loads without error.
* SkillSpec model — missing ``target_agent`` raises ValidationError.
* ``AgentSkillBackend.execute`` in mock mode returns the pass-through stub.
* ``AgentSkillBackend.execute`` with missing env vars raises SkillError(BACKEND_ERROR).
* ``mdk validate`` on an agent using an agent-skill emits the advisory warning.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import SkillImplementationKind, SkillSpec
from movate.core.skill_backend import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
)
from movate.core.skill_backend.agent import AgentSkillBackend
from movate.core.skill_loader import load_skill

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_skill_dict(**overrides: Any) -> dict[str, Any]:
    """Minimal valid ``kind: agent`` SkillSpec dict."""
    base: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Skill",
        "name": "summarizer-call",
        "version": "0.1.0",
        "description": "Calls the deployed summarizer agent",
        "schema": {
            "input": {"text": "string"},
            "output": {"summary": "string"},
        },
        "implementation": {
            "kind": "agent",
            "target_agent": "summarizer",
            "timeout_s": 30,
        },
    }
    base.update(overrides)
    return base


def _make_skill_bundle(tmp_path: Path) -> Any:
    """Write a kind:agent skill.yaml to disk and load it as a SkillBundle."""
    skill_dir = tmp_path / "skills" / "summarizer-call"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: summarizer-call\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {text: string}\n"
        "  output: {summary: string}\n"
        "implementation:\n"
        "  kind: agent\n"
        "  target_agent: summarizer\n"
        "  timeout_s: 30\n"
    )
    return load_skill(skill_dir)


# ---------------------------------------------------------------------------
# SkillSpec model — kind: agent
# ---------------------------------------------------------------------------


class TestSkillSpecAgentKind:
    def test_valid_agent_skill_loads(self) -> None:
        """A complete kind:agent spec parses without error."""
        spec = SkillSpec.model_validate(_agent_skill_dict())
        assert spec.implementation.kind == SkillImplementationKind.AGENT
        assert spec.implementation.target_agent == "summarizer"
        assert spec.implementation.timeout_s == 30

    def test_target_agent_defaults_timeout(self) -> None:
        """``timeout_s`` defaults to 30 when omitted."""
        d = _agent_skill_dict()
        del d["implementation"]["timeout_s"]
        spec = SkillSpec.model_validate(d)
        assert spec.implementation.timeout_s == 30

    def test_missing_target_agent_raises(self) -> None:
        """``target_agent`` is required for kind:agent — missing it must fail at parse time."""
        d = _agent_skill_dict()
        del d["implementation"]["target_agent"]
        with pytest.raises(ValidationError, match="target_agent"):
            SkillSpec.model_validate(d)

    def test_empty_target_agent_raises(self) -> None:
        """An empty ``target_agent`` string is also rejected."""
        d = _agent_skill_dict()
        d["implementation"]["target_agent"] = ""
        with pytest.raises(ValidationError, match="target_agent"):
            SkillSpec.model_validate(d)


# ---------------------------------------------------------------------------
# AgentSkillBackend — mock mode
# ---------------------------------------------------------------------------


class TestAgentSkillBackendMock:
    """Tests that exercise the mock short-circuit path (ctx.mock=True)."""

    def test_mock_returns_stub_with_input(self, tmp_path: Path) -> None:
        """Mock mode returns ``{_agent_skill_mock: True, **input}``
        without touching any real endpoint."""
        bundle = _make_skill_bundle(tmp_path)
        backend = AgentSkillBackend()
        ctx = SkillExecutionContext(mock=True)
        input_data = {"text": "hello world"}

        result = asyncio.get_event_loop().run_until_complete(
            backend.execute(bundle, input_data, ctx)
        )

        assert result["_agent_skill_mock"] is True
        assert result["text"] == "hello world"

    def test_mock_does_not_call_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No HTTP calls are made in mock mode — even if env vars are set."""
        monkeypatch.setenv("MOVATE_RUNTIME_URL", "http://fake-runtime")
        monkeypatch.setenv("MOVATE_API_KEY", "fake-key")

        bundle = _make_skill_bundle(tmp_path)
        backend = AgentSkillBackend()
        ctx = SkillExecutionContext(mock=True)

        # Patch MovateClient in the movate.core.client module so any import
        # of it by the backend would get the mock.
        with patch("movate.core.client.MovateClient") as mock_client:
            result = asyncio.get_event_loop().run_until_complete(
                backend.execute(bundle, {"text": "foo"}, ctx)
            )

        # Client constructor must NOT have been called.
        mock_client.assert_not_called()
        assert result["_agent_skill_mock"] is True


# ---------------------------------------------------------------------------
# AgentSkillBackend — live mode failure paths (no real network needed)
# ---------------------------------------------------------------------------


class TestAgentSkillBackendLive:
    def test_missing_env_vars_raises_backend_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When MOVATE_RUNTIME_URL / MOVATE_API_KEY are unset in live mode,
        the backend raises SkillError(BACKEND_ERROR) immediately."""
        monkeypatch.delenv("MOVATE_RUNTIME_URL", raising=False)
        monkeypatch.delenv("MOVATE_API_KEY", raising=False)

        bundle = _make_skill_bundle(tmp_path)
        backend = AgentSkillBackend()
        ctx = SkillExecutionContext(mock=False)

        with pytest.raises(SkillError) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                backend.execute(bundle, {"text": "hello"}, ctx)
            )

        err = exc_info.value
        assert err.type == SkillErrorType.BACKEND_ERROR
        assert "MOVATE_RUNTIME_URL" in err.message
        assert "MOVATE_API_KEY" in err.message

    def test_movate_client_error_raises_backend_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A MovateClientError from submit_job surfaces as SkillError(BACKEND_ERROR)."""
        monkeypatch.setenv("MOVATE_RUNTIME_URL", "http://fake-runtime")
        monkeypatch.setenv("MOVATE_API_KEY", "fake-key")

        from movate.core.client import MovateClientError  # noqa: PLC0415

        bundle = _make_skill_bundle(tmp_path)
        backend = AgentSkillBackend()
        ctx = SkillExecutionContext(mock=False)

        mock_client_instance = AsyncMock()
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_instance.submit_job = AsyncMock(
            side_effect=MovateClientError(
                status_code=404,
                code="agent_not_found",
                message="agent 'summarizer' not registered",
            )
        )

        with (
            patch("movate.core.client.MovateClient", return_value=mock_client_instance),
            pytest.raises(SkillError) as exc_info,
        ):
            asyncio.get_event_loop().run_until_complete(
                backend.execute(bundle, {"text": "hello"}, ctx)
            )

        err = exc_info.value
        assert err.type == SkillErrorType.BACKEND_ERROR
        assert "summarizer" in err.message


# ---------------------------------------------------------------------------
# Backend is registered in the dispatch table
# ---------------------------------------------------------------------------


def test_agent_backend_registered() -> None:
    """Importing the agent module registers it with the dispatch table."""
    from movate.core.skill_backend.base import _BACKENDS  # noqa: PLC0415

    assert SkillImplementationKind.AGENT in _BACKENDS
    assert isinstance(_BACKENDS[SkillImplementationKind.AGENT], AgentSkillBackend)


# ---------------------------------------------------------------------------
# mdk validate — advisory for kind: agent skills
# ---------------------------------------------------------------------------


class TestValidateAgentSkillAdvisory:
    """Validate that `mdk validate` emits the advisory warning for kind:agent skills."""

    @staticmethod
    def _scaffold_agent_with_agent_skill(
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Add a kind:agent skill declaration to the rag-qa agent."""
        # Write the agent-skill to the skills/ directory.
        skill_dir = project / "skills" / "summarizer-call"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.yaml").write_text(
            "api_version: movate/v1\n"
            "kind: Skill\n"
            "name: summarizer-call\n"
            "version: 0.1.0\n"
            "schema:\n"
            "  input: {text: string}\n"
            "  output: {summary: string}\n"
            "implementation:\n"
            "  kind: agent\n"
            "  target_agent: summarizer\n"
        )
        # Declare the skill in the rag-qa agent.yaml.
        import yaml  # noqa: PLC0415

        agent_yaml = project / "agents" / "rag-qa" / "agent.yaml"
        data = yaml.safe_load(agent_yaml.read_text())
        data.setdefault("skills", []).append("summarizer-call")
        agent_yaml.write_text(yaml.dump(data))

    def test_validate_emits_advisory_for_agent_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``mdk validate`` prints the agent-skill advisory (yellow !) without
        failing (exit 0 — it's a warning, not an error)."""
        # Bootstrap a full project.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "proj", "--skip-snapshot", "--with-agents", "rag-qa"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        project = tmp_path / "proj"
        monkeypatch.chdir(project)

        self._scaffold_agent_with_agent_skill(project, monkeypatch)

        result = runner.invoke(
            app,
            ["validate", "agents/rag-qa"],
            env={"COLUMNS": "200"},
        )
        # Advisory = exit 0 (warning, not error).
        assert result.exit_code == 0, result.stdout + result.stderr
        # Advisory text must mention the skill name, kind:agent, and the target.
        assert "summarizer-call" in result.stdout
        assert "kind: agent" in result.stdout
        assert "summarizer" in result.stdout
        assert (
            "ensure it's deployed" in result.stdout.lower() or "deployed" in result.stdout.lower()
        )

    def test_validate_advisory_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The advisory must not cause exit code 2, even with --strict."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "proj", "--skip-snapshot", "--with-agents", "rag-qa"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        project = tmp_path / "proj"
        monkeypatch.chdir(project)
        self._scaffold_agent_with_agent_skill(project, monkeypatch)

        result = runner.invoke(
            app,
            ["validate", "--strict", "agents/rag-qa"],
            env={"COLUMNS": "200"},
        )
        # Even under --strict the advisory is exit 0 — it's not a lint issue,
        # it's a backend-availability notice (like the http/mcp advisories).
        assert result.exit_code == 0, result.stdout + result.stderr
