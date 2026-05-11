"""Pure logic for ``movate diff`` — compare two agent directories.

The CLI surface is in :mod:`movate.cli.diff`. This module knows nothing
about rendering; it just loads both agents and produces structured
deltas. That separation means the same `diff_agents` function can power
the Rich terminal output, the ``-o json`` machine-readable output, and
the ``-o markdown`` PR-description output without any rendering
ambiguity.

The output is intentionally **wide**: every comparable field shows up in
`AgentDiff.field_deltas` even when unchanged. Renderers decide whether
to hide unchanged rows (default) or surface them (`--verbose`). This is
cheaper than threading "show unchanged?" through the comparison pass.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from movate.core.loader import AgentBundle, AgentLoadError, load_agent

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDelta:
    """One comparable scalar/structured field.

    Both ``a`` and ``b`` may be ``None`` to indicate "field absent on this
    side" (e.g. one agent declares ``owner`` and the other doesn't).
    """

    name: str
    a: Any | None
    b: Any | None

    @property
    def changed(self) -> bool:
        return self.a != self.b


@dataclass
class DatasetSummary:
    """Cheap structural summary of an eval dataset (JSONL).

    The full dataset isn't diffed in detail — operators run `movate eval`
    for that. The summary is enough to spot "case count dropped" or
    "dataset rewritten" at a glance.
    """

    path: str
    """Dataset path as declared in agent.yaml (relative to agent_dir)."""

    exists: bool
    case_count: int = 0
    sha256: str = ""

    @classmethod
    def from_bundle(cls, bundle: AgentBundle) -> DatasetSummary | None:
        if not bundle.spec.evals.dataset:
            return None
        path = bundle.spec.evals.dataset
        full = (bundle.agent_dir / path).resolve()
        if not full.exists():
            return cls(path=path, exists=False)
        raw = full.read_bytes()
        case_count = sum(1 for line in raw.decode().splitlines() if line.strip())
        return cls(
            path=path,
            exists=True,
            case_count=case_count,
            sha256=hashlib.sha256(raw).hexdigest(),
        )


@dataclass
class AgentDiff:
    """Structured comparison of two agents.

    Renderers consume this — see :mod:`movate.cli.diff`.
    """

    a_path: Path
    b_path: Path
    a_name: str
    b_name: str
    a_version: str
    b_version: str

    field_deltas: list[FieldDelta] = field(default_factory=list)
    """Every metadata field, in render order. Includes unchanged rows so
    renderers can offer a `--verbose` view."""

    a_prompt_hash: str = ""
    b_prompt_hash: str = ""
    a_prompt_template: str = ""
    b_prompt_template: str = ""

    a_input_schema: dict[str, Any] = field(default_factory=dict)
    b_input_schema: dict[str, Any] = field(default_factory=dict)
    a_output_schema: dict[str, Any] = field(default_factory=dict)
    b_output_schema: dict[str, Any] = field(default_factory=dict)

    a_dataset: DatasetSummary | None = None
    b_dataset: DatasetSummary | None = None

    # ----- derived helpers ---------------------------------------------------

    @property
    def prompt_changed(self) -> bool:
        return self.a_prompt_hash != self.b_prompt_hash

    @property
    def input_schema_changed(self) -> bool:
        return self.a_input_schema != self.b_input_schema

    @property
    def output_schema_changed(self) -> bool:
        return self.a_output_schema != self.b_output_schema

    @property
    def dataset_changed(self) -> bool:
        if self.a_dataset is None and self.b_dataset is None:
            return False
        if self.a_dataset is None or self.b_dataset is None:
            return True
        return self.a_dataset.sha256 != self.b_dataset.sha256

    def changed_field_deltas(self) -> list[FieldDelta]:
        return [d for d in self.field_deltas if d.changed]

    def has_any_change(self) -> bool:
        return (
            any(d.changed for d in self.field_deltas)
            or self.prompt_changed
            or self.input_schema_changed
            or self.output_schema_changed
            or self.dataset_changed
        )

    # ----- unified-diff helpers (used by renderers) --------------------------

    def prompt_unified_diff(self, context_lines: int = 3) -> str:
        """Standard unified diff of the prompt templates. Empty string if
        the prompts are byte-identical."""
        if not self.prompt_changed:
            return ""
        a_lines = self.a_prompt_template.splitlines(keepends=True)
        b_lines = self.b_prompt_template.splitlines(keepends=True)
        diff = difflib.unified_diff(
            a_lines,
            b_lines,
            fromfile=f"{self.a_name}/prompt",
            tofile=f"{self.b_name}/prompt",
            n=context_lines,
        )
        return "".join(diff)

    def schema_unified_diff(self, which: str, context_lines: int = 3) -> str:
        """Unified diff of an input or output schema, rendered as pretty
        JSON so semantic changes (e.g. a new required field) are visible
        line-by-line."""
        if which == "input":
            a, b, changed = self.a_input_schema, self.b_input_schema, self.input_schema_changed
            label = "input.schema"
        elif which == "output":
            a, b, changed = self.a_output_schema, self.b_output_schema, self.output_schema_changed
            label = "output.schema"
        else:
            raise ValueError(f"unknown schema: {which!r}")
        if not changed:
            return ""
        a_json = json.dumps(a, indent=2, sort_keys=True).splitlines(keepends=True)
        b_json = json.dumps(b, indent=2, sort_keys=True).splitlines(keepends=True)
        diff = difflib.unified_diff(
            a_json,
            b_json,
            fromfile=f"{self.a_name}/{label}",
            tofile=f"{self.b_name}/{label}",
            n=context_lines,
        )
        return "".join(diff)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AgentDiffError(Exception):
    """Raised when one or both sides fail to load. Bubbles to exit-code 2."""


def diff_agents(a_path: Path, b_path: Path) -> AgentDiff:
    """Load both agents and produce a structured diff.

    Raises :class:`AgentDiffError` if either side fails to load — the
    caller should map that to a non-zero exit so a malformed agent
    doesn't masquerade as "no differences".
    """
    try:
        a_bundle = load_agent(a_path)
    except AgentLoadError as exc:
        raise AgentDiffError(f"failed to load {a_path}: {exc}") from exc
    try:
        b_bundle = load_agent(b_path)
    except AgentLoadError as exc:
        raise AgentDiffError(f"failed to load {b_path}: {exc}") from exc

    return _build_diff(a_bundle, b_bundle, a_path, b_path)


def _build_diff(
    a: AgentBundle,
    b: AgentBundle,
    a_path: Path,
    b_path: Path,
) -> AgentDiff:
    """Pure factory — kept separate from :func:`diff_agents` so tests
    can build AgentDiff directly from in-memory bundles without writing
    YAML to disk."""

    deltas: list[FieldDelta] = [
        FieldDelta("api_version", a.spec.api_version, b.spec.api_version),
        FieldDelta("kind", a.spec.kind, b.spec.kind),
        FieldDelta("name", a.spec.name, b.spec.name),
        FieldDelta("version", a.spec.version, b.spec.version),
        FieldDelta("description", a.spec.description or None, b.spec.description or None),
        FieldDelta("owner", a.spec.owner or None, b.spec.owner or None),
        FieldDelta("runtime", a.spec.runtime.value, b.spec.runtime.value),
        FieldDelta("model.provider", a.spec.model.provider, b.spec.model.provider),
        FieldDelta(
            "model.params",
            a.spec.model.params or None,
            b.spec.model.params or None,
        ),
        FieldDelta(
            "model.fallback",
            tuple(f.provider for f in a.spec.model.fallback) or None,
            tuple(f.provider for f in b.spec.model.fallback) or None,
        ),
    ]

    return AgentDiff(
        a_path=a_path,
        b_path=b_path,
        a_name=a.spec.name,
        b_name=b.spec.name,
        a_version=a.spec.version,
        b_version=b.spec.version,
        field_deltas=deltas,
        a_prompt_hash=a.prompt_hash,
        b_prompt_hash=b.prompt_hash,
        a_prompt_template=a.prompt_template,
        b_prompt_template=b.prompt_template,
        a_input_schema=a.input_schema,
        b_input_schema=b.input_schema,
        a_output_schema=a.output_schema,
        b_output_schema=b.output_schema,
        a_dataset=DatasetSummary.from_bundle(a),
        b_dataset=DatasetSummary.from_bundle(b),
    )


# ---------------------------------------------------------------------------
# JSON / Markdown renderers — kept here (not in cli/diff.py) so tests can
# assert on the rendered output without booting the CliRunner.
# ---------------------------------------------------------------------------


def render_diff_json(d: AgentDiff) -> str:
    """Stable, sorted-key JSON for piping into other tools."""
    payload: dict[str, Any] = {
        "a": {
            "path": str(d.a_path),
            "name": d.a_name,
            "version": d.a_version,
            "prompt_hash": d.a_prompt_hash,
        },
        "b": {
            "path": str(d.b_path),
            "name": d.b_name,
            "version": d.b_version,
            "prompt_hash": d.b_prompt_hash,
        },
        "fields": [
            {"name": x.name, "a": x.a, "b": x.b, "changed": x.changed}
            for x in d.field_deltas
        ],
        "prompt_changed": d.prompt_changed,
        "input_schema_changed": d.input_schema_changed,
        "output_schema_changed": d.output_schema_changed,
        "dataset_changed": d.dataset_changed,
        "has_any_change": d.has_any_change(),
    }
    if d.a_dataset is not None:
        payload["a"]["dataset"] = {
            "path": d.a_dataset.path,
            "exists": d.a_dataset.exists,
            "case_count": d.a_dataset.case_count,
            "sha256": d.a_dataset.sha256,
        }
    if d.b_dataset is not None:
        payload["b"]["dataset"] = {
            "path": d.b_dataset.path,
            "exists": d.b_dataset.exists,
            "case_count": d.b_dataset.case_count,
            "sha256": d.b_dataset.sha256,
        }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_diff_markdown(d: AgentDiff) -> str:
    """GFM-flavoured markdown — paste into a PR description.

    Renders only the changed rows; "no changes" surfaces as a single
    line so reviewers can tell the diff actually ran. Includes the
    prompt unified diff in a fenced ``diff`` block when relevant.
    """
    lines: list[str] = []
    lines.append(f"## `movate diff` — {d.a_name} → {d.b_name}\n")

    if not d.has_any_change():
        lines.append("_No differences detected._\n")
        return "".join(lines) + "\n"

    changed = d.changed_field_deltas()
    if changed:
        lines.append("### Metadata\n")
        lines.append("| field | before | after |\n")
        lines.append("|---|---|---|\n")
        for x in changed:
            lines.append(f"| `{x.name}` | {_md_cell(x.a)} | {_md_cell(x.b)} |\n")
        lines.append("\n")

    if d.prompt_changed:
        lines.append("### Prompt\n")
        lines.append(f"Hash: `{d.a_prompt_hash[:12]}…` → `{d.b_prompt_hash[:12]}…`\n\n")
        lines.append("```diff\n")
        lines.append(d.prompt_unified_diff())
        lines.append("```\n\n")

    if d.input_schema_changed:
        lines.append("### `input.schema`\n")
        lines.append("```diff\n")
        lines.append(d.schema_unified_diff("input"))
        lines.append("```\n\n")

    if d.output_schema_changed:
        lines.append("### `output.schema`\n")
        lines.append("```diff\n")
        lines.append(d.schema_unified_diff("output"))
        lines.append("```\n\n")

    if d.dataset_changed:
        lines.append("### `evals.dataset`\n")
        lines.append(_md_dataset_row(d.a_dataset, d.b_dataset))
        lines.append("\n")

    return "".join(lines)


def _md_cell(value: Any) -> str:
    """Render a single cell value safely for GFM. Backticks become escaped,
    pipes are escaped (would break the column), and None becomes em-dash."""
    if value is None:
        return "—"
    text = json.dumps(value) if isinstance(value, (dict, list, tuple)) else str(value)
    return "`" + text.replace("`", "\\`").replace("|", "\\|") + "`"


def _md_dataset_row(
    a: DatasetSummary | None,
    b: DatasetSummary | None,
) -> str:
    def fmt(ds: DatasetSummary | None) -> str:
        if ds is None:
            return "_(no dataset)_"
        if not ds.exists:
            return f"`{ds.path}` _(missing)_"
        return f"`{ds.path}` ({ds.case_count} cases, sha=`{ds.sha256[:12]}…`)"

    return f"- before: {fmt(a)}\n- after:  {fmt(b)}\n"
