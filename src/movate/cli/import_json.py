"""``mdk import json <file>`` — generic JSON-to-MDK-agent importer.

Companion to ``mdk import lyzr``. Where the Lyzr importer is bespoke to
Lyzr's JSON shape, this one accepts a generic JSON file already roughly
in MDK shape (or close to it) and writes the corresponding agent.yaml
+ prompt.md + schema/{input,output}.json on disk.

Use cases:

* Programmatically generated agents — some script emits a JSON description,
  this turns it into the canonical on-disk layout.
* Importing from a framework whose export format the operator has already
  shaped into roughly-MDK JSON.
* Round-tripping via JSON for tooling (e.g. saving an agent definition
  to a database row, reconstituting it later).

Expected JSON shape (minimal):

    {
      "name": "my-agent",
      "version": "0.1.0",
      "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
      "prompt": "<prompt body>"
    }

Optional fields:

    description, owner, tags, runtime,
    model.params, model.fallback,
    schema.input, schema.output,
    evals.dataset, evals.judge,
    timeouts, budget,
    goals, objectives, examples

If ``prompt`` is a path-shaped string (starts with ``./`` or ends in
``.md``), it's treated as a path reference and the file is copied
verbatim. Otherwise it's written to ``prompt.md`` as the body.

If ``schema.input`` / ``schema.output`` are dicts, they're written to
``schema/input.json`` / ``schema/output.json`` respectively. If they're
strings (path refs), they're left as-is in agent.yaml (operator
arranges the files separately).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from movate.cli.import_lyzr import import_app

console = Console()
err = Console(stderr=True)


_DEFAULT_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["message"],
    "properties": {"message": {"type": "string", "minLength": 1}},
}

_DEFAULT_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["response"],
    "properties": {"response": {"type": "string"}},
}


class _JsonImportError(Exception):
    """Raised when the JSON definition can't be mapped to an MDK agent."""


@import_app.command("json")
def import_json(
    json_file: Path = typer.Argument(
        ...,
        help="Path to a JSON file describing an MDK agent.",
        exists=True,
        file_okay=True,
        dir_okay=False,
    ),
    output_dir: Path = typer.Option(
        Path("./agents"),
        "--output",
        "-o",
        help="Parent directory for the new agent. Default: ./agents",
    ),
    name_override: str = typer.Option(
        None,
        "--name",
        help=(
            "Override the agent name (otherwise read from the JSON's `name` field "
            "or the JSON filename stem). Must be lowercase-alphanumeric-hyphen."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing agent directory of the same name.",
    ),
) -> None:
    """Generic JSON → MDK agent importer.

    [bold]Examples:[/bold]

      [dim]# Import a JSON description into ./agents/<name>/[/dim]
      $ mdk import json ./exported-agent.json

      [dim]# Override the output dir + agent name[/dim]
      $ mdk import json ./exported-agent.json -o ./customer-agents --name billing-agent

      [dim]# Re-import (overwrite existing files)[/dim]
      $ mdk import json ./exported-agent.json --force

    The original JSON is preserved at ``<agent>/source.json`` for
    diff / audit.
    """
    try:
        raw_text = json_file.read_text()
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        err.print(f"[red]✗ parse error:[/red] {json_file} is not valid JSON: {exc}")
        raise typer.Exit(code=2) from None

    if not isinstance(data, dict):
        err.print(f"[red]✗ shape error:[/red] {json_file} must be a JSON object at the top level")
        raise typer.Exit(code=2)

    try:
        plan = _build_plan(data, name_override=name_override, fallback_name=json_file.stem)
    except _JsonImportError as exc:
        err.print(f"[red]✗ import error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    agent_dir = output_dir / plan["agent_name"]
    if agent_dir.exists() and not force:
        err.print(
            f"[red]✗ agent dir exists:[/red] {agent_dir} already exists. "
            f"Pass --force to overwrite or pick a different --output / --name."
        )
        raise typer.Exit(code=2)

    _write_agent(agent_dir, plan, source=data)
    _render_summary(plan, agent_dir, json_file)


# ---------------------------------------------------------------------------
# Plan building (pure; unit-testable without IO)
# ---------------------------------------------------------------------------


def _build_plan(
    data: dict[str, Any],
    *,
    name_override: str | None,
    fallback_name: str,
) -> dict[str, Any]:
    """Read the JSON dict and produce a plan dict describing what to write.

    Centralizes the field mapping; tests exercise it directly without
    touching the filesystem.
    """
    raw_name = name_override or data.get("name") or fallback_name
    if not raw_name:
        raise _JsonImportError("JSON has no 'name' field and no --name override was given")
    agent_name = _normalize_name(raw_name)

    description = data.get("description", "")
    owner = data.get("owner", "")
    tags = list(data.get("tags", []) or [])
    runtime = data.get("runtime")

    # Model block — strict shape because it's the most error-prone.
    model_raw = data.get("model")
    if not isinstance(model_raw, dict):
        raise _JsonImportError(
            "JSON 'model' field must be an object with at least a 'provider' key"
        )
    provider = model_raw.get("provider")
    if not isinstance(provider, str) or not provider:
        raise _JsonImportError("JSON 'model.provider' is required and must be a non-empty string")
    params = model_raw.get("params", {}) or {}
    if not isinstance(params, dict):
        raise _JsonImportError("JSON 'model.params' must be an object")
    fallback = model_raw.get("fallback", []) or []
    if not isinstance(fallback, list):
        raise _JsonImportError("JSON 'model.fallback' must be a list")

    # Prompt: either raw text body or a path reference.
    prompt_raw = data.get("prompt")
    if not isinstance(prompt_raw, str) or not prompt_raw.strip():
        raise _JsonImportError(
            "JSON 'prompt' is required and must be a non-empty string "
            "(either the prompt body or a path reference like './prompt.md')"
        )
    prompt_is_path = prompt_raw.startswith(("./", "/")) or prompt_raw.endswith(".md")

    # Schemas: dicts get written to files; strings stay as path refs.
    schema_raw = data.get("schema", {}) or {}
    if not isinstance(schema_raw, dict):
        raise _JsonImportError("JSON 'schema' must be an object")
    input_schema = schema_raw.get("input")
    output_schema = schema_raw.get("output")

    # Pass-through fields — written verbatim into agent.yaml when present.
    evals_block = data.get("evals", {}) or {}
    timeouts_block = data.get("timeouts", {}) or {}
    budget_block = data.get("budget", {}) or {}
    goals = list(data.get("goals", []) or [])
    objectives = list(data.get("objectives", []) or [])
    examples = list(data.get("examples", []) or [])

    return {
        "agent_name": agent_name,
        "version": data.get("version", "0.1.0"),
        "description": description,
        "owner": owner,
        "tags": tags,
        "runtime": runtime,
        "provider": provider,
        "params": params,
        "fallback": fallback,
        "prompt_raw": prompt_raw,
        "prompt_is_path": prompt_is_path,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "evals": evals_block,
        "timeouts": timeouts_block,
        "budget": budget_block,
        "goals": goals,
        "objectives": objectives,
        "examples": examples,
    }


def _normalize_name(raw: str) -> str:
    """Coerce a free-form name into MDK's slug shape.

    MDK agent names must match ``^[a-z0-9][a-z0-9-]*[a-z0-9]$``. We lowercase
    and replace non-alphanumeric runs with hyphens — same algorithm as the
    Lyzr importer's ``_slugify_name``, just without the Lyzr-specific
    version-stripping.
    """
    import re  # noqa: PLC0415

    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not slug:
        raise _JsonImportError(f"could not slugify agent name {raw!r}")
    return slug


# ---------------------------------------------------------------------------
# Writers (filesystem side; not unit-tested in isolation)
# ---------------------------------------------------------------------------


def _write_agent(
    agent_dir: Path,
    plan: dict[str, Any],
    *,
    source: dict[str, Any],
) -> None:
    """Write agent.yaml + prompt.md + schemas + source.json. Idempotent;
    overwrites unconditionally once the caller has handled --force."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(parents=True, exist_ok=True)

    # Preserve the source for diff / audit.
    (agent_dir / "source.json").write_text(json.dumps(source, indent=2) + "\n")

    # prompt.md
    if plan["prompt_is_path"]:
        # The JSON declares a path reference — leave it as-is; operator
        # is responsible for putting the prompt file there themselves.
        # Still drop a placeholder prompt.md so `mdk validate` doesn't
        # immediately fail on a missing file (operator overwrites).
        if not (agent_dir / "prompt.md").exists():
            (agent_dir / "prompt.md").write_text(
                f"<!-- placeholder — source JSON referenced {plan['prompt_raw']!r}.\n"
                f"     Replace with the actual prompt body before running. -->\n"
                f"\n{{{{ input.message }}}}\n"
            )
    else:
        (agent_dir / "prompt.md").write_text(plan["prompt_raw"])

    # schemas
    if isinstance(plan["input_schema"], dict):
        (agent_dir / "schema" / "input.json").write_text(
            json.dumps(plan["input_schema"], indent=2) + "\n"
        )
    elif not (agent_dir / "schema" / "input.json").exists():
        (agent_dir / "schema" / "input.json").write_text(
            json.dumps(_DEFAULT_INPUT_SCHEMA, indent=2) + "\n"
        )

    if isinstance(plan["output_schema"], dict):
        (agent_dir / "schema" / "output.json").write_text(
            json.dumps(plan["output_schema"], indent=2) + "\n"
        )
    elif not (agent_dir / "schema" / "output.json").exists():
        (agent_dir / "schema" / "output.json").write_text(
            json.dumps(_DEFAULT_OUTPUT_SCHEMA, indent=2) + "\n"
        )

    (agent_dir / "agent.yaml").write_text(_render_agent_yaml(plan))


def _render_agent_yaml(plan: dict[str, Any]) -> str:
    """Render agent.yaml from the plan. Uses pyyaml so we don't reimplement
    the YAML escaping the Lyzr importer does by hand — the JSON importer
    target is structured-shape (not Lyzr's mostly-free-form metadata),
    so yaml.safe_dump's output is fine."""
    doc: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Agent",
        "name": plan["agent_name"],
        "version": plan["version"],
    }
    if plan["description"]:
        doc["description"] = plan["description"]
    if plan["owner"]:
        doc["owner"] = plan["owner"]
    if plan["runtime"]:
        doc["runtime"] = plan["runtime"]

    model_block: dict[str, Any] = {"provider": plan["provider"]}
    if plan["params"]:
        model_block["params"] = plan["params"]
    if plan["fallback"]:
        model_block["fallback"] = plan["fallback"]
    doc["model"] = model_block

    doc["prompt"] = plan["prompt_raw"] if plan["prompt_is_path"] else "./prompt.md"
    doc["schema"] = {
        "input": "./schema/input.json",
        "output": "./schema/output.json",
    }

    if plan["evals"]:
        doc["evals"] = plan["evals"]
    if plan["timeouts"]:
        doc["timeouts"] = plan["timeouts"]
    if plan["budget"]:
        doc["budget"] = plan["budget"]
    if plan["tags"]:
        doc["tags"] = plan["tags"]
    if plan["goals"]:
        doc["goals"] = plan["goals"]
    if plan["objectives"]:
        doc["objectives"] = plan["objectives"]
    if plan["examples"]:
        doc["examples"] = plan["examples"]

    header = (
        "# Imported from JSON via `mdk import json`.\n"
        "# Source preserved at ./source.json for diff / audit.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


def _render_summary(plan: dict[str, Any], agent_dir: Path, json_file: Path) -> None:
    """Print a Rich summary table showing what was imported + next steps."""
    table = Table(
        title=f"✓ {plan['agent_name']} — imported from json",
        show_header=False,
        title_style="bold green",
    )
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("source", str(json_file))
    table.add_row("agent dir", str(agent_dir))
    table.add_row("name", plan["agent_name"])
    table.add_row("provider", plan["provider"])
    if plan["runtime"]:
        table.add_row("runtime", plan["runtime"])
    if plan["objectives"]:
        table.add_row("objectives", str(len(plan["objectives"])))
    if plan["examples"]:
        table.add_row("examples", str(len(plan["examples"])))
    if plan["prompt_is_path"]:
        table.add_row(
            "prompt",
            f"[yellow]path ref[/yellow] [dim]({plan['prompt_raw']})[/dim]",
        )
    else:
        char_count = len(plan["prompt_raw"])
        table.add_row("prompt", f"[dim]inline ({char_count} chars)[/dim]")
    console.print(table)
    console.print("[dim]Next:[/dim]")
    console.print(f"  mdk validate {agent_dir}")
    console.print(f'  mdk run {agent_dir} \'{{"message": "hi"}}\' --mock')
