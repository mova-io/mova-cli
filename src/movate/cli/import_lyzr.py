"""``mdk import lyzr <file>`` — synthesize an MDK agent from a Lyzr Studio agent definition.

Lyzr customers can paste an exported agent JSON (visible in Lyzr Studio
under Agent → Detail → JSON) and get a working MDK agent directory in
return. Two output modes:

* ``--runtime lyzr`` (default) — generated agent uses ``runtime: lyzr``
  and calls back into Lyzr at inference time. Useful for evaluating
  the existing customer agent through MDK without changing where it runs.

* ``--runtime litellm`` — generated agent is MDK-native. The Lyzr
  agent_instructions become the prompt; LiteLLM does the model call.
  Useful for the migration target: same prompt + model + examples, but
  MDK runs it.

What gets imported
------------------

Mapped:
  name, description, agent_instructions → prompt.md
  agent_goal → goals: [...]
  agent_role → tags: [...]
  examples (JSON-encoded) → examples: [...]
  provider_id + model → model.provider
  temperature, top_p → model.params
  _id → preserved in agent.yaml comment (and in ./lyzr-original.json)
  managed_agents → comment in agent.yaml + migration hint

Dropped (intentionally — not supported on MDK yet):
  tools, tool_configs, mcp_resources, mcp_prompts (v1.1 — tool registry)
  voice_config, image_output_config (out of scope)
  git_agent, proxy_config, a2a_tools (Lyzr-specific)
  max_iterations (no direct MDK mapping; documented in comment)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()
err = Console(stderr=True)

import_app = typer.Typer(
    name="import",
    help="Import agents into MDK from other frameworks (Lyzr today; more later).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# OpenAI/Anthropic provider name → LiteLLM family. The Lyzr JSON's
# ``provider_id`` is "OpenAI" / "Anthropic" / etc. and the ``model``
# is a bare model id; the importer pairs them into a LiteLLM string.
_LYZR_PROVIDER_TO_LITELLM = {
    "OpenAI": "openai",
    "openai": "openai",
    "Anthropic": "anthropic",
    "anthropic": "anthropic",
    "Azure": "azure",
    "AzureOpenAI": "azure",
    "Google": "gemini",
    "Gemini": "gemini",
}


@import_app.command("lyzr")
def import_lyzr(
    json_file: Path = typer.Argument(
        ...,
        help="Path to a Lyzr agent JSON file (export from Lyzr Studio).",
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
    runtime: str = typer.Option(
        "lyzr",
        "--runtime",
        help=(
            "MDK runtime for the imported agent. 'lyzr' calls back to "
            "Lyzr at inference time (eval/bench an existing agent). "
            "'litellm' makes the agent MDK-native (migration target)."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing agent directory of the same name.",
    ),
) -> None:
    """Synthesize an MDK agent from a Lyzr Studio agent JSON definition.

    [bold]Examples:[/bold]

      [dim]# Import as a Lyzr-runtime agent (calls Lyzr at runtime)[/dim]
      $ mdk import lyzr ./tesla-manager.json

      [dim]# Import as MDK-native (migration target — runs through LiteLLM)[/dim]
      $ mdk import lyzr ./tesla-manager.json --runtime litellm

      [dim]# Custom output location[/dim]
      $ mdk import lyzr ./tesla-manager.json -o ./customer-agents

    The original Lyzr JSON is preserved at ``<agent>/lyzr-original.json``
    for diffing + audit.
    """
    if runtime not in ("lyzr", "litellm"):
        err.print(
            f"[red]✗ bad flag:[/red] --runtime must be 'lyzr' or 'litellm', "
            f"got {runtime!r}"
        )
        raise typer.Exit(code=2)

    try:
        lyzr_def = json.loads(json_file.read_text())
    except json.JSONDecodeError as exc:
        err.print(f"[red]✗ parse error:[/red] {json_file} is not valid JSON: {exc}")
        raise typer.Exit(code=2) from None

    try:
        plan = _build_plan(lyzr_def, runtime=runtime)
    except _LyzrImportError as exc:
        err.print(f"[red]✗ import error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    agent_dir = output_dir / plan["agent_name"]
    if agent_dir.exists() and not force:
        err.print(
            f"[red]✗ agent dir exists:[/red] {agent_dir} already exists. "
            f"Use --force to overwrite, or pass --output to a different parent."
        )
        raise typer.Exit(code=2)

    _write_agent(agent_dir, plan, lyzr_def)

    _render_summary(plan, agent_dir, json_file)


# ---------------------------------------------------------------------------
# Plan-building (pure; testable independent of filesystem)
# ---------------------------------------------------------------------------


class _LyzrImportError(Exception):
    """Raised when a Lyzr JSON definition can't be mapped to MDK."""


def _build_plan(lyzr_def: dict[str, Any], *, runtime: str) -> dict[str, Any]:
    """Read a Lyzr agent JSON dict and produce a plan describing what
    to write. Pure function — no IO. Centralizes the field mapping
    so unit tests can assert against the plan dict directly.
    """
    raw_name = lyzr_def.get("name") or ""
    if not raw_name:
        raise _LyzrImportError(
            "Lyzr definition is missing the required 'name' field"
        )
    agent_name = _slugify_name(raw_name)

    lyzr_id = lyzr_def.get("_id") or ""

    instructions = (lyzr_def.get("agent_instructions") or "").strip()
    if not instructions:
        raise _LyzrImportError(
            "Lyzr definition is missing 'agent_instructions' — "
            "MDK requires a non-empty prompt"
        )

    goals: list[str] = []
    agent_goal = (lyzr_def.get("agent_goal") or "").strip()
    if agent_goal:
        goals.append(agent_goal)

    tags: list[str] = ["imported-from-lyzr"]
    agent_role = (lyzr_def.get("agent_role") or "").strip()
    if agent_role:
        tags.append(_slugify_name(agent_role))

    # Examples on Lyzr live as a JSON-encoded string. Parse opportunistically.
    examples = _parse_examples(lyzr_def.get("examples"))

    # Model mapping: Lyzr's provider_id + model → LiteLLM string.
    provider_id = (lyzr_def.get("provider_id") or "").strip()
    model = (lyzr_def.get("model") or "").strip()
    if runtime == "lyzr":
        # Runtime: lyzr means we call Lyzr's API. The provider string
        # is the Lyzr agent ID, not the underlying model.
        if not lyzr_id:
            raise _LyzrImportError(
                "Cannot import with --runtime lyzr: Lyzr definition is "
                "missing the '_id' field. Re-export from Lyzr Studio."
            )
        provider_str = f"lyzr/{lyzr_id}"
    else:
        # Runtime: litellm — pair provider_id + model into LiteLLM format.
        family = _LYZR_PROVIDER_TO_LITELLM.get(provider_id)
        if not family:
            raise _LyzrImportError(
                f"Cannot map Lyzr provider_id={provider_id!r} to a LiteLLM "
                f"family. Known: {sorted(_LYZR_PROVIDER_TO_LITELLM)}. "
                f"Edit agent.yaml after import or open an issue to add a mapping."
            )
        if not model:
            raise _LyzrImportError(
                "Lyzr definition is missing the 'model' field"
            )
        provider_str = f"{family}/{model}"

    # Numeric coercion — Lyzr ships these as strings (e.g. "0.8", "1").
    params: dict[str, Any] = {}
    if "temperature" in lyzr_def:
        params["temperature"] = _coerce_float(lyzr_def["temperature"], "temperature")
    if "top_p" in lyzr_def:
        params["top_p"] = _coerce_float(lyzr_def["top_p"], "top_p")

    managed_agents = lyzr_def.get("managed_agents") or []

    # Build the output input/output schemas. Lyzr's response_format
    # is text by default; we model the agent contract as
    # {message: string} → {response: string}.
    response_format_type = (
        lyzr_def.get("response_format", {}).get("type")
        if isinstance(lyzr_def.get("response_format"), dict)
        else None
    )

    return {
        "agent_name": agent_name,
        "lyzr_id": lyzr_id,
        "raw_name": raw_name,
        "description": (lyzr_def.get("description") or "").strip(),
        "instructions": instructions,
        "goals": goals,
        "tags": tags,
        "examples": examples,
        "runtime": runtime,
        "provider_str": provider_str,
        "params": params,
        "managed_agents": managed_agents,
        "agent_role": agent_role,
        "lyzr_response_format": response_format_type,
        "max_iterations": lyzr_def.get("max_iterations"),
    }


def _slugify_name(raw: str) -> str:
    """Convert Lyzr's free-form name (potentially with versions like
    'v1 (MAY 08, 2026, 09:21 AM PST)') into an MDK-valid slug
    (lowercase, hyphen-separated)."""
    # Drop version suffix in parentheses
    base = re.sub(r"\s*\([^)]*\)\s*", "", raw).strip()
    # Drop trailing 'v1', 'v2', etc.
    base = re.sub(r"\s+v\d+\s*$", "", base, flags=re.IGNORECASE).strip()
    # Lowercase + replace runs of non-alphanumerics with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if not slug:
        raise _LyzrImportError(
            f"Could not slugify Lyzr agent name {raw!r}"
        )
    # MDK agent names must start with a letter or digit and end the same way
    return slug


def _parse_examples(raw: Any) -> list[dict[str, str]]:
    """Lyzr stores examples as a JSON-encoded STRING (not a list).
    Parse opportunistically; return empty list on failure."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw  # already-parsed shape, support both
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or item.get("input") or ""
        assistant = item.get("assistant") or item.get("output") or ""
        if user and assistant:
            out.append({"user": str(user), "assistant": str(assistant)})
    return out


def _coerce_float(raw: Any, field: str) -> float:
    """Lyzr ships numeric params as strings. Coerce."""
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise _LyzrImportError(
            f"Lyzr field {field!r} is not a valid number: {raw!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _write_agent(
    agent_dir: Path, plan: dict[str, Any], lyzr_def: dict[str, Any]
) -> None:
    """Write the agent directory: agent.yaml + prompt.md + schemas + examples
    + lyzr-original.json. Idempotent — overwrites unconditionally
    once the caller has handled the --force gate."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(parents=True, exist_ok=True)

    # Preserve the source JSON for diff + audit
    (agent_dir / "lyzr-original.json").write_text(
        json.dumps(lyzr_def, indent=2) + "\n"
    )

    (agent_dir / "agent.yaml").write_text(_render_agent_yaml(plan))
    (agent_dir / "prompt.md").write_text(_render_prompt_md(plan))
    (agent_dir / "schema" / "input.json").write_text(_INPUT_SCHEMA)
    (agent_dir / "schema" / "output.json").write_text(_OUTPUT_SCHEMA)


def _render_agent_yaml(plan: dict[str, Any]) -> str:
    """Render agent.yaml. Hand-written (not pyyaml-dumped) so we
    control formatting + can interleave comments."""
    lines: list[str] = [
        "api_version: movate/v1",
        "kind: Agent",
        "",
        f"name: {plan['agent_name']}",
        "version: 0.1.0",
    ]
    if plan["description"]:
        # Block-scalar for multi-line description
        desc = plan["description"].replace("\n", "\n  ")
        lines.append(f"description: |\n  {desc}")

    lines.extend([
        "",
        f"# Imported from Lyzr agent _id={plan['lyzr_id']!r}",
        f"# Original name: {plan['raw_name']!r}",
        "# See ./lyzr-original.json for the source definition.",
    ])
    if plan["managed_agents"]:
        lines.append(
            f"# Lyzr 'managed_agents' ({len(plan['managed_agents'])} role agents) "
            f"are routed internally by Lyzr today."
        )
        lines.append(
            "# Migration target: replicate as MDK workflow with conditional "
            "edges (v1.1)."
        )
    if plan["max_iterations"]:
        lines.append(
            f"# Lyzr max_iterations={plan['max_iterations']} (ReAct loop "
            f"control; no direct MDK mapping)."
        )

    if plan["goals"]:
        # 'goals' will become a first-class AgentSpec field in v0.7.
        # Render as comments for now so the imported info isn't lost
        # but `mdk validate` doesn't reject the unknown key.
        lines.append("")
        lines.append("# goals (lifted to first-class AgentSpec field in v0.7):")
        for goal in plan["goals"]:
            lines.append(f"#   - {goal}")

    lines.extend([
        "",
        f"# runtime={plan['runtime']}: "
        + (
            "calls back to Lyzr at inference time (eval/bench an existing agent)"
            if plan["runtime"] == "lyzr"
            else "MDK-native (LiteLLM does the model call)"
        ),
        f"runtime: {plan['runtime']}",
        "model:",
        f"  provider: {plan['provider_str']}",
    ])
    if plan["params"]:
        lines.append("  params:")
        for key, val in plan["params"].items():
            lines.append(f"    {key}: {val}")

    lines.extend([
        "",
        "prompt: ./prompt.md",
        "",
        "schema:",
        "  input: ./schema/input.json",
        "  output: ./schema/output.json",
    ])

    if plan["examples"]:
        lines.extend([
            "",
            "# Imported examples — smoke-test on `mdk validate` (v0.7 feature).",
        ])

    if plan["tags"]:
        lines.append("")
        lines.append("tags:")
        for tag in plan["tags"]:
            lines.append(f"  - {tag}")

    return "\n".join(lines) + "\n"


def _render_prompt_md(plan: dict[str, Any]) -> str:
    """Render prompt.md. Lyzr's agent_instructions become the body;
    we append a JSON-shaped output stub + the input.message reference."""
    parts: list[str] = [
        "<!-- IMPORTED FROM LYZR — edit as needed; the import script will",
        "     not regenerate this file. See ./lyzr-original.json for source. -->",
        "",
        plan["instructions"],
    ]
    if plan["examples"]:
        parts.extend(["", "## Examples", ""])
        for i, ex in enumerate(plan["examples"], 1):
            parts.append(f"**Example {i}**")
            parts.append("")
            parts.append(f"User: {ex['user']}")
            parts.append(f"Assistant: {ex['assistant']}")
            parts.append("")

    parts.extend([
        "",
        "# Customer message",
        "",
        "{{ input.message }}",
        "",
        "# Output format",
        "",
        'Return JSON of the shape: `{"response": "<your answer>"}`. No prose, no code fences.',
    ])
    return "\n".join(parts) + "\n"


_INPUT_SCHEMA = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["message"],
  "properties": {
    "message": { "type": "string", "minLength": 1 }
  }
}
"""

_OUTPUT_SCHEMA = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["response"],
  "properties": {
    "response": { "type": "string" }
  }
}
"""


# ---------------------------------------------------------------------------
# Summary rendering (post-import)
# ---------------------------------------------------------------------------


def _render_summary(
    plan: dict[str, Any], agent_dir: Path, json_file: Path
) -> None:
    """Print a Rich summary table showing what was imported + next steps."""
    table = Table(
        title=f"✓ {plan['agent_name']} — imported from lyzr → runtime: {plan['runtime']}",
        show_header=False,
        title_style="bold green",
    )
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("source", str(json_file))
    table.add_row("agent dir", str(agent_dir))
    table.add_row("name", plan["agent_name"])
    table.add_row("runtime", plan["runtime"])
    table.add_row("provider", plan["provider_str"])
    if plan["goals"]:
        table.add_row("goals", str(len(plan["goals"])))
    if plan["examples"]:
        table.add_row("examples", str(len(plan["examples"])))
    if plan["managed_agents"]:
        table.add_row(
            "managed agents",
            f"[yellow]{len(plan['managed_agents'])}[/yellow] "
            f"[dim](Lyzr-internal; not ported — see comment in agent.yaml)[/dim]",
        )

    console.print(table)

    # Next-step hints — different per runtime
    if plan["runtime"] == "lyzr":
        console.print("[dim]Next:[/dim]")
        console.print(
            "  export LYZR_API_KEY=sk-default-...   "
            "[dim](from Lyzr Studio → Agent → API Key)[/dim]"
        )
        console.print(f"  mdk validate {agent_dir}")
        console.print(
            f"  mdk run {agent_dir} '{{\"message\": \"hi\"}}'   "
            "[dim](calls Lyzr at runtime)[/dim]"
        )
        if plan["managed_agents"]:
            console.print(
                "\n  [dim]Migration tip:[/dim] re-import with "
                "[bold]--runtime litellm[/bold] to generate an MDK-native "
                "version, then run the same eval against both."
            )
    else:
        console.print("[dim]Next:[/dim]")
        console.print(f"  mdk validate {agent_dir}")
        console.print(
            f"  mdk run {agent_dir} '{{\"message\": \"hi\"}}'   "
            "[dim](MDK-native via LiteLLM)[/dim]"
        )
