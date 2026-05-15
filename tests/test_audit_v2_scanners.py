"""Sprint S — audit v2 scanner tests.

Targets the 5 new scanners added in Sprint S:

* floating-model-tag — model uses ``:latest`` / ``:stable``
* missing-version    — agent.yaml has no version
* missing-fallback   — no model.fallback declared
* prompt-too-long    — prompt.md > 8000 chars
* schema-no-required — input schema has no non-optional fields

Existing scanners (v1) are exercised in :mod:`tests.test_audit`. This
file is additive — no overlap with the v1 fixtures or assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.audit.scanners import (
    SCANNERS,
    scan_floating_model_tag,
    scan_missing_fallback,
    scan_missing_version,
    scan_prompt_too_long,
    scan_schema_no_required,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_agent(
    tmp_path: Path,
    *,
    name: str = "demo",
    provider: str = "openai/gpt-4o-mini-2024-07-18",
    version: str | None = "0.1.0",
    fallback: bool = False,
    schema_input: str | None = "{ q: string }",
    prompt: str = "minimal prompt",
) -> Path:
    """Build an agent dir under tmp_path/agents/<name>/.

    Each kwarg flips one auditable property. Defaults produce a clean
    agent that none of the v2 scanners flag.
    """
    agent_dir = tmp_path / "agents" / name
    agent_dir.mkdir(parents=True)

    parts: list[str] = [
        "api_version: movate/v1",
        "kind: Agent",
        f"name: {name}",
    ]
    if version:
        parts.append(f"version: {version}")
    parts.append("model:")
    parts.append(f"  provider: {provider}")
    if fallback:
        parts.append("  fallback:")
        parts.append("    - provider: anthropic/claude-haiku-4-5-20251001")
    parts.append("prompt: ./prompt.md")
    if schema_input is not None:
        parts.append("schema:")
        parts.append(f"  input: {schema_input}")
        parts.append("  output: { a: string }")
    (agent_dir / "agent.yaml").write_text("\n".join(parts) + "\n")
    (agent_dir / "prompt.md").write_text(prompt)
    return agent_dir


# ---------------------------------------------------------------------------
# floating-model-tag
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFloatingModelTag:
    def test_latest_flagged(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, provider="openai/gpt-4o-mini:latest")
        findings = scan_floating_model_tag(agent_dir, "demo")
        assert len(findings) == 1
        assert findings[0].category == "floating-model-tag"

    def test_stable_flagged(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, provider="anthropic/claude:stable")
        findings = scan_floating_model_tag(agent_dir, "demo")
        assert len(findings) == 1

    def test_pinned_version_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, provider="openai/gpt-4o-mini-2024-07-18")
        findings = scan_floating_model_tag(agent_dir, "demo")
        assert findings == []

    def test_no_agent_yaml_no_finding(self, tmp_path: Path) -> None:
        """Empty dir → no false positive."""
        empty = tmp_path / "agents" / "ghost"
        empty.mkdir(parents=True)
        findings = scan_floating_model_tag(empty, "ghost")
        assert findings == []


# ---------------------------------------------------------------------------
# missing-version
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMissingVersion:
    def test_no_version_flagged_as_warning(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, version=None)
        findings = scan_missing_version(agent_dir, "demo")
        assert len(findings) == 1
        assert findings[0].severity.value == "warning"

    def test_with_version_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, version="1.2.3")
        findings = scan_missing_version(agent_dir, "demo")
        assert findings == []


# ---------------------------------------------------------------------------
# missing-fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMissingFallback:
    def test_no_fallback_flagged(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, fallback=False)
        findings = scan_missing_fallback(agent_dir, "demo")
        assert len(findings) == 1
        assert findings[0].category == "missing-fallback"
        assert findings[0].severity.value == "warning"

    def test_with_fallback_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, fallback=True)
        findings = scan_missing_fallback(agent_dir, "demo")
        assert findings == []


# ---------------------------------------------------------------------------
# prompt-too-long
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPromptTooLong:
    def test_short_prompt_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, prompt="short")
        findings = scan_prompt_too_long(agent_dir, "demo")
        assert findings == []

    def test_long_prompt_flagged(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, prompt="x" * 10_000)
        findings = scan_prompt_too_long(agent_dir, "demo")
        assert len(findings) == 1
        assert findings[0].severity.value == "warning"
        # Char count appears in the message
        assert "10," in findings[0].message

    def test_no_prompt_md_no_finding(self, tmp_path: Path) -> None:
        """Missing prompt.md is a different scanner's concern."""
        agent_dir = _make_agent(tmp_path)
        (agent_dir / "prompt.md").unlink()
        findings = scan_prompt_too_long(agent_dir, "demo")
        assert findings == []


# ---------------------------------------------------------------------------
# schema-no-required
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchemaNoRequired:
    def test_only_optional_fields_flagged(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, schema_input='{ "q?": string, "n?": integer }')
        findings = scan_schema_no_required(agent_dir, "demo")
        assert len(findings) == 1

    def test_at_least_one_required_clean(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path, schema_input='{ "q": string, "n?": integer }')
        findings = scan_schema_no_required(agent_dir, "demo")
        assert findings == []

    def test_path_form_skipped(self, tmp_path: Path) -> None:
        """Path-form schemas can't be audited inline; skipped."""
        agent_dir = _make_agent(tmp_path, schema_input='"./schema/input.json"')
        findings = scan_schema_no_required(agent_dir, "demo")
        assert findings == []


# ---------------------------------------------------------------------------
# Integration: new scanners registered in SCANNERS map
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_v2_scanners_registered() -> None:
    """Every v2 scanner must appear in the SCANNERS registry so the
    CLI's category filter recognizes them."""
    expected_v2 = {
        "floating-model-tag",
        "missing-version",
        "missing-fallback",
        "prompt-too-long",
        "schema-no-required",
    }
    assert expected_v2 <= set(SCANNERS.keys())
