"""F3 (#112): grounding-intent detection → RAG scaffold in `mdk init --llm`.

When the `--llm` description implies the agent should answer from a
knowledge source (docs, FAQ, policy corpus, a URL, "answer questions
about X"), the scaffolder emits a RAG-shaped agent that works through
the ADR 023 pre-retrieval engine:

* ``agent.yaml`` declares ``skills: [kb-vector-lookup]`` and a
  ``retrieval: {auto_into: context, query_from: question}`` block.
* the input schema has an OPTIONAL ``context: list[string]`` field
  (auto-filled by retrieval) alongside the required question field.
* the prompt answers FROM ``input.context``, cites by index, and
  declines on empty context.

A non-grounding description (classifier / summarizer / transformer)
scaffolds exactly as before — no skills, no retrieval block. These
tests are hermetic: they run entirely through the offline ``--mock``
provider path (no API key, no network), which classifies grounding
intent deterministically the same way the meta-prompt asks the real
LLM to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent
from movate.providers.mock import (
    _build_scaffold_response,
    _looks_like_grounding_description,
    _parse_scaffold_description,
)
from movate.scaffold.llm_scaffold import (
    _EXAMPLE_CLASSIFIER,
    _EXAMPLE_EXTRACTION,
    _EXAMPLE_FAQ,
    _EXAMPLE_RAG,
    _EXAMPLE_SUMMARIZER,
    _META_PROMPT,
)

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _scaffold(
    *, name: str, description: str, target: Path, monkeypatch: pytest.MonkeyPatch
) -> object:
    """Run `mdk init <name> --llm <description> --mock --target <target>`."""
    monkeypatch.chdir(target)
    return runner.invoke(
        app,
        [
            "init",
            name,
            "--llm",
            description,
            "--mock",
            "--target",
            str(target),
        ],
    )


# ---------------------------------------------------------------------------
# Unit: grounding-intent detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroundingDetection:
    @pytest.mark.parametrize(
        "description",
        [
            "answer questions about our help docs",
            "an FAQ agent for our pricing policies",
            "answer customer questions based on our documentation",
            "look things up in our internal knowledge base",
            "a grounded QA agent over the company handbook",
            "answer questions from the wiki",
        ],
    )
    def test_grounding_descriptions_detected(self, description: str) -> None:
        assert _looks_like_grounding_description(description) is True

    @pytest.mark.parametrize(
        "description",
        [
            "classify short text into sentiment labels",
            "summarize a block of text into N words",
            "extract structured fields from an invoice",
            "translate English to French",
            "echo the user's input back to them",
        ],
    )
    def test_non_grounding_descriptions_not_detected(self, description: str) -> None:
        assert _looks_like_grounding_description(description) is False

    def test_url_in_description_is_grounding_intent(self) -> None:
        """A URL implies "answer from this source" → grounding (F5/F7
        will close the ingest loop; F3 only needs to RECOGNIZE intent)."""
        assert _looks_like_grounding_description(
            "answer questions based on https://example.com/faq"
        )
        assert _looks_like_grounding_description("summarize content from www.docs.example.com")

    def test_parse_description_from_meta_prompt(self) -> None:
        """The mock pulls the operator's description back out of the
        scaffold meta-prompt to classify it."""
        prompt = _META_PROMPT.format(
            description="answer questions about our help docs",
            name="docs-qa",
            target_model="openai/gpt-4o-mini-2024-07-18",
            example_faq=_EXAMPLE_FAQ,
            example_classifier=_EXAMPLE_CLASSIFIER,
            example_summarizer=_EXAMPLE_SUMMARIZER,
            example_extraction=_EXAMPLE_EXTRACTION,
            example_rag=_EXAMPLE_RAG,
        )
        assert _parse_scaffold_description(prompt) == "answer questions about our help docs"

    def test_parse_description_missing_marker_returns_empty(self) -> None:
        """A prompt without the USER DESCRIPTION block (e.g. the retry
        prompt) parses to empty → classified non-grounding (safe default)."""
        assert _parse_scaffold_description("no description block here") == ""


# ---------------------------------------------------------------------------
# Unit: synthesized scaffold shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSynthesizedScaffoldShape:
    def test_rag_exemplar_is_valid_and_rag_shaped(self) -> None:

        payload = json.loads(_EXAMPLE_RAG)
        ay = payload["agent_yaml"]
        assert ay["skills"] == ["kb-vector-lookup"]
        assert ay["retrieval"]["auto_into"] == "context"
        # context is OPTIONAL — auto-filled by pre-retrieval.
        assert "context" not in payload["input_schema"]["required"]
        assert "context" in payload["input_schema"]["properties"]

    def test_mock_grounding_payload_is_rag_shaped(self) -> None:

        payload = json.loads(_build_scaffold_response("docs-qa", grounding=True))
        ay = payload["agent_yaml"]
        assert ay["skills"] == ["kb-vector-lookup"]
        assert ay["retrieval"] == {"auto_into": "context", "query_from": "question"}
        assert "context" not in payload["input_schema"]["required"]
        ctx = payload["input_schema"]["properties"]["context"]
        assert ctx["type"] == "array"
        assert ctx["items"]["type"] == "string"
        # Prompt grounds on input.context and declines on empty.
        assert "input.context" in payload["prompt_md"]
        assert "grounded" in payload["prompt_md"]

    def test_mock_non_grounding_payload_has_no_rag_keys(self) -> None:
        """Regression guard: the generic scaffold carries no skills /
        retrieval keys — unchanged from the pre-F3 shape."""

        payload = json.loads(_build_scaffold_response("echo-agent", grounding=False))
        assert "skills" not in payload["agent_yaml"]
        assert "retrieval" not in payload["agent_yaml"]

    def test_default_grounding_arg_is_non_grounding(self) -> None:
        """`_build_scaffold_response(name)` without the keyword arg yields
        the original generic shape — back-compat for any existing caller."""
        assert _build_scaffold_response("x") == _build_scaffold_response("x", grounding=False)


# ---------------------------------------------------------------------------
# CLI end-to-end — grounding scaffold (the F3 happy path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGroundingScaffoldEndToEnd:
    def test_grounding_description_scaffolds_rag_agent(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _scaffold(
            name="docs-qa",
            description="answer questions about our help docs",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        agent_yaml = tmp_path / "docs-qa" / "agent.yaml"
        assert agent_yaml.is_file()
        spec = yaml.safe_load(agent_yaml.read_text())

        # RAG shape: skills + retrieval block.
        assert spec["skills"] == ["kb-vector-lookup"]
        assert spec["retrieval"]["auto_into"] == "context"
        assert spec["retrieval"]["query_from"] == "question"

        # Optional-context input schema (canonical layout: YAML file, #127).
        input_schema = yaml.safe_load((tmp_path / "docs-qa" / "schema" / "input.yaml").read_text())
        assert "context" in input_schema["properties"]
        assert "question" in input_schema["properties"]
        # `context` is auto-filled by pre-retrieval → NOT required.
        assert "context" not in input_schema["required"]

        # Grounded prompt mentions context + grounding.
        prompt = (tmp_path / "docs-qa" / "prompt.md").read_text()
        assert "input.context" in prompt

        # The built-in skill was provisioned into the project skills/ dir.
        assert (tmp_path / "skills" / "kb-vector-lookup").is_dir()

    def test_grounding_scaffold_passes_load_agent(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The written RAG scaffold loads cleanly — skill resolution + the
        ADR 023 retrieval cross-link both resolve."""
        result = _scaffold(
            name="policy-bot",
            description="answer questions about our HR policies",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        bundle = load_agent(tmp_path / "policy-bot")
        assert bundle.spec.retrieval.auto_retrieval_enabled is True
        assert bundle.spec.retrieval.auto_into == "context"
        assert {s.spec.name for s in bundle.skills} == {"kb-vector-lookup"}

    def test_grounding_scaffold_passes_mdk_validate(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end `mdk validate` — including ADR 023's load-time
        retrieval checks (skill resolves, auto_into field shape)."""
        init_result = _scaffold(
            name="kb-bot",
            description="answer questions based on our documentation",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert init_result.exit_code == 0, init_result.stdout + init_result.stderr
        validate_result = runner.invoke(app, ["validate", str(tmp_path / "kb-bot")])
        assert validate_result.exit_code == 0, validate_result.stdout + validate_result.stderr

    def test_url_description_scaffolds_rag_agent(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A URL in the description is recognized as grounding intent and
        produces the RAG shape (ingest itself is deferred to F7)."""
        result = _scaffold(
            name="site-qa",
            description="answer questions based on https://example.com/docs",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "site-qa" / "agent.yaml").read_text())
        assert spec["skills"] == ["kb-vector-lookup"]
        assert spec["retrieval"]["auto_into"] == "context"

    def test_mock_grounding_scaffold_is_deterministic(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two `--mock` runs of the same grounding description produce
        byte-identical agent.yaml — the offline path is deterministic."""
        r1 = _scaffold(
            name="det-a",
            description="answer questions about our help docs",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        r2 = _scaffold(
            name="det-b",
            description="answer questions about our help docs",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert r1.exit_code == 0 and r2.exit_code == 0
        a = yaml.safe_load((tmp_path / "det-a" / "agent.yaml").read_text())
        b = yaml.safe_load((tmp_path / "det-b" / "agent.yaml").read_text())
        # Same retrieval + skills regardless of the name coercion.
        assert a["skills"] == b["skills"] == ["kb-vector-lookup"]
        assert a["retrieval"] == b["retrieval"]


# ---------------------------------------------------------------------------
# CLI end-to-end — non-grounding regression guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonGroundingRegression:
    @pytest.mark.parametrize(
        ("name", "description"),
        [
            ("sentiment", "classify short text into sentiment labels"),
            ("tldr", "summarize a block of text into a short paragraph"),
            ("xform", "transform input JSON into a normalized output shape"),
        ],
    )
    def test_non_grounding_scaffold_has_no_rag_block(
        self,
        name: str,
        description: str,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _scaffold(
            name=name, description=description, target=tmp_path, monkeypatch=monkeypatch
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / name / "agent.yaml").read_text())
        assert "skills" not in spec
        assert "retrieval" not in spec
        # No skills/ dir is created for a non-grounding scaffold.
        assert not (tmp_path / "skills").exists()

    def test_non_grounding_scaffold_still_loads_and_validates(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _scaffold(
            name="classifier-agent",
            description="classify short text into sentiment labels",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        bundle = load_agent(tmp_path / "classifier-agent")
        # Pre-retrieval is OFF for the non-grounding path.
        assert bundle.spec.retrieval.auto_retrieval_enabled is False
        assert bundle.skills == []
        validate_result = runner.invoke(app, ["validate", str(tmp_path / "classifier-agent")])
        assert validate_result.exit_code == 0, validate_result.stdout + validate_result.stderr
