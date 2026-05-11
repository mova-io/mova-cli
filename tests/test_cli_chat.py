"""``movate chat <agent>`` — interactive REPL bound to one agent.

Contract:
* Two turns then ``:q`` → both responses appear, REPL exits 0.
* Multi-field-input agent → exit 2 with a clear "can't auto-wrap"
  message (chat only handles single-required-string-field schemas).
* Empty input loops without firing a turn (no zero-byte runs).
* ``:q`` / ``exit`` / ``quit`` all terminate the loop.

Tests use ``--mock`` so they're hermetic (no API keys, no network)
and inject responses via the ``MOVATE_MOCK_RESPONSE`` env var.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app


def _scaffold_chat_agent(agent_dir: Path) -> Path:
    """Minimal single-required-string-field agent suitable for chat."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "chat-demo",
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
    (agent_dir / "prompt.md").write_text("respond to {{ input.message }}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["message"],
                "properties": {"message": {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["reply"],
                "properties": {"reply": {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"message": "x"}, "expected": {"reply": "x"}}) + "\n"
    )
    return agent_dir


def _scaffold_multi_field_agent(agent_dir: Path) -> Path:
    """Agent with TWO required fields — chat can't auto-wrap this."""
    _scaffold_chat_agent(agent_dir)
    # Overwrite the input schema with a two-field requirement.
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["message", "context"],
                "properties": {
                    "message": {"type": "string"},
                    "context": {"type": "string"},
                },
            }
        )
    )
    return agent_dir


@pytest.mark.unit
def test_chat_runs_two_turns_then_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two user lines + `:q` → both produce a response; exit 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"reply": "ok"}')
    agent_dir = _scaffold_chat_agent(tmp_path / "chat-demo")

    runner = CliRunner(mix_stderr=False)
    # Three lines piped on stdin: two messages, then `:q` to end.
    result = runner.invoke(
        cli_app,
        ["chat", str(agent_dir), "--mock"],
        input="hello\nhow are you\n:q\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The structured JSON for each turn lands on stdout. We expect two
    # response blocks because we typed two messages before :q.
    stdout_blocks = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(stdout_blocks) == 2, result.stdout
    for block in stdout_blocks:
        payload = json.loads(block)
        assert payload["data"] == {"reply": "ok"}


@pytest.mark.unit
def test_chat_rejects_multi_field_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent with > 1 required field can't be auto-wrapped — exit 2
    with a hint about using `movate run` instead."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_dir = _scaffold_multi_field_agent(tmp_path / "multi-field")

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(cli_app, ["chat", str(agent_dir), "--mock"])
    assert result.exit_code == 2
    assert "auto-wrap" in result.stderr
    assert "movate run" in result.stderr


@pytest.mark.unit
def test_chat_empty_input_does_not_fire_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank lines should loop silently — no dispatch, no output —
    so the operator can hit Enter without burning a run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"reply": "ok"}')
    agent_dir = _scaffold_chat_agent(tmp_path / "chat-demo")

    runner = CliRunner(mix_stderr=False)
    # Three blanks, one real message, then exit.
    result = runner.invoke(
        cli_app,
        ["chat", str(agent_dir), "--mock"],
        input="\n\n\nactual message\n:q\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Only the real message produced a stdout block.
    stdout_blocks = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(stdout_blocks) == 1


@pytest.mark.unit
@pytest.mark.parametrize("exit_token", [":q", "exit", "quit", ":quit"])
def test_chat_all_exit_tokens_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_token: str
) -> None:
    """Each of the documented exit tokens cleanly ends the REPL."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"reply": "ok"}')
    agent_dir = _scaffold_chat_agent(tmp_path / "chat-demo")

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        cli_app,
        ["chat", str(agent_dir), "--mock"],
        input=f"{exit_token}\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "chat ended" in result.stderr


@pytest.mark.unit
def test_chat_eof_terminates_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-D / closed stdin should be treated the same as :q —
    no traceback, exit 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"reply": "ok"}')
    agent_dir = _scaffold_chat_agent(tmp_path / "chat-demo")

    runner = CliRunner(mix_stderr=False)
    # Empty stdin → Prompt.ask raises EOFError on first read.
    result = runner.invoke(cli_app, ["chat", str(agent_dir), "--mock"], input="")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "chat ended" in result.stderr


# ---------------------------------------------------------------------------
# Conversation memory
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chat_advertises_memory_status_in_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner should tell the operator whether memory is on or off
    — there's no other visible signal during a session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"reply": "ok"}')
    agent_dir = _scaffold_chat_agent(tmp_path / "chat-demo")
    runner = CliRunner(mix_stderr=False)

    # Default: memory on.
    on = runner.invoke(cli_app, ["chat", str(agent_dir), "--mock"], input=":q\n")
    assert on.exit_code == 0
    assert "memory on" in on.stderr

    # --no-memory: stays off.
    off = runner.invoke(cli_app, ["chat", str(agent_dir), "--mock", "--no-memory"], input=":q\n")
    assert off.exit_code == 0
    assert "memory off" in off.stderr


@pytest.mark.unit
def test_chat_passes_history_to_executor_when_memory_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turn 2's executor invocation should receive turn 1's
    user/assistant pair as history. We intercept executor.execute()
    to capture the kwargs and assert the history shape."""
    from movate.core.executor import Executor  # noqa: PLC0415
    from movate.providers.base import Message  # noqa: PLC0415

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"reply": "echo"}')
    agent_dir = _scaffold_chat_agent(tmp_path / "chat-demo")

    captured_histories: list[list[Message] | None] = []
    real_execute = Executor.execute

    async def spy_execute(self, bundle, request, **kwargs):  # type: ignore[no-untyped-def]
        # Copy the history list at capture time — chat mutates it
        # in-place across turns, so storing the reference would have
        # every captured slot point at the FINAL state.
        h = kwargs.get("history")
        captured_histories.append(list(h) if h is not None else None)
        return await real_execute(self, bundle, request, **kwargs)

    monkeypatch.setattr(Executor, "execute", spy_execute)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        cli_app,
        ["chat", str(agent_dir), "--mock"],
        input="first message\nsecond message\n:q\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # Two turns dispatched.
    assert len(captured_histories) == 2
    # Turn 1: empty history (memory is on, but no prior turns).
    assert captured_histories[0] == []
    # Turn 2: turn 1's user+assistant pair as history.
    turn2_history = captured_histories[1]
    assert turn2_history is not None
    assert len(turn2_history) == 2
    assert turn2_history[0].role == "user"
    assert turn2_history[0].content == "first message"
    assert turn2_history[1].role == "assistant"
    # Assistant content is response.human_readable if available, else
    # JSON-encoded response.data. The chat-demo agent's output uses
    # `reply` (not one of the executor's recognised human-readable
    # keys), so we get the JSON fallback — but either way it must
    # contain the model's actual response text.
    assert "echo" in turn2_history[1].content


@pytest.mark.unit
def test_chat_does_not_pass_history_under_no_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-memory`` passes ``history=None`` (not ``[]``) — proves
    each turn is truly independent at the executor level. None vs []
    is a meaningful distinction: None = "didn't opt in"; [] = "first
    turn"."""
    from movate.core.executor import Executor  # noqa: PLC0415

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"reply": "ok"}')
    agent_dir = _scaffold_chat_agent(tmp_path / "chat-demo")

    captured_histories: list[object] = []
    real_execute = Executor.execute

    async def spy_execute(self, bundle, request, **kwargs):  # type: ignore[no-untyped-def]
        captured_histories.append(kwargs.get("history"))
        return await real_execute(self, bundle, request, **kwargs)

    monkeypatch.setattr(Executor, "execute", spy_execute)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        cli_app,
        ["chat", str(agent_dir), "--mock", "--no-memory"],
        input="hello\nfollow-up\n:q\n",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(captured_histories) == 2
    # Both calls saw None — no opt-in, no history.
    assert captured_histories == [None, None]
