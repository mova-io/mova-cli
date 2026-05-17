"""Tests for the AgentMetadata nested block extension (backlog item 29).

Covers:
- ``AgentMetadata`` model: loads with all fields, all optional, capabilities validation
- ``AgentSpec`` backward-compatibility: loads without ``metadata:`` block unchanged
- ``AgentSpec`` with full ``metadata:`` block
- ``mdk show`` renders the "Marketplace metadata" section when block is present
- ``mdk show`` renders nothing extra when block is absent
- ``mdk validate`` emits advisory when no metadata block at all
- ``mdk validate`` emits warning for bad examples (missing ``output`` key)
- ``mdk validate`` emits warning for empty ``owner`` string
- ``mdk validate`` passes cleanly with a well-formed metadata block
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import AgentMetadata, AgentSpec

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    """Remove terminal escape codes so assertions don't depend on Rich colours."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _minimal_agent_yaml(
    *,
    name: str = "demo-agent",
    extra: str = "",
) -> str:
    """Minimal valid agent.yaml content with optional extra YAML appended."""
    return (
        "api_version: movate/v1\n"
        "kind: Agent\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: { who: string }\n"
        "  output: { greeting: string }\n"
        + extra
    )


def _write_agent(
    parent: Path,
    *,
    name: str = "demo-agent",
    extra_yaml: str = "",
) -> Path:
    """Create a minimal agent directory with optional extra YAML in agent.yaml."""
    agent_dir = parent / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.yaml").write_text(_minimal_agent_yaml(name=name, extra=extra_yaml))
    (agent_dir / "prompt.md").write_text("Hello {{ input.who }}!")
    return agent_dir


# ---------------------------------------------------------------------------
# AgentMetadata model unit tests
# ---------------------------------------------------------------------------


class TestAgentMetadataModel:
    def test_all_fields_optional_defaults(self) -> None:
        """All fields on AgentMetadata have safe defaults — empty block parses."""
        m = AgentMetadata()
        assert m.persona is None
        assert m.role is None
        assert m.capabilities == []
        assert m.tags == []
        assert m.examples == []
        assert m.owner is None

    def test_full_metadata_block(self) -> None:
        """All fields parse when supplied."""
        m = AgentMetadata(
            persona="A friendly FAQ bot for Acme Corp",
            role="customer-support",
            capabilities=["question-answering", "knowledge-retrieval"],
            tags=["faq", "support"],
            examples=[
                {"input": {"q": "Return policy?"}, "output": {"a": "30 days"}},
            ],
            owner="team-support@acme.com",
        )
        assert m.persona == "A friendly FAQ bot for Acme Corp"
        assert m.role == "customer-support"
        assert m.capabilities == ["question-answering", "knowledge-retrieval"]
        assert m.tags == ["faq", "support"]
        assert len(m.examples) == 1
        assert m.owner == "team-support@acme.com"

    def test_capabilities_slug_validation_rejects_spaces(self) -> None:
        """Capabilities with spaces (not URL-safe) are rejected at parse time."""
        with pytest.raises(ValidationError, match="lowercase alphanumeric"):
            AgentMetadata(capabilities=["faq lookup"])

    def test_capabilities_slug_validation_rejects_uppercase(self) -> None:
        with pytest.raises(ValidationError, match="lowercase alphanumeric"):
            AgentMetadata(capabilities=["FAQ-lookup"])

    def test_capabilities_slug_validation_accepts_valid(self) -> None:
        m = AgentMetadata(capabilities=["faq-lookup", "ticket-routing", "summarize"])
        assert len(m.capabilities) == 3

    def test_capabilities_single_char_rejected(self) -> None:
        """Single-char capability doesn't match the slug regex (need at least 2 chars)."""
        with pytest.raises(ValidationError):
            AgentMetadata(capabilities=["a"])

    def test_no_extra_fields(self) -> None:
        """extra='forbid' means unknown keys raise an error."""
        with pytest.raises(ValidationError):
            AgentMetadata(**{"unknown_field": "value"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AgentSpec backward-compatibility
# ---------------------------------------------------------------------------


class TestAgentSpecBackwardCompat:
    def test_loads_without_metadata_block(self) -> None:
        """Existing agent.yaml without metadata: loads unchanged; spec.metadata is None."""
        raw = yaml.safe_load(_minimal_agent_yaml())
        spec = AgentSpec.model_validate(raw)
        assert spec.metadata is None

    def test_flat_persona_role_still_work(self) -> None:
        """Flat top-level persona/role/capabilities fields still parse."""
        raw = yaml.safe_load(
            _minimal_agent_yaml(
                extra=(
                    "persona: Concise and technical\n"
                    "role: data-analysis\n"
                    "capabilities:\n"
                    "  - sql-gen\n"
                )
            )
        )
        spec = AgentSpec.model_validate(raw)
        assert spec.persona == "Concise and technical"
        assert spec.role == "data-analysis"
        assert spec.capabilities == ["sql-gen"]
        assert spec.metadata is None  # nested block not set

    def test_loads_with_full_metadata_block(self) -> None:
        """AgentSpec parses a full metadata: block into AgentMetadata."""
        raw = yaml.safe_load(
            _minimal_agent_yaml(
                extra=(
                    "metadata:\n"
                    "  persona: A friendly FAQ bot\n"
                    "  role: customer-support\n"
                    "  capabilities:\n"
                    "    - question-answering\n"
                    "    - knowledge-retrieval\n"
                    "  tags:\n"
                    "    - faq\n"
                    "    - support\n"
                    "  owner: team@acme.com\n"
                    "  examples:\n"
                    "    - input:\n"
                    "        question: What is your return policy\n"
                    "      output:\n"
                    "        answer: 30 days\n"
                )
            )
        )
        spec = AgentSpec.model_validate(raw)
        assert spec.metadata is not None
        assert spec.metadata.persona == "A friendly FAQ bot"
        assert spec.metadata.role == "customer-support"
        assert spec.metadata.capabilities == ["question-answering", "knowledge-retrieval"]
        assert spec.metadata.tags == ["faq", "support"]
        assert spec.metadata.owner == "team@acme.com"
        assert len(spec.metadata.examples) == 1
        assert spec.metadata.examples[0]["input"] == {"question": "What is your return policy"}

    def test_metadata_block_with_only_persona(self) -> None:
        """Partial metadata block — only persona set — parses cleanly."""
        raw = yaml.safe_load(
            _minimal_agent_yaml(extra="metadata:\n  persona: A helpful assistant\n")
        )
        spec = AgentSpec.model_validate(raw)
        assert spec.metadata is not None
        assert spec.metadata.persona == "A helpful assistant"
        assert spec.metadata.role is None
        assert spec.metadata.capabilities == []

    def test_metadata_capabilities_slug_validated(self) -> None:
        """Invalid capability slug inside metadata: block raises ValidationError."""
        raw = yaml.safe_load(
            _minimal_agent_yaml(
                extra="metadata:\n  capabilities:\n    - 'bad slug with spaces'\n"
            )
        )
        with pytest.raises(ValidationError, match="lowercase alphanumeric"):
            AgentSpec.model_validate(raw)


# ---------------------------------------------------------------------------
# mdk show rendering
# ---------------------------------------------------------------------------


class TestShowRendering:
    def test_show_no_metadata_block_no_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When metadata: is absent, the 'Marketplace metadata' section is not rendered."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(tmp_path)
        result = runner.invoke(app, ["show", str(agent_dir)])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        cleaned = _strip_ansi(result.stdout)
        assert "Marketplace metadata" not in cleaned

    def test_show_with_metadata_block_renders_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When metadata: is present, the show table includes the section header."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml=(
                "metadata:\n"
                "  persona: A friendly FAQ bot\n"
                "  role: customer-support\n"
            ),
        )
        result = runner.invoke(app, ["show", str(agent_dir)])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        cleaned = _strip_ansi(result.stdout)
        assert "Marketplace metadata" in cleaned
        assert "customer-support" in cleaned
        assert "A friendly FAQ bot" in cleaned

    def test_show_metadata_capabilities(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml=(
                "metadata:\n"
                "  capabilities:\n"
                "    - question-answering\n"
                "    - ticket-routing\n"
            ),
        )
        result = runner.invoke(app, ["show", str(agent_dir)])
        assert result.exit_code == 0
        cleaned = _strip_ansi(result.stdout)
        assert "question-answering" in cleaned
        assert "ticket-routing" in cleaned

    def test_show_metadata_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml="metadata:\n  owner: team-support@acme.com\n",
        )
        result = runner.invoke(app, ["show", str(agent_dir)])
        assert result.exit_code == 0
        cleaned = _strip_ansi(result.stdout)
        assert "team-support@acme.com" in cleaned

    def test_show_metadata_example_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The show table shows the count of examples, not the raw content."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml=(
                "metadata:\n"
                "  examples:\n"
                "    - input:\n"
                "        q: What is your return policy\n"
                "      output:\n"
                "        a: 30 days\n"
                "    - input:\n"
                "        q: Hours\n"
                "      output:\n"
                "        a: 9 to 5\n"
            ),
        )
        result = runner.invoke(app, ["show", str(agent_dir)])
        assert result.exit_code == 0
        cleaned = _strip_ansi(result.stdout)
        assert "2 example" in cleaned

    def test_show_metadata_tags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml=(
                "metadata:\n"
                "  tags:\n"
                "    - faq\n"
                "    - support\n"
            ),
        )
        result = runner.invoke(app, ["show", str(agent_dir)])
        assert result.exit_code == 0
        cleaned = _strip_ansi(result.stdout)
        assert "faq" in cleaned
        assert "support" in cleaned


# ---------------------------------------------------------------------------
# mdk validate checks
# ---------------------------------------------------------------------------


class TestValidateMetadataChecks:
    def test_validate_no_metadata_emits_discovery_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no metadata block at all, validate emits a dim discovery hint (exit 0)."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(tmp_path)
        result = runner.invoke(app, ["validate", str(agent_dir)])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        cleaned = _strip_ansi(result.stdout)
        # Hint mentions the metadata block
        assert "metadata" in cleaned.lower()

    def test_validate_example_missing_output_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An example missing the 'output' key triggers a yellow advisory (exit 0)."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml=(
                "metadata:\n"
                "  examples:\n"
                "    - input:\n"
                "        question: What is the return policy\n"
                # deliberately no 'output' key
            ),
        )
        result = runner.invoke(app, ["validate", str(agent_dir)])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        cleaned = _strip_ansi(result.stdout)
        assert "output" in cleaned

    def test_validate_example_missing_input_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An example missing the 'input' key also triggers a yellow advisory."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml=(
                "metadata:\n"
                "  examples:\n"
                "    - output:\n"
                "        answer: 30 days\n"
                # deliberately no 'input' key
            ),
        )
        result = runner.invoke(app, ["validate", str(agent_dir)])
        assert result.exit_code == 0
        cleaned = _strip_ansi(result.stdout)
        assert "input" in cleaned.lower()

    def test_validate_empty_owner_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A metadata.owner set to empty string triggers a yellow warning."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml="metadata:\n  owner: ''\n",
        )
        result = runner.invoke(app, ["validate", str(agent_dir)])
        assert result.exit_code == 0
        cleaned = _strip_ansi(result.stdout)
        assert "owner" in cleaned.lower()

    def test_validate_well_formed_metadata_passes_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A complete, valid metadata block produces exit 0 with no warning lines."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml=(
                "metadata:\n"
                "  persona: A helpful support bot\n"
                "  role: customer-support\n"
                "  capabilities:\n"
                "    - question-answering\n"
                "  tags:\n"
                "    - faq\n"
                "  owner: support@acme.com\n"
                "  examples:\n"
                "    - input:\n"
                "        question: Return policy\n"
                "      output:\n"
                "        answer: 30 days\n"
            ),
        )
        result = runner.invoke(app, ["validate", str(agent_dir)])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        cleaned = _strip_ansi(result.stdout)
        # Should have the green checkmark success line
        assert spec_name_in_output(cleaned, "demo-agent")

    def test_validate_non_email_owner_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A team name (non-email) is accepted as owner."""
        monkeypatch.chdir(tmp_path)
        agent_dir = _write_agent(
            tmp_path,
            extra_yaml="metadata:\n  owner: Platform Team\n",
        )
        result = runner.invoke(app, ["validate", str(agent_dir)])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        # No owner warning
        cleaned = _strip_ansi(result.stdout)
        assert "owner" not in cleaned.lower() or "Platform Team" not in cleaned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def spec_name_in_output(text: str, name: str) -> bool:
    """True if the agent name appears anywhere in the validate output."""
    return name in text
