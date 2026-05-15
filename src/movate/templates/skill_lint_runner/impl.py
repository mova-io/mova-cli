"""Implementation for the __SKILL_NAME__ skill — Python lint via ruff.

Why ruff:

* **Fast** — written in Rust; results come back in <100ms for most
  files. Doesn't slow down the agent's reasoning loop.
* **Single binary** — no per-rule plugin install dance.
* **Stable JSON output** — `--output-format json` returns a
  predictable schema we can map directly to the skill's output.
* **Covers the common cases** — bugbear, isort, pyflakes, pycodestyle,
  pylint subset. Good signal for a code-review agent without
  drowning it in stylistic nits.

The skill is intentionally LOCAL — it expects `ruff` on PATH. For
remote / containerized agents, swap this impl for one that posts the
file to a sandbox service. The input/output schema stays the same.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.skill_backend import SkillExecutionContext


# Max subprocess time. ruff is fast; if it hangs past 30s something
# is wrong (huge directory, locked filesystem). We surface a clean
# warning rather than blocking the agent forever.
_DEFAULT_TIMEOUT_S = 30.0


async def run(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Run ``ruff check`` on the requested path; return findings.

    Returns ``{"findings": [...], "ruff_version": "..."}``. Each
    finding is a dict with ``file``, ``line``, ``column``, ``code``,
    ``message``, ``severity``. On failure (binary missing, timeout,
    invalid JSON), returns an empty findings list + a ``warning``
    field describing what went wrong — the agent sees the error
    rather than crashing.

    The ``select`` input limits which rule codes ruff reports
    (e.g. ``["E501", "F401"]``); empty means "use project defaults".
    """
    path = input["path"]
    select = input.get("select") or []

    # Honor call_ms_budget when set; floor at 5s and cap at 60s so
    # the agent can't accidentally hang the worker.
    if ctx.call_ms_budget:
        timeout_s = max(5.0, min(60.0, ctx.call_ms_budget / 1000.0))
    else:
        timeout_s = _DEFAULT_TIMEOUT_S

    cmd = ["ruff", "check", "--output-format", "json", path]
    if select:
        # ruff's --select takes a comma-separated list. The skill's
        # input is an array (more natural for the LLM); we join here.
        cmd.extend(["--select", ",".join(select)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except FileNotFoundError:
        return {
            "findings": [],
            "ruff_version": "",
            "warning": (
                "`ruff` binary not on PATH. Install with `pip install ruff` "
                "or `uv tool install ruff`."
            ),
        }
    except TimeoutError:
        return {
            "findings": [],
            "ruff_version": "",
            "warning": f"ruff exceeded {timeout_s:.0f}s timeout on {path}",
        }

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    # ruff exits 1 when it finds issues — that's NOT an error from
    # our point of view (it's the signal we want). Exit codes other
    # than 0 or 1 indicate the tool itself broke.
    if proc.returncode not in (0, 1):
        return {
            "findings": [],
            "ruff_version": "",
            "warning": f"ruff exited {proc.returncode}: {stderr.strip()[:300]}",
        }

    # ruff --output-format json returns a list of finding dicts. An
    # empty list = no issues. Bad JSON = ruff broke its own contract.
    try:
        raw_findings = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError as exc:
        return {
            "findings": [],
            "ruff_version": "",
            "warning": f"could not parse ruff JSON output: {exc}",
        }

    # Map ruff's schema to the skill's stable output schema. Ruff's
    # native fields (filename, location, code, message) translate
    # 1:1; we add `severity` derived from code prefix (E/W/F/N/B...)
    # so the agent can sort findings without a code-table.
    findings = [
        {
            "file": f.get("filename", ""),
            "line": f.get("location", {}).get("row", 0),
            "column": f.get("location", {}).get("column", 0),
            "code": f.get("code", ""),
            "message": f.get("message", ""),
            "severity": _severity_for(f.get("code", "")),
        }
        for f in raw_findings
    ]

    return {
        "findings": findings,
        "ruff_version": await _ruff_version(),
    }


def _severity_for(code: str) -> str:
    """Map ruff rule prefixes to a 3-tier severity bucket.

    The bucketing matches the conventions ruff itself uses in its
    rule taxonomy: E/F are correctness-likely-bugs, W is warnings,
    everything else is style/nit-grade.
    """
    if not code:
        return "info"
    prefix = code[0]
    if prefix in ("E", "F", "B"):
        return "error"
    if prefix in ("W",):
        return "warning"
    return "info"


async def _ruff_version() -> str:
    """Run `ruff --version` and return its output (best-effort)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ruff",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
