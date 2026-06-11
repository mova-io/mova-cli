"""``mdk patterns`` catalog surface — list / search / info (+ ``--json``).

Covers:
* ``mdk patterns list`` renders every registered pattern + topology.
* ``mdk patterns list --json`` emits the stable catalog contract
  (``{name, kind, description, topology, init_command}`` per record).
* ``mdk patterns search <term>`` filters case-insensitively over
  name + description + topology (hit + miss, table + JSON).
* ``mdk patterns info <name>`` shows the full record + scaffold snippet +
  capped file listing; unknown names exit 2 with a closest-match hint.

The surface is READ-ONLY over :data:`movate.templates.PATTERN_TEMPLATES`;
no scaffolding happens here, so the tests are hermetic by construction.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import PATTERN_TEMPLATES, list_patterns

runner = CliRunner(mix_stderr=False)

# The catalog record contract shared by list/search --json (info adds files).
RECORD_KEYS = ["name", "kind", "description", "topology", "init_command"]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatternsList:
    def test_list_renders_every_pattern(self) -> None:
        """The table includes every registry name so operators never grep."""
        result = runner.invoke(app, ["patterns", "list"], env={"COLUMNS": "300"})
        assert result.exit_code == 0, result.stdout + result.stderr
        for name in PATTERN_TEMPLATES:
            assert name in result.stdout, f"pattern {name!r} missing from list"

    def test_list_surfaces_topology_and_adr(self) -> None:
        result = runner.invoke(app, ["patterns", "list"], env={"COLUMNS": "300"})
        assert "ADR 038" in result.stdout
        assert "SUPERVISOR" in result.stdout  # topology column

    def test_list_json_is_sorted_records_with_stable_shape(self) -> None:
        """``--json`` emits one record per pattern, sorted by name, with the
        exact key set — the catalog contract scripts depend on."""
        result = runner.invoke(app, ["patterns", "list", "--json"], env={"COLUMNS": "300"})
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert [r["name"] for r in payload] == list_patterns()
        for record in payload:
            assert list(record.keys()) == RECORD_KEYS
            assert record["kind"] in {"agent", "workflow"}
            assert record["init_command"] == f"mdk init <target-dir> --pattern {record['name']}"

    def test_list_json_kind_mirrors_registry_bool(self) -> None:
        result = runner.invoke(app, ["patterns", "list", "--json"], env={"COLUMNS": "300"})
        payload = {r["name"]: r for r in json.loads(result.stdout)}
        assert payload["chatbot"]["kind"] == "agent"  # the one single-agent pattern
        assert payload["expense-approval"]["kind"] == "workflow"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatternsSearch:
    def test_search_hit_matches_name_substring(self) -> None:
        result = runner.invoke(app, ["patterns", "search", "expense"], env={"COLUMNS": "300"})
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "expense-approval" in result.stdout
        # Non-matching patterns are filtered out of the table.
        assert "pii-detection" not in result.stdout

    def test_search_is_case_insensitive_over_description(self) -> None:
        """`HUMAN` appears in descriptions/topologies — lowercase query hits."""
        result = runner.invoke(
            app, ["patterns", "search", "human", "--json"], env={"COLUMNS": "300"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        names = {r["name"] for r in json.loads(result.stdout)}
        assert "human-escalation" in names
        assert "approval-timeout" in names  # matched via description/topology

    def test_search_json_records_match_list_shape(self) -> None:
        result = runner.invoke(
            app, ["patterns", "search", "expense", "--json"], env={"COLUMNS": "300"}
        )
        payload = json.loads(result.stdout)
        assert payload, "expected at least one match"
        for record in payload:
            assert list(record.keys()) == RECORD_KEYS

    def test_search_miss_renders_hint_and_exits_zero(self) -> None:
        result = runner.invoke(
            app, ["patterns", "search", "zz-no-such-thing"], env={"COLUMNS": "300"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "No patterns match" in result.stdout
        assert "mdk patterns list" in result.stdout

    def test_search_miss_json_is_empty_array(self) -> None:
        result = runner.invoke(
            app, ["patterns", "search", "zz-no-such-thing", "--json"], env={"COLUMNS": "300"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatternsInfo:
    def test_info_known_pattern_shows_record_and_files(self) -> None:
        result = runner.invoke(
            app, ["patterns", "info", "expense-approval"], env={"COLUMNS": "300"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        out = result.stdout
        assert "expense-approval" in out
        assert "workflow" in out
        assert "DECISION" in out  # topology line
        assert "mdk init <target-dir> --pattern expense-approval" in out
        assert "workflow.yaml" in out  # file listing of the bundle

    def test_info_json_shape_is_record_plus_files(self) -> None:
        result = runner.invoke(
            app, ["patterns", "info", "expense-approval", "--json"], env={"COLUMNS": "300"}
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert list(payload.keys()) == [*RECORD_KEYS, "files"]
        assert payload["name"] == "expense-approval"
        assert payload["kind"] == "workflow"
        assert payload["init_command"] == "mdk init <target-dir> --pattern expense-approval"
        # Files: relative POSIX paths, deduped + sorted, capped at 50.
        assert payload["files"], "expected the bundle to ship files"
        assert len(payload["files"]) <= 50
        assert payload["files"] == sorted(payload["files"])
        for rel in payload["files"]:
            assert not rel.startswith("/"), f"expected relative path, got {rel!r}"

    def test_info_chatbot_is_agent_kind(self) -> None:
        result = runner.invoke(
            app, ["patterns", "info", "chatbot", "--json"], env={"COLUMNS": "300"}
        )
        payload = json.loads(result.stdout)
        assert payload["kind"] == "agent"
        assert "agent.yaml" in payload["files"]

    def test_info_unknown_exits_2_with_closest_match(self) -> None:
        """Typos exit 2 (usage error) and suggest the nearest catalog name."""
        result = runner.invoke(app, ["patterns", "info", "expense-aproval"], env={"COLUMNS": "300"})
        assert result.exit_code == 2, result.stdout + result.stderr
        assert "unknown pattern" in result.stderr
        assert "expense-approval" in result.stderr  # difflib suggestion
        assert "mdk patterns list" in result.stderr

    def test_info_unknown_without_close_match_still_exits_2(self) -> None:
        result = runner.invoke(
            app, ["patterns", "info", "zz-no-such-thing"], env={"COLUMNS": "300"}
        )
        assert result.exit_code == 2
        assert "unknown pattern" in result.stderr
