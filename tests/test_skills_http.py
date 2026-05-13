"""Tests for the HTTP skill backend (Skills PR 2 — ADR 002 PR 2 of N).

Two layers:

* Pure-function helpers — URL templating, auth header construction.
  No httpx, no I/O.
* `HttpSkillBackend.execute` end-to-end against `httpx.MockTransport`.
  Hermetic, deterministic, fast.

The SkillImplementation model-validator changes (rejecting empty
``entry``, non-http URLs, bad ``auth`` shapes) are covered alongside
the existing skill-spec tests in test_skills.py — same module, same
fixture style. Tests here focus on the *backend* path.

Coverage map (one test per branch):

* Happy POST with JSON body
* Happy GET with query params
* URL Jinja templating against input
* Static headers passthrough
* Bearer auth from env
* Missing auth env var → backend_error
* Auth spec with empty var name
* Non-2xx response → backend_error with status + body excerpt
* Non-JSON response → backend_error
* JSON list response (not a dict) → validation_failed
* httpx timeout → SkillError(TIMEOUT)
* httpx transport error → SkillError(BACKEND_ERROR)
* URL with missing input field → backend_error (StrictUndefined)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from movate.core.skill_backend import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    dispatch_skill,
)
from movate.core.skill_backend.http import (
    HttpSkillBackend,
    _build_auth_header,
    _render_url,
)
from movate.core.skill_loader import load_skill

# ---------------------------------------------------------------------------
# Helpers — synth a SkillBundle pointing at an http endpoint
# ---------------------------------------------------------------------------


def _write_http_skill(
    parent: Path,
    *,
    name: str = "weather",
    entry: str = "https://api.example.test/weather",
    method: str = "POST",
    auth: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
    input_schema: str = "{city: string}",
    output_schema: str = "{temperature: number}",
) -> Path:
    """Drop an http-kind skill.yaml under <parent>/<name>/."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    yaml_lines = [
        "api_version: movate/v1",
        "kind: Skill",
        f"name: {name}",
        "version: 0.1.0",
        "schema:",
        f"  input: {input_schema}",
        f"  output: {output_schema}",
        "implementation:",
        "  kind: http",
        f"  entry: {entry}",
        f"  method: {method}",
    ]
    if auth is not None:
        yaml_lines.append(f"  auth: {auth}")
    if headers:
        yaml_lines.append("  headers:")
        for k, v in headers.items():
            yaml_lines.append(f"    {k}: {v}")
    if timeout_seconds is not None:
        yaml_lines.append(f"  timeout_seconds: {timeout_seconds}")
    (skill_dir / "skill.yaml").write_text("\n".join(yaml_lines) + "\n")
    return skill_dir


def _ctx() -> SkillExecutionContext:
    return SkillExecutionContext(call_ms_budget=30_000)


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


def test_render_url_no_placeholders_is_fastpath() -> None:
    """Literal URLs short-circuit without invoking Jinja. Important
    for the common case where the URL is static."""
    assert _render_url("https://api.example.test/lookup", {"x": 1}) == (
        "https://api.example.test/lookup"
    )


def test_render_url_substitutes_input_fields() -> None:
    rendered = _render_url(
        "https://api.example.test/cases/{{ input.case_id }}/status",
        {"case_id": "abc-123"},
    )
    assert rendered == "https://api.example.test/cases/abc-123/status"


def test_render_url_missing_input_raises() -> None:
    """StrictUndefined turns ``input.missing`` into a clean exception —
    operators reading a URL with ``/cases//status`` would otherwise
    debug for an hour."""
    with pytest.raises(Exception):
        _render_url(
            "https://api.example.test/cases/{{ input.case_id }}",
            {"unrelated": "x"},
        )


def test_build_auth_header_resolves_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_TOKEN", "abc-secret")
    header = _build_auth_header("bearer-from-env:CRM_TOKEN", "skill-x")
    assert header == "Bearer abc-secret"


def test_build_auth_header_missing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRM_TOKEN", raising=False)
    with pytest.raises(SkillError) as info:
        _build_auth_header("bearer-from-env:CRM_TOKEN", "skill-x")
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "CRM_TOKEN" in info.value.message


def test_build_auth_header_empty_var_name() -> None:
    """``bearer-from-env:`` without a var name should fail loudly."""
    with pytest.raises(SkillError) as info:
        _build_auth_header("bearer-from-env:", "skill-x")
    assert info.value.type == SkillErrorType.BACKEND_ERROR


# ---------------------------------------------------------------------------
# HttpSkillBackend end-to-end via httpx.MockTransport
# ---------------------------------------------------------------------------


def _mock_transport(handler) -> httpx.MockTransport:
    """Wrap a single request-handler callable for httpx."""
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_post_happy_path(tmp_path: Path) -> None:
    """POST body = full input dict; response JSON object passes through
    as the skill's output (then validated by dispatch_skill)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"temperature": 72.5})

    skill_dir = _write_http_skill(tmp_path, entry="https://api.example.test/weather")
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        out = await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()

    assert out == {"temperature": 72.5}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.example.test/weather"
    assert captured["body"] == {"city": "SF"}


@pytest.mark.asyncio
async def test_get_sends_query_params(tmp_path: Path) -> None:
    """GET/DELETE skills send input as query params instead of a body —
    matches how most lookup APIs work."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json={"temperature": 60.0})

    skill_dir = _write_http_skill(
        tmp_path,
        entry="https://api.example.test/weather",
        method="GET",
    )
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        out = await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()

    assert out == {"temperature": 60.0}
    assert captured["query"] == {"city": "SF"}


@pytest.mark.asyncio
async def test_url_template_renders_against_input(tmp_path: Path) -> None:
    """URL with `{{ input.* }}` Jinja placeholders gets substituted."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "ok"})

    skill_dir = _write_http_skill(
        tmp_path,
        name="case-lookup",
        entry="https://api.example.test/cases/{{ input.case_id }}/status",
        input_schema="{case_id: string}",
        output_schema="{status: string}",
    )
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        await backend.execute(bundle, {"case_id": "abc-123"}, _ctx())
    finally:
        await backend.aclose()
    assert captured["url"] == "https://api.example.test/cases/abc-123/status"


@pytest.mark.asyncio
async def test_static_headers_passthrough(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"temperature": 72.5})

    skill_dir = _write_http_skill(
        tmp_path,
        headers={"X-API-Version": "2026-01", "X-Tenant": "movate-dev"},
    )
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()

    assert captured["headers"]["x-api-version"] == "2026-01"
    assert captured["headers"]["x-tenant"] == "movate-dev"


@pytest.mark.asyncio
async def test_bearer_auth_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_TOKEN", "abc-secret")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"temperature": 72.5})

    skill_dir = _write_http_skill(tmp_path, auth="bearer-from-env:CRM_TOKEN")
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()

    assert captured["auth"] == "Bearer abc-secret"


@pytest.mark.asyncio
async def test_missing_auth_env_var_errors_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skill declares ``auth: bearer-from-env:NEVERSET`` but the env var
    isn't set. We expect a clean BACKEND_ERROR — the operator can fix
    by setting the var or removing the auth field."""
    monkeypatch.delenv("NEVERSET", raising=False)
    skill_dir = _write_http_skill(tmp_path, auth="bearer-from-env:NEVERSET")
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "NEVERSET" in info.value.message


@pytest.mark.asyncio
async def test_non_2xx_response_is_backend_error(tmp_path: Path) -> None:
    """Status 4xx/5xx surfaces as backend_error so the LLM can recover
    (different strategy, or give up gracefully)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    skill_dir = _write_http_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "503" in info.value.message
    assert "Service Unavailable" in info.value.message


@pytest.mark.asyncio
async def test_non_json_response_is_backend_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all <html>")

    skill_dir = _write_http_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "JSON" in info.value.message


@pytest.mark.asyncio
async def test_non_dict_response_is_validation_failed(tmp_path: Path) -> None:
    """Response was valid JSON but a list — the skill output schema
    requires an object. Surfaces as validation_failed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    skill_dir = _write_http_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_timeout_surfaces_as_skill_timeout(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated network hang")

    skill_dir = _write_http_skill(tmp_path, timeout_seconds=1)
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.TIMEOUT


@pytest.mark.asyncio
async def test_transport_error_is_backend_error(tmp_path: Path) -> None:
    """Generic httpx.HTTPError (e.g. connection reset, DNS fail) maps
    to backend_error so the LLM gets one consistent vocabulary."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection reset")

    skill_dir = _write_http_skill(tmp_path)
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend(transport=_mock_transport(handler))
    try:
        with pytest.raises(SkillError) as info:
            await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR


@pytest.mark.asyncio
async def test_url_template_missing_input_field_errors(tmp_path: Path) -> None:
    """URL references ``{{ input.x }}`` but ``x`` not in input. Operator
    sees a clear backend_error rather than a hung request."""
    skill_dir = _write_http_skill(
        tmp_path,
        entry="https://api.example.test/cases/{{ input.missing }}",
    )
    bundle = load_skill(skill_dir)
    backend = HttpSkillBackend()
    try:
        with pytest.raises(SkillError) as info:
            # input.missing isn't in the input dict — Jinja's
            # StrictUndefined raises, we wrap as backend_error.
            await backend.execute(bundle, {"city": "SF"}, _ctx())
    finally:
        await backend.aclose()
    assert info.value.type == SkillErrorType.BACKEND_ERROR
    assert "URL" in info.value.message


# ---------------------------------------------------------------------------
# dispatch_skill end-to-end (with the HTTP backend registered)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_skill_routes_to_http_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check: an http-kind skill goes through dispatch_skill
    (input validation → backend dispatch → output validation). The
    output schema validator runs on the response body."""
    # Importing http for its side-effect registers the backend with
    # the dispatch table. Production code does this in the executor;
    # tests touch it explicitly.
    from movate.core.skill_backend import http as _http_backend  # noqa: F401, PLC0415
    from movate.core.skill_backend.base import _BACKENDS  # noqa: PLC0415

    # Swap in a backend wired to MockTransport so the test doesn't
    # touch the network.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"temperature": 72.5})

    monkeypatch.setitem(
        _BACKENDS,
        next(b.kind for b in _BACKENDS.values() if b.kind.value == "http"),
        HttpSkillBackend(transport=_mock_transport(handler)),
    )

    skill_dir = _write_http_skill(tmp_path)
    bundle = load_skill(skill_dir)
    out = await dispatch_skill(bundle, {"city": "SF"}, _ctx())
    assert out == {"temperature": 72.5}
