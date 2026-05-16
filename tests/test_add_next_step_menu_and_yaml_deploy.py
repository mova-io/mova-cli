"""PR #98 — two related demo-prep fixes.

A. **Interactive "what next?" menu after `mdk add`** (TTY-gated).
   Mirrors `mdk menu` + `mdk eval --guided` visual language: numbered
   list, Rich.Prompt.ask, shells out to the selected command. Skipped
   under non-TTY (CI / piped stdout) so scripted use sees only the
   static Panel + summary line.

B. **Deploy fix: YAML schemas compile to JSON in-flight** so the
   runtime's hard-coded `schema/input.json` + `schema/output.json`
   persistence path receives the expected file. The transient
   agent.yaml uploaded to the runtime also has its `schema.input` /
   `schema.output` paths rewritten from `.yaml` → `.json`. On-disk
   agent.yaml is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.deploy import (
    _maybe_rewrite_agent_yaml_for_upload,
    _schema_bytes_for_upload,
)
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    return tmp_path / "proj"


# ---------------------------------------------------------------------------
# A — interactive menu after mdk add (TTY behavior)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_does_not_render_menu_under_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk add` from a CliRunner (non-TTY) should NOT render the
    'What next?' menu — only the static Panel + summary line, same
    as pre-PR-#98 behavior. Keeps CI / scripts deterministic."""
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # The Panel still renders.
    assert "Added faq agent" in result.stdout
    # But the menu does NOT — keep the canonical static surface for
    # any consumer that parses the output (scripts, CI logs).
    assert "What next?" not in result.stdout


@pytest.mark.unit
def test_add_panel_still_includes_static_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: even on non-TTY (no interactive prompt), the
    next-steps list must keep rendering — operators reading the
    output (esp. in CI logs) still need the commands surfaced.

    Post-PR-#106 the single next-steps surface is the `Next:` block
    printed by the shared menu helper (renders in both modes; only
    prompts under TTY). The legacy `Next steps:` block inside the
    Panel body was dropped — it duplicated the helper's output.
    """
    _bootstrap(tmp_path, monkeypatch)
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Helper-printed Next: surface.
    assert "Next:" in result.stdout
    # The three suggested commands appear in the list.
    assert "mdk run" in result.stdout
    assert "mdk eval" in result.stdout
    assert "mdk doctor agent faq" in result.stdout


# ---------------------------------------------------------------------------
# B — deploy YAML→JSON schema upload helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchemaBytesForUpload:
    def test_json_file_passes_through_untouched(self, tmp_path: Path) -> None:
        """JSON schemas should ship as-is — they're the runtime's
        canonical format already."""
        schema = tmp_path / "input.json"
        body = b'{"type":"object","properties":{"x":{"type":"string"}}}'
        schema.write_bytes(body)
        result_bytes, result_name = _schema_bytes_for_upload(schema, label="input")
        assert result_bytes == body
        assert result_name == "input.json"

    def test_yaml_hand_written_json_schema_serializes_to_json(self, tmp_path: Path) -> None:
        """A YAML file that's literally a JSON Schema (has `$schema` or
        `type: object` + `properties`) should be parsed + re-serialized
        as JSON without going through the shorthand compiler."""
        schema = tmp_path / "input.yaml"
        schema.write_text(
            "$schema: https://json-schema.org/draft/2020-12/schema\n"
            "type: object\n"
            "properties:\n"
            "  text:\n"
            "    type: string\n"
            "    pattern: '^[a-z]+$'\n"
            "required: [text]\n"
            "additionalProperties: false\n"
        )
        result_bytes, result_name = _schema_bytes_for_upload(schema, label="input")
        assert result_name == "input.json"
        data = json.loads(result_bytes)
        # Pattern (a JSON-Schema-only feature, not expressible in
        # shorthand) preserved — proves we didn't run it through
        # compile_shorthand.
        assert data["properties"]["text"]["pattern"] == "^[a-z]+$"
        assert data["type"] == "object"

    def test_yaml_shorthand_compiles_to_json_schema(self, tmp_path: Path) -> None:
        """A YAML file in shorthand form (just field:type pairs)
        should run through compile_shorthand and emit valid JSON
        Schema with the strictness defaults filled in."""
        schema = tmp_path / "input.yaml"
        schema.write_text(
            "$defs:\n"
            "  dim: { score: integer(0..3), rationale: string(1..) }\n"
            "score: integer(0..12)\n"
            "tag: a|b|c\n"
        )
        result_bytes, result_name = _schema_bytes_for_upload(schema, label="input")
        assert result_name == "input.json"
        data = json.loads(result_bytes)
        # Range compiled to minimum/maximum.
        assert data["properties"]["score"]["minimum"] == 0
        assert data["properties"]["score"]["maximum"] == 12
        # Enum compiled.
        assert set(data["properties"]["tag"]["enum"]) == {"a", "b", "c"}
        # $defs preserved.
        assert "dim" in data["$defs"]
        # Strict-by-default — additionalProperties locked.
        assert data["additionalProperties"] is False

    def test_yml_extension_works_same_as_yaml(self, tmp_path: Path) -> None:
        schema = tmp_path / "input.yml"
        schema.write_text("text: string\n")
        result_bytes, result_name = _schema_bytes_for_upload(schema, label="input")
        assert result_name == "input.json"
        data = json.loads(result_bytes)
        assert data["properties"]["text"]["type"] == "string"


@pytest.mark.unit
class TestAgentYamlRewriteForUpload:
    def test_yaml_schema_paths_rewritten_to_json(self, tmp_path: Path) -> None:
        """agent.yaml that declares `schema.input: ./schema/input.yaml`
        should be rewritten to point at `./schema/input.json` for the
        upload, since the runtime persists schemas under canonical
        `.json` names."""
        import yaml as _yaml  # noqa: PLC0415

        agent_yaml = tmp_path / "agent.yaml"
        agent_yaml.write_text(
            "api_version: movate/v1\n"
            "kind: Agent\n"
            "name: demo\n"
            "version: 0.1.0\n"
            "schema:\n"
            "  input: ./schema/input.yaml\n"
            "  output: ./schema/output.yml\n"
        )
        bytes_, rewrote = _maybe_rewrite_agent_yaml_for_upload(
            agent_yaml,
            input_schema=tmp_path / "schema" / "input.yaml",
            output_schema=tmp_path / "schema" / "output.yml",
        )
        assert rewrote is True
        rewritten = _yaml.safe_load(bytes_)
        assert rewritten["schema"]["input"] == "./schema/input.json"
        assert rewritten["schema"]["output"] == "./schema/output.json"

    def test_json_schema_paths_passed_through(self, tmp_path: Path) -> None:
        """agent.yaml already declaring `.json` paths needs no rewrite —
        return the original bytes + rewrote=False."""
        agent_yaml = tmp_path / "agent.yaml"
        original = (
            "api_version: movate/v1\n"
            "schema:\n"
            "  input: ./schema/input.json\n"
            "  output: ./schema/output.json\n"
        )
        agent_yaml.write_text(original)
        bytes_, rewrote = _maybe_rewrite_agent_yaml_for_upload(
            agent_yaml,
            input_schema=None,
            output_schema=None,
        )
        assert rewrote is False
        assert bytes_ == original.encode()

    def test_inline_shorthand_schemas_no_rewrite(self, tmp_path: Path) -> None:
        """When schemas are inline-shorthand in agent.yaml (no path),
        there's nothing to rewrite."""
        agent_yaml = tmp_path / "agent.yaml"
        original = (
            "api_version: movate/v1\n"
            "schema:\n"
            "  input:\n"
            "    text: string\n"
            "  output:\n"
            "    message: string\n"
        )
        agent_yaml.write_text(original)
        bytes_, rewrote = _maybe_rewrite_agent_yaml_for_upload(
            agent_yaml,
            input_schema=None,
            output_schema=None,
        )
        assert rewrote is False
        assert bytes_ == original.encode()

    def test_no_schema_block_no_rewrite(self, tmp_path: Path) -> None:
        """agent.yaml without a `schema:` block at all — no-op."""
        agent_yaml = tmp_path / "agent.yaml"
        original = "api_version: movate/v1\nname: demo\nversion: 0.1.0\n"
        agent_yaml.write_text(original)
        bytes_, rewrote = _maybe_rewrite_agent_yaml_for_upload(
            agent_yaml,
            input_schema=None,
            output_schema=None,
        )
        assert rewrote is False
        assert bytes_ == original.encode()
