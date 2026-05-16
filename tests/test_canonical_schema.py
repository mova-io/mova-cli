"""PR #103 — canonical schema format compiler.

Business-readable schema DSL with semantic types (`email`,
`currency_usd`, `markdown`, `enum`, `list[T]`), top-level `required`
list, per-field `description` / `example` / `ui`, and reusable
`types:` blocks compiled to JSON Schema `$defs`.

Three layers of integration tested here:

1. **Compiler unit tests** — every type-table entry, modifier,
   error path.
2. **Loader integration** — `version: 1` detection routes through
   the canonical compiler; existing shorthand + JSON Schema paths
   unchanged (regression).
3. **CLI integration** — `mdk schema compile <file>` writes JSON
   Schema; `--check` validates without writing; `--format=pretty`
   indents.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.canonical_schema import (
    CanonicalSchemaError,
    compile_canonical,
    is_canonical_format,
)
from movate.core.loader import load_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsCanonicalFormat:
    def test_version_1_dict_is_canonical(self) -> None:
        assert is_canonical_format({"version": 1, "type": "object", "fields": {}})

    def test_no_version_is_not_canonical(self) -> None:
        assert not is_canonical_format({"type": "object", "properties": {}})

    def test_wrong_version_is_not_canonical(self) -> None:
        assert not is_canonical_format({"version": 2, "fields": {}})

    def test_non_dict_is_not_canonical(self) -> None:
        assert not is_canonical_format("not a dict")
        assert not is_canonical_format([])
        assert not is_canonical_format(None)


# ---------------------------------------------------------------------------
# Semantic types
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSemanticTypes:
    def _compile_one_field(self, type_name: str, **extra: object) -> dict[str, object]:
        spec: dict[str, object] = {"type": type_name}
        spec.update(extra)
        result = compile_canonical({"version": 1, "type": "object", "fields": {"f": spec}})
        return result["properties"]["f"]  # type: ignore[no-any-return,index]

    def test_string_alias_text_maps_to_string(self) -> None:
        for name in ("string", "text"):
            schema = self._compile_one_field(name)
            assert schema == {"type": "string"}

    def test_email_emits_format_email(self) -> None:
        schema = self._compile_one_field("email")
        assert schema == {"type": "string", "format": "email"}

    def test_url_emits_format_uri(self) -> None:
        schema = self._compile_one_field("url")
        assert schema == {"type": "string", "format": "uri"}

    def test_phone_emits_x_mdk_format(self) -> None:
        schema = self._compile_one_field("phone")
        assert schema["type"] == "string"
        assert schema["x-mdk-format"] == "phone"

    def test_date_and_datetime_emit_iso_formats(self) -> None:
        assert self._compile_one_field("date")["format"] == "date"
        assert self._compile_one_field("datetime")["format"] == "date-time"

    def test_uuid_emits_format_uuid(self) -> None:
        assert self._compile_one_field("uuid")["format"] == "uuid"

    def test_currency_usd_emits_integer_with_x_mdk_format(self) -> None:
        """USD amounts are integer CENTS — no float rounding hell.
        The x-mdk-format flag tells a future form generator to render
        with a currency input widget."""
        schema = self._compile_one_field("currency_usd")
        assert schema["type"] == "integer"
        assert schema["x-mdk-format"] == "currency-usd-cents"
        # Default minimum 0 — no negative dollar amounts.
        assert schema["minimum"] == 0

    def test_markdown_emits_x_mdk_format(self) -> None:
        schema = self._compile_one_field("markdown")
        assert schema["type"] == "string"
        assert schema["x-mdk-format"] == "markdown"

    def test_integer_and_number(self) -> None:
        assert self._compile_one_field("integer") == {"type": "integer"}
        assert self._compile_one_field("number") == {"type": "number"}

    def test_boolean(self) -> None:
        assert self._compile_one_field("boolean") == {"type": "boolean"}


# ---------------------------------------------------------------------------
# Composite types
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCompositeTypes:
    def test_enum_with_values(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"color": {"type": "enum", "values": ["red", "green", "blue"]}},
            }
        )
        assert result["properties"]["color"] == {
            "type": "string",
            "enum": ["red", "green", "blue"],
        }

    def test_enum_without_values_errors(self) -> None:
        with pytest.raises(CanonicalSchemaError, match="values"):
            compile_canonical(
                {
                    "version": 1,
                    "type": "object",
                    "fields": {"color": {"type": "enum"}},
                }
            )

    def test_list_of_strings(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"tags": {"type": "list[string]"}},
            }
        )
        assert result["properties"]["tags"] == {
            "type": "array",
            "items": {"type": "string"},
        }

    def test_list_of_emails(self) -> None:
        """Inner type can be a semantic type too — `list[email]`
        compiles to array-of-format:email strings."""
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"recipients": {"type": "list[email]"}},
            }
        )
        items = result["properties"]["recipients"]["items"]
        assert items["type"] == "string"
        assert items["format"] == "email"

    def test_list_with_empty_inner_errors(self) -> None:
        with pytest.raises(CanonicalSchemaError, match="inner type"):
            compile_canonical(
                {
                    "version": 1,
                    "type": "object",
                    "fields": {"x": {"type": "list[]"}},
                }
            )

    def test_nested_object(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {
                    "address": {
                        "type": "object",
                        "fields": {
                            "street": {"type": "string"},
                            "city": {"type": "string"},
                        },
                        "required": ["street", "city"],
                    }
                },
            }
        )
        addr = result["properties"]["address"]
        assert addr["type"] == "object"
        assert addr["additionalProperties"] is False
        assert set(addr["properties"]) == {"street", "city"}
        assert set(addr["required"]) == {"street", "city"}

    def test_nested_object_required_unknown_field_errors(self) -> None:
        with pytest.raises(CanonicalSchemaError, match="not in declared"):
            compile_canonical(
                {
                    "version": 1,
                    "type": "object",
                    "fields": {
                        "addr": {
                            "type": "object",
                            "fields": {"street": {"type": "string"}},
                            "required": ["nonexistent"],
                        }
                    },
                }
            )


# ---------------------------------------------------------------------------
# Custom types via $defs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCustomTypes:
    def test_reusable_type_compiles_to_dollar_defs(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "types": {
                    "scoring": {
                        "type": "object",
                        "fields": {
                            "score": {"type": "integer", "min": 0, "max": 3},
                            "rationale": {"type": "string"},
                        },
                        "required": ["score", "rationale"],
                    }
                },
                "fields": {
                    "budget": {"type": "scoring"},
                    "authority": {"type": "scoring"},
                },
            }
        )
        # Both fields reference the same $def.
        assert result["properties"]["budget"] == {"$ref": "#/$defs/scoring"}
        assert result["properties"]["authority"] == {"$ref": "#/$defs/scoring"}
        # $defs block carries the resolved object schema.
        scoring = result["$defs"]["scoring"]
        assert scoring["type"] == "object"
        assert scoring["properties"]["score"]["minimum"] == 0
        assert scoring["properties"]["score"]["maximum"] == 3

    def test_unsafe_type_name_errors(self) -> None:
        with pytest.raises(CanonicalSchemaError):
            compile_canonical(
                {
                    "version": 1,
                    "type": "object",
                    "types": {"bad/name": {"type": "object", "fields": {}}},
                    "fields": {},
                }
            )

    def test_ref_to_undeclared_type_emits_ref_anyway(self) -> None:
        """Unknown type-names compile to a $ref blindly — the
        downstream JSON Schema validator will surface the dangling
        ref. This mirrors the shorthand compiler's behavior."""
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"x": {"type": "some_undeclared_thing"}},
            }
        )
        assert result["properties"]["x"] == {"$ref": "#/$defs/some_undeclared_thing"}


# ---------------------------------------------------------------------------
# Common modifiers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModifiers:
    def test_description_carried_through(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"name": {"type": "string", "description": "The lead's name"}},
            }
        )
        assert result["properties"]["name"]["description"] == "The lead's name"

    def test_example_normalizes_to_examples_list(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"name": {"type": "string", "example": "Sarah"}},
            }
        )
        assert result["properties"]["name"]["examples"] == ["Sarah"]

    def test_examples_plural_passes_through(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"tag": {"type": "string", "examples": ["alpha", "beta"]}},
            }
        )
        assert result["properties"]["tag"]["examples"] == ["alpha", "beta"]

    def test_min_max_on_integer_emit_minimum_maximum(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"score": {"type": "integer", "min": 0, "max": 100}},
            }
        )
        assert result["properties"]["score"]["minimum"] == 0
        assert result["properties"]["score"]["maximum"] == 100

    def test_min_max_on_string_emit_min_max_length(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"name": {"type": "string", "min": 1, "max": 80}},
            }
        )
        assert result["properties"]["name"]["minLength"] == 1
        assert result["properties"]["name"]["maxLength"] == 80

    def test_explicit_max_length_for_markdown(self) -> None:
        """`max_length:` is the unambiguous form for string-types
        like markdown where `max:` could be confused with numeric."""
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"body": {"type": "markdown", "max_length": 4000}},
            }
        )
        assert result["properties"]["body"]["maxLength"] == 4000

    def test_ui_hint_carried_as_x_mdk_ui(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"notes": {"type": "string", "ui": "textarea"}},
            }
        )
        assert result["properties"]["notes"]["x-mdk-ui"] == "textarea"


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTopLevel:
    def test_title_and_description_preserved(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "title": "My Schema",
                "description": "Long form description.",
                "fields": {"x": {"type": "string"}},
            }
        )
        assert result["title"] == "My Schema"
        assert result["description"] == "Long form description."

    def test_required_list_passes_through(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a"],
            }
        )
        assert result["required"] == ["a"]

    def test_required_unknown_field_errors(self) -> None:
        with pytest.raises(CanonicalSchemaError, match="not declared"):
            compile_canonical(
                {
                    "version": 1,
                    "type": "object",
                    "fields": {"a": {"type": "string"}},
                    "required": ["b"],  # typo
                }
            )

    def test_ui_block_at_top_carries_as_x_mdk_ui(self) -> None:
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"a": {"type": "string"}},
                "ui": {"order": ["a"], "groups": {"main": {"title": "Main", "fields": ["a"]}}},
            }
        )
        assert "x-mdk-ui" in result
        assert result["x-mdk-ui"]["order"] == ["a"]

    def test_contracts_block_passed_as_extension(self) -> None:
        """v1: contracts carried through unchanged. v2 will compile
        them to JSON Schema allOf/if-then-else."""
        result = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {"score": {"type": "integer"}},
                "contracts": [{"rule": "score >= 0", "message": "non-negative"}],
            }
        )
        assert result["x-mdk-contracts"] == [{"rule": "score >= 0", "message": "non-negative"}]

    def test_unsupported_version_errors(self) -> None:
        with pytest.raises(CanonicalSchemaError, match="version"):
            compile_canonical({"version": 99, "type": "object", "fields": {}})

    def test_non_object_root_errors(self) -> None:
        with pytest.raises(CanonicalSchemaError, match="object"):
            compile_canonical({"version": 1, "type": "string"})


# ---------------------------------------------------------------------------
# Loader integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoaderIntegration:
    def _scaffold(self, tmp_path: Path, input_yaml: str, output_yaml: str) -> Path:
        agent_dir = tmp_path / "agent"
        (agent_dir / "schema").mkdir(parents=True)
        (agent_dir / "schema" / "input.yaml").write_text(input_yaml)
        (agent_dir / "schema" / "output.yaml").write_text(output_yaml)
        (agent_dir / "prompt.md").write_text("Test.\n")
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent(
                """\
                api_version: movate/v1
                kind: Agent
                name: test-agent
                version: 0.1.0
                description: test
                prompt: ./prompt.md
                schema:
                  input: ./schema/input.yaml
                  output: ./schema/output.yaml
                model:
                  provider: openai/gpt-4o-mini
                """
            )
        )
        return agent_dir

    def test_loader_routes_canonical_through_canonical_compiler(self, tmp_path: Path) -> None:
        """`version: 1` at the top of the schema file should route
        through `compile_canonical`, not `compile_shorthand`."""
        canonical = textwrap.dedent(
            """\
            version: 1
            type: object
            fields:
              email_addr:
                type: email
                description: Where to send the result.
            required: [email_addr]
            """
        )
        agent_dir = self._scaffold(tmp_path, canonical, canonical)
        bundle = load_agent(agent_dir)
        # `format: email` is canonical-only (the shorthand compiler
        # has no syntax for it) — so its presence proves we took the
        # canonical path.
        assert bundle.input_schema["properties"]["email_addr"]["format"] == "email"

    def test_loader_still_routes_shorthand_through_shorthand_compiler(self, tmp_path: Path) -> None:
        """Regression: schemas without `version: 1` should still
        compile via the existing shorthand path."""
        shorthand = "text: string\n"
        agent_dir = self._scaffold(tmp_path, shorthand, "message: string\n")
        bundle = load_agent(agent_dir)
        # Strict-by-default — additionalProperties locked. That's the
        # shorthand compiler's signature behavior.
        assert bundle.input_schema["additionalProperties"] is False
        assert bundle.input_schema["properties"]["text"]["type"] == "string"


# ---------------------------------------------------------------------------
# CLI: `mdk schema compile`
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchemaCompileCommand:
    def _write_canonical(self, tmp_path: Path) -> Path:
        path = tmp_path / "in.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                version: 1
                type: object
                title: Demo
                fields:
                  name:
                    type: string
                    description: Lead name
                  amount:
                    type: currency_usd
                    example: 12000000
                required: [name]
                """
            )
        )
        return path

    def test_compile_to_stdout(self, tmp_path: Path) -> None:
        src = self._write_canonical(tmp_path)
        result = runner.invoke(app, ["schema", "compile", str(src)], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr
        data = json.loads(result.stdout)
        assert data["title"] == "Demo"
        assert data["properties"]["amount"]["x-mdk-format"] == "currency-usd-cents"
        assert data["required"] == ["name"]

    def test_compile_to_file(self, tmp_path: Path) -> None:
        src = self._write_canonical(tmp_path)
        out = tmp_path / "out.json"
        result = runner.invoke(
            app,
            ["schema", "compile", str(src), "-o", str(out)],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert out.is_file()
        data = json.loads(out.read_text())
        assert data["title"] == "Demo"

    def test_compile_pretty_format(self, tmp_path: Path) -> None:
        src = self._write_canonical(tmp_path)
        result = runner.invoke(
            app,
            ["schema", "compile", str(src), "--format", "json-schema-pretty"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        # Pretty output has newlines + indentation; compact form has neither.
        assert "\n  " in result.stdout

    def test_check_flag_validates_without_writing(self, tmp_path: Path) -> None:
        src = self._write_canonical(tmp_path)
        result = runner.invoke(
            app,
            ["schema", "compile", str(src), "--check"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        combined = result.stdout + result.stderr
        assert "compiles cleanly" in combined
        assert "form=canonical" in combined
        # `--check` does NOT write to stdout (nothing JSON-parseable
        # for a downstream pipe).
        assert "{" not in result.stdout

    def test_check_on_bad_schema_errors(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("version: 1\ntype: object\nfields:\n  x:\n    type: bogus_type\n")
        # `bogus_type` isn't semantic and isn't declared in $defs;
        # compiles to a $ref (silent — that's documented). The
        # check should still succeed because we only fail on
        # CanonicalSchemaError. So pick an actual error case:
        truly_bad = tmp_path / "really-bad.yaml"
        truly_bad.write_text(
            "version: 1\ntype: object\nfields:\n  x:\n    description: missing type\n"
        )
        result = runner.invoke(
            app,
            ["schema", "compile", str(truly_bad), "--check"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 2

    def test_compile_routes_shorthand_through_shorthand_compiler(self, tmp_path: Path) -> None:
        """`mdk schema compile` on a shorthand file should detect the
        form and compile via the shorthand path."""
        src = tmp_path / "short.yaml"
        src.write_text("name: string\nemail_addr: string\n")
        result = runner.invoke(
            app,
            ["schema", "compile", str(src), "--check"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0
        assert "form=shorthand" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# `nullable: true` modifier  (PR #114)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNullableModifier:
    """`nullable: true` widens a field's type to accept JSON null.

    Used in extractor.output.yaml + code-reviewer.output.yaml where
    the business meaning of a field includes "we don't have this
    info" — e.g. `contact_name: nullable=true` means "valid string
    OR null when not mentioned in the source."""

    def test_string_with_nullable_widens_type(self) -> None:
        schema = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {
                    "contact_name": {"type": "string", "nullable": True},
                },
                "required": ["contact_name"],
            }
        )
        assert schema["properties"]["contact_name"]["type"] == ["string", "null"]

    def test_integer_with_nullable_widens_type(self) -> None:
        schema = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {
                    "line": {"type": "integer", "min": 1, "nullable": True},
                },
                "required": ["line"],
            }
        )
        prop = schema["properties"]["line"]
        assert prop["type"] == ["integer", "null"]
        # Bounds preserved alongside the widened type.
        assert prop["minimum"] == 1

    def test_enum_with_nullable_includes_null_in_enum(self) -> None:
        """`type: enum + nullable: true` must add null to the `enum`
        values too — otherwise the enum constraint blocks null even
        though the type widened. Symmetric with JSON Schema 2020-12."""
        schema = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {
                    "urgency": {
                        "type": "enum",
                        "values": ["low", "medium", "high"],
                        "nullable": True,
                    },
                },
                "required": ["urgency"],
            }
        )
        prop = schema["properties"]["urgency"]
        assert prop["type"] == ["string", "null"]
        assert None in prop["enum"]
        # Original values still there.
        assert "low" in prop["enum"]
        assert "high" in prop["enum"]

    def test_nullable_false_or_missing_keeps_strict_type(self) -> None:
        """`nullable: false` (or absence) leaves the type unchanged."""
        schema = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {
                    "name": {"type": "string", "nullable": False},
                    "age": {"type": "integer"},
                },
                "required": ["name", "age"],
            }
        )
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["age"]["type"] == "integer"

    def test_nullable_email_validates_null_and_email(self) -> None:
        """Round-trip with jsonschema validation: nullable email
        accepts both `null` and a valid address."""
        from jsonschema import Draft202012Validator  # noqa: PLC0415

        schema = compile_canonical(
            {
                "version": 1,
                "type": "object",
                "fields": {
                    "email": {"type": "email", "nullable": True},
                },
                "required": ["email"],
            }
        )
        prop = schema["properties"]["email"]
        # Type widened from `string` → `["string", "null"]`.
        assert prop["type"] == ["string", "null"]
        # Format preserved from the `email` semantic-type mapping.
        assert prop.get("format") == "email"
        validator = Draft202012Validator(schema)
        # Both should pass — nullable means "valid email OR null".
        validator.validate({"email": "sarah@example.com"})
        validator.validate({"email": None})
        # A non-string non-null should still fail (type constraint).
        # Format validation of the email itself needs an explicit
        # FormatChecker — not enforced by default. That's downstream
        # policy; here we just verify type widening + null acceptance.
        with pytest.raises(Exception):
            validator.validate({"email": 42})
