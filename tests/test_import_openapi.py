"""Sprint P — `mdk import openapi` tests.

Three layers:

1. **Parser** — :func:`parse_openapi` correctly lifts operations from
   JSON and YAML specs; rejects Swagger 2.0; handles missing fields.
2. **Generator** — :func:`skill_yaml_for` produces a skill.yaml-shaped
   dict with correct HTTP impl, schema, and side_effects.
3. **CLI** — `mdk import openapi` writes skills, respects --dry-run /
   --only / --prefix / --force.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.importers import (
    OpenAPIParseError,
    parse_openapi,
    skill_yaml_for,
)

runner = CliRunner(mix_stderr=False)


# Sample specs used across tests
_PETSTORE_SPEC: dict = {
    "openapi": "3.0.0",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://petstore.example.com/api"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List all pets",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "array"}}},
                    }
                },
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "tag": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                        },
                    }
                },
            },
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "getPetById",
                "summary": "Find pet by ID",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
    "components": {
        "schemas": {
            "Pet": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                },
            }
        }
    },
}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseOpenapi:
    def test_parses_json_spec(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        assert len(ops) == 3
        ids = {op.operation_id for op in ops}
        assert ids == {"listPets", "createPet", "getPetById"}

    def test_parses_yaml_spec(self) -> None:
        ops = parse_openapi(yaml.safe_dump(_PETSTORE_SPEC))
        assert len(ops) == 3

    def test_path_parameter_marked_required(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        get_pet = next(op for op in ops if op.operation_id == "getPetById")
        params = {p.name: p for p in get_pet.parameters}
        assert params["petId"].required is True
        assert params["petId"].location == "path"

    def test_query_parameter_marked_optional(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        list_pets = next(op for op in ops if op.operation_id == "listPets")
        params = {p.name: p for p in list_pets.parameters}
        assert params["limit"].required is False
        assert params["limit"].location == "query"
        assert params["limit"].type_ == "integer"

    def test_request_body_properties_become_parameters(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        create = next(op for op in ops if op.operation_id == "createPet")
        names = {p.name for p in create.parameters}
        assert "name" in names
        assert "tag" in names
        # `name` is in body's required[] → required True
        name_param = next(p for p in create.parameters if p.name == "name")
        assert name_param.required is True

    def test_method_is_uppercased(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        assert all(op.method.isupper() for op in ops)

    def test_swagger_2_rejected_with_hint(self) -> None:
        spec = {"swagger": "2.0", "paths": {}}
        with pytest.raises(OpenAPIParseError, match=r"2\.0|Swagger"):
            parse_openapi(json.dumps(spec))

    def test_missing_paths_raises(self) -> None:
        spec = {"openapi": "3.0.0", "info": {"title": "x", "version": "1"}}
        with pytest.raises(OpenAPIParseError, match="paths"):
            parse_openapi(json.dumps(spec))

    def test_empty_paths_raises(self) -> None:
        spec = {"openapi": "3.0.0", "paths": {}}
        with pytest.raises(OpenAPIParseError):
            parse_openapi(json.dumps(spec))

    def test_unsupported_version_raises(self) -> None:
        spec = {"openapi": "4.0.0", "paths": {"/x": {"get": {"responses": {}}}}}
        with pytest.raises(OpenAPIParseError, match="version"):
            parse_openapi(json.dumps(spec))

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(OpenAPIParseError):
            parse_openapi("not: : valid: : yaml:")

    def test_operation_without_id_synthesizes_name(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {"/foo/bar": {"get": {"responses": {}}}},
        }
        ops = parse_openapi(json.dumps(spec))
        assert len(ops) == 1
        # Synthesized as `<method>-<slugified-path>`
        assert "foo" in ops[0].operation_id
        assert "bar" in ops[0].operation_id

    def test_duplicate_operation_ids_disambiguated(self) -> None:
        """Two operations sharing operationId get -2, -3 suffixes."""
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/a": {"get": {"operationId": "same", "responses": {}}},
                "/b": {"get": {"operationId": "same", "responses": {}}},
            },
        }
        ops = parse_openapi(json.dumps(spec))
        ids = [op.operation_id for op in ops]
        assert "same" in ids
        assert any(i.endswith("-2") for i in ids)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkillYamlFor:
    def test_basic_shape(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        list_pets = next(op for op in ops if op.operation_id == "listPets")
        doc = skill_yaml_for(list_pets, server_url="https://petstore.example.com/api")
        assert doc["api_version"] == "movate/v1"
        assert doc["kind"] == "Skill"
        assert doc["name"] == "listPets"

    def test_get_is_read_only(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        list_pets = next(op for op in ops if op.operation_id == "listPets")
        doc = skill_yaml_for(list_pets)
        assert doc["side_effects"] == "read-only"

    def test_post_is_mutates_state(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        create = next(op for op in ops if op.operation_id == "createPet")
        doc = skill_yaml_for(create)
        assert doc["side_effects"] == "mutates-state"

    def test_http_method_in_impl(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        get = next(op for op in ops if op.operation_id == "getPetById")
        doc = skill_yaml_for(get)
        assert doc["implementation"]["kind"] == "http"
        assert doc["implementation"]["method"] == "GET"

    def test_url_prepends_server(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        get = next(op for op in ops if op.operation_id == "getPetById")
        doc = skill_yaml_for(get, server_url="https://petstore.example.com/api")
        assert doc["implementation"]["entry"].startswith("https://petstore.example.com/api")
        # Path template preserved (Jinja interpolation happens at runtime)
        assert "{petId}" in doc["implementation"]["entry"]

    def test_input_schema_uses_optional_marker(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        list_pets = next(op for op in ops if op.operation_id == "listPets")
        doc = skill_yaml_for(list_pets)
        # Optional param gets `?` suffix
        assert "limit?" in doc["schema"]["input"]
        assert doc["schema"]["input"]["limit?"] == "integer"

    def test_auth_placeholder_emitted(self) -> None:
        ops = parse_openapi(json.dumps(_PETSTORE_SPEC))
        doc = skill_yaml_for(ops[0])
        assert "bearer-from-env" in doc["implementation"]["auth"]
        assert "OPENAPI_TOKEN" in doc["implementation"]["auth"]

    def test_description_falls_back_to_method_path(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {"/x": {"get": {"responses": {}}}},
        }
        ops = parse_openapi(json.dumps(spec))
        doc = skill_yaml_for(ops[0])
        # No summary/description in source → default to method + path
        assert "GET" in doc["description"] or "/x" in doc["description"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    """Write the petstore spec to a tmp JSON file."""
    path = tmp_path / "petstore.json"
    path.write_text(json.dumps(_PETSTORE_SPEC))
    return path


@pytest.mark.unit
def test_cli_writes_one_skill_per_operation(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    result = runner.invoke(app, ["import", "openapi", str(spec_file), "--target", str(target)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Three skill directories created
    assert (target / "listPets" / "skill.yaml").is_file()
    assert (target / "createPet" / "skill.yaml").is_file()
    assert (target / "getPetById" / "skill.yaml").is_file()


@pytest.mark.unit
def test_cli_generated_skill_parses_as_valid_yaml(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    runner.invoke(app, ["import", "openapi", str(spec_file), "--target", str(target)])
    data = yaml.safe_load((target / "listPets" / "skill.yaml").read_text())
    assert data["kind"] == "Skill"
    assert data["name"] == "listPets"


@pytest.mark.unit
def test_cli_dry_run_does_not_write(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    result = runner.invoke(
        app,
        [
            "import",
            "openapi",
            str(spec_file),
            "--target",
            str(target),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    # Nothing written
    assert not target.exists()


@pytest.mark.unit
def test_cli_only_filters_to_named_operations(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    result = runner.invoke(
        app,
        [
            "import",
            "openapi",
            str(spec_file),
            "--target",
            str(target),
            "--only",
            "getPetById",
        ],
    )
    assert result.exit_code == 0
    # Only the named operation was generated
    assert (target / "getPetById" / "skill.yaml").is_file()
    assert not (target / "listPets").exists()
    assert not (target / "createPet").exists()


@pytest.mark.unit
def test_cli_only_no_match_exits_2(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    result = runner.invoke(
        app,
        [
            "import",
            "openapi",
            str(spec_file),
            "--target",
            str(target),
            "--only",
            "nonexistent",
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_prefix_applied_to_names(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    result = runner.invoke(
        app,
        [
            "import",
            "openapi",
            str(spec_file),
            "--target",
            str(target),
            "--prefix",
            "pet-",
        ],
    )
    assert result.exit_code == 0
    # Directories use the prefix
    assert (target / "pet-listPets" / "skill.yaml").is_file()
    # Embedded name field uses the prefix too
    data = yaml.safe_load((target / "pet-listPets" / "skill.yaml").read_text())
    assert data["name"] == "pet-listPets"


@pytest.mark.unit
def test_cli_refuses_overwrite_without_force(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    # First import succeeds
    runner.invoke(app, ["import", "openapi", str(spec_file), "--target", str(target)])
    # Second import refuses
    second = runner.invoke(app, ["import", "openapi", str(spec_file), "--target", str(target)])
    assert second.exit_code == 2


@pytest.mark.unit
def test_cli_force_overwrites(tmp_path: Path, spec_file: Path) -> None:
    target = tmp_path / "skills"
    runner.invoke(app, ["import", "openapi", str(spec_file), "--target", str(target)])
    second = runner.invoke(
        app,
        [
            "import",
            "openapi",
            str(spec_file),
            "--target",
            str(target),
            "--force",
        ],
    )
    assert second.exit_code == 0


@pytest.mark.unit
def test_cli_server_override(tmp_path: Path, spec_file: Path) -> None:
    """--server overrides spec.servers[0].url in the generated impl."""
    target = tmp_path / "skills"
    runner.invoke(
        app,
        [
            "import",
            "openapi",
            str(spec_file),
            "--target",
            str(target),
            "--server",
            "https://staging.example.com",
        ],
    )
    data = yaml.safe_load((target / "listPets" / "skill.yaml").read_text())
    assert data["implementation"]["entry"].startswith("https://staging.example.com")


@pytest.mark.unit
def test_cli_missing_spec_file_exits_2(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    ghost = tmp_path / "missing.yaml"
    result = runner.invoke(app, ["import", "openapi", str(ghost), "--target", str(target)])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_malformed_spec_exits_2(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json at all")
    result = runner.invoke(app, ["import", "openapi", str(bad), "--target", str(target)])
    assert result.exit_code == 2
