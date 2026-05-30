"""Tests for ``scripts/gen_roadmap.py`` — the self-syncing roadmap generator.

Per ADR 058, ``ROADMAP.md`` is a generated, CI-freshness-gated artifact joined
from ``roadmap.yaml`` (intent) + ``shipped.jsonl`` (CalVer-keyed ship ledger).
These tests pin the contract that makes the roadmap structurally incapable of
drifting:

* ``--check`` passes on the committed artifact (the CI gate is green at HEAD).
* a ``roadmap.yaml`` edit without a regenerate makes ``--check`` fail (the gate
  actually catches staleness).
* the Shipped table is sorted newest-CalVer-first (the D5 ordering contract).
* ``shipped.jsonl`` and ``roadmap.yaml`` stay consistent (every ledger line maps
  to a ``status: shipped`` item at the same version/PR, and vice-versa).

The script is loaded via ``importlib`` (the same pattern as
``test_gen_daily_changelog.py`` / ``test_bump_version_script.py``) since
``scripts/`` isn't an importable package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gen_roadmap.py"
ROADMAP_YAML = REPO_ROOT / "roadmap.yaml"
SHIPPED_JSONL = REPO_ROOT / "shipped.jsonl"
ROADMAP_MD = REPO_ROOT / "ROADMAP.md"

_spec = importlib.util.spec_from_file_location("gen_roadmap", SCRIPT)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _run_check() -> int:
    """Invoke the script's ``--check`` path and return its exit code."""
    argv = ["gen_roadmap.py", "--check"]
    old = sys.argv
    sys.argv = argv
    try:
        return gen.main()
    finally:
        sys.argv = old


# --- The committed artifact is fresh (the CI gate is green at HEAD) ----------


def test_check_passes_on_committed_artifact() -> None:
    assert _run_check() == 0


def test_render_is_deterministic() -> None:
    """Re-rendering the same inputs yields byte-identical output (no timestamps)."""
    items = gen._load_items(ROADMAP_YAML)
    ledger = gen._load_ledger(SHIPPED_JSONL)
    assert gen.render(items, ledger) == gen.render(items, ledger)


def test_committed_md_matches_render() -> None:
    assert ROADMAP_MD.read_text(encoding="utf-8") == gen._build()


# --- A stale roadmap.yaml makes --check fail (the gate catches drift) --------


def test_check_fails_when_yaml_edited_without_regen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edit roadmap.yaml in-place (without regenerating ROADMAP.md) and assert
    --check goes red. Restores the file afterward so the repo is left clean."""
    original = ROADMAP_YAML.read_text(encoding="utf-8")
    try:
        # Append a new planned item — changes the render, not the committed md.
        ROADMAP_YAML.write_text(
            original
            + "\n  - id: a-brand-new-planned-item\n"
            + "    title: A brand new planned item\n"
            + "    status: planned\n",
            encoding="utf-8",
        )
        assert _run_check() == 1
    finally:
        ROADMAP_YAML.write_text(original, encoding="utf-8")
    # Sanity: once restored, the gate is green again.
    assert _run_check() == 0


# --- The Shipped table is CalVer-descending ----------------------------------


def test_shipped_table_is_calver_desc() -> None:
    items = gen._load_items(ROADMAP_YAML)
    ledger = gen._load_ledger(SHIPPED_JSONL)
    md = gen.render(items, ledger)

    # Pull the shipped_version column out of every table row, in render order.
    versions: list[str] = []
    in_table = False
    for line in md.splitlines():
        if line.startswith("| id | title |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|") or line.startswith("| --- "):
                if line.startswith("| --- "):
                    continue
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            versions.append(cells[-1])

    assert versions, "no shipped rows rendered"
    keys = [gen._version_key(v) for v in versions]
    assert keys == sorted(keys, reverse=True), f"shipped table not CalVer-desc: {versions}"


# --- shipped.jsonl <-> roadmap.yaml consistency ------------------------------


def test_ledger_and_yaml_are_consistent() -> None:
    items = {i["id"]: i for i in gen._load_items(ROADMAP_YAML)}
    ledger = gen._load_ledger(SHIPPED_JSONL)

    ledger_ids = set()
    for row in ledger:
        rid = row.get("id")
        assert rid in items, f"shipped.jsonl id {rid!r} has no roadmap.yaml item"
        ledger_ids.add(rid)
        item = items[rid]
        assert item.get("status") == "shipped", f"{rid} in ledger but not status: shipped"
        assert str(item.get("shipped_version")) == str(row.get("version")), (
            f"{rid}: version mismatch yaml vs ledger"
        )
        assert int(item.get("pr")) == int(row.get("pr")), f"{rid}: pr mismatch yaml vs ledger"

    # Every shipped roadmap item must have a ledger line (the durable record).
    for rid, item in items.items():
        if item.get("status") == "shipped":
            assert rid in ledger_ids, f"shipped item {rid} missing from shipped.jsonl"


def test_ledger_lines_are_valid_json_with_required_fields() -> None:
    for lineno, raw in enumerate(SHIPPED_JSONL.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        obj = json.loads(raw)
        for field in ("version", "pr", "id", "title"):
            assert field in obj, f"shipped.jsonl:{lineno} missing {field!r}"


def test_depends_on_targets_exist() -> None:
    """Every depends_on id must resolve to a real item (no dangling deps)."""
    items = {i["id"]: i for i in gen._load_items(ROADMAP_YAML)}
    for item in items.values():
        for dep in item.get("depends_on") or []:
            assert dep in items, f"{item['id']} depends_on unknown id {dep!r}"
