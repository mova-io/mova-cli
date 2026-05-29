"""``mdk playground serve`` CLI command — help, install gate, key hygiene.

Three guarantees this command must keep:

1. **The ``--api-key-env`` doc bug is gone.** The original docstring's
   quickstart showed ``--api-key-env MOVATE_API_KEY`` — a flag that never
   existed. The real flag is ``--api-key`` (env-resolved). Assert the
   corrected help so the bug can't silently come back.
2. **Clean install hint when the ``[playground]`` extra is absent.** The
   command exits 2 with a copy-paste install hint, never a raw ImportError.
3. **The bearer key is never echoed/logged** to stdout/stderr by the
   command's launch path — it's a server-side secret.

The new flags (``--no-history`` / ``--persist-uploads``) are also asserted
present so the help stays a contract.
"""

from __future__ import annotations

import builtins
import re

import pytest
import typer
from typer.testing import CliRunner

from movate.cli import playground
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

pytestmark = pytest.mark.unit

# Invoke through the full ``mdk`` app so the test mirrors the real
# ``mdk playground serve`` invocation (the standalone single-command
# Typer flattens the ``serve`` subcommand away).
_SERVE = ["playground", "serve"]


def _help_text() -> str:
    """Render ``mdk playground serve --help`` as a CI-robust string.

    Two CI-specific gotchas the help assertions have to defeat (same
    recipe as ``tests/test_version_v_shortcut.py``):

    1. **Narrow-terminal truncation/wrap**: in CI's non-TTY terminal Rich
       renders the options panel too narrow, so flag names wrap or get
       elided. Force a wide terminal via ``env={"COLUMNS": "200"}``.
    2. **ANSI escapes inside option names**: CI runs with ``FORCE_COLOR=1``,
       so Rich styles ``--`` and the flag name as separate spans — a raw
       substring search misses them. Strip ANSI, then collapse whitespace
       so wrapped/padded flag rows flatten to a single searchable string.
    """
    result = runner.invoke(app, [*_SERVE, "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Strip ANSI escapes first (CI sets FORCE_COLOR=1).
    plain = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", result.stdout)
    # Then collapse all whitespace (wrap newlines + Rich table padding).
    return " ".join(plain.split())


def test_help_does_not_mention_nonexistent_api_key_env_flag() -> None:
    """The ``--api-key-env`` flag never existed — it must not appear."""
    help_text = _help_text()
    assert "--api-key-env" not in help_text
    # The real flag is present.
    assert "--api-key" in help_text


def test_module_docstring_drops_api_key_env() -> None:
    """The quickstart in the module docstring must not show the bad flag."""
    assert "--api-key-env" not in (playground.__doc__ or "")


def test_help_lists_new_flags() -> None:
    help_text = _help_text()
    assert "--no-history" in help_text
    assert "--persist-uploads" in help_text


def test_help_keeps_core_flags() -> None:
    """Back-compat: the pre-existing flags are unchanged."""
    help_text = _help_text()
    for flag in ("--runtime-url", "--api-key", "--port", "--host", "--headless"):
        assert flag in help_text


def test_install_hint_when_chainlit_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent ``[playground]`` extra → exit 2 + copy-paste install hint."""
    printed: list[str] = []
    monkeypatch.setattr(
        playground.err, "print", lambda *args, **_: printed.append(" ".join(map(str, args)))
    )

    # Force the "not installed" branch deterministically.
    def fake_ensure() -> None:
        playground.err.print("chainlit not installed — install hint here")
        raise typer.Exit(code=2)

    monkeypatch.setattr(playground, "_ensure_chainlit_installed", fake_ensure)

    result = runner.invoke(app, _SERVE)
    assert result.exit_code == 2
    assert any("not installed" in line for line in printed)


def test_real_ensure_chainlit_exits_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real preflight surfaces an install hint (not ImportError) when
    chainlit can't be imported."""
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "chainlit":
            raise ImportError("no chainlit")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    printed: list[str] = []
    monkeypatch.setattr(
        playground.err, "print", lambda *args, **_: printed.append(" ".join(map(str, args)))
    )

    with pytest.raises(typer.Exit) as exc:
        playground._ensure_chainlit_installed()
    assert exc.value.exit_code == 2
    assert any("playground" in line for line in printed)


def test_bearer_key_not_echoed_on_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved API key must never be printed by the launch path."""
    secret = "mvt_live_SUPERSECRET_DO_NOT_LOG"
    printed: list[str] = []
    monkeypatch.setattr(
        playground.err, "print", lambda *args, **_: printed.append(" ".join(map(str, args)))
    )
    # Skip the chainlit-installed preflight + the python-version warning so
    # we exercise the env-export + launch banner with a key set.
    monkeypatch.setattr(playground, "_ensure_chainlit_installed", lambda: None)
    monkeypatch.setattr(playground, "_warn_if_unstable_python", lambda: None)

    captured_env: dict[str, str] = {}

    def fake_run(cmd: list[str], env: dict[str, str], check: bool) -> object:
        captured_env.update(env)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(playground.subprocess, "run", fake_run)

    result = runner.invoke(
        app,
        [*_SERVE, "--api-key", secret, "--headless"],
    )
    assert result.exit_code == 0

    # The secret rides in the SERVER process env (server-side only)...
    assert captured_env.get("MDK_PLAYGROUND_API_KEY") == secret
    # ...but is NEVER echoed to stdout/stderr or the printed banner.
    assert secret not in result.stdout
    assert secret not in (result.stderr or "")
    assert all(secret not in line for line in printed)


def test_no_history_flag_sets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground, "_ensure_chainlit_installed", lambda: None)
    monkeypatch.setattr(playground, "_warn_if_unstable_python", lambda: None)
    monkeypatch.setattr(playground.err, "print", lambda *a, **k: None)
    captured_env: dict[str, str] = {}

    def fake_run(cmd: list[str], env: dict[str, str], check: bool) -> object:
        captured_env.update(env)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(playground.subprocess, "run", fake_run)
    result = runner.invoke(app, [*_SERVE, "--no-history", "--persist-uploads", "--headless"])
    assert result.exit_code == 0
    assert captured_env.get("MDK_PLAYGROUND_NO_HISTORY") == "1"
    assert captured_env.get("MDK_PLAYGROUND_PERSIST_UPLOADS") == "1"
