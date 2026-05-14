"""Tests for the ``ROLE_TEMPLATES`` catalog.

Companion to :mod:`tests.test_templates`. Where that file exercises
the legacy *shape* templates (``faq``, ``summarizer``, ...), this
file exercises the opinionated *role* templates (``support-triage``,
``sql-writer``, ...) introduced for ``mdk add``.

For each role we assert:

* The template dir is present and ships the four canonical files
  (``agent.yaml``, ``prompt.md``, ``evals/dataset.jsonl``, ``ROLE.md``).
* :func:`get_template_path` resolves the name (role-first lookup).
* :func:`load_agent` on the scaffolded copy succeeds — the schema
  shorthand parses, the prompt template loads, marketplace metadata
  validates.
* The eval dataset is well-formed JSONL with ``input`` + ``expected_output``.
* Marketplace metadata (``role``, ``persona``, ``capabilities``,
  ``tags``) is populated — the Mova iO wizard depends on these.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from movate.core.loader import load_agent
from movate.templates import (
    ROLE_TEMPLATES,
    get_template_path,
    list_roles,
    list_templates,
)

ROLE_NAMES = sorted(ROLE_TEMPLATES.keys())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_role_registry_exposes_expected_roles() -> None:
    """The five day-one roles must be present. New roles are welcome
    (this assertion uses ``>=``) but the original five are load-bearing
    for Mova iO's wizard catalog and customer docs."""
    expected = {
        "support-triage",
        "sql-writer",
        "reply-drafter",
        "text-classifier",
        "document-summarizer",
    }
    assert expected.issubset(set(ROLE_TEMPLATES.keys()))
    assert list_roles() == sorted(ROLE_TEMPLATES.keys())


@pytest.mark.unit
def test_roles_and_shapes_share_no_names() -> None:
    """Role names must not collide with shape-template names — the
    role-first lookup in :func:`get_template_path` would silently
    shadow shapes otherwise."""
    assert set(ROLE_TEMPLATES.keys()).isdisjoint(set(list_templates()))


@pytest.mark.unit
@pytest.mark.parametrize("name", ROLE_NAMES)
def test_role_dir_is_present_and_complete(name: str) -> None:
    """Every role ships agent.yaml + prompt.md + dataset + ROLE.md."""
    path = get_template_path(name)
    assert path.is_dir()
    assert (path / "agent.yaml").is_file()
    assert (path / "prompt.md").is_file()
    assert (path / "evals" / "dataset.jsonl").is_file()
    assert (path / "ROLE.md").is_file(), (
        f"role {name!r} missing ROLE.md — roles ship when-to-use guidance"
    )


# ---------------------------------------------------------------------------
# Load each role end-to-end
# ---------------------------------------------------------------------------


def _scaffold_role(dst: Path, *, role: str, name: str = "demo") -> Path:
    """Clone a role template into ``dst`` and stamp the agent name."""
    src = get_template_path(role)
    shutil.copytree(src, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.mark.unit
@pytest.mark.parametrize("role", ROLE_NAMES)
def test_role_loads_via_load_agent(role: str, tmp_path: Path) -> None:
    """Scaffolded role dir must validate end-to-end through the loader."""
    dst = tmp_path / role
    _scaffold_role(dst, role=role)
    bundle = load_agent(dst)
    assert bundle.spec.api_version == "movate/v1"
    assert bundle.spec.kind == "Agent"
    assert bundle.spec.name == "demo"
    # Role marketplace metadata is populated (load_agent surfaces these
    # via AgentSpec — the wizard reads them directly).
    assert bundle.spec.role == role
    assert bundle.spec.persona, f"role {role!r} ships an empty persona"
    assert bundle.spec.capabilities, f"role {role!r} ships no capabilities"
    assert bundle.spec.tags, f"role {role!r} ships no tags"


@pytest.mark.unit
@pytest.mark.parametrize("role", ROLE_NAMES)
def test_role_dataset_is_well_formed(role: str, tmp_path: Path) -> None:
    """Every dataset row parses + has input + expected_output keys."""
    dst = tmp_path / role
    _scaffold_role(dst, role=role)
    raw = (dst / "evals" / "dataset.jsonl").read_bytes().decode().splitlines()
    rows = [json.loads(line) for line in raw if line.strip()]
    assert len(rows) >= 1, f"role {role!r} ships an empty dataset"
    for row in rows:
        assert "input" in row, f"role {role!r} dataset row missing 'input': {row}"
        assert "expected_output" in row, (
            f"role {role!r} dataset row missing 'expected_output': {row}"
        )


@pytest.mark.unit
@pytest.mark.parametrize("role", ROLE_NAMES)
def test_role_no_unsubstituted_placeholder(role: str, tmp_path: Path) -> None:
    """After scaffolding + name substitution, ``__AGENT_NAME__`` must
    not appear anywhere in the agent directory. (Belt-and-braces — the
    placeholder leaking would land in the agent's surfaced name.)"""
    dst = tmp_path / role
    _scaffold_role(dst, role=role, name="demo")
    for entry in dst.rglob("*"):
        if entry.is_file() and entry.suffix in {".yaml", ".md"}:
            assert "__AGENT_NAME__" not in entry.read_text(), f"placeholder leaked in {entry}"
