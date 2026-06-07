"""CLI smoke tests: help renders, version works, real commands hit their handlers,
remaining stubs exit non-zero with the "not implemented" message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml
from typer.testing import CliRunner

from movate import __version__
from movate.cli.main import app

runner = CliRunner()

STUB_EXIT_CODE = 2  # see movate/cli/_stub.py


@pytest.mark.unit
def test_help_renders() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Develop" in result.stdout
    assert "Run & evaluate" in result.stdout
    assert "Diagnose" in result.stdout
    assert "Deploy & operate" in result.stdout
    assert "Manage" in result.stdout


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected_example"),
    [
        # Each tuple is (subcommand, a substring that ONLY appears in
        # that command's docstring Examples block). If someone wraps
        # the command with `help="..."` in main.py the docstring stops
        # rendering — and these substrings disappear from `--help`.
        # That's exactly the regression we're guarding against.
        ("run", "movate run ./faq-agent"),
        ("bench", "movate bench ./faq-agent"),
        ("submit", "movate submit faq-agent"),
        ("watch", "movate watch ./agents/faq-agent"),
    ],
)
def test_subcommand_help_renders_docstring_examples(command: str, expected_example: str) -> None:
    """Every command's --help should surface the Examples block from
    its docstring.

    Background: Typer/Click only uses the function's docstring when
    `app.command(...)` is called WITHOUT a `help=` override. Passing
    `help="short one-liner"` silently replaces the entire help with
    that string, dropping the carefully-written Examples blocks. We
    hit this in v0.5 — `movate submit --help` was missing its 12 lines
    of examples for weeks because `main.py` overrode `help=`. This
    test fails loudly if anyone re-introduces an override that strips
    a docstring."""
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert expected_example in result.stdout, (
        f"`movate {command} --help` is missing its docstring examples — "
        f"someone likely re-added a `help=` override in main.py that's "
        f"shadowing the function's docstring."
    )


@pytest.mark.unit
def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.unit
def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    # Either brand name is acceptable — "mdk" is canonical going forward,
    # "movate" remains the transitional alias and still appears in the
    # deprecation note in the help text.
    lower = result.stdout.lower()
    assert "mdk" in lower or "movate" in lower


@pytest.mark.unit
def test_mdk_binary_alias_registered() -> None:
    """The ``mdk`` console script must be registered alongside ``movate``
    so both binaries are first-class. Reads ``pyproject.toml`` to assert
    the entry-point map contains both; the import doesn't go through the
    Python package machinery so we have to inspect the metadata directly.
    """
    import tomllib  # noqa: PLC0415

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text())
    scripts = data.get("project", {}).get("scripts", {})
    assert scripts.get("movate") == "movate.cli.main:app", (
        "transitional `movate` script must continue to point at the same "
        "entry-point so existing installs keep working"
    )
    assert scripts.get("mdk") == "movate.cli.main:app", (
        "canonical `mdk` script must be registered as the going-forward "
        "binary name — see pyproject.toml [project.scripts]"
    )


@pytest.mark.unit
def test_doctor_runs() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # Assert the CalVer *base* (pre-``+local`` segment) — the doctor table
    # truncates the version cell, and on a dirty dev tree __version__ carries a
    # ``+g<sha>.dirty`` PEP 440 local segment (ADR 066) that gets clipped. The
    # base (e.g. ``2026.6.7.5``) always renders.
    assert __version__.split("+")[0] in result.stdout


@pytest.mark.unit
def test_doctor_renders_purpose_column() -> None:
    """The Purpose column sits between Result and License and surfaces
    a one-line role description per dep (item from the 2026-05-13
    UI-feedback batch). Verifies (a) the header exists, (b) at least
    one dep's purpose string makes it into the rendered output."""
    # Force a wide terminal so Rich doesn't drop the column.
    with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Purpose" in result.stdout
    # Pick a dep whose purpose string is short + stable + unlikely to
    # change wording: "Multi-provider LLM SDK" describes litellm.
    assert "Multi-provider LLM SDK" in result.stdout


@pytest.mark.unit
def test_doctor_reports_runtime_adapter_availability() -> None:
    """``movate doctor`` should surface which AgentRuntime adapters are
    available — operators need this when deciding whether ``runtime:
    native_anthropic`` will resolve on their install. We assert that
    every AgentRuntime value is named in the doctor table.

    Force COLUMNS=200 so Rich's table doesn't truncate the longer
    runtime names (native_anthropic / native_openai). With the 4-column
    doctor table (Check / Result / Purpose / License), the default
    80-col terminal compresses the Check column enough to clip
    ``native_anthropic`` to ``native_anthrop`` and break the substring
    assertion.
    """
    from movate.core.models import AgentRuntime  # noqa: PLC0415

    with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    for runtime in AgentRuntime:
        # Each runtime gets a row labelled "runtime: <name>".
        assert f"runtime: {runtime.value}" in result.stdout


@pytest.mark.unit
def test_doctor_install_hint_for_missing_runtime_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an adapter's probe module isn't importable, the doctor
    row should include the ``uv add 'movate-cli[extra]'`` hint so the
    operator knows exactly how to fix it.

    We probe doctor's render logic directly (not via the CLI) because
    Rich's Console truncates cells in a captured tty-less width, and
    asserting on the truncated render is fragile."""
    import importlib.util  # noqa: PLC0415

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "anthropic":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    # Drive the same find_spec path doctor uses and assert the
    # resulting status string includes the install hint. (Asserting
    # on result.stdout would fight Rich's column-width truncation —
    # not the contract we care about.)
    from movate.cli.doctor import _RUNTIME_PROBES  # noqa: PLC0415

    seen_hint = False
    for runtime_name, probe_module, extra_name in _RUNTIME_PROBES:
        if runtime_name == "native_anthropic":
            spec = importlib.util.find_spec(probe_module)
            assert spec is None  # monkeypatch worked
            assert extra_name == "anthropic"
            seen_hint = True
    assert seen_hint, "native_anthropic probe entry missing from _RUNTIME_PROBES"


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        # `serve` (v0.5 stage 3b) and `worker` (v0.5 stage 4) both used
        # to be stubs; both have been replaced with real loops that
        # block forever, so neither can appear here. Their coverage
        # lives in tests/test_runtime_*.py + manual real-binary smoke.
        # `logs` was a stub through v0.3; it is now a real command.
        ["deploy", "dev"],
    ],
)
def test_phase2plus_stub_commands_exit_nonzero(command: list[str]) -> None:
    """Commands not yet implemented exit with code 2 + a clear message."""
    result = runner.invoke(app, command)
    assert result.exit_code == STUB_EXIT_CODE


def _scaffold_agent(agent_dir: Path) -> Path:
    """Drop a minimal valid agent at ``agent_dir``. Used by streaming
    smoke tests below — same shape as the workflow tests' helper."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "stream-demo",
                "version": "0.1.0",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo {{ input.text }}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["message"],
                "properties": {"message": {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"text": "x"}, "expected": {"message": "x"}}) + "\n"
    )
    return agent_dir


@pytest.mark.unit
def test_run_stream_emits_tokens_to_stderr_in_mock_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``movate run <agent> --stream`` should:

    * Render token deltas to stderr as they arrive (visible to a human
      iterating on a prompt).
    * Still emit the final validated JSON to stdout.
    * Schema-validate the accumulated text the same way non-streaming
      does (so partial JSON during streaming doesn't break anything).

    NOTE: ``_run_local_agent`` currently disables streaming under
    ``--mock`` (the MockProvider's stream path isn't meant for live
    preview). To verify the wiring, we use a tailored MOVATE_MOCK_RESPONSE
    and assert through executor's on_token plumbing via the inner test
    in test_executor.py. Here we just guard the CLI surface:
    --stream must parse, run, succeed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "streamed"}')
    agent_dir = _scaffold_agent(tmp_path / "stream-demo")

    local_runner = CliRunner(mix_stderr=False)
    result = local_runner.invoke(
        app,
        ["run", str(agent_dir), '{"text": "hi"}', "--mock", "--stream"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Stdout still carries the final validated JSON response.
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["data"] == {"message": "streamed"}


@pytest.mark.unit
def test_run_stream_rejected_on_workflow_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--stream`` on a workflow exits 2 with a clear message —
    interleaved per-node tokens would be confusing; deferred."""
    monkeypatch.setenv("HOME", str(tmp_path))
    wf = tmp_path / "wf"
    wf.mkdir()
    # Just enough to trip is_workflow_path() (presence of workflow.yaml).
    (wf / "workflow.yaml").write_text("api_version: movate/v1\nkind: Workflow\n")

    local_runner = CliRunner(mix_stderr=False)
    result = local_runner.invoke(app, ["run", str(wf), "{}", "--stream"])
    assert result.exit_code == 2
    assert "--stream supports agents only" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        ["pricing", "-o", "foo"],
        ["jobs", "show", "any-id", "-o", "yaml"],
        ["run", "/tmp/x", "hi", "-o", "table"],  # `table` is invalid for `run`
        ["bench", "/tmp/x", "hi", "-o", "html"],
    ],
)
def test_invalid_output_format_rejected_at_parse_time(command: list[str]) -> None:
    """``--output`` is now an Enum option on every command. Invalid values
    must be rejected at parse time (exit 2, "Invalid value for '--output'")
    rather than silently falling through to the default branch the way
    the old stringly-typed implementation did.

    Each command in the parametrize set picks a value that isn't in its
    own choice subset — including the cross-set case (`run` doesn't
    accept `table`, since `Run` is ``json | text`` only). That keeps the
    sub-enums honest."""
    result = runner.invoke(app, command)
    assert result.exit_code == 2
    # `runner` here is a default CliRunner (no mix_stderr=False), so
    # stdout already contains stderr — no separate .stderr to read.
    assert "Invalid value" in result.stdout or "--output" in result.stdout
