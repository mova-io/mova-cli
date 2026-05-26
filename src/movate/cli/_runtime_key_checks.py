"""Shared runtime-bearer auth-check helpers for the ``movate`` CLI.

Two small helpers used by BOTH ``movate auth`` (the pull / seed /
refresh-runtime-key adopt paths) and ``movate deploy`` (the agents-mode
auto-recovery path) after they mint / pull / save a runtime bearer key:

* :func:`_verify_bearer_roundtrip` — confirm a candidate bearer is
  ADMIN-capable against a deployed runtime (the capability uploads need).
* :func:`_warn_if_shell_shadows_runtime_key` — warn when a stale shell
  export will shadow the just-saved key (shell wins over the credentials
  file unless the saved runtime key is file-authoritative, ADR 022).

Both used to be duplicated across ``cli/auth.py`` and ``cli/deploy.py``
(#103). They now live here — a leaf module that imports nothing from
``auth`` or ``deploy``, so both can import it at module load without the
auth↔deploy circular-import risk that previously forced lazy imports.

Functions stay private (leading underscore) but are importable across the
``movate.cli`` package — the existing convention for cross-module CLI
helpers.
"""

from __future__ import annotations

import os

import httpx
from rich.console import Console

err = Console(stderr=True)

# Status-code thresholds for :func:`_verify_bearer_roundtrip`. Kept local
# to this module so it has no dependency on ``deploy``'s constant block.
_HTTP_BAD_REQUEST = 400
_HTTP_FORBIDDEN = 403


def _warn_if_shell_shadows_runtime_key(*, key_env: str, fresh_key: str) -> None:
    """Warn when a stale ``$<key_env>`` in the shell will shadow the just-saved key.

    Shell-exported env vars take precedence over ``~/.movate/credentials``
    (autoload only fills a var that isn't already set; it never clobbers a
    shell export). So a stale bearer left over from an earlier deploy would
    OVERRIDE the fresh key just minted + saved — and the next ``mdk run
    --target`` would send the stale key and 401. Warn at save time, on
    stderr, rather than letting the operator discover it through a confusing
    401 later. Never fails the command.

    Only warns when the resolved value's source is the *shell* (via
    :func:`credentials.key_source`, the same primitive ``mdk auth status``
    uses) AND it DIFFERS from the freshly-saved key. Under ADR 022 a
    file-authoritative runtime key means the saved value can win over a
    differing shell export — in which case ``key_source`` reports
    ``credentials_file`` and there is nothing to warn about, so the gate
    correctly stays silent.
    """
    from movate.credentials import key_source  # noqa: PLC0415

    shell_value = os.environ.get(key_env, "").strip()
    if key_source(key_env) == "shell" and shell_value and shell_value != fresh_key:
        err.print(
            f"[yellow]⚠[/yellow] a stale [bold]{key_env}[/bold] is exported in your "
            f"shell and will OVERRIDE the key just saved (shell wins) — run "
            f"[bold]unset {key_env}[/bold] (and remove it from your profile) so the "
            f"new key takes effect."
        )
        err.print(
            f"  Fix it: [cyan]mdk fix unshadow-runtime-keys --apply[/cyan] "
            f"(comments the stale export) — then [bold]unset {key_env}[/bold] in "
            f"this shell."
        )


def _verify_bearer_roundtrip(*, base_url: str, key: str) -> tuple[bool, str]:
    """Confirm a candidate bearer is ADMIN-capable — the capability a deploy needs.

    The deploy bearer performs admin uploads (``POST/PUT /api/v1/agents``,
    both gated on the ``admin`` scope). A bearer that merely authenticates
    (``read``) is NOT good enough: it sails through a ``GET /api/v1/agents``
    probe yet 403s on the very first agent upload. So this probes the
    admin-scoped, read-only ``GET /api/v1/auth/keys`` endpoint instead and
    only declares the bearer ready when the runtime grants admin.

    Returns ``(verified, reason)``:

    * **2xx** — authenticated AND admin-capable → ``(True, "")``.
    * **403** — authenticated but the key lacks the ``admin`` scope (the
      live regression: an in-pod mint defaulted to ``read,run,eval``) →
      ``(False, "HTTP 403 (key lacks admin scope; uploads need admin)")``.
    * **401** — bad/unknown bearer → ``(False, "HTTP 401")``.
    * transport error → ``(False, "runtime unreachable (...)")``.

    The recovery path uses this so it never declares "bearer key ready" — nor
    overwrites a previously-working saved key — for a candidate that can't
    actually deploy.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(
                f"{base_url}/api/v1/auth/keys",
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        return False, f"runtime unreachable ({type(exc).__name__})"
    if resp.status_code < _HTTP_BAD_REQUEST:
        return True, ""
    if resp.status_code == _HTTP_FORBIDDEN:
        return False, "HTTP 403 (key lacks admin scope; uploads need admin)"
    return False, f"HTTP {resp.status_code}"
