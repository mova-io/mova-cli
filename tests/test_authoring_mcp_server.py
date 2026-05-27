"""ADR 025 PR4 — the ``mdk mcp serve`` authoring MCP server tests.

Hermetic (tmp_path projects + the deterministic mock-run verify path — no API
keys, no network, no ``~/.movate`` writes). Coverage:

* **Self-describing manifest** — the server lists a ``plan_<action>`` +
  ``apply_<action>`` tool per catalog action (+ ``validate``/``run``), with
  schemas derived from each action's ``args_model`` (never hand-written).
* **plan_<action> = dry-run** — a ``tools/call`` for ``plan_*`` returns the
  ActionPlan (diff, gate) and writes nothing.
* **apply_<action> routes through the driver** — mutates via the primitive
  against the tmp project, with ``requires_confirmation`` surfaced/enforced for
  a gated (cost/networked/destructive) action.
* **validate / run** — the catalog-wide tools load + mock-smoke an agent.
* **JSON-RPC framing** — initialize handshake, stdio loop, notification skip,
  unknown-method error, tool-level error (isError) vs JSON-RPC error.
* **Lazy import** — importing ``movate.cli.main`` does not import the MCP
  server engine (base install / non-mcp commands unaffected).
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.authoring.catalog import action_names, get_action
from movate.authoring.mcp_server import (
    MCP_PROTOCOL_VERSION,
    AuthoringMCPServer,
    build_server,
    serve_stdio,
)

# Importing movate.cli.main here is safe for the lazy-import contract: the MCP
# server engine is imported lazily inside `mdk mcp serve`, so this top-level
# import does NOT pull in movate.authoring.mcp_server. The dedicated
# subprocess test (test_cli_main_import_does_not_load_mcp_server) asserts that.
from movate.cli.main import app

_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: greeter
version: 0.1.0
description: A test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
  params:
    temperature: 0.0
prompt: ./prompt.md
schema:
  input:
    text: string
  output:
    message: string
evals:
  dataset: ./evals/dataset.jsonl
"""

_PROMPT = "You are a greeter. Reply with a greeting.\n"
_DATASET = '{"input": {"text": "hi"}, "expected": {"message": "hello"}}\n'


def _make_project(root: Path, *, agent: str = "greeter") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("# test project\n")
    agent_dir = root / "agents" / agent
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML.replace("name: greeter", f"name: {agent}"))
    (agent_dir / "prompt.md").write_text(_PROMPT)
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text(_DATASET)
    return root


def _snapshot_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".mdk" not in p.parts and ".movate" not in p.parts:
            out[str(p.relative_to(root))] = p.read_text(encoding="utf-8")
    return out


def _server(root: Path) -> AuthoringMCPServer:
    return build_server(root)


def _call(server: AuthoringMCPServer, name: str, arguments: dict, msg_id: int = 1) -> dict:
    """Make a tools/call and return the (parsed) result frame's ``result``."""
    frame = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert frame is not None
    assert "error" not in frame, frame
    return frame["result"]


def _structured(result: dict) -> dict:
    """Pull the structuredContent out of an MCP tools/call result."""
    assert result["isError"] is False, result
    return result["structuredContent"]


# ---------------------------------------------------------------------------
# self-describing manifest (generated from the catalog)
# ---------------------------------------------------------------------------


def test_manifest_lists_plan_and_apply_per_action(tmp_path: Path) -> None:
    server = _server(_make_project(tmp_path / "proj"))
    tools = {t["name"]: t for t in server.tool_manifest()}
    for action in action_names():
        assert f"plan_{action}" in tools
        assert f"apply_{action}" in tools
    assert "validate" in tools
    assert "run" in tools
    # one plan + one apply per action, plus validate + run
    assert len(tools) == 2 * len(action_names()) + 2


def test_manifest_schemas_derived_from_catalog(tmp_path: Path) -> None:
    """Each plan tool's inputSchema is the action's own args_model JSON schema."""
    server = _server(_make_project(tmp_path / "proj"))
    tools = {t["name"]: t for t in server.tool_manifest()}
    expected = get_action("add-context").args_model.model_json_schema()
    assert tools["plan_add-context"]["inputSchema"] == expected
    # The apply tool merges the driver's control knobs onto the action's props.
    apply_props = tools["apply_add-context"]["inputSchema"]["properties"]
    assert "name" in apply_props  # the action's own arg
    assert {"confirmed", "fast_mode", "verify"} <= set(apply_props)


def test_manifest_is_json_serializable(tmp_path: Path) -> None:
    server = _server(_make_project(tmp_path / "proj"))
    payload = server.tool_manifest()
    assert json.loads(json.dumps(payload)) == payload


def test_tools_list_method(tmp_path: Path) -> None:
    server = _server(_make_project(tmp_path / "proj"))
    frame = server.handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert frame is not None
    assert frame["id"] == 7
    assert isinstance(frame["result"]["tools"], list)
    assert frame["result"]["tools"]  # non-empty


# ---------------------------------------------------------------------------
# initialize handshake
# ---------------------------------------------------------------------------


def test_initialize_returns_protocol_version(tmp_path: Path) -> None:
    server = _server(_make_project(tmp_path / "proj"))
    frame = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert frame is not None
    result = frame["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "mdk-authoring"
    assert "tools" in result["capabilities"]


def test_notification_yields_no_response(tmp_path: Path) -> None:
    server = _server(_make_project(tmp_path / "proj"))
    # No "id" → a notification; the server must not reply.
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_json_rpc_error(tmp_path: Path) -> None:
    server = _server(_make_project(tmp_path / "proj"))
    frame = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "bogus/method"})
    assert frame is not None
    assert frame["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# plan_<action> — dry-run, no writes
# ---------------------------------------------------------------------------


def test_plan_tool_returns_plan_and_writes_nothing(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    before = _snapshot_tree(root)
    result = _call(
        server,
        "plan_add-context",
        {"agent": "greeter", "name": "tone", "body": "# Tone\nBe warm.\n"},
    )
    payload = _structured(result)
    assert payload["action"] == "add-context"
    assert "+# Tone" in payload["diff"]
    assert payload["requires_confirmation"] is False
    assert _snapshot_tree(root) == before  # NO writes


def test_plan_gated_action_surfaces_requires_confirmation(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    result = _call(server, "plan_set-retrieval", {"agent": "greeter", "auto_into": "context"})
    payload = _structured(result)
    assert payload["requires_confirmation"] is True
    assert "cost" in payload["side_effects"]


def test_plan_unknown_action_is_method_not_found(tmp_path: Path) -> None:
    server = _server(_make_project(tmp_path / "proj"))
    frame = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "plan_no-such-action", "arguments": {}},
        }
    )
    assert frame is not None
    assert frame["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# apply_<action> — routes through the driver, mutates via the primitive
# ---------------------------------------------------------------------------


def test_apply_tool_mutates_via_driver(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    result = _call(
        server,
        "apply_add-context",
        {"agent": "greeter", "name": "tone", "body": "# Tone\nBe warm.\n", "fast_mode": True},
    )
    payload = _structured(result)
    assert payload["applied"] is True
    assert payload["result"]["summary"]
    # The file landed + agent.yaml was wired (the shipped primitive's effect).
    assert (root / "agents" / "greeter" / "contexts" / "tone.md").is_file()
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert "tone" in data["contexts"]
    # Verify ran (D3) through the driver.
    assert payload["verify"]["ok"] is True
    assert payload["verify"]["validated"] is True


def test_apply_gated_action_refuses_without_confirmation(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    # set-model is cost/confirm-gated; without confirmed it must NOT apply.
    result = _call(
        server,
        "apply_set-model",
        {"agent": "greeter", "provider": "anthropic/claude-sonnet-4-6", "fast_mode": True},
    )
    assert result["isError"] is True
    assert "confirm" in result["content"][0]["text"].lower()
    # The agent.yaml is unchanged.
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert data["model"]["provider"] == "openai/gpt-4o-mini-2024-07-18"


def test_apply_gated_action_applies_with_confirmation(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    result = _call(
        server,
        "apply_set-model",
        {"agent": "greeter", "provider": "anthropic/claude-sonnet-4-6", "confirmed": True},
    )
    payload = _structured(result)
    assert payload["applied"] is True
    data = yaml.safe_load((root / "agents" / "greeter" / "agent.yaml").read_text())
    assert data["model"]["provider"] == "anthropic/claude-sonnet-4-6"


def test_apply_bad_args_is_tool_error_not_crash(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    # Missing required `name` → pydantic ValidationError → surfaced as isError.
    result = _call(server, "apply_add-context", {"agent": "greeter", "fast_mode": True})
    assert result["isError"] is True


# ---------------------------------------------------------------------------
# validate / run catalog-wide tools
# ---------------------------------------------------------------------------


def test_validate_tool_ok(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    payload = _structured(_call(server, "validate", {"agent": "greeter"}))
    assert payload["ok"] is True
    assert payload["agent"] == "greeter"


def test_validate_tool_missing_agent_is_tool_error(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    result = _call(server, "validate", {"agent": "ghost"})
    assert result["isError"] is True


def test_run_tool_mock_smoke(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    payload = _structured(_call(server, "run", {"agent": "greeter"}))
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# stdio JSON-RPC loop (the transport)
# ---------------------------------------------------------------------------


def test_serve_stdio_round_trips(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    requests = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "plan_add-context",
                        "arguments": {"agent": "greeter", "name": "tone"},
                    },
                }
            ),
        ]
    )
    out = io.StringIO()
    serve_stdio(server, io.StringIO(requests + "\n"), out)
    frames = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    # initialize + tools/list + tools/call → 3 responses; the notification got none.
    assert [f["id"] for f in frames] == [1, 2, 3]
    assert frames[0]["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION


def test_serve_stdio_non_json_line_yields_error_frame(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    server = _server(root)
    out = io.StringIO()
    serve_stdio(server, io.StringIO("not json at all\n"), out)
    frame = json.loads(out.getvalue().strip())
    assert frame["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# lazy import — the base CLI must not pull in the MCP server engine
# ---------------------------------------------------------------------------


def test_cli_main_import_does_not_load_mcp_server() -> None:
    """Importing movate.cli.main (and running a non-mcp command) must NOT import
    the MCP server engine — the base install / other commands are unaffected."""
    code = (
        "import sys\n"
        "import movate.cli.main  # noqa: F401\n"
        "assert 'movate.authoring.mcp_server' not in sys.modules, "
        "'mcp_server was eagerly imported by movate.cli.main'\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_mcp_command_is_registered() -> None:
    """`mdk mcp serve` is wired into the top-level app (additive command group)."""
    runner = CliRunner()
    result = runner.invoke(app, ["mcp", "serve", "--help"])
    # exit_code 0 is the robust registration signal: an unregistered subcommand
    # exits 2 ("No such command 'serve'"). We deliberately do NOT assert on the
    # Rich-rendered help body — under CI's narrow non-TTY width Typer/Rich wraps
    # option names across panel rows and interleaves box-drawing + ANSI codes,
    # so any substring match on a flag name is fragile. The `--list-tools` flag
    # itself is covered behaviorally by test_list_tools_flag_prints_manifest.
    assert result.exit_code == 0, result.output
    # The usage line is short and never wraps; strip ANSI before matching so the
    # assertion survives Rich styling regardless of terminal width.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    assert "mcp serve" in plain


def test_list_tools_flag_prints_manifest(tmp_path: Path) -> None:
    proj = _make_project(tmp_path / "proj")
    runner = CliRunner()
    result = runner.invoke(app, ["mcp", "serve", "--project", str(proj), "--list-tools"])
    assert result.exit_code == 0, result.output
    assert "plan_add-context" in result.output
    assert "apply_add-context" in result.output


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
