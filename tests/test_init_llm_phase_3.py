"""Phase 3 of mdk init --llm: UX polish.

Five items added on top of Phase 2:

1. **Name-constraint enforcement** — ``agent_yaml.name`` is forced to
   the CLI's ``<name>`` argument after generation, so a forgetful LLM
   that echoes the few-shot exemplar's name doesn't break the dir↔
   agent.yaml correspondence.
2. **Rich spinner during generation** — wrapped via ``_progress.spinner``.
   Hard to assert visually; we verify the call doesn't crash and the
   import path stays wired.
3. **Cost echo** — total cost in USD computed from the rolled-up
   TokenUsage + pricing table, surfaced in both the success Panel and
   the greppable summary line. Missing pricing entry → ``unknown``.
4. **Post-success hint** — ``_console.hint`` stderr line pointing at
   ``prompt.md`` for review; respects ``--quiet``.
5. **Greppable summary line** — ``mdk_init_summary: name=... llm=...
   model=... input_tokens=... output_tokens=... cost_usd=...
   retried=... ok=...``. Mirrors audit / eval / doctor for CI parity.

These tests exercise the full CLI path with ``--mock`` so they're
hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli._progress import spinner
from movate.cli.init import (
    _accumulate_tokens,
    _safe_cost,
)
from movate.cli.main import app
from movate.core.models import TokenUsage

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Shared canned valid payload
# ---------------------------------------------------------------------------


def _valid_agent_payload(name: str = "canned-name") -> dict:
    """A GeneratedAgent-shaped dict that load_agent will accept."""
    return {
        "agent_yaml": {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": name,
            "version": "0.1.0",
            "description": "test",
            "owner": "",
            "model": {
                "provider": "openai/gpt-4o-mini-2024-07-18",
                "params": {"temperature": 0.0, "max_tokens": 512},
            },
            "prompt": "./prompt.md",
            "schema": {"input": "./schema/input.yaml", "output": "./schema/output.yaml"},
            "evals": {"dataset": "./evals/dataset.jsonl"},
        },
        "prompt_md": "Reply: {{ input.text }}",
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        },
        "sample_evals": [
            {"input": {"text": "x"}, "expected": {"message": "y"}},
        ],
    }


@pytest.fixture
def mock_canned_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set MOVATE_MOCK_RESPONSE to a valid GeneratedAgent payload whose
    `agent_yaml.name` is DIFFERENT from what the CLI will pass —
    so the name-constraint test can verify the override fires."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", json.dumps(_valid_agent_payload("canned-name")))


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Item 1: name-constraint enforcement
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_name_constraint_forces_agent_yaml_name_to_cli_arg(
    tmp_path: Path,
    mock_canned_response: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM echoes the wrong name in agent_yaml, the CLI
    must override it to match the directory name."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "expected-name",
            "--llm",
            "a description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    import yaml  # noqa: PLC0415

    parsed = yaml.safe_load((tmp_path / "expected-name" / "agent.yaml").read_text())
    # The LLM (mock) returned name="canned-name" but the CLI override
    # forced it to "expected-name" to match the directory.
    assert parsed["name"] == "expected-name"


# ---------------------------------------------------------------------------
# Item 2: spinner wired (smoke — no visual assertion)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_spinner_import_path_wires_through() -> None:
    """The Phase 3 code imports ``spinner`` from ``movate.cli._progress``
    — verify both ends of the import chain exist so a broken import
    surfaces here rather than at runtime."""
    # Smoke: the spinner is a context manager. Build + tear down once
    # to make sure no import-time invariant blew up.
    with spinner("phase-3 wiring test"):
        pass


# ---------------------------------------------------------------------------
# Item 3: cost echo
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSafeCost:
    def test_known_model_returns_cost(self) -> None:
        """The default scaffold model is in the pricing table — a
        non-trivial TokenUsage should produce a positive cost."""
        tokens = TokenUsage(input=1000, output=500)
        cost = _safe_cost(model="openai/gpt-4o-mini-2024-07-18", tokens=tokens)
        assert cost is not None
        assert cost > 0

    def test_unknown_model_returns_none(self) -> None:
        """A model not in the pricing table must NOT raise — scaffold
        should fall back to ``cost_usd=unknown`` rather than abort."""
        tokens = TokenUsage(input=1000, output=500)
        cost = _safe_cost(model="bogus/model-that-does-not-exist", tokens=tokens)
        assert cost is None

    def test_zero_tokens_returns_zero(self) -> None:
        """Empty usage → zero cost (not ``None``). The provider was
        called and pricing succeeded; nothing was actually billed."""
        cost = _safe_cost(model="openai/gpt-4o-mini-2024-07-18", tokens=TokenUsage())
        assert cost == 0.0


@pytest.mark.unit
def test_success_panel_includes_cost_line(
    tmp_path: Path,
    mock_canned_response: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Rich success Panel should include a ``Cost:`` row when the
    pricing lookup succeeds. We assert against the Panel body content
    via stdout."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "cost-test-agent",
            "--llm",
            "a description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert "Cost:" in result.stdout
    assert "USD" in result.stdout


# ---------------------------------------------------------------------------
# Item 4: post-success hint (respects --quiet)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_post_success_hint_appears_on_stderr(
    tmp_path: Path,
    mock_canned_response: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --quiet, the stderr hint pointing at prompt.md should
    fire after a successful scaffold."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "hint-test-agent",
            "--llm",
            "a description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    # The success hint should appear on stderr (where _console.hint
    # routes by default).
    assert "scaffolded by --llm" in result.stderr or "prompt.md" in result.stderr


@pytest.mark.unit
def test_post_success_hint_suppressed_under_quiet(
    tmp_path: Path,
    mock_canned_response: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --quiet, the stderr hint must NOT fire so CI logs stay clean."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "--quiet",
            "init",
            "--bare",
            "quiet-test-agent",
            "--llm",
            "a description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    # Under --quiet, the stderr hint is suppressed.
    assert "scaffolded by --llm" not in result.stderr


@pytest.mark.unit
def test_dry_run_hint_points_at_rerun(
    tmp_path: Path,
    mock_canned_response: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run gets its own hint variant — pointing at the re-run
    command, not at prompt.md (which doesn't exist yet)."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "dry-hint-agent",
            "--llm",
            "a description",
            "--mock",
            "--dry-run",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert "preview" in result.stderr.lower() or "re-run" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Item 5: greppable mdk_init_summary line
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_greppable_summary_line_on_success(
    tmp_path: Path,
    mock_canned_response: None,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summary line must include every documented key and ok=true
    on the happy path."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "summary-agent",
            "--llm",
            "a description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    line = result.stdout
    # Every key/value pair the CI parser depends on must be present.
    assert "mdk_init_summary:" in line
    assert "name=summary-agent" in line
    assert "llm=true" in line
    assert "model=openai/gpt-4o-mini-2024-07-18" in line
    assert "input_tokens=" in line
    assert "output_tokens=" in line
    assert "cost_usd=" in line
    assert "retried=false" in line
    assert "ok=true" in line


@pytest.mark.unit
def test_summary_line_marks_retry(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When attempt 1 fails (here: a schema-violating payload that fails
    generation), the unified retry loop now fires a second attempt and
    the summary line carries ``retried=true``.

    Post-PR (retry on transport/JSON/schema errors): a first-attempt
    ``LLMScaffoldError`` no longer exits immediately — it earns a retry.
    Both attempts return the same wrong response (explicit
    MOVATE_MOCK_RESPONSE is stateless across calls), so the retry hits
    the same failure → exit 2, BUT now with a summary line marked
    retried=true."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"not": "the right shape"}')
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "init",
            "--bare",
            "retry-agent",
            "--llm",
            "a description",
            "--mock",
            "--target",
            str(tmp_path),
        ],
    )
    # Both attempts fail at generation (schema mismatch) → exit 2 (hard
    # scaffold failure), and the retry path DID run.
    assert result.exit_code == 2
    line = result.stdout
    assert "mdk_init_summary:" in line
    assert "retried=true" in line
    assert "ok=false" in line


# ---------------------------------------------------------------------------
# Helper: _accumulate_tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAccumulateTokens:
    def test_sums_input_and_output(self) -> None:
        a = TokenUsage(input=100, output=50)
        b = TokenUsage(input=200, output=75)
        c = _accumulate_tokens(a, b)
        assert c.input == 300
        assert c.output == 125

    def test_sums_cached_input(self) -> None:
        a = TokenUsage(input=100, output=50, cached_input=20)
        b = TokenUsage(input=0, output=0, cached_input=10)
        c = _accumulate_tokens(a, b)
        assert c.cached_input == 30

    def test_returns_fresh_instance_not_mutation(self) -> None:
        """The helper must NOT mutate the running TokenUsage."""
        a = TokenUsage(input=100, output=50)
        b = TokenUsage(input=200, output=75)
        _accumulate_tokens(a, b)
        # `a` is untouched.
        assert a.input == 100
        assert a.output == 50
