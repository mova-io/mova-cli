"""Format implementations — pure functions, no I/O except :func:`format_file`.

Each formatter takes a string, returns a string. Idempotent: running
the formatter twice produces the same output as running it once. This
property is checked in tests — a formatter that's not idempotent is
useless (the CI ``--check`` mode would fight the operator's editor).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml


class FormatError(Exception):
    """Raised when a file can't be parsed by its expected format.

    e.g. ``foo.yaml`` is invalid YAML, or ``dataset.jsonl`` has a line
    that isn't valid JSON. Caller (the CLI) maps this to exit-2 with
    the offending file path so the operator can fix it manually.
    """


# ---------------------------------------------------------------------------
# Canonical key orderings for known YAML schemas
# ---------------------------------------------------------------------------


# AgentSpec key order — derived from movate.core.models and PRD §3.
# Top-of-file = identity; middle = behavior; bottom = metadata / advanced.
# Keys not listed here preserve their original relative position
# AFTER the canonical block.
AGENT_YAML_KEY_ORDER: tuple[str, ...] = (
    "api_version",
    "kind",
    "name",
    "description",
    "owner",
    "model",
    "providers",
    "prompt",
    "input_schema",
    "output_schema",
    "policy",
    "memory",
    "knowledge",
    "skills",
    "tools",
    "context",
    "guardrails",
    "reflection",
    "eval",
    "metadata",
    "tags",
)

# Top-level movate.yaml — project config.
MOVATE_YAML_KEY_ORDER: tuple[str, ...] = (
    "api_version",
    "kind",
    "name",
    "description",
    "version",
    "owner",
    "defaults",
    "providers",
    "storage",
    "tracing",
    "policy",
    "deploy",
    "tenants",
    "metadata",
)

# Policy YAML — guardrail / model-policy file.
POLICY_YAML_KEY_ORDER: tuple[str, ...] = (
    "api_version",
    "kind",
    "name",
    "description",
    "allowed_providers",
    "denied_providers",
    "max_cost_per_run",
    "max_tokens",
    "fallback_chain",
    "rules",
    "metadata",
)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


class Format(StrEnum):
    """Recognized file formats. Unknown files raise on attempt to format."""

    AGENT_YAML = "agent_yaml"
    MOVATE_YAML = "movate_yaml"
    POLICY_YAML = "policy_yaml"
    GENERIC_YAML = "generic_yaml"
    PROMPT = "prompt"
    JSONL = "jsonl"


def detect_format(path: Path) -> Format | None:
    """Guess the format from filename + extension. Returns ``None`` if
    we don't recognize the file.

    Detection order matters: we want ``agents/foo/agent.yaml`` to map
    to :attr:`Format.AGENT_YAML` (which knows the canonical key order),
    not :attr:`Format.GENERIC_YAML`. The CLI walker uses this to skip
    files we don't know how to format.
    """
    name = path.name.lower()
    suffix = path.suffix.lower()

    if name == "agent.yaml":
        return Format.AGENT_YAML
    if name in {"movate.yaml", "mdk.yaml"}:
        return Format.MOVATE_YAML
    if name == "policy.yaml":
        return Format.POLICY_YAML
    if suffix in {".yaml", ".yml"}:
        return Format.GENERIC_YAML
    if name == "prompt.md" or (
        suffix == ".md" and ("prompts" in path.parts or "contexts" in path.parts)
    ):
        return Format.PROMPT
    if suffix == ".jsonl":
        return Format.JSONL
    if suffix == ".json" and "kb" in path.parts:
        return Format.JSONL
    return None


# ---------------------------------------------------------------------------
# YAML formatter
# ---------------------------------------------------------------------------


def format_yaml(text: str, *, key_order: tuple[str, ...] = ()) -> str:
    """Re-emit YAML with normalized indent + key order.

    If ``key_order`` is provided, top-level mapping keys are reordered
    to match (unknown keys go to the end, preserving their relative
    order). Nested mappings keep their original order — we don't try
    to be clever about deep schemas in MVP.

    Raises :class:`FormatError` on invalid YAML.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FormatError(f"invalid YAML: {exc}") from exc

    if data is None:
        # Empty / comment-only file: preserve as a single trailing newline.
        return ""

    if isinstance(data, dict) and key_order:
        data = _reorder_keys(data, key_order)

    # Canonical emit options: block style, 2-space indent, no flow
    # collections at the top level, sort_keys=False (we control order).
    return yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        allow_unicode=True,
        width=100,
    )


def _reorder_keys(data: dict, order: tuple[str, ...]) -> dict:
    """Return a new dict with keys ordered per ``order``, unknowns last.

    Preserves original *relative* order of unknown keys — this matters
    so operators who add a custom field don't see it bouncing around
    on every fmt run.
    """
    result: dict = {}
    seen: set[str] = set()
    for key in order:
        if key in data:
            result[key] = data[key]
            seen.add(key)
    for key, value in data.items():
        if key not in seen:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Prompt (Markdown) formatter
# ---------------------------------------------------------------------------


def format_prompt(text: str) -> str:
    """Normalize a Markdown prompt file.

    Rules:
    1. Strip trailing whitespace from every line.
    2. Collapse runs of blank lines to at most one.
    3. Ensure exactly one trailing newline at EOF.

    Leading whitespace is NOT touched — indentation often matters in
    prompts (code blocks, structured examples).
    """
    lines = text.splitlines()
    stripped = [line.rstrip() for line in lines]

    # Collapse multi-blank-line runs.
    collapsed: list[str] = []
    last_was_blank = False
    for line in stripped:
        is_blank = line == ""
        if is_blank and last_was_blank:
            continue
        collapsed.append(line)
        last_was_blank = is_blank

    # Drop trailing blank lines, then guarantee exactly one final newline.
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    if not collapsed:
        return ""
    return "\n".join(collapsed) + "\n"


# ---------------------------------------------------------------------------
# JSONL formatter
# ---------------------------------------------------------------------------


def format_jsonl(text: str) -> str:
    """Normalize a JSONL file (one JSON object per non-blank line).

    Rules:
    1. Blank lines are dropped.
    2. Each non-blank line must parse as JSON; invalid → :class:`FormatError`.
    3. Re-emit each line with ``json.dumps`` for canonical whitespace
       (no trailing whitespace, no extra spaces around colons / commas).
    4. Single trailing newline at EOF.

    Note: we deliberately don't sort keys inside individual records —
    eval datasets often have an implicit narrative order (input first,
    then expected_output) that operators want preserved.
    """
    lines = text.splitlines()
    out: list[str] = []
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise FormatError(f"line {lineno} is not valid JSON: {exc.msg}") from exc
        # ensure_ascii=False so embedded unicode (eg. é, 日本語) survives
        # round-trip; separators control whitespace tightly.
        out.append(json.dumps(obj, ensure_ascii=False, separators=(", ", ": ")))
    if not out:
        return ""
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Generic entry point — dispatch on detected format
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormatResult:
    """Outcome of formatting one file.

    ``changed`` is the field the CLI cares about most — drives the
    ``--check`` exit code and the "N files reformatted" summary.
    ``after`` is the formatted text (== before if changed is False).
    """

    path: Path
    before: str
    after: str
    format: Format

    @property
    def changed(self) -> bool:
        return self.before != self.after


def format_text(path: Path, text: str) -> FormatResult:
    """Format ``text`` per the format detected from ``path``.

    Raises :class:`FormatError` if the file's format isn't recognized
    OR if the parser rejects the content. The CLI distinguishes "not
    a formattable file" (skip silently) from "invalid content" (loud
    operator error) by checking :func:`detect_format` first.
    """
    fmt = detect_format(path)
    if fmt is None:
        raise FormatError(f"unrecognized file format: {path.name}")

    if fmt is Format.AGENT_YAML:
        after = format_yaml(text, key_order=AGENT_YAML_KEY_ORDER)
    elif fmt is Format.MOVATE_YAML:
        after = format_yaml(text, key_order=MOVATE_YAML_KEY_ORDER)
    elif fmt is Format.POLICY_YAML:
        after = format_yaml(text, key_order=POLICY_YAML_KEY_ORDER)
    elif fmt is Format.GENERIC_YAML:
        after = format_yaml(text)
    elif fmt is Format.PROMPT:
        after = format_prompt(text)
    elif fmt is Format.JSONL:
        after = format_jsonl(text)
    else:  # pragma: no cover — exhaustive enum dispatch
        raise FormatError(f"no formatter for {fmt}")

    return FormatResult(path=path, before=text, after=after, format=fmt)


def format_file(path: Path, *, write: bool = True) -> FormatResult:
    """Read ``path``, format, and (by default) write back if changed.

    Use ``write=False`` for ``--check`` / ``--diff`` modes — the
    result still tells you ``.changed``, but the file isn't touched.
    Atomic via temp+rename so a crash mid-write can't corrupt the
    file (the formatter is most-likely-running-in-CI scenario).
    """
    text = path.read_text()
    result = format_text(path, text)
    if write and result.changed:
        tmp = path.with_suffix(path.suffix + ".fmt.tmp")
        tmp.write_text(result.after)
        tmp.replace(path)
    return result
