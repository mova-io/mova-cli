"""Regression tests for the input coercers' file-path guard in ``mdk run``.

Background
----------
``mdk run <agent> '<json>'`` (and local/workflow/remote variants) used to
crash with ``OSError: [Errno 63] File name too long`` whenever the inline
JSON argument exceeded the OS filename limit (~255 bytes). The coercers
treated the positional arg as a possible file path and called
``Path(raw).is_file()``, which invokes ``os.stat``. ``os.stat`` raises
``ENAMETOOLONG`` for an oversized name, and ``Path.is_file()`` only swallows
a whitelist of errnos — it re-raises ``ENAMETOOLONG`` instead of returning
``False`` — so it propagated and crashed the CLI.

A short input stays under the limit and works, masking the bug.

The fix wraps the check in ``_looks_like_existing_file`` which treats any
``OSError`` as "not a file", letting an oversized JSON string fall through
to ``json.loads`` as intended.

Covers, for each of the three coercers (local agent / workflow / remote):
1. A long JSON object string (>255 chars) parses into a dict WITHOUT raising
   ``OSError`` (the original crash).
2. A real existing file path still loads + parses as before (regression).
3. A short bare JSON string still parses (regression).
Plus a direct unit test of ``_looks_like_existing_file``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from movate.cli.run import (
    _coerce_agent_input,
    _coerce_remote_agent_input,
    _coerce_workflow_input,
    _looks_like_existing_file,
)

# Comfortably over the ~255-byte NAME_MAX boundary so the regression is real
# even on filesystems with shorter limits.
_LONG_PADDING = "y" * 500


def _long_json() -> str:
    payload = {"question": "x", "context": [_LONG_PADDING]}
    raw = json.dumps(payload)
    assert len(raw) > 255  # guard: this must exceed NAME_MAX to reproduce
    return raw


def _make_bundle() -> Any:
    """Minimal AgentBundle stand-in.

    The long-JSON and file-path branches return before the input schema or
    name is consulted, so a lightweight stub is sufficient here.
    """
    return SimpleNamespace(
        input_schema={"type": "object", "properties": {}, "required": []},
        spec=SimpleNamespace(name="test-agent"),
    )


@pytest.mark.unit
class TestLooksLikeExistingFile:
    def test_oversized_string_returns_false_not_raises(self) -> None:
        # A >255-char string can't be a valid filename; os.stat raises
        # ENAMETOOLONG, which the helper must swallow into False.
        assert _looks_like_existing_file("z" * 500) is False

    def test_existing_file_returns_true(self, tmp_path: Path) -> None:
        f = tmp_path / "input.json"
        f.write_text("{}")
        assert _looks_like_existing_file(str(f)) is True

    def test_short_nonexistent_path_returns_false(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.json"
        assert _looks_like_existing_file(str(missing)) is False


@pytest.mark.unit
class TestCoerceAgentInput:
    def test_long_inline_json_does_not_crash(self) -> None:
        raw = _long_json()
        result = _coerce_agent_input(raw, _make_bundle())
        assert result == {"question": "x", "context": [_LONG_PADDING]}

    def test_existing_file_path_still_loads(self, tmp_path: Path) -> None:
        f = tmp_path / "payload.json"
        f.write_text(json.dumps({"question": "from-file"}))
        result = _coerce_agent_input(str(f), _make_bundle())
        assert result == {"question": "from-file"}

    def test_short_bare_json_still_parses(self) -> None:
        result = _coerce_agent_input('{"a": 1}', _make_bundle())
        assert result == {"a": 1}


@pytest.mark.unit
class TestCoerceWorkflowInput:
    def test_long_inline_json_does_not_crash(self) -> None:
        raw = _long_json()
        result = _coerce_workflow_input(raw)
        assert result == {"question": "x", "context": [_LONG_PADDING]}

    def test_existing_file_path_still_loads(self, tmp_path: Path) -> None:
        f = tmp_path / "state.json"
        f.write_text(json.dumps({"step": "start"}))
        result = _coerce_workflow_input(str(f))
        assert result == {"step": "start"}

    def test_short_bare_json_still_parses(self) -> None:
        result = _coerce_workflow_input('{"a": 1}')
        assert result == {"a": 1}


@pytest.mark.unit
class TestCoerceRemoteAgentInput:
    def test_long_inline_json_does_not_crash(self) -> None:
        # The long-JSON branch returns before any local-bundle / network
        # resolution, so no runtime is required.
        raw = _long_json()
        result = _coerce_remote_agent_input(raw, "test-agent")
        assert result == {"question": "x", "context": [_LONG_PADDING]}

    def test_existing_file_path_still_loads(self, tmp_path: Path) -> None:
        # A valid JSON dict in the file returns before the auto-wrap
        # fallback, so no local bundle is needed.
        f = tmp_path / "remote-payload.json"
        f.write_text(json.dumps({"question": "from-file"}))
        result = _coerce_remote_agent_input(str(f), "test-agent")
        assert result == {"question": "from-file"}

    def test_short_bare_json_still_parses(self) -> None:
        result = _coerce_remote_agent_input('{"a": 1}', "test-agent")
        assert result == {"a": 1}
