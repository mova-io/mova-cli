"""``mdk env`` — env-var management (Sprint O Day 8-9).

Distinct from ``mdk secrets`` (values): this module owns **names +
presence**. The operator question we answer:

  "What env vars does my project need? Are they all set in my shell?
   Do they match the deploy-target's set?"

Discovery sources (in priority order, deduped by name):

1. ``.env.example`` at the project root — operator-curated list. The
   ground truth when present; everything else is best-effort
   detection.
2. ``$VAR`` / ``${VAR}`` references in every ``agent.yaml`` —
   typically model API keys threaded into provider params.
3. ``os.environ["VAR"]`` / ``os.environ.get("VAR")`` references in
   every skill's ``impl.py`` and the project's Python sources under
   common locations.

Why not parse imports? Discovery is intentionally lexical (regex)
rather than AST-based. False positives are rarer than false negatives
in practice, and the audit's job is to surface the operator's intent,
not to perfectly enumerate every runtime branch. When the operator
disagrees with the discovered set, ``.env.example`` is the override.

Naming note: the module is ``movate.env_mgmt`` (not ``movate.env``)
to avoid ambiguity with environment-variable libraries downstream
might pull in. The CLI verb stays ``mdk env``.
"""

from __future__ import annotations

from movate.env_mgmt.discovery import (
    EnvSource,
    EnvVarRef,
    discover_env_vars,
    parse_env_example,
)

__all__ = [
    "EnvSource",
    "EnvVarRef",
    "discover_env_vars",
    "parse_env_example",
]
