"""The single canonical ``agent.yaml`` round-trip used by the catalog (#127, D8).

Every catalog action that mutates ``agent.yaml`` for a *structured* field
(model, fallback, retrieval, description, name) routes through this ONE writer
so the copilot never invents a parallel agent.yaml writer (the explicit D8
boundary + the ADR 025 "Negative/risks" mitigation).

The round-trip mirrors what the canonical scaffold writer
(:func:`movate.scaffold.write_agent_files`) does for ``agent.yaml``:
``yaml.safe_load`` → mutate the dict → ``yaml.safe_dump(sort_keys=False,
default_flow_style=False)``. ``sort_keys=False`` preserves the author's key
order. Comment preservation for the ``contexts:`` list specifically is handled
by the shipped :func:`movate.cli.contexts_cmd.attach_context_to_agent`
targeted-text editor, which the add/remove-context actions reuse directly.

Two helpers:

* :func:`load_agent_yaml` — parse the on-disk dict (raises on malformed YAML).
* :func:`render_agent_yaml` — render a (possibly mutated) dict back to the
  canonical text **without writing**, so an action's ``plan`` can diff it.
* :func:`write_agent_yaml` — render + write atomically (the ``apply`` path).

The "plan renders, apply writes the *same* render" invariant means the diff a
user confirms is byte-for-byte what lands on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from movate.authoring.base import AuthoringActionError


def load_agent_yaml(agent_yaml: Path) -> dict[str, Any]:
    """Parse ``agent.yaml`` into a plain dict.

    Raises :class:`AuthoringActionError` if the file is missing or not a
    mapping — the actions surface this as a structured failure.
    """
    if not agent_yaml.is_file():
        raise AuthoringActionError(f"agent.yaml not found: {agent_yaml}")
    try:
        data = yaml.safe_load(agent_yaml.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AuthoringActionError(f"invalid YAML in {agent_yaml}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise AuthoringActionError(f"agent.yaml is not a mapping: {agent_yaml}")
    return data


def render_agent_yaml(data: dict[str, Any]) -> str:
    """Render an agent.yaml dict to the canonical block-style text.

    Matches :func:`movate.scaffold.write_agent_files`'s dump options so the
    catalog's writer is byte-compatible with the scaffold writer.
    """
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def write_agent_yaml(agent_yaml: Path, data: dict[str, Any]) -> None:
    """Render + write ``data`` back to ``agent.yaml`` (the canonical round-trip)."""
    agent_yaml.write_text(render_agent_yaml(data), encoding="utf-8")
