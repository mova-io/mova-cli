"""Pydantic model validation: agent.yaml contract, request/response shape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movate.core.models import (
    AgentSpec,
    ErrorInfo,
    Metrics,
    ModelConfig,
    ModelFallback,
    RunRequest,
    RunResponse,
)

# ---------------------------------------------------------------------------
# AgentSpec
# ---------------------------------------------------------------------------


def _minimal_agent_dict(**overrides: object) -> dict:
    base: dict = {
        "api_version": "movate/v1",
        "kind": "Agent",
        "name": "demo",
        "version": "0.1.0",
        "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
        "prompt": "./prompt.md",
        "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_agent_spec_minimal_parse() -> None:
    spec = AgentSpec.model_validate(_minimal_agent_dict())
    assert spec.api_version == "movate/v1"
    assert spec.kind == "Agent"
    assert spec.name == "demo"
    assert spec.model.provider == "openai/gpt-4o-mini-2024-07-18"


@pytest.mark.unit
def test_agent_spec_rejects_wrong_api_version() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_minimal_agent_dict(api_version="movate/v2"))


@pytest.mark.unit
def test_agent_spec_rejects_wrong_kind() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_minimal_agent_dict(kind="Workflow"))


@pytest.mark.unit
def test_agent_spec_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_minimal_agent_dict(unknown_field=True))


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["1.0", "1", "1.2.3.4", "v1.0.0", "abc"])
def test_agent_spec_rejects_non_semver(bad: str) -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_minimal_agent_dict(version=bad))


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["Demo", "demo_agent", "demo.agent", "-demo", "demo-"])
def test_agent_spec_rejects_invalid_name(bad: str) -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_minimal_agent_dict(name=bad))


# ---------------------------------------------------------------------------
# ModelConfig — provider format + floating-tag rejection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider",
    [
        "openai/gpt-4o-mini-2024-07-18",
        "anthropic/claude-sonnet-4-6",
        "azure/gpt-4.1",
    ],
)
def test_model_config_accepts_pinned_providers(provider: str) -> None:
    cfg = ModelConfig.model_validate({"provider": provider})
    assert cfg.provider == provider


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider",
    [
        "openai/latest",
        "anthropic/stable",
        "openai/gpt-4o-latest",
    ],
)
def test_model_config_rejects_floating_tags(provider: str) -> None:
    with pytest.raises(ValidationError, match="floating model tag"):
        ModelConfig.model_validate({"provider": provider})


@pytest.mark.unit
def test_model_config_rejects_missing_provider_prefix() -> None:
    with pytest.raises(ValidationError, match="LiteLLM model string"):
        ModelConfig.model_validate({"provider": "gpt-4o-mini"})


@pytest.mark.unit
def test_model_config_with_fallback() -> None:
    cfg = ModelConfig.model_validate(
        {
            "provider": "openai/gpt-4o-mini-2024-07-18",
            "fallback": [{"provider": "anthropic/claude-haiku-4-5-20251001"}],
        }
    )
    assert len(cfg.fallback) == 1
    assert isinstance(cfg.fallback[0], ModelFallback)


# ---------------------------------------------------------------------------
# RunRequest / RunResponse
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_request_generates_request_id() -> None:
    req = RunRequest(agent="demo", input={"text": "hi"})
    assert req.request_id  # uuid populated
    assert len(req.request_id) >= 32


@pytest.mark.unit
def test_run_response_success_default_metrics() -> None:
    resp = RunResponse(status="success", data={"message": "ok"})
    assert resp.status == "success"
    assert resp.metrics.cost_usd == 0.0
    assert resp.error is None


@pytest.mark.unit
def test_run_response_error_attaches_info() -> None:
    info = ErrorInfo(type="schema_error", message="boom", retryable=False)
    resp = RunResponse(status="error", error=info, metrics=Metrics(latency_ms=42))
    assert resp.error is not None
    assert resp.error.type == "schema_error"
    assert resp.metrics.latency_ms == 42
