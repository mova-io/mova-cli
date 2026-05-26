"""Robustness batch for ``mdk init --llm`` (4 parts).

Covers the hardening shipped together as one PR:

1. **Scaffold-aware mock** — bare ``--mock`` (no MOVATE_MOCK_RESPONSE)
   synthesizes a valid GeneratedAgent so offline scaffolding SUCCEEDS;
   an explicit MOVATE_MOCK_RESPONSE still wins.
2. **Retry on transport/JSON/schema errors** — a first-attempt
   LLMScaffoldError now earns a retry (previously exited immediately);
   two failures → debug artifact + non-zero exit.
3. **Key-matched generated model** — the GENERATED agent's
   ``model.provider`` matches the key the operator actually has;
   ``--llm-model`` is honored; ``--mock`` defaults sanely.
4. **Eval-row validation** — ``sample_evals`` rows are validated against
   the generated I/O schemas before writing; empty evals are legal.

All provider/network calls are mocked; no real ``~/.movate`` is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import movate.cli.init as init_mod
from movate.cli.init import (
    _DEFAULT_LLM_MODEL,
    _pick_target_model,
    _try_validate,
    _validate_sample_evals,
)
from movate.cli.main import app
from movate.core.models import TokenUsage
from movate.providers.base import CompletionResponse
from movate.scaffold import GeneratedAgent

runner = CliRunner(mix_stderr=False)


_PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LYZR_API_KEY",
)


def _strip_all_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)


def _valid_agent_payload(name: str = "robust-agent") -> dict[str, Any]:
    """A GeneratedAgent-shaped dict that load_agent accepts."""
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
            "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
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
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Part 1 — scaffold-aware mock
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScaffoldAwareMock:
    def test_bare_mock_dry_run_succeeds(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--mock --dry-run` WITHOUT MOVATE_MOCK_RESPONSE now succeeds —
        the synthesized agent passes validation; no files written."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "dry-mock-agent",
                "--llm",
                "an offline test agent",
                "--mock",
                "--dry-run",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # No files written in dry-run.
        assert not (tmp_path / "dry-mock-agent").exists()
        assert "preview" in result.stdout.lower() or "dry-run" in result.stdout.lower()

    def test_bare_mock_real_write_produces_loadable_agent(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--mock` (no env override) writes a runnable agent that
        passes load_agent — the core Part-1 bugfix."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "write-mock-agent",
                "--llm",
                "an offline test agent",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        target = tmp_path / "write-mock-agent"
        assert (target / "agent.yaml").is_file()
        assert (target / "prompt.md").is_file()
        assert (target / "schema" / "input.json").is_file()
        assert (target / "schema" / "output.json").is_file()
        # load_agent accepts it (validate command exercises the same path).
        from movate.core.loader import load_agent  # noqa: PLC0415

        bundle = load_agent(target)
        assert bundle.spec.name == "write-mock-agent"

    def test_explicit_mock_response_still_overrides(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit MOVATE_MOCK_RESPONSE must defeat the scaffold
        synthesis (phase-3 force-feed contract). We set a DISTINCTIVE
        valid payload and confirm its description survives — proving the
        synthesized generic payload did NOT replace it."""
        _strip_all_provider_keys(monkeypatch)
        payload = _valid_agent_payload("explicit-agent")
        payload["agent_yaml"]["description"] = "EXPLICIT-OVERRIDE-MARKER"
        monkeypatch.setenv("MOVATE_MOCK_RESPONSE", json.dumps(payload))
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "explicit-agent",
                "--llm",
                "ignored description",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        parsed = yaml.safe_load((tmp_path / "explicit-agent" / "agent.yaml").read_text())
        assert parsed["description"] == "EXPLICIT-OVERRIDE-MARKER"

    def test_mock_provider_detects_scaffold_prompt_directly(self) -> None:
        """Unit-level: a default MockProvider returns synthesized JSON for
        a scaffold prompt, and the canned default for a non-scaffold one."""
        import asyncio  # noqa: PLC0415

        from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415
        from movate.providers.mock import MockProvider  # noqa: PLC0415
        from movate.scaffold import generate_agent_from_description  # noqa: PLC0415

        provider = MockProvider()  # default response, not overridden
        result = asyncio.run(
            generate_agent_from_description(
                description="a thing",
                name="parsed-name-agent",
                model="openai/gpt-4o-mini-2024-07-18",
                provider=provider,
            )
        )
        # Name parsed out of the meta-prompt's "AGENT NAME:" line.
        assert result.agent.agent_yaml["name"] == "parsed-name-agent"

        # A non-scaffold prompt still gets the canned default.
        plain = asyncio.run(
            provider.complete(
                CompletionRequest(
                    provider="mock",
                    messages=[Message(role="user", content="just a normal prompt")],
                )
            )
        )
        assert json.loads(plain.text) == {"message": "mock response"}


# ---------------------------------------------------------------------------
# Part 2 — retry on transport/JSON/schema errors
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """Provider double: returns each scripted response text in order.

    Each entry is the literal ``text`` body the provider returns. The
    scaffold generator then parses/validates it — so junk text exercises
    the LLMScaffoldError path, valid GeneratedAgent JSON the happy path.
    """

    name = "scripted"
    version = "0.0.1"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, request: Any) -> CompletionResponse:
        idx = min(self.calls, len(self._responses) - 1)
        text = self._responses[idx]
        self.calls += 1
        return CompletionResponse(text=text, tokens=TokenUsage(input=10, output=5))

    async def stream(self, request: Any):  # pragma: no cover - unused here
        raise NotImplementedError

    async def embed(self, text: str, *, model: str):  # pragma: no cover - unused
        raise NotImplementedError


class _FakeRuntime:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.storage = None
        self.tracer = None
        self.executor = None


@pytest.fixture
def patch_runtime_provider(monkeypatch: pytest.MonkeyPatch):
    """Patch build_local_runtime/shutdown_runtime so a scripted provider
    drives the scaffold without any real network or MockProvider."""

    def _patch(provider: Any) -> _FakeRuntime:
        rt = _FakeRuntime(provider)

        async def _build(*_a: object, **_k: object) -> _FakeRuntime:
            return rt

        async def _shutdown(*_a: object, **_k: object) -> None:
            return None

        # init.py imports these names from movate.cli._runtime at call time.
        import movate.cli._runtime as runtime_mod  # noqa: PLC0415

        monkeypatch.setattr(runtime_mod, "build_local_runtime", _build)
        monkeypatch.setattr(runtime_mod, "shutdown_runtime", _shutdown)
        return rt

    return _patch


@pytest.mark.unit
class TestRetryOnTransportError:
    def test_first_attempt_junk_then_valid_succeeds(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_runtime_provider: Any,
    ) -> None:
        """Attempt 1 returns non-JSON prose (LLMScaffoldError); the retry
        returns a valid payload → scaffold SUCCEEDS on attempt 2."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "fake")  # pass the pre-flight key gate
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        valid = json.dumps(_valid_agent_payload("recovered-agent"))
        provider = _ScriptedProvider(["this is not json at all, sorry", valid])
        patch_runtime_provider(provider)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "recovered-agent",
                "--llm",
                "a description",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert provider.calls == 2  # retry actually fired
        assert (tmp_path / "recovered-agent" / "agent.yaml").is_file()
        # Summary line marks the retry.
        assert "retried=true" in result.stdout
        assert "ok=true" in result.stdout

    def test_two_transport_failures_exits_two_with_artifact(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_runtime_provider: Any,
    ) -> None:
        """Both attempts return junk → exit 2 (hard scaffold failure) and
        a debug artifact is written."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "fake")  # pass the pre-flight key gate
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        provider = _ScriptedProvider(["not json #1", "still not json #2"])
        patch_runtime_provider(provider)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "doomed-agent",
                "--llm",
                "a description",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 2
        assert provider.calls == 2
        assert not (tmp_path / "doomed-agent").exists()
        assert (tmp_path / ".mdk" / "llm-init-failed-doomed-agent.json").is_file()
        assert "retried=true" in result.stdout

    def test_validation_failure_then_valid_exits_zero(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_runtime_provider: Any,
    ) -> None:
        """Attempt 1 PARSES but fails load-validation (broken schema);
        the feedback retry returns a valid payload → success."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "fake")  # pass the pre-flight key gate
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        # Attempt 1: valid GeneratedAgent JSON but an invalid JSON Schema
        # (type "datetime" is not a real JSON Schema type) → load_agent fails.
        broken = _valid_agent_payload("schema-fix-agent")
        broken["input_schema"]["properties"]["text"] = {"type": "datetime"}
        good = json.dumps(_valid_agent_payload("schema-fix-agent"))
        provider = _ScriptedProvider([json.dumps(broken), good])
        patch_runtime_provider(provider)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "schema-fix-agent",
                "--llm",
                "a description",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert provider.calls == 2
        assert "retried=true" in result.stdout


# ---------------------------------------------------------------------------
# Part 3 — key-matched generated model
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPickTargetModel:
    def test_anthropic_only_picks_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        model = _pick_target_model(llm_model=_DEFAULT_LLM_MODEL, mock=False)
        assert model.startswith("anthropic/")
        assert model != _DEFAULT_LLM_MODEL

    def test_explicit_llm_model_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        # Operator passed a non-default --llm-model → it wins over the
        # key-mapping, for the generated agent too.
        model = _pick_target_model(llm_model="openai/gpt-4o", mock=False)
        assert model == "openai/gpt-4o"

    def test_mock_mode_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        # --mock: even with a key present, default is fine (offline).
        assert _pick_target_model(llm_model=_DEFAULT_LLM_MODEL, mock=True) == _DEFAULT_LLM_MODEL

    def test_no_key_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _strip_all_provider_keys(monkeypatch)
        assert _pick_target_model(llm_model=_DEFAULT_LLM_MODEL, mock=False) == _DEFAULT_LLM_MODEL

    def test_openai_wins_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        monkeypatch.setenv("OPENAI_API_KEY", "fake")
        # PROVIDER_KEY_ENV_VARS order: OpenAI is checked first.
        assert _pick_target_model(llm_model=_DEFAULT_LLM_MODEL, mock=False).startswith("openai/")


@pytest.mark.unit
class TestGeneratedModelCoercion:
    def test_anthropic_key_yields_anthropic_provider_in_agent_yaml(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_runtime_provider: Any,
    ) -> None:
        """End-to-end: with only ANTHROPIC_API_KEY set, the generated
        agent.yaml's model.provider is coerced to an anthropic model —
        even though the LLM (here the scripted double) emitted openai."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        # The scripted payload declares openai — the CLI must override it.
        payload = _valid_agent_payload("anthro-agent")
        assert payload["agent_yaml"]["model"]["provider"].startswith("openai/")
        provider = _ScriptedProvider([json.dumps(payload)])
        patch_runtime_provider(provider)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "anthro-agent",
                "--llm",
                "a description",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        parsed = yaml.safe_load((tmp_path / "anthro-agent" / "agent.yaml").read_text())
        assert parsed["model"]["provider"].startswith("anthropic/")

    def test_explicit_llm_model_written_into_generated_agent(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_runtime_provider: Any,
    ) -> None:
        """`--llm-model X` is written into the generated agent.yaml."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "fake")
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        payload = _valid_agent_payload("explicit-model-agent")
        provider = _ScriptedProvider([json.dumps(payload)])
        patch_runtime_provider(provider)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "explicit-model-agent",
                "--llm",
                "a description",
                "--llm-model",
                "anthropic/claude-haiku-4-5-20251001",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        parsed = yaml.safe_load((tmp_path / "explicit-model-agent" / "agent.yaml").read_text())
        assert parsed["model"]["provider"] == "anthropic/claude-haiku-4-5-20251001"

    def test_mock_mode_keeps_default_provider(
        self, tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--mock` writes the default (openai) provider — sane offline."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "mock-default-agent",
                "--llm",
                "a description",
                "--mock",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        parsed = yaml.safe_load((tmp_path / "mock-default-agent" / "agent.yaml").read_text())
        assert parsed["model"]["provider"] == _DEFAULT_LLM_MODEL


# ---------------------------------------------------------------------------
# Part 4 — sample_evals validation against generated schemas
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateSampleEvals:
    def test_conforming_rows_pass(self) -> None:
        agent = GeneratedAgent.model_validate(_valid_agent_payload("ok-evals"))
        assert _validate_sample_evals(agent) is None

    def test_empty_evals_is_legal(self) -> None:
        payload = _valid_agent_payload("no-evals")
        payload["sample_evals"] = []
        agent = GeneratedAgent.model_validate(payload)
        assert _validate_sample_evals(agent) is None

    def test_expected_violating_output_schema_is_rejected(self) -> None:
        payload = _valid_agent_payload("bad-expected")
        # output_schema requires {"message": <string>}; this row's expected
        # omits "message" and adds a disallowed key.
        payload["sample_evals"] = [{"input": {"text": "x"}, "expected": {"wrong": "value"}}]
        agent = GeneratedAgent.model_validate(payload)
        err = _validate_sample_evals(agent)
        assert err is not None
        assert "expected" in err
        assert "output_schema" in err

    def test_input_violating_input_schema_is_rejected(self) -> None:
        payload = _valid_agent_payload("bad-input")
        # input_schema requires {"text": <string>}; this row omits it.
        payload["sample_evals"] = [{"input": {"nope": 1}, "expected": {"message": "y"}}]
        agent = GeneratedAgent.model_validate(payload)
        err = _validate_sample_evals(agent)
        assert err is not None
        assert "input" in err
        assert "input_schema" in err

    def test_try_validate_surfaces_eval_error(self, tmp_path: Path) -> None:
        """_try_validate (load + eval cross-check) returns the eval error
        string so the retry loop can re-prompt."""
        payload = _valid_agent_payload("eval-err-agent")
        payload["sample_evals"] = [{"input": {"text": "x"}, "expected": {"message": 123}}]
        agent = GeneratedAgent.model_validate(payload)
        err = _try_validate(agent, name="eval-err-agent")
        assert err is not None
        assert "output_schema" in err

    def test_try_validate_passes_conforming_agent(self) -> None:
        agent = GeneratedAgent.model_validate(_valid_agent_payload("good-agent"))
        assert _try_validate(agent, name="good-agent") is None

    def test_eval_error_drives_retry_loop(
        self,
        tmp_path: Path,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_runtime_provider: Any,
    ) -> None:
        """A non-conforming sample_evals row triggers the retry: attempt 1
        loads fine but its eval row violates output_schema; the feedback
        retry returns a conforming payload → success with retried=true."""
        _strip_all_provider_keys(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "fake")  # pass the pre-flight key gate
        monkeypatch.delenv("MOVATE_MOCK_RESPONSE", raising=False)
        bad = _valid_agent_payload("eval-retry-agent")
        bad["sample_evals"] = [{"input": {"text": "x"}, "expected": {"message": 123}}]
        good = json.dumps(_valid_agent_payload("eval-retry-agent"))
        provider = _ScriptedProvider([json.dumps(bad), good])
        patch_runtime_provider(provider)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "eval-retry-agent",
                "--llm",
                "a description",
                "--target",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert provider.calls == 2
        assert "retried=true" in result.stdout


# Reference the init module so an accidental import break surfaces here.
assert hasattr(init_mod, "_run_llm_scaffold")
