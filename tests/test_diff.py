"""``movate diff`` — core comparison + CLI integration.

The matrix below covers the contracts the renderers depend on:

* identical agents → ``has_any_change() is False``
* metadata-only changes (name / version / model.provider) → field_deltas reflect them
* prompt-only changes → prompt_changed True; unified diff is non-empty
* schema-only changes → input/output_schema_changed True
* dataset-only changes → dataset_changed True
* load failures → AgentDiffError surfaces, exit code 2 from CLI
* output mode switches (table / json / markdown) all render
* ``--fail-on-change`` exits 1 when diffs exist
* ``--prompt-only`` and ``--schemas-only`` are mutually exclusive
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.diff import (
    AgentDiffError,
    diff_agents,
    render_diff_json,
    render_diff_markdown,
)
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_a(tmp_path: Path) -> Path:
    return scaffold_agent(tmp_path / "a", name="agent-a")


@pytest.fixture
def agent_b(tmp_path: Path) -> Path:
    return scaffold_agent(tmp_path / "b", name="agent-b")


def _replace_in_yaml(agent_dir: Path, old: str, new: str) -> None:
    yaml = agent_dir / "agent.yaml"
    yaml.write_text(yaml.read_text().replace(old, new))


# ---------------------------------------------------------------------------
# Core: identical / unchanged paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_identical_agents_have_no_change(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    d = diff_agents(a, b)
    # Names equal (we passed the same name); every field delta unchanged;
    # no prompt / schema / dataset change.
    assert not d.has_any_change()
    assert not any(x.changed for x in d.field_deltas)
    assert not d.prompt_changed
    assert not d.input_schema_changed
    assert not d.output_schema_changed
    assert not d.dataset_changed


@pytest.mark.unit
def test_same_directory_diffed_against_itself_is_unchanged(agent_a: Path) -> None:
    d = diff_agents(agent_a, agent_a)
    assert not d.has_any_change()


# ---------------------------------------------------------------------------
# Core: metadata deltas
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_name_delta_surfaces_in_field_deltas(agent_a: Path, agent_b: Path) -> None:
    d = diff_agents(agent_a, agent_b)
    name_delta = next(x for x in d.field_deltas if x.name == "name")
    assert name_delta.changed
    assert name_delta.a == "agent-a"
    assert name_delta.b == "agent-b"
    assert d.has_any_change()


@pytest.mark.unit
def test_version_delta(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    _replace_in_yaml(b, "version: 0.1.0", "version: 0.2.0")
    d = diff_agents(a, b)
    v = next(x for x in d.field_deltas if x.name == "version")
    assert v.changed and v.a == "0.1.0" and v.b == "0.2.0"


@pytest.mark.unit
def test_changed_field_deltas_excludes_unchanged(agent_a: Path, agent_b: Path) -> None:
    d = diff_agents(agent_a, agent_b)
    changed = d.changed_field_deltas()
    # Only `name` differs between the default scaffolds.
    assert [x.name for x in changed] == ["name"]


# ---------------------------------------------------------------------------
# Core: prompt diff
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prompt_diff_detects_content_change(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    # Append a line to b's prompt — hash should diverge.
    prompt_b = b / "prompt.md"
    prompt_b.write_text(prompt_b.read_text() + "\n\nExtra instruction at the end.\n")

    d = diff_agents(a, b)
    assert d.prompt_changed
    assert d.a_prompt_hash != d.b_prompt_hash
    diff_text = d.prompt_unified_diff()
    assert diff_text  # non-empty
    assert "Extra instruction at the end." in diff_text
    assert "--- demo/prompt" in diff_text
    assert "+++ demo/prompt" in diff_text


@pytest.mark.unit
def test_prompt_unified_diff_empty_when_unchanged(agent_a: Path) -> None:
    d = diff_agents(agent_a, agent_a)
    assert d.prompt_unified_diff() == ""


# ---------------------------------------------------------------------------
# Core: schema diff
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_input_schema_change_detected(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    # Scaffold layout uses `schema/input.json` and `schema/output.json`.
    schema = b / "schema" / "input.json"
    payload = json.loads(schema.read_text())
    payload.setdefault("properties", {})["added_field"] = {"type": "string"}
    schema.write_text(json.dumps(payload, indent=2))

    d = diff_agents(a, b)
    assert d.input_schema_changed
    assert "added_field" in d.schema_unified_diff("input")


@pytest.mark.unit
def test_output_schema_change_detected(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    schema = b / "schema" / "output.json"
    payload = json.loads(schema.read_text())
    # Add a required field — semantic breaking change.
    payload.setdefault("required", []).append("new_required")
    payload.setdefault("properties", {})["new_required"] = {"type": "string"}
    schema.write_text(json.dumps(payload, indent=2))

    d = diff_agents(a, b)
    assert d.output_schema_changed
    assert "new_required" in d.schema_unified_diff("output")


@pytest.mark.unit
def test_schema_unified_diff_unknown_which_rejected(agent_a: Path) -> None:
    d = diff_agents(agent_a, agent_a)
    with pytest.raises(ValueError, match="unknown schema"):
        d.schema_unified_diff("something")


# ---------------------------------------------------------------------------
# Core: dataset diff
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dataset_change_detected_when_case_count_changes(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    ds_b = b / "evals" / "dataset.jsonl"
    # Append one more case to b's dataset.
    ds_b.write_text(ds_b.read_text() + json.dumps({"input": {"question": "extra"}}) + "\n")

    d = diff_agents(a, b)
    assert d.dataset_changed
    assert d.a_dataset is not None and d.b_dataset is not None
    assert d.b_dataset.case_count == d.a_dataset.case_count + 1


# ---------------------------------------------------------------------------
# Core: load failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_diff_raises_when_one_side_missing(tmp_path: Path, agent_a: Path) -> None:
    missing = tmp_path / "nowhere"
    with pytest.raises(AgentDiffError, match="failed to load"):
        diff_agents(agent_a, missing)


# ---------------------------------------------------------------------------
# Renderers — JSON / Markdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_json_is_valid_json(agent_a: Path, agent_b: Path) -> None:
    d = diff_agents(agent_a, agent_b)
    payload = json.loads(render_diff_json(d))
    assert payload["a"]["name"] == "agent-a"
    assert payload["b"]["name"] == "agent-b"
    assert payload["has_any_change"] is True
    # All field rows are present, with changed=bool.
    field_names = [row["name"] for row in payload["fields"]]
    assert "model.provider" in field_names
    assert all(isinstance(row["changed"], bool) for row in payload["fields"])


@pytest.mark.unit
def test_render_markdown_no_diff_path(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    md = render_diff_markdown(diff_agents(a, b))
    # Empty-diff path is a single "no differences" line — not a metadata table.
    assert "_No differences detected._" in md
    assert "| field |" not in md


@pytest.mark.unit
def test_render_markdown_with_diff_includes_table(agent_a: Path, agent_b: Path) -> None:
    md = render_diff_markdown(diff_agents(agent_a, agent_b))
    assert "## `movate diff` — agent-a → agent-b" in md
    assert "| field | before | after |" in md
    assert "`name`" in md


@pytest.mark.unit
def test_markdown_cell_escapes_pipes(tmp_path: Path) -> None:
    """A description containing a literal pipe would break the GFM
    column. The renderer must escape it."""
    a = scaffold_agent(tmp_path / "a", name="demo")
    b = scaffold_agent(tmp_path / "b", name="demo")
    _replace_in_yaml(
        b,
        "description: A new movate agent",
        'description: "has | a pipe"',
    )
    md = render_diff_markdown(diff_agents(a, b))
    # `|` in the cell must be backslash-escaped to keep table alignment.
    assert "has \\| a pipe" in md


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_diff_help_renders() -> None:
    r = runner.invoke(cli_app, ["diff", "--help"])
    assert r.exit_code == 0
    plain = r.stdout
    assert "diff" in plain.lower()
    assert "--prompt-only" in plain.replace("\n", "").replace(" ", "")
    assert "--fail-on-change" in plain.replace("\n", "").replace(" ", "")


@pytest.mark.unit
def test_cli_diff_no_diff_exits_0(agent_a: Path) -> None:
    r = runner.invoke(cli_app, ["diff", str(agent_a), str(agent_a)])
    assert r.exit_code == 0
    assert "no differences" in r.stdout.lower()


@pytest.mark.unit
def test_cli_diff_with_changes_exits_0_by_default(agent_a: Path, agent_b: Path) -> None:
    r = runner.invoke(cli_app, ["diff", str(agent_a), str(agent_b)])
    assert r.exit_code == 0


@pytest.mark.unit
def test_cli_diff_fail_on_change_exits_1(agent_a: Path, agent_b: Path) -> None:
    r = runner.invoke(
        cli_app, ["diff", str(agent_a), str(agent_b), "--fail-on-change"]
    )
    assert r.exit_code == 1


@pytest.mark.unit
def test_cli_diff_fail_on_change_no_change_still_0(agent_a: Path) -> None:
    r = runner.invoke(
        cli_app, ["diff", str(agent_a), str(agent_a), "--fail-on-change"]
    )
    assert r.exit_code == 0


@pytest.mark.unit
def test_cli_diff_json_output_is_parseable(agent_a: Path, agent_b: Path) -> None:
    r = runner.invoke(cli_app, ["diff", str(agent_a), str(agent_b), "-o", "json"])
    assert r.exit_code == 0
    payload = json.loads(r.stdout)
    assert payload["has_any_change"] is True


@pytest.mark.unit
def test_cli_diff_markdown_output(agent_a: Path, agent_b: Path) -> None:
    r = runner.invoke(cli_app, ["diff", str(agent_a), str(agent_b), "-o", "markdown"])
    assert r.exit_code == 0
    assert "## `movate diff`" in r.stdout


@pytest.mark.unit
def test_cli_diff_load_failure_exits_2(tmp_path: Path, agent_a: Path) -> None:
    missing = tmp_path / "nowhere"
    r = runner.invoke(cli_app, ["diff", str(agent_a), str(missing)])
    assert r.exit_code == 2


@pytest.mark.unit
def test_cli_diff_prompt_only_and_schemas_only_mutually_exclusive(
    agent_a: Path, agent_b: Path,
) -> None:
    r = runner.invoke(
        cli_app,
        ["diff", str(agent_a), str(agent_b), "--prompt-only", "--schemas-only"],
    )
    assert r.exit_code == 2
    assert "mutually exclusive" in r.stdout.lower()


@pytest.mark.unit
def test_cli_diff_prompt_only_suppresses_metadata(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="alpha")
    b = scaffold_agent(tmp_path / "b", name="beta")
    # Change the prompt in b so there's a prompt diff to render.
    p = b / "prompt.md"
    p.write_text(p.read_text() + "\nextra\n")

    r = runner.invoke(cli_app, ["diff", str(a), str(b), "--prompt-only"])
    assert r.exit_code == 0
    # Prompt section renders; metadata "Metadata" table does NOT.
    assert "Prompt" in r.stdout
    assert "Metadata" not in r.stdout
