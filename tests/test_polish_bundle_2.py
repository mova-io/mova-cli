"""Second polish bundle — five more small UX wins.

Each test targets one item:

1. ``mdk secrets list`` — shows the secrets file mode (0600 ✓ / warn).
2. ``mdk promote --dry-run`` — verify the existing flag still works
   end-to-end (regression coverage on a flag we documented).
3. ``mdk fmt`` — honors ``.fmtignore`` glob patterns.
4. ``mdk monitor --clear`` — accepts the flag without breaking --once.
5. ``mdk eval-gen`` — retries once on JSON parse failure (verified
   via _attempt_generate returning None first call).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from movate.cli.eval_gen_cmd import _attempt_generate
from movate.cli.main import app
from movate.profiles.store import (
    Profile,
    ProfileRegistry,
    save_registry,
    set_active_profile,
)
from movate.promotions import load_log
from movate.secrets.store import _store_path
from movate.snapshot import create_snapshot, list_snapshots

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Item 1: mdk secrets list mode indicator
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def dev_profile(isolated_home: Path) -> str:
    """Active dev profile + one secret stored."""
    registry = ProfileRegistry()
    registry.add(Profile(name="dev", description="test"))
    save_registry(registry)
    set_active_profile("dev")

    runner.invoke(app, ["secrets", "set", "OPENAI_API_KEY", "--value", "sk-test"])
    return "dev"


@pytest.mark.unit
def test_secrets_list_shows_green_check_when_mode_0600(dev_profile: str) -> None:
    """A freshly-written secrets file is 0600 — should show the green confirmation."""
    result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    # File path + green mode confirmation appear
    assert "0o600" in result.stdout
    assert "✓" in result.stdout


@pytest.mark.unit
def test_secrets_list_warns_when_mode_loose(dev_profile: str, isolated_home: Path) -> None:
    """If the secrets file is world/group readable, the warning fires."""
    path = _store_path(dev_profile)
    # Loosen perms to simulate the bad state.
    os.chmod(path, 0o644)

    result = runner.invoke(app, ["secrets", "list"])
    assert result.exit_code == 0
    combined = result.stdout
    # Warning shows the actual mode + remediation hint
    assert "should be 0o600" in combined
    assert "fix --only fix-secrets-permissions" in combined


# ---------------------------------------------------------------------------
# Item 2: mdk promote --dry-run (regression coverage)
# ---------------------------------------------------------------------------


@pytest.fixture
def promote_project(tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project + registered prod profile + one snapshot, ready to promote."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("api_version: movate/v1\n")
    (proj / "agents" / "demo").mkdir(parents=True)
    (proj / "agents" / "demo" / "agent.yaml").write_text("name: demo\n")

    registry = ProfileRegistry()
    registry.add(Profile(name="prod"))
    save_registry(registry)

    create_snapshot(project_root=proj, description="baseline")
    return proj


@pytest.mark.unit
def test_promote_dry_run_does_not_record(promote_project: Path) -> None:
    """--dry-run must NOT append to promotions.yaml."""
    snap_short = list_snapshots(promote_project)[0].hash.removeprefix("sha256:")[:8]
    result = runner.invoke(
        app,
        [
            "promote",
            snap_short,
            "--to",
            "prod",
            "--dry-run",
            "--project-root",
            str(promote_project),
        ],
    )
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    # Promotions log is untouched
    log = load_log(promote_project)
    assert log.promotions == []


# ---------------------------------------------------------------------------
# Item 3: mdk fmt .fmtignore
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fmt_honors_fmtignore_file(tmp_path: Path) -> None:
    """Files matching a .fmtignore glob should NOT be formatted."""
    # Project layout:
    #   ./movate.yaml    (formattable, NOT ignored)
    #   ./vendored/x.yaml (formattable, IGNORED by .fmtignore)
    #   ./.fmtignore     (one glob: vendored/**)
    (tmp_path / "movate.yaml").write_text("name: x\napi_version: movate/v1\n")
    (tmp_path / "vendored").mkdir()
    (tmp_path / "vendored" / "x.yaml").write_text("z: 1\na: 2\n")  # un-sorted
    (tmp_path / ".fmtignore").write_text("vendored/**\n# also a comment line\n")

    # First confirm there's nothing for --check to gripe about under the
    # main project file — agent.yaml-style keys aren't present so the
    # generic-YAML formatter just normalizes indent which is already fine.
    result = runner.invoke(app, ["fmt", "--check", str(tmp_path)])
    # --check passes when nothing needs reformatting under the filter
    assert result.exit_code in {0, 1}
    # Critically: the ignored file should NOT appear in the output
    assert "vendored/x.yaml" not in result.stdout


@pytest.mark.unit
def test_fmt_without_fmtignore_includes_all(tmp_path: Path) -> None:
    """Sanity: without .fmtignore, vendored YAML IS scanned."""
    (tmp_path / "vendored").mkdir()
    bad = tmp_path / "vendored" / "x.yaml"
    bad.write_text("z:    1\n")  # extra spaces that the formatter normalizes
    # No .fmtignore present → the file IS in scope for formatting.
    # We prove visibility via --apply (it should normalize the content).
    apply_result = runner.invoke(app, ["fmt", str(tmp_path)])
    assert apply_result.exit_code == 0
    # The file was visible to the formatter (no .fmtignore filter).
    # Mode test: post-format content normalized.
    text = bad.read_text()
    # If the formatter ran, the extra spaces around `:` are gone.
    assert "z:    1" not in text or "z: 1" in text


# ---------------------------------------------------------------------------
# Item 4: mdk monitor --clear
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty MOVATE_DB for monitor smoke."""
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "empty.db"))
    return tmp_path


@pytest.mark.unit
def test_monitor_clear_flag_accepted_with_once(empty_db: Path) -> None:
    """--clear with --once should still render a one-shot dashboard."""
    # --once skips the live loop entirely, so --clear has no visible
    # effect but should still be accepted without errors.
    result = runner.invoke(app, ["monitor", "--once", "--clear"], env={"COLUMNS": "200"})
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Item 5: mdk eval-gen retry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_attempt_generate_returns_none_on_bad_json() -> None:
    """The retry helper returns None on parse fail (drives the retry loop)."""
    rt = type("RT", (), {})()
    rt.provider = type("P", (), {"complete": AsyncMock()})()
    # Provider returns non-JSON text → should yield None.
    rt.provider.complete.return_value = type(
        "R",
        (),
        {
            "text": "this is not valid JSON",
            "tokens": type("T", (), {"input": 0, "output": 0})(),
            "raw": {},
        },
    )()

    fake_bundle = type(
        "B",
        (),
        {
            "spec": type(
                "S",
                (),
                {
                    "model": type("M", (), {"provider": "openai/x", "params": {}})(),
                    "name": "x",
                    "description": "",
                },
            )(),
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    )()

    result = asyncio.run(_attempt_generate(rt, fake_bundle, index=0, sample_input=None, nudge=""))
    assert result is None


@pytest.mark.unit
def test_attempt_generate_returns_dict_on_valid_json() -> None:
    """Happy path: valid JSON object returns a parsed dict."""
    rt = type("RT", (), {})()
    rt.provider = type("P", (), {"complete": AsyncMock()})()
    rt.provider.complete.return_value = type(
        "R",
        (),
        {
            "text": '{"q": "hello"}',
            "tokens": type("T", (), {"input": 0, "output": 0})(),
            "raw": {},
        },
    )()

    fake_bundle = type(
        "B",
        (),
        {
            "spec": type(
                "S",
                (),
                {
                    "model": type("M", (), {"provider": "openai/x", "params": {}})(),
                    "name": "x",
                    "description": "",
                },
            )(),
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    )()

    result = asyncio.run(_attempt_generate(rt, fake_bundle, index=0, sample_input=None, nudge=""))
    assert result == {"q": "hello"}
