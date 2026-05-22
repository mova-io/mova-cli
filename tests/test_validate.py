"""Tests for _check_vector_kb_empty in mdk validate.

Covers:
1. Agent with kb-vector skill + 0 chunks in storage → warning printed.
2. Agent with kb-vector skill + >=1 chunk in storage → no warning.
3. Agent without any kb-vector skill → no warning (storage never called).
4. Storage error → no warning, no crash (silently swallowed).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from movate.cli.validate import _check_vector_kb_empty
from movate.core.models import KbChunk


def _make_bundle(skill_names: list[str]) -> MagicMock:
    """Create a minimal AgentBundle mock with the given skill names."""
    bundle = MagicMock()
    bundle.spec.name = "test-agent"
    skills = []
    for name in skill_names:
        skill = MagicMock()
        skill.spec.name = name
        skills.append(skill)
    bundle.skills = skills
    return bundle


def _make_console() -> MagicMock:
    con = MagicMock()
    con.print = MagicMock()
    return con


@pytest.mark.unit
class TestCheckVectorKbEmpty:
    def test_warns_when_vector_kb_empty(self) -> None:
        """kb-vector skill declared but 0 chunks → warning printed."""
        bundle = _make_bundle(["kb-vector-lookup"])
        con = _make_console()

        mock_storage = AsyncMock()
        mock_storage.init = AsyncMock()
        mock_storage.list_kb_chunks = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("movate.storage.build_storage", return_value=mock_storage):
            _check_vector_kb_empty(bundle, con)

        assert con.print.called
        printed = " ".join(str(call) for call in con.print.call_args_list)
        assert "kb-vector-lookup" in printed
        assert "0 chunks" in printed

    def test_silent_when_chunks_exist(self) -> None:
        """kb-vector skill declared and >=1 chunk exists → no warning."""
        bundle = _make_bundle(["kb-vector-lookup"])
        con = _make_console()

        chunk = MagicMock(spec=KbChunk)
        mock_storage = AsyncMock()
        mock_storage.init = AsyncMock()
        mock_storage.list_kb_chunks = AsyncMock(return_value=[chunk])
        mock_storage.close = AsyncMock()

        with patch("movate.storage.build_storage", return_value=mock_storage):
            _check_vector_kb_empty(bundle, con)

        con.print.assert_not_called()

    def test_silent_when_no_kb_skill(self) -> None:
        """Agent without kb-vector skill → storage never touched, no warning."""
        bundle = _make_bundle(["kb-lookup", "send-email"])
        con = _make_console()

        with patch("movate.storage.build_storage") as mock_build:
            _check_vector_kb_empty(bundle, con)

        mock_build.assert_not_called()
        con.print.assert_not_called()

    def test_silent_on_storage_error(self) -> None:
        """Any storage exception → silently swallowed, no warning, no crash."""
        bundle = _make_bundle(["kb-vector-lookup"])
        con = _make_console()

        def _boom() -> object:
            raise RuntimeError("no database here")

        with patch("movate.storage.build_storage", side_effect=_boom):
            _check_vector_kb_empty(bundle, con)  # must not raise

        con.print.assert_not_called()

    def test_case_insensitive_skill_name(self) -> None:
        """'KB-Vector-Lookup' (mixed case) should still trigger the check."""
        bundle = _make_bundle(["KB-Vector-Lookup"])
        con = _make_console()

        mock_storage = AsyncMock()
        mock_storage.init = AsyncMock()
        mock_storage.list_kb_chunks = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("movate.storage.build_storage", return_value=mock_storage):
            _check_vector_kb_empty(bundle, con)

        assert con.print.called

    def test_hint_contains_agent_name_and_ingest_cmd(self) -> None:
        """Warning output should mention agent name and ingest hint."""
        bundle = _make_bundle(["kb-vector-lookup"])
        bundle.spec.name = "my-rag-agent"
        con = _make_console()

        mock_storage = AsyncMock()
        mock_storage.init = AsyncMock()
        mock_storage.list_kb_chunks = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("movate.storage.build_storage", return_value=mock_storage):
            _check_vector_kb_empty(bundle, con)

        printed = " ".join(str(call) for call in con.print.call_args_list)
        assert "my-rag-agent" in printed
        assert "ingest" in printed.lower()
