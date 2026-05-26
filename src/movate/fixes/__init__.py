"""``mdk fix`` — auto-remediate common diagnostic findings (Sprint P).

The companion to ``mdk doctor``: doctor diagnoses, fix repairs. Every
fix is:

* **Idempotent** — running fix twice on a clean tree is a no-op.
* **Reversible** when reasonable — destructive operations (e.g. wiping
  a malformed state file) get an explicit confirmation gate.
* **Discoverable** — each fix is a :class:`Fix` instance with a label,
  description, and a single :meth:`Fix.apply` method. Adding a new
  fix is a one-class change; the dispatcher picks it up automatically.

Default behavior is :option:`--dry-run` (preview). Operators opt into
writes with :option:`--apply`. Same convention as ``mdk fmt`` /
``mdk migrate`` / ``mdk promote``.

Bundled fixes (MVP):

* ``ensure-movate-dir`` — create ``.movate/`` if absent
* ``ensure-gitignore`` — create ``.gitignore`` with movate ignores
* ``ensure-env-from-example`` — ``cp .env.example .env`` if .env missing
* ``fix-secrets-permissions`` — chmod 0600 on ``~/.movate/secrets/*.yaml``
* ``ensure-agents-dir`` — create empty ``agents/.gitkeep``
* ``fix-yaml-style`` — run ``mdk fmt`` over the project (delegates)
* ``unshadow-runtime-keys`` — comment a stale ``export <VAR>=...`` shell-profile
  line that shadows a saved key in ``~/.movate/credentials``

What we deliberately DON'T auto-fix:

* Missing API keys — operator must supply.
* Profile selection — explicit operator decision.
* Model-policy violations — security implications.
* Anything outside ``$project_root`` or ``~/.movate/`` — out of scope.
"""

from __future__ import annotations

from movate.fixes.registry import (
    Fix,
    FixResult,
    FixStatus,
    available_fixes,
    diagnose_and_fix,
)

__all__ = [
    "Fix",
    "FixResult",
    "FixStatus",
    "available_fixes",
    "diagnose_and_fix",
]
