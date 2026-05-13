"""Agent loader: parse an agent directory into a validated AgentBundle.

Resolves relative paths, validates JSON schemas, and computes a stable hash
of the prompt template body for run-record traceability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, select_autoescape
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from movate.core.models import AgentSpec
from movate.core.schema_shorthand import SchemaShorthandError, compile_shorthand


class AgentLoadError(Exception):
    """Raised when an agent directory fails to load or validate."""


@dataclass
class AgentBundle:
    """Fully-resolved agent: spec, prompt template, validated schemas, hash."""

    spec: AgentSpec
    agent_dir: Path
    prompt_template: str
    prompt_hash: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator

    def render_prompt(self, input_data: dict[str, Any]) -> str:
        """Render the prompt template with the ``input.*`` namespace.

        No filesystem, network, or other globals are exposed to templates.
        """
        env = Environment(
            autoescape=select_autoescape(disabled_extensions=("md",)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        template = env.from_string(self.prompt_template)
        return template.render(input=input_data)


def load_agent(path: str | Path) -> AgentBundle:
    """Load an agent directory. Raises AgentLoadError on any validation failure."""
    agent_dir = Path(path).resolve()
    if not agent_dir.is_dir():
        raise AgentLoadError(f"agent path is not a directory: {agent_dir}")

    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.exists():
        raise AgentLoadError(f"agent.yaml not found in {agent_dir}")

    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError as exc:
        raise AgentLoadError(f"invalid YAML in {yaml_path}: {exc}") from exc

    try:
        spec = AgentSpec.model_validate(raw)
    except ValidationError as exc:
        raise AgentLoadError(f"agent.yaml validation failed:\n{exc}") from exc

    prompt_path = (agent_dir / spec.prompt).resolve()
    if not prompt_path.exists():
        raise AgentLoadError(f"prompt file not found: {prompt_path}")
    prompt_text = prompt_path.read_text()
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

    input_schema = _resolve_schema(spec.schemas.input, agent_dir=agent_dir, label="input")
    output_schema = _resolve_schema(spec.schemas.output, agent_dir=agent_dir, label="output")

    try:
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
    except Exception as exc:
        raise AgentLoadError(f"invalid JSON schema: {exc}") from exc

    return AgentBundle(
        spec=spec,
        agent_dir=agent_dir,
        prompt_template=prompt_text,
        prompt_hash=prompt_hash,
        input_schema=input_schema,
        output_schema=output_schema,
        input_validator=Draft202012Validator(input_schema),
        output_validator=Draft202012Validator(output_schema),
    )


def _resolve_schema(
    raw: str | dict[str, Any],
    *,
    agent_dir: Path,
    label: str,
) -> dict[str, Any]:
    """Resolve one of the two ``schema:`` forms into a JSON Schema dict.

    * **path string** → read the file from disk and parse as JSON.
      Original behavior; still preferred for complex contracts.
    * **inline shorthand dict** → compile via
      :func:`compile_shorthand`. Strict-by-default object schema,
      same downstream API.

    Validation errors from either path are normalized to
    :class:`AgentLoadError` so the CLI surfaces one consistent
    error surface to operators.
    """
    if isinstance(raw, dict):
        try:
            return compile_shorthand(raw, root_label=label)
        except SchemaShorthandError as exc:
            raise AgentLoadError(f"inline schema shorthand error: {exc}") from exc
    # Path string — resolve relative to the agent dir and read.
    return _load_json(agent_dir / raw)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentLoadError(f"schema file not found: {path}")
    try:
        data: Any = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentLoadError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentLoadError(f"schema {path} must be a JSON object, got {type(data).__name__}")
    return data
