"""Tests for enterprise connectors (ADR 052 Phase 1 — Action Fabric).

Three connectors (Workday, Salesforce, SAP), eight skill variants total,
tested against ``httpx.MockTransport`` through the existing
``HttpSkillBackend``.

Coverage map:
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
# Workday connector tests
# ---------------------------------------------------------------------------


class TestWorkdayWorkerGet:
    """workday-worker-get -- GET /ccx/api/v1/{tenant}/workers/{worker_id}."""

    @pytest.mark.asyncio
    async def test_get_url_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKDAY_ACCESS_TOKEN", "test-wd-token-111")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
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
            "https://{{ input.instance_host }}"
            "/ccx/api/v1/{{ input.tenant }}/workers/{{ input.worker_id }}"
        )
        skill_dir = _write_http_skill(
            tmp_path,
            name="workday-worker-get",
            entry=entry,
            method="GET",
            auth="bearer-from-env:WORKDAY_ACCESS_TOKEN",
            headers={"Accept": "application/json"},
            input_schema={
                "instance_host": "string",
                "tenant": "string",
                "worker_id": "string",
            },
            output_schema={"workerID": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "wd3-impl-services1.workday.com",
                "tenant": "mycompany",
                "worker_id": "W12345",
            },
            _ctx(),
        )

        assert capture["method"] == "GET"
        assert capture["url"].startswith(
            "https://wd3-impl-services1.workday.com/ccx/api/v1/mycompany/workers/W12345"
        )
        assert capture["headers"]["authorization"] == "Bearer test-wd-token-111"
        assert result["workerID"] == "W12345"
        assert result["legalName"]["firstName"] == "Alice"

        await backend.aclose()


class TestWorkdayWorkerCreate:
    """workday-worker-create -- POST /ccx/api/v1/{tenant}/workers."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKDAY_ACCESS_TOKEN", "test-wd-token-222")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
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
            },
            input_schema={
                "instance_host": "string",
                "tenant": "string",
                "firstName": "string",
                "lastName": "string",
                "hireDate": "string",
            },
            output_schema={"workerID": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "wd3-impl-services1.workday.com",
                "tenant": "mycompany",
                "firstName": "Bob",
                "lastName": "Smith",
                "hireDate": "2024-03-01",
                "jobTitle": "Product Manager",
                "department": "Product",
            },
            _ctx(),
        )

        assert capture["method"] == "POST"
        assert (
            capture["url"] == "https://wd3-impl-services1.workday.com/ccx/api/v1/mycompany/workers"
        )
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
                    {
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

        entry = (
            "https://{{ input.instance_host }}"
            "/ccx/api/v1/{{ input.tenant }}"
            "/workers/{{ input.worker_id }}/timeOffBalance"
        )
        skill_dir = _write_http_skill(
            tmp_path,
            name="workday-timeoff-balance",
            entry=entry,
            method="GET",
            auth="bearer-from-env:WORKDAY_ACCESS_TOKEN",
            headers={"Accept": "application/json"},
            input_schema={
                "instance_host": "string",
                "tenant": "string",
                "worker_id": "string",
            },
            output_schema={"workerID": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "wd3-impl-services1.workday.com",
                "tenant": "mycompany",
                "worker_id": "W12345",
            },
            _ctx(),
        )

        expected_prefix = (
            "https://wd3-impl-services1.workday.com"
            "/ccx/api/v1/mycompany/workers/W12345/timeOffBalance"
        )
        assert capture["method"] == "GET"
        assert capture["url"].startswith(expected_prefix)
        assert capture["headers"]["authorization"] == "Bearer test-wd-token-333"
        assert result["workerID"] == "W12345"
        assert result["balances"][0]["timeOffType"] == "Vacation"
        assert result["balances"][0]["balanceAmount"] == 15.0

        await backend.aclose()


# ---------------------------------------------------------------------------
# Salesforce connector tests
# ---------------------------------------------------------------------------


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
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = (
            "https://{{ input.instance_host }}"
            "/services/data/v59.0/sobjects/Account/{{ input.account_id }}"
        )
        skill_dir = _write_http_skill(
            tmp_path,
            name="salesforce-account-get",
            entry=entry,
            method="GET",
            auth="bearer-from-env:SALESFORCE_ACCESS_TOKEN",
            headers={"Accept": "application/json"},
            input_schema={
                "instance_host": "string",
                "account_id": "string",
            },
            output_schema={"Id": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "mycompany.my.salesforce.com",
                "account_id": "001xx000003DGbYAAW",
            },
            _ctx(),
        )

        assert capture["method"] == "GET"
        assert capture["url"].startswith(
            "https://mycompany.my.salesforce.com"
            "/services/data/v59.0/sobjects/Account/001xx000003DGbYAAW"
        )
        assert capture["headers"]["authorization"] == "Bearer test-sf-token-aaa"
        assert result["Id"] == "001xx000003DGbYAAW"
        assert result["Name"] == "Acme Corp"

        await backend.aclose()


class TestSalesforceCaseCreate:
    """salesforce-case-create -- POST /services/data/v59.0/sobjects/Case."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "test-sf-token-bbb")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "id": "500xx000000bZKLAA2",
                "success": True,
                "errors": [],
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = "https://{{ input.instance_host }}/services/data/v59.0/sobjects/Case"
        skill_dir = _write_http_skill(
            tmp_path,
            name="salesforce-case-create",
            entry=entry,
            method="POST",
            auth="bearer-from-env:SALESFORCE_ACCESS_TOKEN",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            input_schema={
                "instance_host": "string",
                "Subject": "string",
            },
            output_schema={"id": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "mycompany.my.salesforce.com",
                "Subject": "Login page not loading",
                "Priority": "High",
                "Status": "New",
                "Origin": "Web",
            },
            _ctx(),
        )

        assert capture["method"] == "POST"
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
            },
            capture=capture,
        )
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
            },
            output_schema={"totalSize": "number"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "mycompany.my.salesforce.com",
                "soql": "SELECT Id, FirstName, LastName FROM Contact WHERE LastName = 'Smith'",
            },
            _ctx(),
        )

        assert capture["method"] == "GET"
        assert "/services/data/v59.0/query/" in capture["url"]
        assert capture["headers"]["authorization"] == "Bearer test-sf-token-ccc"
        assert result["totalSize"] == 1
        assert result["records"][0]["LastName"] == "Smith"

        await backend.aclose()


# ---------------------------------------------------------------------------
# SAP connector tests
# ---------------------------------------------------------------------------


class TestSAPEmployeeGet:
    """sap-employee-get -- GET .../A_BusinessPartner('{id}')."""

    @pytest.mark.asyncio
    async def test_get_url_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAP_API_KEY", "test-sap-key-aaa")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "BusinessPartner": "1000000",
                "FirstName": "Hans",
                "LastName": "Mueller",
                "BusinessPartnerFullName": "Hans Mueller",
                "BusinessPartnerCategory": "2",
                "Industry": "Manufacturing",
                "CreationDate": "2023-01-10",
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = (
            "https://{{ input.instance_host }}"
            "/sap/opu/odata/sap/API_BUSINESS_PARTNER"
            "/A_BusinessPartner('{{ input.partner_id }}')"
        )
        skill_dir = _write_http_skill(
            tmp_path,
            name="sap-employee-get",
            entry=entry,
            method="GET",
            auth="bearer-from-env:SAP_API_KEY",
            headers={"Accept": "application/json"},
            input_schema={
                "instance_host": "string",
                "partner_id": "string",
            },
            output_schema={"BusinessPartner": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "my-s4hana.sap-api.com",
                "partner_id": "1000000",
            },
            _ctx(),
        )

        assert capture["method"] == "GET"
        assert (
            "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner('1000000')" in capture["url"]
        )
        assert capture["headers"]["authorization"] == "Bearer test-sap-key-aaa"
        assert result["BusinessPartner"] == "1000000"
        assert result["FirstName"] == "Hans"

        await backend.aclose()


class TestSAPPurchaseOrderCreate:
    """sap-purchaseorder-create -- POST .../A_PurchaseOrder."""

    @pytest.mark.asyncio
    async def test_post_url_body_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SAP_API_KEY", "test-sap-key-bbb")

        capture: dict[str, Any] = {}
        transport = _mock_transport(
            json_body={
                "PurchaseOrder": "4500000001",
                "CompanyCode": "1000",
                "PurchaseOrderType": "NB",
                "Supplier": "17300001",
                "PurchasingOrganization": "1000",
                "CreationDate": "2024-06-01",
            },
            capture=capture,
        )
        backend = HttpSkillBackend(transport=transport)

        entry = (
            "https://{{ input.instance_host }}"
            "/sap/opu/odata/sap"
            "/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
        )
        skill_dir = _write_http_skill(
            tmp_path,
            name="sap-purchaseorder-create",
            entry=entry,
            method="POST",
            auth="bearer-from-env:SAP_API_KEY",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CSRF-Token": "Fetch",
            },
            input_schema={
                "instance_host": "string",
                "CompanyCode": "string",
                "Supplier": "string",
                "PurchasingOrganization": "string",
            },
            output_schema={"PurchaseOrder": "string"},
        )

        skill = load_skill(skill_dir)
        result = await backend.execute(
            skill,
            {
                "instance_host": "my-s4hana.sap-api.com",
                "CompanyCode": "1000",
                "PurchaseOrderType": "NB",
                "Supplier": "17300001",
                "PurchasingOrganization": "1000",
                "PurchasingGroup": "001",
                "DocumentCurrency": "USD",
            },
            _ctx(),
        )

        expected = (
            "https://my-s4hana.sap-api.com"
            "/sap/opu/odata/sap"
            "/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
        )
        assert capture["method"] == "POST"
        assert capture["url"] == expected
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
