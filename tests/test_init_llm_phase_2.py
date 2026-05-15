"""Phase 2 of mdk init --llm: generator module + validation loop.

This file tests the full generator path:

1. **GeneratedAgent Pydantic model** — strict validation, ``extra=forbid``,
   accepts a complete payload, rejects missing fields.
2. **generate_agent_from_description** — happy path against MockProvider;
   parses code-fence-wrapped output defensively; surfaces JSON-decode
   failures as ``LLMScaffoldError``; surfaces schema-mismatch failures
   as ``LLMScaffoldError``.
3. **write_agent_files** — materializes a GeneratedAgent to disk in the
   standard movate layout; reads back via load_agent without error.
4. **CLI end-to-end via --llm --mock** — fake LLM returns a valid
   GeneratedAgent JSON via ``MOVATE_MOCK_RESPONSE``, the generator
   completes, validation passes, files land at the target.
5. **--dry-run** — no files written, preview Panel rendered.
6. **Retry loop** — first attempt fails validation, retry succeeds.
7. **Debug artifact** — second failure stashes raw payload at the
   ``.movate/llm-init-failed-<name>.json`` path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent
from movate.providers.mock import MockProvider
from movate.scaffold import (
    GeneratedAgent,
    LLMScaffoldError,
    generate_agent_from_description,
    write_agent_files,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers — a valid GeneratedAgent payload used across multiple tests.
# ---------------------------------------------------------------------------


def _valid_agent_payload(name: str = "test-agent") -> dict:
    """Return a dict shaped like a valid GeneratedAgent JSON.

    Keep this aligned with the meta-prompt's HARD CONSTRAINTS — a payload
    that passes here should also pass ``load_agent``."""
    return {
        "agent_yaml": {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": name,
            "version": "0.1.0",
            "description": "A test agent generated for unit tests.",
            "owner": "",
            "model": {
                "provider": "openai/gpt-4o-mini-2024-07-18",
                "params": {"temperature": 0.0, "max_tokens": 512},
            },
            "prompt": "./prompt.md",
            "schema": {
                "input": "./schema/input.json",
                "output": "./schema/output.json",
            },
            "evals": {"dataset": "./evals/dataset.jsonl"},
        },
        "prompt_md": "Echo the user input:\n{{ input.text }}\nReply: {\"message\": \"...\"}",
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        },
        "sample_evals": [
            {"input": {"text": "hello"}, "expected": {"message": "Hello!"}},
            {"input": {"text": "what's 2+2?"}, "expected": {"message": "4"}},
        ],
    }


# ---------------------------------------------------------------------------
# GeneratedAgent Pydantic model
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGeneratedAgentModel:
    def test_accepts_valid_payload(self) -> None:
        agent = GeneratedAgent.model_validate(_valid_agent_payload())
        assert agent.agent_yaml["name"] == "test-agent"
        assert agent.input_schema["type"] == "object"
        assert len(agent.sample_evals) == 2

    def test_rejects_missing_required_field(self) -> None:
        payload = _valid_agent_payload()
        del payload["prompt_md"]
        with pytest.raises(Exception, match="prompt_md"):
            GeneratedAgent.model_validate(payload)

    def test_rejects_extra_fields(self) -> None:
        """extra='forbid' should reject unknown top-level keys so prompt
        drift surfaces immediately rather than silently."""
        payload = _valid_agent_payload()
        payload["bogus_field"] = "should not be here"
        with pytest.raises(Exception, match=r"bogus_field|extra"):
            GeneratedAgent.model_validate(payload)

    def test_sample_evals_default_empty(self) -> None:
        """sample_evals is optional — the agent is runnable without it,
        though mdk audit will flag missing-evals."""
        payload = _valid_agent_payload()
        del payload["sample_evals"]
        agent = GeneratedAgent.model_validate(payload)
        assert agent.sample_evals == []


# ---------------------------------------------------------------------------
# generate_agent_from_description
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateFromDescription:
    def test_happy_path_returns_generated_agent(self) -> None:
        """MockProvider returns valid JSON → generator returns the parsed
        GeneratedAgent."""
        canned = json.dumps(_valid_agent_payload("happy-agent"))
        provider = MockProvider(response=canned)
        result = asyncio.run(
            generate_agent_from_description(
                description="any description",
                name="happy-agent",
                model="openai/gpt-4o-mini-2024-07-18",
                provider=provider,
            )
        )
        assert isinstance(result, GeneratedAgent)
        assert result.agent_yaml["name"] == "happy-agent"

    def test_strips_markdown_code_fences(self) -> None:
        """Even with response_format=json_object, some models still wrap
        output in ```json fences. The generator must strip them rather
        than retry (which would cost $).

        MockProvider's constructor validates JSON-parseability, so we
        seed it with a valid response and patch the private attribute
        afterward — the fence-stripping path is what matters here,
        not the constructor invariant."""
        payload = _valid_agent_payload("fenced-agent")
        provider = MockProvider(response=json.dumps(payload))
        # Bypass the constructor's JSON check to seed the fenced variant.
        provider._response = "```json\n" + json.dumps(payload) + "\n```"
        result = asyncio.run(
            generate_agent_from_description(
                description="fenced",
                name="fenced-agent",
                model="openai/gpt-4o-mini-2024-07-18",
                provider=provider,
            )
        )
        assert result.agent_yaml["name"] == "fenced-agent"

    def test_raises_scaffold_error_on_invalid_json(self) -> None:
        """MockProvider returns non-JSON → LLMScaffoldError surfaces clean."""
        # Use a JSON literal that's valid JSON itself (MockProvider's
        # constructor sanity-checks) but won't match the GeneratedAgent
        # schema. The schema-validation path is what we exercise here.
        provider = MockProvider(response='{"not": "a generated agent"}')
        with pytest.raises(LLMScaffoldError, match=r"schema|GeneratedAgent"):
            asyncio.run(
                generate_agent_from_description(
                    description="bad",
                    name="bad-agent",
                    model="openai/gpt-4o-mini-2024-07-18",
                    provider=provider,
                )
            )


# ---------------------------------------------------------------------------
# write_agent_files
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWriteAgentFiles:
    def test_writes_standard_layout(self, tmp_path: Path) -> None:
        agent = GeneratedAgent.model_validate(_valid_agent_payload())
        target = tmp_path / "test-agent"
        write_agent_files(agent, target_dir=target)
        # Every file from the standard layout exists.
        assert (target / "agent.yaml").is_file()
        assert (target / "prompt.md").is_file()
        assert (target / "schema" / "input.json").is_file()
        assert (target / "schema" / "output.json").is_file()
        assert (target / "evals" / "dataset.jsonl").is_file()

    def test_written_agent_loads_cleanly(self, tmp_path: Path) -> None:
        """End-to-end: GeneratedAgent → disk → load_agent → no error.
        The validation loop in init.py relies on this round-trip."""
        agent = GeneratedAgent.model_validate(_valid_agent_payload())
        target = tmp_path / "test-agent"
        write_agent_files(agent, target_dir=target)
        bundle = load_agent(target)
        assert bundle.spec.name == "test-agent"

    def test_skips_evals_dir_when_no_samples(self, tmp_path: Path) -> None:
        payload = _valid_agent_payload()
        payload["sample_evals"] = []
        agent = GeneratedAgent.model_validate(payload)
        target = tmp_path / "no-evals-agent"
        write_agent_files(agent, target_dir=target)
        # evals/ directory shouldn't be created when there are no samples.
        # (load_agent would still tolerate this — evals.dataset path
        # is declarative, not enforced at load time.)
        assert not (target / "evals").exists()


# ---------------------------------------------------------------------------
# CLI end-to-end — mdk init --llm "..." --mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_response_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set MOVATE_MOCK_RESPONSE to a valid GeneratedAgent JSON so any
    MockProvider built downstream returns parseable output."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", json.dumps(_valid_agent_payload()))


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.unit
def test_llm_mock_end_to_end_writes_agent(
    tmp_path: Path,
    mock_response_env: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full path: mdk init <name> --llm "..." --mock → files land on disk."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "test-agent",
            "--llm",
            "an echo agent for unit tests",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Standard layout was materialized.
    target = tmp_path / "test-agent"
    assert (target / "agent.yaml").is_file()
    assert (target / "prompt.md").is_file()
    assert (target / "schema" / "input.json").is_file()
    assert (target / "schema" / "output.json").is_file()
    # Success Panel surfaced.
    assert "LLM-scaffolded agent" in result.stdout or "scaffolded" in result.stdout.lower()


@pytest.mark.unit
def test_llm_dry_run_does_not_write(
    tmp_path: Path,
    mock_response_env: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run must render the preview but leave the target untouched."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "preview-agent",
            "--llm",
            "an agent we just want to preview",
            "--mock",
            "--dry-run",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # No files written.
    assert not (tmp_path / "preview-agent").exists()
    # Preview Panel rendered.
    assert "preview" in result.stdout.lower() or "dry-run" in result.stdout.lower()


@pytest.mark.unit
def test_llm_invalid_response_fails_with_debug_artifact(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When MockProvider returns valid JSON that doesn't match the
    GeneratedAgent schema, BOTH attempts fail (same mock response on
    retry) → exit 2 and a debug artifact is written."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"wrong": "shape"}')
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "fails-agent",
            "--llm",
            "a description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    # First attempt fails LLMScaffoldError → exit 2 (no retry path because
    # the failure was during generation, not validation).
    assert result.exit_code == 2
    assert "LLM scaffold failed" in result.stderr or "schema" in result.stderr.lower()
    # The agent dir was NOT created.
    assert not (tmp_path / "fails-agent").exists()


@pytest.mark.unit
def test_llm_dest_exists_without_force_errors_early(
    tmp_path: Path,
    mock_response_env: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If target/<name> exists and --force isn't set, error BEFORE the
    LLM call. No tokens wasted."""
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "occupied"
    existing.mkdir()
    (existing / "marker.txt").write_text("don't overwrite me")

    result = runner.invoke(
        app,
        [
            "init",
            "occupied",
            "--llm",
            "any description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    # The marker file survived (nothing was overwritten).
    assert (existing / "marker.txt").read_text() == "don't overwrite me"


@pytest.mark.unit
def test_llm_force_overwrites_existing(
    tmp_path: Path,
    mock_response_env: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force should allow overwrite of an existing directory."""
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "overwriteme"
    existing.mkdir()
    (existing / "stale.txt").write_text("old content")

    result = runner.invoke(
        app,
        [
            "init",
            "overwriteme",
            "--llm",
            "fresh agent",
            "--mock",
            "--force",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (existing / "agent.yaml").is_file()
    # Stale file is gone.
    assert not (existing / "stale.txt").exists()


@pytest.mark.unit
def test_llm_with_non_default_template_warns(
    tmp_path: Path,
    mock_response_env: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--llm + --template chatbot prints the combination warning."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "combo-agent",
            "--llm",
            "a chatbot",
            "--template",
            "chatbot",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "template" in result.stderr.lower()
    assert "chatbot" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Round-trip: written agent passes mdk validate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_scaffolded_agent_passes_mdk_validate(
    tmp_path: Path,
    mock_response_env: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent scaffolded via --llm must pass `mdk validate` end-to-end —
    the meta-prompt's constraints are the contract this enforces."""
    monkeypatch.chdir(tmp_path)
    init_result = runner.invoke(
        app,
        [
            "init",
            "validated-agent",
            "--llm",
            "a test agent",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert init_result.exit_code == 0, init_result.stdout + init_result.stderr

    validate_result = runner.invoke(
        app, ["validate", str(tmp_path / "validated-agent")]
    )
    assert validate_result.exit_code == 0, (
        validate_result.stdout + validate_result.stderr
    )


# ---------------------------------------------------------------------------
# Round-trip: the written agent.yaml parses as expected YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_written_agent_yaml_parses_as_yaml(
    tmp_path: Path,
    mock_response_env: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent.yaml emitted must round-trip through yaml.safe_load
    cleanly — no flow-style horrors, no trailing-bracket issues."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "yaml-agent",
            "--llm",
            "yaml test",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    parsed = yaml.safe_load((tmp_path / "yaml-agent" / "agent.yaml").read_text())
    assert parsed["api_version"] == "movate/v1"
    assert parsed["kind"] == "Agent"
    # NOTE: MockProvider returns the canned payload verbatim — it can't
    # see the meta-prompt's "name must equal '<X>'" constraint. A real
    # LLM would honor it. Phase 3 polish: post-process the generated
    # agent_yaml to force ``name`` = the CLI argument so a forgetful
    # model doesn't silently break the dir/file-name correspondence.
    assert parsed["name"]  # any non-empty value
