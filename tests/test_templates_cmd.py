"""``mdk templates`` CLI — list / show / --json (ADR 028).

Covers:
* ``mdk templates list`` renders every template + the new workflow_starter.
* ``mdk templates list --json`` emits a stable JSON contract.
* ``mdk templates list --shape workflow`` filters to workflow templates.
* ``mdk templates show <name>`` renders metadata + file tree.
* ``mdk templates show <name> --json`` round-trips one record.
* ``mdk templates show <bad>`` exits non-zero with a clear message.

The legacy fallback (template missing ``template.yaml``) is exercised
indirectly: the renderer never crashes when one is absent; we don't
remove ``template.yaml`` files in tests (rule 5 — ADR 028 makes them
part of the template contract).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import TEMPLATES, WORKFLOW_TEMPLATES

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTemplatesList:
    def test_list_renders_every_agent_template(self) -> None:
        """The list table includes every name in TEMPLATES so operators
        don't have to grep the registry."""
        result = runner.invoke(app, ["templates", "list"], env={"COLUMNS": "240"})
        assert result.exit_code == 0, result.stdout + result.stderr
        for name in TEMPLATES:
            assert name in result.stdout, f"agent template {name!r} missing from list"

    def test_list_renders_workflow_starter(self) -> None:
        """Workflow templates surface alongside agents (ADR 028)."""
        result = runner.invoke(app, ["templates", "list"], env={"COLUMNS": "240"})
        assert result.exit_code == 0, result.stdout + result.stderr
        for name in WORKFLOW_TEMPLATES:
            assert name in result.stdout, f"workflow template {name!r} missing"

    def test_list_footer_points_at_init_and_add(self) -> None:
        result = runner.invoke(app, ["templates", "list"], env={"COLUMNS": "240"})
        assert "mdk init" in result.stdout
        assert "mdk add" in result.stdout

    def test_list_json_is_a_sorted_array_of_records(self) -> None:
        """``--json`` emits a stable, sorted-by-name JSON contract."""
        result = runner.invoke(app, ["templates", "list", "--json"], env={"COLUMNS": "240"})
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        names = [row["name"] for row in payload]
        assert names == sorted(names), "JSON output must be name-sorted"
        # Every record has the documented keys.
        for row in payload:
            assert set(row) == {
                "name",
                "title",
                "description",
                "tags",
                "shape",
                "recommended_for",
                "directory",
            }, f"unexpected JSON keys: {set(row)}"
            assert row["shape"] in {"agent", "workflow"}
            assert isinstance(row["tags"], list)

    def test_list_json_includes_workflow_starter(self) -> None:
        result = runner.invoke(app, ["templates", "list", "--json"], env={"COLUMNS": "240"})
        payload = json.loads(result.stdout)
        names = {row["name"] for row in payload}
        for wf in WORKFLOW_TEMPLATES:
            assert wf in names, f"workflow {wf!r} missing from JSON"

    def test_list_shape_filter_workflow_only(self) -> None:
        result = runner.invoke(
            app,
            ["templates", "list", "--shape", "workflow", "--json"],
            env={"COLUMNS": "240"},
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload, "filter returned no rows — workflow_starter must exist"
        assert all(row["shape"] == "workflow" for row in payload)

    def test_list_shape_filter_rejects_unknown(self) -> None:
        result = runner.invoke(
            app, ["templates", "list", "--shape", "bogus"], env={"COLUMNS": "240"}
        )
        assert result.exit_code != 0
        # Message lands on stderr per CLI convention.
        assert "shape" in (result.stderr + result.stdout).lower()


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTemplatesShow:
    def test_show_renders_metadata_and_tree(self) -> None:
        result = runner.invoke(app, ["templates", "show", "faq"], env={"COLUMNS": "240"})
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "faq" in result.stdout
        # Metadata sections render.
        assert "description" in result.stdout
        assert "tags" in result.stdout
        assert "recommended for" in result.stdout
        # The footer points at the natural next step.
        assert "mdk init" in result.stdout
        # File tree includes the canonical files for the template.
        assert "agent.yaml" in result.stdout
        assert "prompt.md" in result.stdout

    def test_show_workflow_starter_renders(self) -> None:
        """Workflow templates also resolve via show."""
        result = runner.invoke(
            app, ["templates", "show", "workflow-starter"], env={"COLUMNS": "240"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "workflow" in result.stdout.lower()
        assert "workflow.yaml" in result.stdout

    def test_show_json_record_round_trips(self) -> None:
        result = runner.invoke(
            app,
            ["templates", "show", "faq", "--json"],
            env={"COLUMNS": "240"},
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        record = json.loads(result.stdout)
        assert record["name"] == "faq"
        assert record["shape"] == "agent"
        assert record["title"]
        assert record["description"]
        assert isinstance(record["tags"], list)

    def test_show_unknown_template_exits_one(self) -> None:
        result = runner.invoke(
            app, ["templates", "show", "nope-not-a-template"], env={"COLUMNS": "240"}
        )
        assert result.exit_code == 1
        # The error message names the failure clearly.
        combined = result.stdout + result.stderr
        assert "unknown template" in combined.lower() or "nope-not-a-template" in combined
