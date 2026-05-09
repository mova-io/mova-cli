"""Every packaged template must scaffold, validate, and run end-to-end with --mock."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.templates import TEMPLATES, get_template_path, list_templates
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

# Per-template canonical input + an output the MockProvider can return that
# satisfies that template's output schema. Keep these in sync with the
# template directories.
CANONICAL: dict[str, tuple[dict, str]] = {
    "default": (
        {"text": "hello"},
        '{"message": "ok"}',
    ),
    "faq": (
        {"question": "What is movate?"},
        '{"answer": "A platform for agents.", "confidence": 0.9}',
    ),
    "summarizer": (
        {"text": "One two three four five six seven eight.", "max_words": 5},
        '{"summary": "Eight words counted briefly here.", "word_count": 5}',
    ),
    "classifier": (
        {
            "text": "I loved this movie!",
            "labels": ["positive", "negative", "neutral"],
        },
        '{"label": "positive"}',
    ),
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_template_registry_exposes_all_known() -> None:
    assert set(TEMPLATES.keys()) == {"default", "faq", "summarizer", "classifier"}
    assert list_templates() == sorted(TEMPLATES.keys())


@pytest.mark.unit
@pytest.mark.parametrize("name", list(TEMPLATES.keys()))
def test_template_dir_is_present_and_complete(name: str) -> None:
    """Every template ships with the four files a loader expects."""
    path = get_template_path(name)
    assert (path / "agent.yaml").is_file()
    assert (path / "prompt.md").is_file()
    assert (path / "schema" / "input.json").is_file()
    assert (path / "schema" / "output.json").is_file()
    assert (path / "evals" / "dataset.jsonl").is_file()


@pytest.mark.unit
def test_template_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown template"):
        get_template_path("nope")


# ---------------------------------------------------------------------------
# Scaffold + load each template
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("template", list(TEMPLATES.keys()))
def test_scaffold_each_template_loads(template: str, tmp_path: Path) -> None:
    """Scaffolded directory must validate via the loader."""
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    bundle = load_agent(dst)
    assert bundle.spec.api_version == "movate/v1"
    assert bundle.spec.kind == "Agent"
    assert bundle.spec.name == "demo"


@pytest.mark.unit
@pytest.mark.parametrize("template", list(TEMPLATES.keys()))
def test_template_dataset_is_well_formed_jsonl(template: str, tmp_path: Path) -> None:
    """Every dataset row parses and has both 'input' and 'expected' keys."""
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    raw = (dst / "evals" / "dataset.jsonl").read_bytes().decode().splitlines()
    rows = [json.loads(line) for line in raw if line.strip()]
    assert len(rows) >= 1
    for row in rows:
        assert "input" in row
        assert "expected" in row


# ---------------------------------------------------------------------------
# End-to-end execution per template (mock provider, canonical input)
# ---------------------------------------------------------------------------


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


@pytest.mark.unit
@pytest.mark.parametrize("template", list(TEMPLATES.keys()))
async def test_template_runs_end_to_end_with_mock(
    template: str,
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    bundle = load_agent(dst)

    payload, mock_response = CANONICAL[template]
    provider = MockProvider(response=mock_response)
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    response = await executor.execute(bundle, RunRequest(agent="demo", input=payload))
    assert response.status == "success", f"{template} failed: {response.error}"
    # Output validates against template's schema
    assert response.data == json.loads(mock_response)


# ---------------------------------------------------------------------------
# Optional judge.yaml.example presence
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("template", ["faq", "summarizer"])
def test_subjective_templates_ship_judge_example(template: str, tmp_path: Path) -> None:
    dst = tmp_path / template
    scaffold_agent(dst, name="demo", template=template)
    assert (dst / "evals" / "judge.yaml.example").is_file()


@pytest.mark.unit
def test_classifier_does_not_need_judge_example(tmp_path: Path) -> None:
    """Exact-match works for finite-label classifiers; no judge needed."""
    dst = tmp_path / "classifier"
    scaffold_agent(dst, name="demo", template="classifier")
    assert not (dst / "evals" / "judge.yaml.example").exists()
