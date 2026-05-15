"""``mdk fmt`` — gofmt/prettier for movate config files.

Normalizes:

* ``agent.yaml`` / ``movate.yaml`` / ``policy.yaml`` — YAML key order
  (for known schemas), indentation, trailing newline.
* ``prompt.md`` and other prompts — trailing whitespace per line,
  consistent newline at EOF.
* ``*.jsonl`` (eval datasets) — strip blank lines, validate each
  line is JSON, normalize whitespace inside each line.

The premise: every mature dev ecosystem ends up with a formatter so
PRs stop debating style (does ``model`` come before ``prompt`` in
agent.yaml? do prompts end with a blank line?). One canonical answer
checked in CI, zero debate ever.

Three modes:

* **write** (default) — rewrite each file in place.
* ``--check`` — exit non-zero if anything would change. CI mode.
* ``--diff`` — print a unified diff per changed file, don't write.

Module layout:

* :func:`format_text` — pure function: given file contents + path,
  return formatted contents. The CLI is a thin wrapper. Easy to test;
  easy to wire into pre-commit / editor save hooks later.
* :func:`format_file` — convenience: reads + writes through
  :func:`format_text`.
"""

from __future__ import annotations

from movate.fmt.formatter import (
    AGENT_YAML_KEY_ORDER,
    MOVATE_YAML_KEY_ORDER,
    POLICY_YAML_KEY_ORDER,
    FormatError,
    FormatResult,
    detect_format,
    format_file,
    format_jsonl,
    format_prompt,
    format_text,
    format_yaml,
)

__all__ = [
    "AGENT_YAML_KEY_ORDER",
    "MOVATE_YAML_KEY_ORDER",
    "POLICY_YAML_KEY_ORDER",
    "FormatError",
    "FormatResult",
    "detect_format",
    "format_file",
    "format_jsonl",
    "format_prompt",
    "format_text",
    "format_yaml",
]
