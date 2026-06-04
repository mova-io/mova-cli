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
Three connectors (Workday, Salesforce, SAP), eight skill variants total,
tested against ``httpx.MockTransport`` through the existing
``HttpSkillBackend``.
* Workday worker-get         — GET, correct URL with tenant+worker_id, auth
* Workday worker-create      — POST, correct URL with tenant, body, auth
* Workday timeoff-balance    — GET, correct URL with tenant+worker_id, auth
* Salesforce account-get     — GET, correct URL with account_id, auth
* Salesforce case-create     — POST, correct URL, JSON body, auth
* Salesforce contact-search  — GET, correct URL with SOQL query, auth
* SAP employee-get           — GET, correct OData URL with partner_id, auth
* SAP purchaseorder-create   — POST, correct OData URL, JSON body, auth
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
            input_schema={
                "instance_host": "string",
                "short_description": "string",
            output_schema={"result": "string"},
        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "myco.service-now.com",
                "urgency": 1,
                "priority": 1,
            _ctx(),
        assert capture["method"] == "POST"
        assert capture["url"] == "https://myco.service-now.com/api/now/table/incident"
        assert capture["headers"]["authorization"] == "Bearer test-snow-123"
        assert capture["body"]["short_description"] == "Server down"
        assert result["result"]["sys_id"] == "abc123"
        assert result["result"]["number"] == "INC0012345"
        await backend.aclose()
class TestServiceNowIncidentGet:
    """servicenow-incident-get -- GET."""
    async def test_get_url_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERVICENOW_API_KEY", "test-snow-456")
# Workday connector tests
class TestWorkdayWorkerGet:
    """workday-worker-get -- GET /ccx/api/v1/{tenant}/workers/{worker_id}."""
        monkeypatch.setenv("WORKDAY_ACCESS_TOKEN", "test-wd-token-111")

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
            output_schema={"result": "string"},
        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "myco.service-now.com",
            _ctx(),
        assert capture["method"] == "GET"
        # GET sends input as query params alongside the templated URL.
        assert capture["url"].startswith(
            "https://myco.service-now.com/api/now/table/incident/def456"
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
                    "sys_id": "ghi789",
                    "short_description": "Server down - resolved",
                    "state": "6",
            name="servicenow-incident-update",
            method="PATCH",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "state": "number",
                "state": 6,
                "close_notes": "Restarted the service",
        assert capture["method"] == "PATCH"
        expected = "https://myco.service-now.com/api/now/table/incident/ghi789"
        assert capture["url"] == expected
        assert capture["headers"]["authorization"] == "Bearer test-snow-789"
        assert capture["body"]["state"] == 6
        assert capture["body"]["close_notes"] == "Restarted the service"
        assert result["result"]["state"] == "6"
# ---------------------------------------------------------------------------
# Microsoft Graph connector tests
class TestMSGraphUserCreate:
    """msgraph-user-create -- POST /v1.0/users."""
    async def test_post_url_body_auth(
        monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "test-graph-token-aaa")
                "id": "user-id-001",
                "displayName": "Jane Doe",
                "userPrincipalName": "janedoe@contoso.com",
                "mail": "janedoe@contoso.com",
                "accountEnabled": True,
            name="msgraph-user-create",
            entry="https://graph.microsoft.com/v1.0/users",
            method="POST",
            auth="bearer-from-env:MSGRAPH_ACCESS_TOKEN",
            headers={"Content-Type": "application/json"},
                "displayName": "string",
                "mailNickname": "string",
                "userPrincipalName": "string",
            output_schema={
                "id": "string",
                "mailNickname": "janedoe",
                "passwordProfile": {
                    "forceChangePasswordNextSignIn": True,
                    "password": "SecurePass123!",
        assert capture["method"] == "POST"
        assert capture["url"] == "https://graph.microsoft.com/v1.0/users"
        assert capture["headers"]["authorization"] == "Bearer test-graph-token-aaa"
        assert capture["body"]["displayName"] == "Jane Doe"
        assert capture["body"]["passwordProfile"]["password"] == "SecurePass123!"
        assert result["id"] == "user-id-001"
class TestMSGraphUserResetPassword:
    """msgraph-user-resetpassword -- POST .../resetPassword."""
        monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "test-graph-token-bbb")
                "id": "method-id-001",
                "newPassword": "NewSecure456!",
                "workerID": "W12345",
                "legalName": {"firstName": "Alice", "lastName": "Johnson"},
                "hireDate": "2023-06-15",
                "jobTitle": "Software Engineer",
                "department": "Engineering",
                "employeeStatus": "Active",
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
            "https://{{ input.instance_host }}"
            "/ccx/api/v1/{{ input.tenant }}/workers/{{ input.worker_id }}"
            name="workday-worker-get",
            method="GET",
            auth="bearer-from-env:WORKDAY_ACCESS_TOKEN",
            headers={"Accept": "application/json"},
                "instance_host": "string",
                "tenant": "string",
                "worker_id": "string",
            output_schema={"workerID": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "user_id": "user-abc-123",
                "method_id": "pwd-method-456",
                "newPassword": "NewSecure456!",
                "instance_host": "wd3-impl-services1.workday.com",
                "tenant": "mycompany",
                "worker_id": "W12345",
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
        assert capture["method"] == "GET"
        assert capture["url"].startswith(
            "https://wd3-impl-services1.workday.com/ccx/api/v1/mycompany/workers/W12345"
        assert capture["headers"]["authorization"] == "Bearer test-wd-token-111"
        assert result["workerID"] == "W12345"
        assert result["legalName"]["firstName"] == "Alice"

        await backend.aclose()


class TestMSGraphLicenseAssign:
    """msgraph-license-assign -- POST .../assignLicense."""
class TestWorkdayWorkerCreate:
    """workday-worker-create -- POST /ccx/api/v1/{tenant}/workers."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "test-graph-token-ccc")
        monkeypatch.setenv("WORKDAY_ACCESS_TOKEN", "test-wd-token-222")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "id": "user-xyz-789",
                "displayName": "Jane Doe",
                "assignedLicenses": [
                    {
                        "skuId": "sku-e5-guid",
                        "disabledPlans": [],
                "workerID": "W99999",
                "legalName": {"firstName": "Bob", "lastName": "Smith"},
                "hireDate": "2024-03-01",
                "jobTitle": "Product Manager",
                "department": "Product",
                "employeeStatus": "Active",
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = "https://{{ input.instance_host }}/ccx/api/v1/{{ input.tenant }}/workers"
        skill_dir = _write_http_skill(
            tmp_path,
            name="workday-worker-create",
            entry=entry,
            method="POST",
            auth="bearer-from-env:WORKDAY_ACCESS_TOKEN",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            input_schema={
                "instance_host": "string",
                "tenant": "string",
                "firstName": "string",
                "lastName": "string",
                "hireDate": "string",
            output_schema={"workerID": "string"},
        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
                "instance_host": "wd3-impl-services1.workday.com",
                "tenant": "mycompany",
                "firstName": "Bob",
                "lastName": "Smith",
            _ctx(),
        assert capture["method"] == "POST"
        assert (
            capture["url"] == "https://wd3-impl-services1.workday.com/ccx/api/v1/mycompany/workers"
        assert capture["headers"]["authorization"] == "Bearer test-wd-token-222"
        assert capture["body"]["firstName"] == "Bob"
        assert capture["body"]["lastName"] == "Smith"
        assert result["workerID"] == "W99999"
        await backend.aclose()
class TestWorkdayTimeoffBalance:
    """workday-timeoff-balance -- GET .../workers/{id}/timeOffBalance."""
    @pytest.mark.asyncio
    async def test_get_url_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKDAY_ACCESS_TOKEN", "test-wd-token-333")
        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "workerID": "W12345",
                "balances": [
                        "timeOffType": "Vacation",
                        "balanceAmount": 15.0,
                        "usedAmount": 5.0,
                        "totalEntitlement": 20.0,
                        "unit": "Days",
                        "asOfDate": "2024-06-01",
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
        entry = (
            "https://{{ input.instance_host }}"
            "/ccx/api/v1/{{ input.tenant }}"
            "/workers/{{ input.worker_id }}/timeOffBalance"
        )
            name="workday-timeoff-balance",
            method="GET",
            auth="bearer-from-env:WORKDAY_ACCESS_TOKEN",
            headers={"Accept": "application/json"},
            input_schema={
                "instance_host": "string",
                "tenant": "string",
                "worker_id": "string",
            },
            output_schema={"workerID": "string"},

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "wd3-impl-services1.workday.com",
                "tenant": "mycompany",
                "worker_id": "W12345",
            _ctx(),
        expected_prefix = (
            "https://wd3-impl-services1.workday.com"
            "/ccx/api/v1/mycompany/workers/W12345/timeOffBalance"
        assert capture["method"] == "GET"
        assert capture["url"].startswith(expected_prefix)
        assert capture["headers"]["authorization"] == "Bearer test-wd-token-333"
        assert result["workerID"] == "W12345"
        assert result["balances"][0]["timeOffType"] == "Vacation"
        assert result["balances"][0]["balanceAmount"] == 15.0
        await backend.aclose()
# ---------------------------------------------------------------------------
# Salesforce connector tests
class TestSalesforceAccountGet:
    """salesforce-account-get -- GET /services/data/v59.0/sobjects/Account/{id}."""
    @pytest.mark.asyncio
    async def test_get_url_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "test-sf-token-aaa")
        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "Id": "001xx000003DGbYAAW",
                "Name": "Acme Corp",
                "Industry": "Technology",
                "Type": "Customer",
                "BillingCity": "San Francisco",
                "Phone": "+1-415-555-0100",
                "AnnualRevenue": 50000000,
                "NumberOfEmployees": 500,
            capture=capture,
        backend = HttpSkillBackend(transport=transport)
            "/services/data/v59.0/sobjects/Account/{{ input.account_id }}"
            name="salesforce-account-get",
            auth="bearer-from-env:SALESFORCE_ACCESS_TOKEN",
                "account_id": "string",
            output_schema={"Id": "string"},
                "instance_host": "mycompany.my.salesforce.com",
                "account_id": "001xx000003DGbYAAW",
        assert capture["url"].startswith(
            "https://mycompany.my.salesforce.com"
            "/services/data/v59.0/sobjects/Account/001xx000003DGbYAAW"
        assert capture["headers"]["authorization"] == "Bearer test-sf-token-aaa"
        assert result["Id"] == "001xx000003DGbYAAW"
        assert result["Name"] == "Acme Corp"
class TestSalesforceCaseCreate:
    """salesforce-case-create -- POST /services/data/v59.0/sobjects/Case."""
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "test-sf-token-bbb")
                "id": "500xx000000bZKLAA2",
                "success": True,
                "errors": [],
        entry = "https://{{ input.instance_host }}/services/data/v59.0/sobjects/Case"
            name="salesforce-case-create",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Subject": "string",
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
                "instance_host": "mycompany.my.salesforce.com",
                "Subject": "Login page not loading",
                "Priority": "High",
                "Status": "New",
                "Origin": "Web",
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
        assert (
            capture["url"] == "https://mycompany.my.salesforce.com"
            "/services/data/v59.0/sobjects/Case"
        )
        assert capture["headers"]["authorization"] == "Bearer test-sf-token-bbb"
        assert capture["body"]["Subject"] == "Login page not loading"
        assert capture["body"]["Priority"] == "High"
        assert result["id"] == "500xx000000bZKLAA2"
        assert result["success"] is True

        await backend.aclose()
class TestSalesforceContactSearch:
    """salesforce-contact-search -- GET /services/data/v59.0/query/?q={soql}."""
    @pytest.mark.asyncio
    async def test_get_url_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "test-sf-token-ccc")
        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "totalSize": 1,
                "done": True,
                "records": [
                    {
                        "Id": "003xx000004TmiQAAS",
                        "FirstName": "Jane",
                        "LastName": "Smith",
                        "Email": "jane.smith@acme.com",
                    },
                ],
            capture=capture,
        backend = HttpSkillBackend(transport=transport)
        entry = "https://{{ input.instance_host }}/services/data/v59.0/query/?q={{ input.soql }}"
        skill_dir = _write_http_skill(
            tmp_path,
            name="salesforce-contact-search",
            entry=entry,
            method="GET",
            auth="bearer-from-env:SALESFORCE_ACCESS_TOKEN",
            headers={"Accept": "application/json"},
            input_schema={
                "instance_host": "string",
                "soql": "string",
            output_schema={"totalSize": "number"},
        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
                "instance_host": "mycompany.my.salesforce.com",
                "soql": "SELECT Id, FirstName, LastName FROM Contact WHERE LastName = 'Smith'",
            _ctx(),
        assert capture["method"] == "GET"
        assert "/services/data/v59.0/query/" in capture["url"]
        assert capture["headers"]["authorization"] == "Bearer test-sf-token-ccc"
        assert result["totalSize"] == 1
        assert result["records"][0]["LastName"] == "Smith"
# ---------------------------------------------------------------------------
# SAP connector tests
class TestSAPEmployeeGet:
    """sap-employee-get -- GET .../A_BusinessPartner('{id}')."""
        monkeypatch.setenv("SAP_API_KEY", "test-sap-key-aaa")
                "BusinessPartner": "1000000",
                "FirstName": "Hans",
                "LastName": "Mueller",
                "BusinessPartnerFullName": "Hans Mueller",
                "BusinessPartnerCategory": "2",
                "Industry": "Manufacturing",
                "CreationDate": "2023-01-10",
        entry = (
            "https://{{ input.instance_host }}"
            "/sap/opu/odata/sap/API_BUSINESS_PARTNER"
            "/A_BusinessPartner('{{ input.partner_id }}')"
            name="sap-employee-get",
            auth="bearer-from-env:SAP_API_KEY",
                "partner_id": "string",
            output_schema={"BusinessPartner": "string"},
                "instance_host": "my-s4hana.sap-api.com",
                "partner_id": "1000000",
            "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner('1000000')" in capture["url"]
        assert capture["headers"]["authorization"] == "Bearer test-sap-key-aaa"
        assert result["BusinessPartner"] == "1000000"
        assert result["FirstName"] == "Hans"
class TestSAPPurchaseOrderCreate:
    """sap-purchaseorder-create -- POST .../A_PurchaseOrder."""
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SAP_API_KEY", "test-sap-key-bbb")
                "PurchaseOrder": "4500000001",
                "CompanyCode": "1000",
                "PurchaseOrderType": "NB",
                "Supplier": "17300001",
                "PurchasingOrganization": "1000",
                "CreationDate": "2024-06-01",
            "/sap/opu/odata/sap"
            "/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
            name="sap-purchaseorder-create",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CSRF-Token": "Fetch",
                "CompanyCode": "string",
                "Supplier": "string",
                "PurchasingOrganization": "string",
            output_schema={"PurchaseOrder": "string"},
                "PurchasingGroup": "001",
                "DocumentCurrency": "USD",
        expected = (
            "https://my-s4hana.sap-api.com"
        assert capture["headers"]["authorization"] == "Bearer test-sap-key-bbb"
        assert capture["body"]["CompanyCode"] == "1000"
        assert capture["body"]["Supplier"] == "17300001"
        assert result["PurchaseOrder"] == "4500000001"

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
    def test_registry_has_all_connectors(self) -> None:
        assert "workday" in BUILTIN_CONNECTORS
        assert "salesforce" in BUILTIN_CONNECTORS
        assert "sap" in BUILTIN_CONNECTORS
    def test_workday_metadata(self) -> None:
        wd = BUILTIN_CONNECTORS["workday"]
        assert wd["module"] == "movate.connectors.workday"
        assert len(wd["skills"]) == 3
        assert "workday-worker-get" in wd["skills"]
        assert "workday-worker-create" in wd["skills"]
        assert "workday-timeoff-balance" in wd["skills"]
        assert "WORKDAY_ACCESS_TOKEN" in wd["env_vars"]
        assert "WORKDAY_BASE_URL" in wd["env_vars"]
    def test_salesforce_metadata(self) -> None:
        sf = BUILTIN_CONNECTORS["salesforce"]
        assert sf["module"] == "movate.connectors.salesforce"
        assert len(sf["skills"]) == 3
        assert "salesforce-account-get" in sf["skills"]
        assert "salesforce-case-create" in sf["skills"]
        assert "salesforce-contact-search" in sf["skills"]
        assert "SALESFORCE_ACCESS_TOKEN" in sf["env_vars"]
        assert "SALESFORCE_INSTANCE_URL" in sf["env_vars"]
    def test_sap_metadata(self) -> None:
        sp = BUILTIN_CONNECTORS["sap"]
        assert sp["module"] == "movate.connectors.sap"
        assert len(sp["skills"]) == 2
        assert "sap-employee-get" in sp["skills"]
        assert "sap-purchaseorder-create" in sp["skills"]
        assert "SAP_API_KEY" in sp["env_vars"]
        assert "SAP_BASE_URL" in sp["env_vars"]

    def test_all_connectors_have_required_keys(self) -> None:
        for name, meta in BUILTIN_CONNECTORS.items():
            assert "module" in meta, f"{name} missing 'module'"
            assert "skills" in meta, f"{name} missing 'skills'"
            assert "env_vars" in meta, f"{name} missing 'env_vars'"
            assert "description" in meta, f"{name} missing 'description'"
            assert len(meta["skills"]) > 0, f"{name} has no skills"
            assert len(meta["env_vars"]) > 0, f"{name} has no env_vars"
