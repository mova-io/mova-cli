"""Tests for enterprise connectors (ADR 052 Phase 1 — Action Fabric).

Two connectors, three skill variants each, tested against
``httpx.MockTransport`` through the existing ``HttpSkillBackend``.

Coverage map:
* ServiceNow incident-create  — POST, correct URL, JSON body, auth
* ServiceNow incident-get     — GET, correct URL with sys_id, auth
* ServiceNow incident-update  — PATCH, correct URL with sys_id, body
* MS Graph user-create        — POST, correct URL, JSON body, auth
* MS Graph user-resetpassword — POST, correct URL with user_id
* MS Graph license-assign     — POST, correct URL with user_id
* Connector registry — BUILTIN_CONNECTORS shape and contents
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from movate.connectors import BUILTIN_CONNECTORS
from movate.core.skill_backend import SkillExecutionContext
from movate.core.skill_backend.http import HttpSkillBackend
from movate.core.skill_loader import load_skill

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_http_skill(
    parent: Path,
    *,
    name: str,
    entry: str,
    method: str = "POST",
    auth: str | None = None,
    headers: dict[str, str] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> Path:
    """Write an http-kind skill.yaml under <parent>/<name>/."""
    import yaml  # noqa: PLC0415

    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    spec: dict[str, Any] = {
        "api_version": "movate/v1",
        "kind": "Skill",
        "name": name,
        "version": "1.0.0",
        "schema": {
            "input": input_schema or {"query": "string"},
            "output": output_schema or {"result": "string"},
        },
        "implementation": {
            "kind": "http",
            "entry": entry,
            "method": method,
        },
    }
    if auth:
        spec["implementation"]["auth"] = auth
    if headers:
        spec["implementation"]["headers"] = headers

    (skill_dir / "skill.yaml").write_text(yaml.dump(spec, default_flow_style=False))
    return skill_dir


def _ctx() -> SkillExecutionContext:
    return SkillExecutionContext(call_ms_budget=30_000)


def _mock_transport(
    *,
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    capture: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """Return a mock transport that captures the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["method"] = request.method
            capture["url"] = str(request.url)
            capture["headers"] = dict(request.headers)
            if request.content:
                try:
                    capture["body"] = json.loads(request.content)
                except (ValueError, UnicodeDecodeError):
                    capture["body"] = request.content.decode("utf-8", errors="replace")
        body = json_body or {}
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# ServiceNow connector tests
# ---------------------------------------------------------------------------

# ServiceNow entry URLs use the pattern:
#   https://{{ input.instance_host }}/api/now/table/incident[/{{ sys_id }}]
# where instance_host is the bare hostname (e.g. myco.service-now.com).


class TestServiceNowIncidentCreate:
    """servicenow-incident-create -- POST."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERVICENOW_API_KEY", "test-snow-123")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "result": {
                    "sys_id": "abc123",
                    "number": "INC0012345",
                    "short_description": "Server down",
                    "state": "1",
                    "urgency": "1",
                    "priority": "1",
                }
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = "https://{{ input.instance_host }}/api/now/table/incident"
        skill_dir = _write_http_skill(
            tmp_path,
            name="servicenow-incident-create",
            entry=entry,
            method="POST",
            auth="bearer-from-env:SERVICENOW_API_KEY",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            input_schema={
                "instance_host": "string",
                "short_description": "string",
            },
            output_schema={"result": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "myco.service-now.com",
                "short_description": "Server down",
                "urgency": 1,
                "priority": 1,
            },
            _ctx(),
        )

        assert capture["method"] == "POST"
        assert capture["url"] == "https://myco.service-now.com/api/now/table/incident"
        assert capture["headers"]["authorization"] == "Bearer test-snow-123"
        assert capture["body"]["short_description"] == "Server down"
        assert result["result"]["sys_id"] == "abc123"
        assert result["result"]["number"] == "INC0012345"

        await backend.aclose()


class TestServiceNowIncidentGet:
    """servicenow-incident-get -- GET."""

    @pytest.mark.asyncio
    async def test_get_url_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERVICENOW_API_KEY", "test-snow-456")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "result": {
                    "sys_id": "def456",
                    "number": "INC0012345",
                    "short_description": "Network issue",
                    "state": "2",
                }
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = "https://{{ input.instance_host }}/api/now/table/incident/{{ input.sys_id }}"
        skill_dir = _write_http_skill(
            tmp_path,
            name="servicenow-incident-get",
            entry=entry,
            method="GET",
            auth="bearer-from-env:SERVICENOW_API_KEY",
            headers={"Accept": "application/json"},
            input_schema={
                "instance_host": "string",
                "sys_id": "string",
            },
            output_schema={"result": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "myco.service-now.com",
                "sys_id": "def456",
            },
            _ctx(),
        )

        assert capture["method"] == "GET"
        # GET sends input as query params alongside the templated URL.
        assert capture["url"].startswith(
            "https://myco.service-now.com/api/now/table/incident/def456"
        )
        assert capture["headers"]["authorization"] == "Bearer test-snow-456"
        assert result["result"]["number"] == "INC0012345"

        await backend.aclose()


class TestServiceNowIncidentUpdate:
    """servicenow-incident-update -- PATCH."""

    @pytest.mark.asyncio
    async def test_patch_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERVICENOW_API_KEY", "test-snow-789")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "result": {
                    "sys_id": "ghi789",
                    "number": "INC0012345",
                    "short_description": "Server down - resolved",
                    "state": "6",
                }
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = "https://{{ input.instance_host }}/api/now/table/incident/{{ input.sys_id }}"
        skill_dir = _write_http_skill(
            tmp_path,
            name="servicenow-incident-update",
            entry=entry,
            method="PATCH",
            auth="bearer-from-env:SERVICENOW_API_KEY",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            input_schema={
                "instance_host": "string",
                "sys_id": "string",
                "state": "number",
            },
            output_schema={"result": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "myco.service-now.com",
                "sys_id": "ghi789",
                "state": 6,
                "close_notes": "Restarted the service",
            },
            _ctx(),
        )

        assert capture["method"] == "PATCH"
        expected = "https://myco.service-now.com/api/now/table/incident/ghi789"
        assert capture["url"] == expected
        assert capture["headers"]["authorization"] == "Bearer test-snow-789"
        assert capture["body"]["state"] == 6
        assert capture["body"]["close_notes"] == "Restarted the service"
        assert result["result"]["state"] == "6"

        await backend.aclose()


# ---------------------------------------------------------------------------
# Microsoft Graph connector tests
# ---------------------------------------------------------------------------


class TestMSGraphUserCreate:
    """msgraph-user-create -- POST /v1.0/users."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "test-graph-token-aaa")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "id": "user-id-001",
                "displayName": "Jane Doe",
                "userPrincipalName": "janedoe@contoso.com",
                "mail": "janedoe@contoso.com",
                "accountEnabled": True,
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        skill_dir = _write_http_skill(
            tmp_path,
            name="msgraph-user-create",
            entry="https://graph.microsoft.com/v1.0/users",
            method="POST",
            auth="bearer-from-env:MSGRAPH_ACCESS_TOKEN",
            headers={"Content-Type": "application/json"},
            input_schema={
                "displayName": "string",
                "mailNickname": "string",
                "userPrincipalName": "string",
            },
            output_schema={
                "id": "string",
                "displayName": "string",
            },
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "displayName": "Jane Doe",
                "mailNickname": "janedoe",
                "userPrincipalName": "janedoe@contoso.com",
                "accountEnabled": True,
                "passwordProfile": {
                    "forceChangePasswordNextSignIn": True,
                    "password": "SecurePass123!",
                },
            },
            _ctx(),
        )

        assert capture["method"] == "POST"
        assert capture["url"] == "https://graph.microsoft.com/v1.0/users"
        assert capture["headers"]["authorization"] == "Bearer test-graph-token-aaa"
        assert capture["body"]["displayName"] == "Jane Doe"
        assert capture["body"]["passwordProfile"]["password"] == "SecurePass123!"
        assert result["id"] == "user-id-001"

        await backend.aclose()


class TestMSGraphUserResetPassword:
    """msgraph-user-resetpassword -- POST .../resetPassword."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "test-graph-token-bbb")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "id": "method-id-001",
                "newPassword": "NewSecure456!",
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = (
            "https://graph.microsoft.com/v1.0"
            "/users/{{ input.user_id }}"
            "/authentication"
            "/methods/{{ input.method_id }}"
            "/resetPassword"
        )
        skill_dir = _write_http_skill(
            tmp_path,
            name="msgraph-user-resetpassword",
            entry=entry,
            method="POST",
            auth="bearer-from-env:MSGRAPH_ACCESS_TOKEN",
            headers={"Content-Type": "application/json"},
            input_schema={
                "user_id": "string",
                "method_id": "string",
            },
            output_schema={"id": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "user_id": "user-abc-123",
                "method_id": "pwd-method-456",
                "newPassword": "NewSecure456!",
            },
            _ctx(),
        )

        expected_url = (
            "https://graph.microsoft.com/v1.0"
            "/users/user-abc-123"
            "/authentication"
            "/methods/pwd-method-456"
            "/resetPassword"
        )
        assert capture["method"] == "POST"
        assert capture["url"] == expected_url
        assert capture["headers"]["authorization"] == "Bearer test-graph-token-bbb"
        assert capture["body"]["newPassword"] == "NewSecure456!"
        assert result["id"] == "method-id-001"

        await backend.aclose()


class TestMSGraphLicenseAssign:
    """msgraph-license-assign -- POST .../assignLicense."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "test-graph-token-ccc")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "id": "user-xyz-789",
                "displayName": "Jane Doe",
                "assignedLicenses": [
                    {
                        "skuId": "sku-e5-guid",
                        "disabledPlans": [],
                    },
                ],
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = "https://graph.microsoft.com/v1.0/users/{{ input.user_id }}/assignLicense"
        skill_dir = _write_http_skill(
            tmp_path,
            name="msgraph-license-assign",
            entry=entry,
            method="POST",
            auth="bearer-from-env:MSGRAPH_ACCESS_TOKEN",
            headers={"Content-Type": "application/json"},
            input_schema={"user_id": "string"},
            output_schema={"id": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "user_id": "user-xyz-789",
                "addLicenses": [
                    {"skuId": "sku-e5-guid", "disabledPlans": []},
                ],
                "removeLicenses": [],
            },
            _ctx(),
        )

        expected = "https://graph.microsoft.com/v1.0/users/user-xyz-789/assignLicense"
        assert capture["method"] == "POST"
        assert capture["url"] == expected
        assert capture["headers"]["authorization"] == "Bearer test-graph-token-ccc"
        assert capture["body"]["addLicenses"][0]["skuId"] == "sku-e5-guid"
        assert capture["body"]["removeLicenses"] == []
        assert result["assignedLicenses"][0]["skuId"] == "sku-e5-guid"

        await backend.aclose()


# ---------------------------------------------------------------------------
# Connector registry tests
# ---------------------------------------------------------------------------


class TestConnectorRegistry:
    """BUILTIN_CONNECTORS dict shape and contents."""

    def test_registry_has_both_connectors(self) -> None:
        assert "servicenow" in BUILTIN_CONNECTORS
        assert "msgraph" in BUILTIN_CONNECTORS

    def test_servicenow_metadata(self) -> None:
        sn = BUILTIN_CONNECTORS["servicenow"]
        assert sn["module"] == "movate.connectors.servicenow"
        assert len(sn["skills"]) == 3
        assert "servicenow-incident-create" in sn["skills"]
        assert "servicenow-incident-get" in sn["skills"]
        assert "servicenow-incident-update" in sn["skills"]
        assert "SERVICENOW_API_KEY" in sn["env_vars"]
        assert "SERVICENOW_INSTANCE_URL" in sn["env_vars"]

    def test_msgraph_metadata(self) -> None:
        mg = BUILTIN_CONNECTORS["msgraph"]
        assert mg["module"] == "movate.connectors.msgraph"
        assert len(mg["skills"]) == 3
        assert "msgraph-user-create" in mg["skills"]
        assert "msgraph-user-resetpassword" in mg["skills"]
        assert "msgraph-license-assign" in mg["skills"]
        assert "MSGRAPH_ACCESS_TOKEN" in mg["env_vars"]
        assert "MSGRAPH_TENANT_ID" in mg["env_vars"]

    def test_all_connectors_have_required_keys(self) -> None:
        for name, meta in BUILTIN_CONNECTORS.items():
            assert "module" in meta, f"{name} missing 'module'"
            assert "skills" in meta, f"{name} missing 'skills'"
            assert "env_vars" in meta, f"{name} missing 'env_vars'"
            assert "description" in meta, f"{name} missing 'description'"
            assert len(meta["skills"]) > 0, f"{name} has no skills"
            assert len(meta["env_vars"]) > 0, f"{name} has no env_vars"
