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
from movate.playground.targets import TARGETS_ENV_VAR, PlaygroundTarget, decode_targets

runner = CliRunner(mix_stderr=False)

pytestmark = pytest.mark.unit


def _stub_launch(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stub the chainlit preflight + subprocess launch; capture child env.

    Returns the dict the launch path populates so a test can assert what
    the child ``chainlit run`` process would see (env contract) without
    actually spawning chainlit.
    """
    monkeypatch.setattr(playground, "_ensure_chainlit_installed", lambda: None)
    monkeypatch.setattr(playground, "_warn_if_unstable_python", lambda: None)
    captured_env: dict[str, str] = {}

    def fake_run(cmd: list[str], env: dict[str, str], check: bool) -> object:
        captured_env.update(env)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(playground.subprocess, "run", fake_run)
    return captured_env


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


def test_help_lists_target_flags() -> None:
    """The multi-target opt-in/opt-out flags are part of the help contract."""
    help_text = _help_text()
    assert "--all-targets" in help_text
    assert "--no-targets" in help_text


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


# ---------------------------------------------------------------------------
# Multi-target mode — single-runtime back-compat + target hand-off
# ---------------------------------------------------------------------------

_DEV = PlaygroundTarget(name="dev", url="http://dev", key_env="MDK_DEV_KEY", api_key="dev-tok")
_PROD = PlaygroundTarget(name="prod", url="https://prod", key_env="MDK_PROD_KEY", api_key=None)


def _fake_targets(monkeypatch: pytest.MonkeyPatch, targets: list[PlaygroundTarget]) -> None:
    """Make the launcher see ``targets`` as the configured target list."""
    monkeypatch.setattr(playground, "_load_playground_targets", lambda: list(targets))


def test_no_targets_configured_stays_single_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """No registered targets → original single-runtime launch, no target env."""
    captured_env = _stub_launch(monkeypatch)
    _fake_targets(monkeypatch, [])
    monkeypatch.setattr(playground.err, "print", lambda *a, **k: None)

    result = runner.invoke(app, [*_SERVE, "--headless"])
    assert result.exit_code == 0
    assert TARGETS_ENV_VAR not in captured_env
    assert captured_env.get("MDK_PLAYGROUND_RUNTIME_URL") == "http://127.0.0.1:8000"


def test_auto_multi_target_when_targets_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Targets configured + no explicit --runtime-url → multi-target hand-off."""
    captured_env = _stub_launch(monkeypatch)
    _fake_targets(monkeypatch, [_DEV, _PROD])
    monkeypatch.setattr(playground.err, "print", lambda *a, **k: None)

    result = runner.invoke(app, [*_SERVE, "--headless"])
    assert result.exit_code == 0
    decoded = decode_targets(captured_env.get(TARGETS_ENV_VAR))
    assert {t.name for t in decoded} == {"dev", "prod"}
    assert {t.name for t in decoded if t.key_available} == {"dev"}


def test_explicit_runtime_url_disables_multi_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: an explicit --runtime-url pins single-runtime even with
    targets configured — no target picker, no target env var."""
    captured_env = _stub_launch(monkeypatch)
    _fake_targets(monkeypatch, [_DEV, _PROD])
    monkeypatch.setattr(playground.err, "print", lambda *a, **k: None)

    result = runner.invoke(app, [*_SERVE, "--runtime-url", "http://pinned:9000", "--headless"])
    assert result.exit_code == 0
    assert TARGETS_ENV_VAR not in captured_env
    assert captured_env.get("MDK_PLAYGROUND_RUNTIME_URL") == "http://pinned:9000"


def test_runtime_url_via_env_disables_multi_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit runtime URL via env var also pins single-runtime."""
    captured_env = _stub_launch(monkeypatch)
    _fake_targets(monkeypatch, [_DEV, _PROD])
    monkeypatch.setattr(playground.err, "print", lambda *a, **k: None)

    result = runner.invoke(
        app, [*_SERVE, "--headless"], env={"MDK_PLAYGROUND_RUNTIME_URL": "http://from-env:8000"}
    )
    assert result.exit_code == 0
    assert TARGETS_ENV_VAR not in captured_env


def test_no_targets_flag_forces_single_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-targets forces single-runtime even when targets are configured."""
    captured_env = _stub_launch(monkeypatch)
    _fake_targets(monkeypatch, [_DEV, _PROD])
    monkeypatch.setattr(playground.err, "print", lambda *a, **k: None)

    result = runner.invoke(app, [*_SERVE, "--no-targets", "--headless"])
    assert result.exit_code == 0
    assert TARGETS_ENV_VAR not in captured_env


def test_all_targets_errors_when_none_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """--all-targets with no registered targets → exit 2 + a helpful hint."""
    _stub_launch(monkeypatch)
    _fake_targets(monkeypatch, [])
    printed: list[str] = []
    monkeypatch.setattr(
        playground.err, "print", lambda *a, **k: printed.append(" ".join(map(str, a)))
    )

    result = runner.invoke(app, [*_SERVE, "--all-targets", "--headless"])
    assert result.exit_code == 2
    assert any("no targets" in line.lower() for line in printed)


def test_all_targets_and_no_targets_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing both contradictory flags → exit 2."""
    _stub_launch(monkeypatch)
    monkeypatch.setattr(playground.err, "print", lambda *a, **k: None)
    result = runner.invoke(app, [*_SERVE, "--all-targets", "--no-targets", "--headless"])
    assert result.exit_code == 2


def test_multi_target_banner_never_echoes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-target startup banner prints names/URLs/key_env — never a key."""
    secret = "dev-SUPERSECRET-token"
    dev = PlaygroundTarget(name="dev", url="http://dev", key_env="MDK_DEV_KEY", api_key=secret)
    captured_env = _stub_launch(monkeypatch)
    _fake_targets(monkeypatch, [dev])
    printed: list[str] = []
    monkeypatch.setattr(
        playground.err, "print", lambda *a, **k: printed.append(" ".join(map(str, a)))
    )

    result = runner.invoke(app, [*_SERVE, "--headless"])
    assert result.exit_code == 0
    # The secret is in the child env (server-side) but never in the banner.
    assert secret in captured_env.get(TARGETS_ENV_VAR, "")
    assert all(secret not in line for line in printed)
    assert secret not in result.stdout
    assert secret not in (result.stderr or "")
