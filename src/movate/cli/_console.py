"""Shared stderr console + --quiet-aware ``hint`` helper.

Background: every CLI command used to do
``err = Console(stderr=True); err.print("[dim]...status hint...[/dim]")``.
That works for errors and warnings (operators always want to see those),
but the dim "FYI" prints — "queued j-1 on dev", "no jobs found",
"watching N files" — were leaking into stderr regardless of ``--quiet``,
which breaks the pipe-friendly contract:

  $ movate submit faq-agent '{}' | jq .  # stdout: clean JSON
  $ movate submit faq-agent '{}' -q | jq .  # used to spew hints anyway

The fix is small: a single module-state bool that ``--quiet`` flips,
and a :func:`hint` helper that no-ops while quiet is on. The shared
stderr :data:`stderr` console is exposed so error/warning calls
(which must NEVER be silenced) can keep using it directly without
having to know about the quiet machinery.

Module state instead of an env var because:

* Tests can flip it via :func:`set_quiet` cleanly per-test
  (``monkeypatch`` resets module attrs on teardown).
* The CLI is one process — no subprocess fanout to worry about.
* Env var would also work but bloats the env namespace.
"""

from __future__ import annotations

import typer
from rich.console import Console

stderr = Console(stderr=True)
"""Shared stderr console. Use for error / warning prints that must
NEVER be silenced (--quiet doesn't suppress these on purpose)."""

_quiet: bool = False
_global_target: str | None = None


def set_quiet(value: bool) -> None:
    """Toggle the module-wide quiet flag. Called from the top-level
    Typer callback when ``--quiet`` is passed."""
    global _quiet
    _quiet = value


def is_quiet() -> bool:
    """Read the current quiet flag. Exposed for commands that need
    branching behaviour beyond a simple suppress (e.g. drop a
    spinner when quiet)."""
    return _quiet


def set_global_target(value: str | None) -> None:
    """Set the process-wide default deployment target. Called from the
    top-level Typer callback when ``movate -t <name>`` (or the
    ``MOVATE_TARGET`` env var) is set. Per-command ``--target`` flags
    still win — this is the fallback when none is given."""
    global _global_target
    _global_target = value


def get_global_target() -> str | None:
    """Read the process-wide default deployment target, or ``None``.

    The intended call site is in remote commands' resolve-target
    helper:

      effective = per_command_target or get_global_target()
      target_name, cfg = resolve_target(effective)

    ``resolve_target(None)`` falls back to the config's active
    target, so an unset global means "use the active target" — same
    behaviour as before this option existed."""
    return _global_target


def hint(message: str) -> None:
    """Print a status hint to stderr unless ``--quiet`` is set.

    Use for FYI lines — "queued j-1 on dev", "no jobs found",
    "watching N files" — that an operator wants in interactive mode
    but should NOT appear when stderr is being captured or piped.

    Hard rule: NEVER use this for error or warning messages. Those
    go through :func:`error` / :func:`warn` instead, which always
    survive ``--quiet``."""
    if _quiet:
        return
    stderr.print(message)


def error(message: str, *, context: str | None = None) -> None:
    """Print a red ``✗``-prefixed error to stderr. Always rendered,
    even under ``--quiet`` — operators must see failure.

    With ``context`` we get ``✗ <context>:`` as the prefix, which is
    the right shape for "operation X failed because: <reason>":

      error("connection refused", context="submit")
      # → ✗ submit failed: connection refused

      error("env must be 'live' or 'test'; got 'foo'")
      # → ✗ env must be 'live' or 'test'; got 'foo'

    Doesn't raise ``typer.Exit`` — leaves the caller in control of
    exit code semantics (different commands map errors to different
    codes; the exit-code policy lives at each call site)."""
    if context:
        stderr.print(f"[red]✗ {context} failed:[/red] {message}")
    else:
        stderr.print(f"[red]✗[/red] {message}")


def warn(message: str, *, icon: str = "⚠") -> None:
    """Print a yellow warning to stderr. Always rendered, even under
    ``--quiet`` — warnings are usually "thing degraded but proceeded"
    information the operator wants to know.

    ``icon`` defaults to ``⚠`` (general warning); pass ``⏱`` for
    timeouts so they're scannable in a log. Other icons stay free
    for new shapes (e.g. ``⊘`` for safety-blocked) without growing
    the function surface."""
    stderr.print(f"[yellow]{icon}[/yellow] {message}")


def _mask_key(value: str) -> str:
    """Render a non-leaking fingerprint of a bearer/key value.

    Hard rule (CLAUDE.md security posture): NEVER print a secret in
    full. We show only the last 4 characters, prefixed with ``…``, so
    an operator can eyeball *which* key is in play (e.g. distinguish a
    stale shell export from the freshly-saved one) without the value
    ever appearing in logs / terminal scrollback / CI output.

    * Unset / empty → ``"unset"`` (the source attribution will already
      say ``unset``; this keeps the fingerprint column honest).
    * 1-4 chars → ``…<value>`` (too short to mask meaningfully, but
      still tagged so it's clearly a fingerprint, not the whole key).
    * 5+ chars → ``…<last4>``.
    """
    v = value.strip()
    if not v:
        return "unset"
    return f"…{v[-4:]}"


def echo_remote_context(
    target_name: str,
    target_cfg: object,
    *,
    action: str | None = None,
    suppress: bool = False,
) -> None:
    """Echo one stderr line naming the remote target + credential source.

    Operators kept hitting 401/403 against a deployed runtime with no
    idea WHICH credential or WHICH URL was actually in play — a stale
    shell key shadowing a saved one, the wrong target, etc. Before every
    operator-facing remote (runtime) call we print a single concise
    self-diagnosing line::

        → dev  https://movate-dev….azurecontainerapps.io  key: credentials_file …a1b2

    so a subsequent failure explains itself ("oh — it used my shell key,
    not the saved one").

    Contents (all four are load-bearing): the target NAME, its resolved
    base URL, the credential SOURCE (``shell`` / ``dotenv`` /
    ``credentials_file`` / ``unset`` via :func:`credentials.key_source`),
    and a MASKED key fingerprint (last 4 chars only — see
    :func:`_mask_key`; the full key is NEVER printed). When the saved
    runtime-bearer value overrode a stale shell export (ADR 022, surfaced
    via :func:`credentials.runtime_key_shadowed`) the source is suffixed
    with ``(shell value overridden)`` so the override is transparent at the
    point of use — not silent.

    Goes to **stderr** (machine-readable stdout stays clean) and is
    suppressed when:

    * ``--quiet`` is set (honors the module quiet flag, same as
      :func:`hint`), or
    * ``suppress=True`` — the caller passes this for ``--json`` /
      machine-output modes so scripted use sees nothing extra. The line
      is on stderr regardless, but suppressing it under ``--json`` keeps
      parity with how the rest of the CLI gates human chatter.

    Layer note (CLAUDE.md rule 6): this is a CLI-only concern — the
    echo lives here, NOT in :class:`movate.core.client.MovateClient`,
    so the core/control-plane boundary stays intact.

    Distinct from the shell-shadow 401 hint in ``run.py`` (which fires
    only on an actual rejection): this is the pre-call announcement, not
    the post-failure diagnosis.
    """
    if suppress or _quiet:
        return

    import os  # noqa: PLC0415

    from movate.credentials import key_source, runtime_key_shadowed  # noqa: PLC0415

    key_env = getattr(target_cfg, "key_env", "") or ""
    url = (getattr(target_cfg, "url", "") or "").rstrip("/")
    # Source is derived from the SAME `key_source` primitive `mdk auth status`
    # uses — never hardcoded. After ADR 022 a file-authoritative runtime key
    # resolves to `credentials_file`, so this label is honest (it used to
    # mislabel a shell-sourced key as "credentials file"). When the saved
    # value overrode a stale shell export we append a short, transparent note
    # so the operator sees WHY the masked key isn't their shell value.
    source = key_source(key_env) if key_env else "unset"
    shadow_note = " (shell value overridden)" if key_env and runtime_key_shadowed(key_env) else ""
    fingerprint = _mask_key(os.environ.get(key_env, "")) if key_env else "unset"

    verb = f"{action} " if action else ""
    stderr.print(
        f"[dim]→ {verb}[bold]{target_name}[/bold]  {url}  "
        f"key: {source.replace('_', ' ')} {fingerprint}{shadow_note}[/dim]"
    )
    # ADR 022 D2: when the saved key overrode a stale shell export, emit ONE
    # actionable, self-explaining line (never a silent 401). Fires only at
    # this point of use (a remote call is imminent) — NOT on every CLI
    # invocation from autoload — so it stays low-noise. Reconcile paths: make
    # the shell value durable by persisting it, or fall back to it by
    # clearing the saved key (no override env var — ADR 022 D3).
    if shadow_note:
        stderr.print(
            f"[yellow]⚠[/yellow] ignoring stale [bold]${key_env}[/bold] in your shell — "
            f"using the key saved in [cyan]~/.movate/credentials[/cyan]. "
            f"To make the shell value win, persist it "
            f"([bold]mdk auth save-runtime-key {target_name} -[/bold]), "
            f"or clear the saved key to fall back to the shell."
        )


def confirm_destructive(prompt: str, *, yes: bool) -> None:
    """Gate a destructive operation behind an interactive confirm.

    Pattern: every destructive command (``auth revoke-key``,
    ``config remove-target``, ``tenants clear-budget``) takes a
    ``--yes/-y`` flag and calls this helper first. In a TTY the
    operator gets a yes/no prompt; in a script they pass ``-y`` to
    bypass it. When stdin isn't a TTY and ``-y`` wasn't passed,
    Typer / Click raise ``Abort`` (exit 1) rather than block — so
    CI pipelines fail loud if they forgot ``-y``.

    Centralized here so every destructive command uses identical
    wording shape ("Y/N?") and the same exit semantics."""
    if yes:
        return
    if not typer.confirm(prompt):
        raise typer.Abort()


def success(message: str) -> None:
    """Print a green ``✓``-prefixed success line to stderr.

    Distinct from :func:`hint`: success lines are confirmation that
    a destructive / state-changing op completed, so the operator
    must see them regardless of ``--quiet``. Examples:
    ``✓ revoked <key_id>``, ``✓ active target → 'prod'``."""
    stderr.print(f"[green]✓[/green] {message}")


__all__ = [
    "confirm_destructive",
    "echo_remote_context",
    "error",
    "get_global_target",
    "hint",
    "is_quiet",
    "set_global_target",
    "set_quiet",
    "stderr",
    "success",
    "warn",
]
