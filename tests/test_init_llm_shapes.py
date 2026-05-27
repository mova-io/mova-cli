"""F2 (#111): shape/template-aware `mdk init --llm` scaffolding.

The scaffolder maps a natural-language description to a SHAPE whose
output schema + prompt + sample evals match the described intent, instead
of collapsing every agent to a generic ``{answer, confidence}``:

* QA / FAQ        → ``{answer, confidence}``      (the default)
* Classifier      → ``{label, confidence}``
* Summarizer      → ``{summary, key_points[]}``
* Extraction      → structured named fields (nullable values)
* RAG / grounded  → the F3 shape (``skills: [kb-vector-lookup]`` +
                    ``retrieval.auto_into`` + ``{answer, citations,
                    grounded, confidence}``) — REGRESSION-GUARDED here.

Selection is driven two ways that must agree:
  1. the meta-prompt's SHAPE-SELECTION instruction + one exemplar per
     shape (what the real LLM follows), and
  2. ``providers/mock.py``'s lightweight per-shape detector + per-shape
     mock payloads (the deterministic offline ``--mock`` path).

These tests are hermetic — they run entirely through the offline
``--mock`` provider (no API key, no network), which classifies the shape
deterministically the same way the meta-prompt asks the real LLM to.
Every produced scaffold must pass ``load_agent`` + ``mdk validate``, and
no property KEY may carry a ``?`` suffix (the key-suffix optional trap,
which the canonical-schema parser treats as a required field literally
named ``<field>?``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.loader import load_agent
from movate.providers.mock import (
    _build_scaffold_response,
    _detect_shape,
    _looks_like_grounding_description,
)

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _scaffold(*, name: str, description: str, target: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run `mdk init <name> --llm <description> --mock --target <target>`."""
    monkeypatch.chdir(target)
    return runner.invoke(
        app,
        ["init", name, "--llm", description, "--mock", "--target", str(target)],
    )


def _assert_no_key_suffix_question_marks(schema: dict[str, Any]) -> None:
    """Recursively assert no property KEY ends with ``?``.

    The value-suffix shorthand (``confidence: number?``) is fine; a
    key-suffix (``confidence?:``) is the trap — the parser would treat it
    as a REQUIRED field literally named ``confidence?``. JSON Schema output
    should never emit such a key. Walks ``properties`` and ``required``.
    """
    props = schema.get("properties", {})
    assert isinstance(props, dict)
    for key, sub in props.items():
        assert not key.endswith("?"), f"key-suffix '?' leaked into property: {key!r}"
        if isinstance(sub, dict) and sub.get("type") == "object":
            _assert_no_key_suffix_question_marks(sub)
    for req in schema.get("required", []):
        assert not str(req).endswith("?"), f"key-suffix '?' leaked into required: {req!r}"


# ---------------------------------------------------------------------------
# Unit: per-shape detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShapeDetection:
    @pytest.mark.parametrize(
        ("description", "shape"),
        [
            ("classify short text into sentiment labels", "classifier"),
            ("categorize support tickets by topic", "classifier"),
            ("route incoming emails to the right team", "classifier"),
            ("triage bug reports by severity", "classifier"),
            ("detect the sentiment of a product review", "classifier"),
            ("summarize a block of text into a short paragraph", "summarizer"),
            ("condense a long article into a tl;dr", "summarizer"),
            ("produce a brief recap of a meeting transcript", "summarizer"),
            ("extract structured fields from an invoice", "extraction"),
            ("pull out the contact name and email from an email", "extraction"),
            ("parse line items out of a receipt", "extraction"),
            ("answer general trivia questions", "qa"),
            ("a helpful assistant that responds to user questions", "qa"),
            ("translate English to French", "qa"),
        ],
    )
    def test_detect_shape(self, description: str, shape: str) -> None:
        assert _detect_shape(description) == shape

    def test_qa_is_the_default_fallthrough(self) -> None:
        """A description matching no shape marker classifies as QA."""
        assert _detect_shape("do something vaguely useful") == "qa"

    def test_grounding_is_decided_before_shape(self) -> None:
        """Grounding/RAG is checked BEFORE per-shape detection. A
        description that is BOTH grounding AND e.g. summarization
        ("summarize content from our docs") is grounding-first — the
        complete() dispatch checks grounding before _detect_shape."""
        desc = "summarize content from our documentation"
        assert _looks_like_grounding_description(desc) is True
        # _detect_shape itself only covers the non-grounding shapes; the
        # dispatch in complete() never reaches it for a grounding desc.
        assert _detect_shape(desc) == "summarizer"


# ---------------------------------------------------------------------------
# Unit: per-shape mock payloads are valid + shape-correct
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPerShapeMockPayloads:
    def test_qa_payload_shape(self) -> None:
        payload = json.loads(_build_scaffold_response("qa-bot", shape="qa"))
        out = payload["output_schema"]
        assert set(out["required"]) == {"answer", "confidence"}
        assert "skills" not in payload["agent_yaml"]
        assert "retrieval" not in payload["agent_yaml"]
        _assert_no_key_suffix_question_marks(out)
        _assert_no_key_suffix_question_marks(payload["input_schema"])

    def test_classifier_payload_shape(self) -> None:
        payload = json.loads(_build_scaffold_response("cls-bot", shape="classifier"))
        out = payload["output_schema"]
        assert set(out["required"]) == {"label", "confidence"}
        assert out["properties"]["label"]["type"] == "string"
        assert out["properties"]["confidence"]["type"] == "number"
        assert "skills" not in payload["agent_yaml"]
        _assert_no_key_suffix_question_marks(out)

    def test_summarizer_payload_shape(self) -> None:
        payload = json.loads(_build_scaffold_response("sum-bot", shape="summarizer"))
        out = payload["output_schema"]
        assert set(out["required"]) == {"summary", "key_points"}
        assert out["properties"]["summary"]["type"] == "string"
        kp = out["properties"]["key_points"]
        assert kp["type"] == "array"
        assert kp["items"]["type"] == "string"
        # max_words is an OPTIONAL input knob — present but NOT required.
        in_schema = payload["input_schema"]
        assert "max_words" in in_schema["properties"]
        assert "max_words" not in in_schema["required"]
        _assert_no_key_suffix_question_marks(out)
        _assert_no_key_suffix_question_marks(in_schema)

    def test_extraction_payload_shape(self) -> None:
        payload = json.loads(_build_scaffold_response("ext-bot", shape="extraction"))
        out = payload["output_schema"]
        # The output properties ARE the named fields.
        assert set(out["required"]) == {"contact_name", "email", "organization", "intent"}
        # Omittable fields are nullable (value may be null) — but the KEY is
        # required (present-key, null-value), NOT a key-suffix '?'.
        assert out["properties"]["contact_name"]["type"] == ["string", "null"]
        assert out["properties"]["email"]["type"] == ["string", "null"]
        # A sample eval exercises the null path.
        nulled = [e for e in payload["sample_evals"] if e["expected"]["contact_name"] is None]
        assert nulled, "expected a sample eval demonstrating null extraction"
        _assert_no_key_suffix_question_marks(out)

    def test_default_shape_is_qa(self) -> None:
        """`_build_scaffold_response(name)` with no shape/grounding kwargs
        yields the QA shape — back-compat default for F2."""
        default = json.loads(_build_scaffold_response("x"))
        qa = json.loads(_build_scaffold_response("x", shape="qa"))
        assert default == qa
        assert set(default["output_schema"]["required"]) == {"answer", "confidence"}

    def test_unknown_shape_falls_back_to_qa(self) -> None:
        unknown = json.loads(_build_scaffold_response("x", shape="nonsense"))
        qa = json.loads(_build_scaffold_response("x", shape="qa"))
        assert unknown == qa

    @pytest.mark.parametrize("shape", ["qa", "classifier", "summarizer", "extraction"])
    def test_every_shape_payload_validates_as_generated_agent(self, shape: str) -> None:
        """Each shape's mock payload parses as a GeneratedAgent."""
        from movate.scaffold import GeneratedAgent  # noqa: PLC0415

        payload = json.loads(_build_scaffold_response("any-name", shape=shape))
        agent = GeneratedAgent.model_validate(payload)
        assert agent.agent_yaml["name"] == "any-name"


# ---------------------------------------------------------------------------
# Unit: meta-prompt exemplars are valid + shape-correct
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetaPromptExemplars:
    def test_new_shape_exemplars_parse_as_generated_agent(self) -> None:
        """The summarizer + extraction exemplars wired into the meta-prompt
        parse as GeneratedAgent (they are the contract the LLM imitates)."""
        from movate.scaffold import GeneratedAgent  # noqa: PLC0415
        from movate.scaffold.llm_scaffold import (  # noqa: PLC0415
            _EXAMPLE_EXTRACTION,
            _EXAMPLE_SUMMARIZER,
        )

        for raw in (_EXAMPLE_SUMMARIZER, _EXAMPLE_EXTRACTION):
            payload = json.loads(raw)
            GeneratedAgent.model_validate(payload)
            _assert_no_key_suffix_question_marks(payload["output_schema"])
            _assert_no_key_suffix_question_marks(payload["input_schema"])

    def test_summarizer_exemplar_is_summary_shaped(self) -> None:
        from movate.scaffold.llm_scaffold import _EXAMPLE_SUMMARIZER  # noqa: PLC0415

        out = json.loads(_EXAMPLE_SUMMARIZER)["output_schema"]
        assert set(out["required"]) == {"summary", "key_points"}
        assert out["properties"]["key_points"]["type"] == "array"

    def test_extraction_exemplar_has_nullable_fields(self) -> None:
        from movate.scaffold.llm_scaffold import _EXAMPLE_EXTRACTION  # noqa: PLC0415

        out = json.loads(_EXAMPLE_EXTRACTION)["output_schema"]
        # Named fields, all required keys, nullable values.
        assert "contact_name" in out["required"]
        assert out["properties"]["contact_name"]["type"] == ["string", "null"]

    def test_classifier_exemplar_carries_confidence(self) -> None:
        """F2 widened the classifier exemplar from {label} to
        {label, confidence}."""
        from movate.scaffold.llm_scaffold import _EXAMPLE_CLASSIFIER  # noqa: PLC0415

        out = json.loads(_EXAMPLE_CLASSIFIER)["output_schema"]
        assert set(out["required"]) == {"label", "confidence"}


# ---------------------------------------------------------------------------
# CLI end-to-end — each shape scaffolds, loads, and validates
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShapeScaffoldEndToEnd:
    @pytest.mark.parametrize(
        ("name", "description", "required"),
        [
            ("qa-bot", "answer general trivia questions", {"answer", "confidence"}),
            (
                "cls-bot",
                "classify short text into sentiment labels",
                {"label", "confidence"},
            ),
            (
                "sum-bot",
                "summarize a block of text into a short paragraph",
                {"summary", "key_points"},
            ),
            (
                "ext-bot",
                "extract structured fields from a support email",
                {"contact_name", "email", "organization", "intent"},
            ),
        ],
    )
    def test_shape_scaffolds_expected_output_schema(
        self,
        name: str,
        description: str,
        required: set[str],
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _scaffold(
            name=name, description=description, target=tmp_path, monkeypatch=monkeypatch
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        out_schema = json.loads((tmp_path / name / "schema" / "output.json").read_text())
        assert set(out_schema["required"]) == required
        _assert_no_key_suffix_question_marks(out_schema)
        in_schema = json.loads((tmp_path / name / "schema" / "input.json").read_text())
        _assert_no_key_suffix_question_marks(in_schema)

        # Non-grounding shapes carry NO skills / retrieval block, and no
        # skills/ dir is provisioned for them.
        spec = yaml.safe_load((tmp_path / name / "agent.yaml").read_text())
        assert "skills" not in spec
        assert "retrieval" not in spec
        assert not (tmp_path / "skills").exists()

    @pytest.mark.parametrize(
        ("name", "description"),
        [
            ("qa-load", "answer general trivia questions"),
            ("cls-load", "classify short text into sentiment labels"),
            ("sum-load", "summarize a block of text into a short paragraph"),
            ("ext-load", "extract structured fields from a support email"),
        ],
    )
    def test_shape_scaffold_loads_and_validates(
        self,
        name: str,
        description: str,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every produced scaffold passes load_agent + `mdk validate`, and
        pre-retrieval stays OFF for the non-grounding shapes."""
        result = _scaffold(
            name=name, description=description, target=tmp_path, monkeypatch=monkeypatch
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        bundle = load_agent(tmp_path / name)
        assert bundle.spec.retrieval.auto_retrieval_enabled is False
        assert bundle.skills == []
        # Resolved output schema is free of key-suffix '?' fields.
        _assert_no_key_suffix_question_marks(bundle.output_schema)

        validate_result = runner.invoke(app, ["validate", str(tmp_path / name)])
        assert validate_result.exit_code == 0, validate_result.stdout + validate_result.stderr

    def test_shape_scaffold_is_deterministic(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two `--mock` runs of the same description produce the same shape
        (the offline path is deterministic)."""
        r1 = _scaffold(
            name="det-1",
            description="classify text into topic labels",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        r2 = _scaffold(
            name="det-2",
            description="classify text into topic labels",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert r1.exit_code == 0 and r2.exit_code == 0
        a = json.loads((tmp_path / "det-1" / "schema" / "output.json").read_text())
        b = json.loads((tmp_path / "det-2" / "schema" / "output.json").read_text())
        assert set(a["required"]) == set(b["required"]) == {"label", "confidence"}


# ---------------------------------------------------------------------------
# F3 RAG regression — grounding detection + RAG shape unchanged by F2
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestF3RagRegression:
    @pytest.mark.parametrize(
        "description",
        [
            "answer questions about our help docs",
            "an FAQ agent for our pricing policies",
            "answer customer questions based on our documentation",
            "answer questions based on https://example.com/faq",
        ],
    )
    def test_grounding_detection_unchanged(self, description: str) -> None:
        assert _looks_like_grounding_description(description) is True

    def test_rag_mock_payload_unchanged(self) -> None:
        """The grounding mock payload is byte-identical to F3's — F2's
        shape dispatch must not perturb the RAG branch."""
        payload = json.loads(_build_scaffold_response("docs-qa", grounding=True))
        ay = payload["agent_yaml"]
        assert ay["skills"] == ["kb-vector-lookup"]
        assert ay["retrieval"] == {"auto_into": "context", "query_from": "question"}
        assert set(payload["output_schema"]["required"]) == {
            "answer",
            "citations",
            "grounded",
            "confidence",
        }
        # context is OPTIONAL (auto-filled by pre-retrieval).
        assert "context" not in payload["input_schema"]["required"]
        assert "context" in payload["input_schema"]["properties"]
        assert "input.context" in payload["prompt_md"]

    def test_grounding_wins_over_shape_markers(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A description that contains a shape marker AND grounding intent
        ("summarize ... from our docs") scaffolds the RAG shape, not the
        summarizer shape — grounding is decided first."""
        result = _scaffold(
            name="grounded-sum",
            description="summarize content from our documentation",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        spec = yaml.safe_load((tmp_path / "grounded-sum" / "agent.yaml").read_text())
        assert spec["skills"] == ["kb-vector-lookup"]
        assert spec["retrieval"]["auto_into"] == "context"

    def test_grounding_scaffold_still_loads(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _scaffold(
            name="rag-bot",
            description="answer questions about our HR policies",
            target=tmp_path,
            monkeypatch=monkeypatch,
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        bundle = load_agent(tmp_path / "rag-bot")
        assert bundle.spec.retrieval.auto_retrieval_enabled is True
        assert {s.spec.name for s in bundle.skills} == {"kb-vector-lookup"}
