"""Pre-remote-call context echo — ``echo_remote_context`` (item 94).

Operators kept hitting 401/403 against a deployed runtime with no idea
WHICH credential or WHICH URL was actually in play (a stale shell key
shadowing a saved one, the wrong target, etc.). Before every
operator-facing remote call we now echo one concise stderr line naming
the target, its resolved base URL, the credential SOURCE
(``shell`` / ``dotenv`` / ``credentials_file`` / ``unset`` via
:func:`movate.credentials.key_source`), and a MASKED key fingerprint
(last 4 chars only). A subsequent failure then explains itself.

Tested here:

1. The helper renders all four fields (target, URL, source, masked key)
   and NEVER the full key.
2. The line goes to **stderr**, not stdout.
3. ``suppress=True`` (the ``--json`` path) silences it; ``--quiet`` too.
4. The credential SOURCE attribution reflects :func:`key_source` —
   shell-set vs credentials-file-set produce different labels.
5. Unset key env → ``unset`` source + ``unset`` fingerprint (never a
   stray empty mask).
6. Integration: ``mdk run`` echoes in TEXT mode and is suppressed under
   ``-o json`` (machine-clean stdout preserved).

Console echo goes through Rich (``_console.stderr``), so we assert via
``capsys`` / ``CliRunner.stderr`` — NOT ``caplog`` (which only captures
the stdlib-``logging`` warning path; see ``tests/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli import _console
from movate.cli._console import _mask_key, echo_remote_context
from movate.cli.main import app
from movate.core.user_config import TargetConfig
from movate.credentials.loader import _reset_shadow_state, autoload_credentials

runner = CliRunner(mix_stderr=False)

# A realistic bearer; the LAST four chars are the only part that may
# ever appear in output. Anything else leaking is a security failure.
_FULL_KEY = "mvt_dev_t1_k1_supersecret_value_a1b2"
_LAST4 = "a1b2"


@pytest.fixture(autouse=True)
def _reset_runtime_key_shadow() -> object:
    """The ADR-022 shadow ledger is process-global module state. Reset it
    before AND after each test so the override note in one test never bleeds
    into another that sets env directly (without re-running autoload)."""
    _reset_shadow_state()
    yield
    _reset_shadow_state()


def _hermetic_creds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, contents: str = "") -> None:
    """Point the credentials store at a tmp file + force the file backend so
    ``key_source`` never reads the developer's real ``~/.movate/credentials``
    (or their OS keychain)."""
    creds = tmp_path / "credentials"
    creds.write_text(contents)
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(creds))
    monkeypatch.setenv("MOVATE_CRED_BACKEND", "file")


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mask_key_never_leaks_full_value() -> None:
    """The fingerprint is ``…`` + last 4 chars only — the rest of the
    secret never appears."""
    masked = _mask_key(_FULL_KEY)
    assert masked == f"…{_LAST4}"
    # The secret body must NOT be present.
    assert "supersecret" not in masked
    assert _FULL_KEY not in masked


@pytest.mark.unit
def test_mask_key_unset_and_short() -> None:
    assert _mask_key("") == "unset"
    assert _mask_key("   ") == "unset"
    # Short values are still tagged as a fingerprint, never the raw value
    # passed through unmarked.
    assert _mask_key("abc") == "…abc"


# ---------------------------------------------------------------------------
# Helper rendering + gating
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_echo_renders_all_four_fields_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """target name + base URL + credential source + masked key, all on
    stderr, never the full key on either stream."""
    _hermetic_creds(monkeypatch, tmp_path)
    # Shell-set value → source attributed as "shell".
    monkeypatch.setenv("MDK_DEV_KEY", _FULL_KEY)
    cfg = TargetConfig(
        url="https://movate-dev.example.azurecontainerapps.io/", key_env="MDK_DEV_KEY"
    )

    echo_remote_context("dev", cfg, action="run")

    out = capsys.readouterr()
    # All four load-bearing fields present on stderr.
    assert "dev" in out.err
    assert "https://movate-dev.example.azurecontainerapps.io" in out.err
    assert "shell" in out.err
    assert f"…{_LAST4}" in out.err
    # NEVER the full secret, on either stream.
    assert "supersecret" not in out.err
    assert "supersecret" not in out.out
    assert _FULL_KEY not in out.err
    # stdout stays clean — the echo is stderr-only.
    assert out.out == ""


@pytest.mark.unit
def test_echo_suppressed_under_json_suppress_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``suppress=True`` (the --json path) emits nothing at all."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", _FULL_KEY)
    cfg = TargetConfig(url="https://x.example.com", key_env="MDK_DEV_KEY")

    echo_remote_context("dev", cfg, suppress=True)

    out = capsys.readouterr()
    assert out.err == ""
    assert out.out == ""


@pytest.mark.unit
def test_echo_suppressed_under_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--quiet`` (module flag) silences the echo, same as ``hint``."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", _FULL_KEY)
    cfg = TargetConfig(url="https://x.example.com", key_env="MDK_DEV_KEY")

    _console.set_quiet(True)
    try:
        echo_remote_context("dev", cfg)
    finally:
        _console.set_quiet(False)

    out = capsys.readouterr()
    assert out.err == ""


@pytest.mark.unit
def test_echo_source_attribution_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the key is sourced from ~/.movate/credentials (not the shell),
    the SOURCE field says so — this is the affordance that distinguishes a
    stale shell export from the saved key."""
    _hermetic_creds(monkeypatch, tmp_path, contents=f"MDK_DEV_KEY={_FULL_KEY}\n")
    # Simulate autoload having hydrated the env from the credentials file:
    # the env value matches the file value (and no .env shadows it), so
    # key_source attributes it to credentials_file.
    monkeypatch.chdir(tmp_path)  # no .env here → not dotenv
    monkeypatch.setenv("MDK_DEV_KEY", _FULL_KEY)
    cfg = TargetConfig(url="https://x.example.com", key_env="MDK_DEV_KEY")

    echo_remote_context("dev", cfg)

    out = capsys.readouterr()
    assert "credentials file" in out.err
    assert f"…{_LAST4}" in out.err


@pytest.mark.unit
def test_echo_shows_shell_override_note_when_file_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 022: when the saved runtime key overrode a stale shell export,
    the echo derives the source from key_source (now ``credentials file``,
    NOT a misleading ``shell``) AND appends ``(shell value overridden)`` plus
    one actionable reconcile line — so the override is transparent, never a
    silent 401. The full key never appears."""
    _hermetic_creds(monkeypatch, tmp_path, contents=f"MDK_DEV_KEY={_FULL_KEY}\n")
    monkeypatch.chdir(tmp_path)  # no .env here → not dotenv
    # Stale shell export differs from the saved value → autoload makes the
    # FILE authoritative and records the shadow.
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_STALE_shell_value_zzzz")
    autoload_credentials()
    cfg = TargetConfig(url="https://x.example.com", key_env="MDK_DEV_KEY")

    echo_remote_context("dev", cfg, action="deploy")

    # Rich wraps to the console width; collapse whitespace so phrase
    # assertions don't depend on where the wrap landed.
    err = " ".join(capsys.readouterr().err.split())
    # Honest source attribution: the file won, so it reads "credentials file".
    assert "credentials file" in err
    assert "(shell value overridden)" in err
    # The masked key is the FILE value's last-4, not the stale shell value's.
    assert f"…{_LAST4}" in err
    assert "…zzzz" not in err
    # One actionable reconcile line points at the escape hatches.
    assert "ignoring stale" in err
    assert "save-runtime-key" in err
    # No secret leaks.
    assert "supersecret" not in err
    assert _FULL_KEY not in err


@pytest.mark.unit
def test_echo_no_override_note_when_not_shadowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file-only runtime key (no stale shell export) resolves to the file
    silently — no ``(shell value overridden)`` note and no reconcile line."""
    _hermetic_creds(monkeypatch, tmp_path, contents=f"MDK_DEV_KEY={_FULL_KEY}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    autoload_credentials()
    cfg = TargetConfig(url="https://x.example.com", key_env="MDK_DEV_KEY")

    echo_remote_context("dev", cfg)

    out = capsys.readouterr()
    assert "credentials file" in out.err
    assert "(shell value overridden)" not in out.err
    assert "ignoring stale" not in out.err


@pytest.mark.unit
def test_echo_unset_key_env_says_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unset key env renders ``unset`` for both source and fingerprint —
    no stray empty mask, and clearly flags the missing credential."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = TargetConfig(url="https://x.example.com", key_env="MDK_DEV_KEY")

    echo_remote_context("dev", cfg)

    out = capsys.readouterr()
    # Source AND fingerprint both read "unset"; no bare "…" mask of empty.
    assert "unset" in out.err
    assert "…unset" not in out.err


# ---------------------------------------------------------------------------
# Integration: mdk run echoes in TEXT mode, suppressed under -o json
# ---------------------------------------------------------------------------


def _bootstrap_run_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Init a project + one agent, write a target config pointing at a fake
    URL. Mirrors test_run_remote_target.py's setup."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    cfg_dir = tmp_path / ".movate"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "active: dev\n"
        "targets:\n"
        "  dev:\n"
        "    url: https://movate-dev.example.azurecontainerapps.io\n"
        "    key_env: MDK_DEV_KEY\n"
    )
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_dir / "config.yaml"))


def _ok_run_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "run_id": "11111111-2222-3333-4444-555555555555",
            "job_id": "job-xyz",
            "agent": "faq",
            "agent_version": "0.1.0",
            "prompt_hash": "deadbeef",
            "provider": "mock",
            "provider_version": "1.0",
            "pricing_version": "2024.05",
            "status": "success",
            "input": {"question": "hello"},
            "output": {"answer": "hi back"},
            "metrics": {
                "cost_usd": 0.0012,
                "latency_ms": 480,
                "tokens": {"input": 12, "output": 4, "total": 16},
            },
            "error": None,
            "created_at": "2026-05-15T12:00:00Z",
            "workflow_run_id": None,
            "node_id": None,
        },
    )


def _route_httpx(transport: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport  # type: ignore[assignment]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("httpx.Client", factory)


@pytest.mark.unit
def test_mdk_run_echoes_context_in_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mdk run <agent> --target dev -o text`` echoes the context line on
    stderr; the masked key is present, the full key never is."""
    _bootstrap_run_target(tmp_path, monkeypatch)
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", _FULL_KEY)
    _route_httpx(httpx.MockTransport(_ok_run_handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "-o", "text"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Context echo present: target + URL + source + masked key.
    assert "dev" in result.stderr
    assert "movate-dev.example.azurecontainerapps.io" in result.stderr
    assert f"…{_LAST4}" in result.stderr
    # The full secret never appears on either stream.
    assert "supersecret" not in result.stderr
    assert "supersecret" not in result.stdout


@pytest.mark.unit
def test_mdk_run_suppresses_context_under_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-o json`` is the machine-clean path: the context echo is
    suppressed so stderr carries no human chatter beyond the existing
    summary, and the masked key never appears."""
    _bootstrap_run_target(tmp_path, monkeypatch)
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", _FULL_KEY)
    _route_httpx(httpx.MockTransport(_ok_run_handler), monkeypatch)

    result = runner.invoke(
        app, ["run", "faq", "hello", "--target", "dev", "-o", "json"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The pre-call context echo (the "→ run dev … key: …" line) is gone.
    assert f"…{_LAST4}" not in result.stderr
    assert "key: shell" not in result.stderr
    # Full JSON run view still rendered to stdout (compat preserved).
    assert '"answer": "hi back"' in result.stdout
    # And of course no secret leaks anywhere.
    assert "supersecret" not in result.stderr
    assert "supersecret" not in result.stdout
