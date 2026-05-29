"""Per-template ``template.yaml`` metadata + workflow starter integrity (ADR 028).

These tests pin three invariants:

1. Every registered template (agent + workflow) ships a ``template.yaml``
   that :func:`movate.templates.load_template_info` accepts.
2. The required fields (title, description, recommended_for) are
   non-empty for every template — operators don't see blank rows.
3. The workflow_starter template's ``workflow.yaml`` validates via
   :func:`movate.core.workflow.load_workflow_spec`, its agents load, and
   its dataset is well-formed JSONL — i.e. the canonical workflow
   starter actually models a real workflow.

The discovery surface (``mdk templates``) is exercised in
``tests/test_templates_cmd.py``; this file is concerned with the data.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from movate import templates as templates_mod
from movate.core.loader import load_agent
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.templates import (
    TEMPLATES,
    TEMPLATES_DIR,
    WORKFLOW_TEMPLATES,
    TemplateInfo,
    TemplateInfoLoadError,
    list_template_infos,
    list_workflow_templates,
    load_template_info,
)

# ---------------------------------------------------------------------------
# Registry — workflow_starter is wired in
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_starter_registered() -> None:
    """ADR 028 D2 — the workflow starter is reachable via the registry."""
    assert "workflow-starter" in WORKFLOW_TEMPLATES
    assert "workflow-starter" in list_workflow_templates()


@pytest.mark.unit
def test_workflow_registry_separate_from_agent_registry() -> None:
    """Workflow names live in their own registry so the agent-template
    invariants (every TEMPLATES dir has agent.yaml at root) keep
    holding. Regression guard for the ADR 028 design."""
    assert set(WORKFLOW_TEMPLATES).isdisjoint(set(TEMPLATES))


# ---------------------------------------------------------------------------
# template.yaml is present + parseable for every registered template
# ---------------------------------------------------------------------------


_ALL_REGISTERED = sorted({*TEMPLATES, *WORKFLOW_TEMPLATES})


@pytest.mark.unit
@pytest.mark.parametrize("name", _ALL_REGISTERED)
def test_every_template_ships_template_yaml(name: str) -> None:
    """Every shipped template (agent + workflow) has a ``template.yaml``
    that the loader accepts. This is the contract ADR 028 asks for."""
    info = load_template_info(name)
    assert info.title, f"{name}: title is empty"
    assert info.description, f"{name}: description is empty"
    assert info.recommended_for, f"{name}: recommended_for is empty"
    assert info.shape in {"agent", "workflow"}, f"{name}: unexpected shape {info.shape!r}"


@pytest.mark.unit
def test_list_template_infos_returns_every_registered_template() -> None:
    """:func:`list_template_infos` surfaces both agent and workflow templates."""
    infos = list_template_infos(include_workflows=True)
    names = {i.name for i in infos}
    for n in TEMPLATES:
        assert n in names, f"agent template {n!r} missing from list_template_infos"
    for n in WORKFLOW_TEMPLATES:
        assert n in names, f"workflow template {n!r} missing from list_template_infos"


@pytest.mark.unit
def test_list_template_infos_can_exclude_workflows() -> None:
    """Legacy callers can opt out of the new workflow rows."""
    infos = list_template_infos(include_workflows=False)
    names = {i.name for i in infos}
    assert "workflow-starter" not in names
    # Agents are still there.
    assert "faq" in names


@pytest.mark.unit
def test_template_info_to_dict_is_stable() -> None:
    """The ``--json`` contract is the documented shape of
    :meth:`TemplateInfo.to_dict`. Pin it so refactors don't silently
    break script consumers."""
    info: TemplateInfo = load_template_info("faq")
    data = info.to_dict()
    assert set(data) == {
        "name",
        "title",
        "description",
        "tags",
        "shape",
        "recommended_for",
        "directory",
    }
    # Directory is a relative POSIX path under TEMPLATES_DIR.
    assert data["directory"] == "faq_agent"


@pytest.mark.unit
def test_load_template_info_rejects_missing_required_field(tmp_path: Path) -> None:
    """A template.yaml missing a required key fails loud (regression for
    the "silently empty row" footgun)."""
    # Build a fake template dir with a partial template.yaml.
    fake = tmp_path / "fake_template"
    fake.mkdir()
    (fake / "template.yaml").write_text("title: A title\n")  # missing description, recommended_for

    # Monkey-patch the registry briefly so get_template_path resolves.
    templates_mod.TEMPLATES["__fake__"] = fake.name
    original_dir = templates_mod.TEMPLATES_DIR
    templates_mod.TEMPLATES_DIR = tmp_path
    try:
        with pytest.raises(TemplateInfoLoadError, match="description"):
            load_template_info("__fake__")
    finally:
        del templates_mod.TEMPLATES["__fake__"]
        templates_mod.TEMPLATES_DIR = original_dir


# ---------------------------------------------------------------------------
# workflow_starter — the workflow itself is valid + runnable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_starter_workflow_yaml_validates() -> None:
    """``workflow_starter/workflow.yaml`` parses through the real
    workflow spec loader — no special casing for the template."""
    path = TEMPLATES_DIR / "workflow_starter" / "workflow.yaml"
    assert path.is_file(), "workflow_starter is missing its workflow.yaml"
    spec, parent = load_workflow_spec(path)
    assert spec.name == "workflow-starter"
    assert spec.entrypoint == "draft"
    # Compile validates linearity + ref resolution.
    graph = compile_workflow(spec, parent)
    assert {n.id for n in graph.nodes.values()} == {"draft", "review"}


@pytest.mark.unit
def test_workflow_starter_state_schema_present() -> None:
    """state.json — referenced from workflow.yaml — must exist + be JSON."""
    schema_path = TEMPLATES_DIR / "workflow_starter" / "state.json"
    assert schema_path.is_file()
    data = json.loads(schema_path.read_text())
    assert data.get("type") == "object"
    assert "topic" in data.get("properties", {})


@pytest.mark.unit
@pytest.mark.parametrize("agent_name", ["draft", "review"])
def test_workflow_starter_agent_loads(agent_name: str) -> None:
    """Each agent inside workflow_starter loads via the canonical agent
    loader — no special-casing. Replaces the ``__AGENT_NAME__`` token
    first via a tmp copy so the loader's name validator is satisfied."""
    agent_dir = TEMPLATES_DIR / "workflow_starter" / "agents" / agent_name
    assert (agent_dir / "agent.yaml").is_file(), f"{agent_name}: missing agent.yaml"
    # Copy to a tmp location with the placeholder substituted — load_agent
    # rejects the literal ``__AGENT_NAME__`` placeholder.
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / agent_name
        shutil.copytree(agent_dir, dest)
        spec_path = dest / "agent.yaml"
        spec_path.write_text(
            spec_path.read_text().replace("__AGENT_NAME__", f"starter-{agent_name}")
        )
        bundle = load_agent(dest)
        assert bundle.spec.name == f"starter-{agent_name}"


@pytest.mark.unit
def test_workflow_starter_dataset_well_formed() -> None:
    """The workflow-level eval dataset is JSONL with input + expected."""
    raw = (TEMPLATES_DIR / "workflow_starter" / "evals" / "dataset.jsonl").read_text()
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines, "dataset is empty"
    for i, line in enumerate(lines, start=1):
        row = json.loads(line)
        assert "input" in row, f"row {i}: missing input"
        assert "expected" in row, f"row {i}: missing expected"
        # input must carry the workflow's initial state key.
        assert "topic" in row["input"], f"row {i}: missing topic in input"


@pytest.mark.unit
def test_workflow_starter_agents_have_complementary_io() -> None:
    """draft writes the key review reads — the wiring contract of a
    workflow pipeline. Pinned so a refactor that renames the bridge
    key surfaces here rather than at runtime."""
    wf_root = TEMPLATES_DIR / "workflow_starter"
    draft_out = yaml.safe_load(
        (wf_root / "agents" / "draft" / "schema" / "output.yaml").read_text()
    )
    review_in = yaml.safe_load(
        (wf_root / "agents" / "review" / "schema" / "input.yaml").read_text()
    )
    # draft output must include 'draft' (the bridge key).
    assert "draft" in draft_out.get("fields", {}), "draft node must produce a 'draft' field"
    assert "draft" in review_in.get("fields", {}), "review node must accept 'draft' as input"
