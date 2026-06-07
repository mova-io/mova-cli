"""Tests for the Tool Registry Phase 1 (ADR 052).

Covers: ToolDescriptor model, ToolResolver, exec backend, storage CRUD,
and the ToolDescriptor -> SkillBundle bridge.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from movate.core.models import (
    SkillImplementation,
    SkillImplementationKind,
    SkillSpec,
)
from movate.core.skill_backend.base import SkillExecutionContext, dispatch_skill
from movate.core.skill_loader import SkillBundle
from movate.core.tool_registry.bridge import tool_descriptor_to_skill_bundle
from movate.core.tool_registry.models import (
    ToolBackendConfig,
    ToolDescriptor,
    ToolGovernance,
    ToolScope,
)
from movate.core.tool_registry.resolver import (
    ToolResolutionError,
    ToolResolver,
    parse_tool_ref,
)
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# ToolDescriptor model tests
# ---------------------------------------------------------------------------


class TestToolDescriptor:
    def test_valid_descriptor(self) -> None:
        d = ToolDescriptor(
            name="jira.create-issue",
            version="1.2.0",
            scope=ToolScope.TENANT,
            description="Create a Jira issue",
            tags=["crm", "jira"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            backend=ToolBackendConfig(
                kind="mcp",
                config={"entry": "npx jira-mcp", "tool": "create"},
            ),
            credentials_ref="jira-cloud",
            governance=ToolGovernance(mutating=True, default_grant=False),
            owner="team-platform",
        )
        assert d.name == "jira.create-issue"
        assert d.version == "1.2.0"
        assert d.scope == ToolScope.TENANT
        assert d.governance.mutating is True

    def test_invalid_name_rejects_uppercase(self) -> None:
        with pytest.raises(Exception, match="tool name"):
            ToolDescriptor(
                name="Jira.Create",
                version="1.0.0",
                backend=ToolBackendConfig(kind="exec", config={}),
            )

    def test_invalid_version_rejects_non_semver(self) -> None:
        with pytest.raises(Exception, match="semver"):
            ToolDescriptor(
                name="my-tool",
                version="v1",
                backend=ToolBackendConfig(kind="exec", config={}),
            )

    def test_stamp_now(self) -> None:
        d = ToolDescriptor(
            name="my-tool",
            version="1.0.0",
            backend=ToolBackendConfig(kind="exec", config={}),
        )
        stamped = d.stamp_now()
        assert stamped.created_at is not None
        assert stamped.updated_at is not None


# ---------------------------------------------------------------------------
# parse_tool_ref tests
# ---------------------------------------------------------------------------


class TestParseToolRef:
    def test_name_only(self) -> None:
        name, constraint = parse_tool_ref("jira.create-issue")
        assert name == "jira.create-issue"
        assert constraint == "*"

    def test_name_at_version(self) -> None:
        name, constraint = parse_tool_ref("jira.create-issue@^1.2.0")
        assert name == "jira.create-issue"
        assert constraint == "^1.2.0"

    def test_exact_version(self) -> None:
        name, constraint = parse_tool_ref("tool@1.0.0")
        assert name == "tool"
        assert constraint == "1.0.0"


# ---------------------------------------------------------------------------
# ToolResolver tests
# ---------------------------------------------------------------------------


class TestToolResolver:
    @pytest.fixture
    def storage(self) -> object:
        """In-memory storage double for the resolver."""
        return InMemoryStorage()

    def test_resolve_tenant_tool(self, storage: object) -> None:
        d = ToolDescriptor(
            name="my-tool",
            version="1.0.0",
            scope=ToolScope.TENANT,
            backend=ToolBackendConfig(kind="exec", config={"entry": "echo hello"}),
            tenant_id="local",
            updated_at=datetime.now(UTC),
        )

        async def _run() -> ToolDescriptor:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d)  # type: ignore[union-attr]
            resolver = ToolResolver(store=storage, tenant_id="local")  # type: ignore[arg-type]
            return await resolver.resolve("my-tool")

        result = asyncio.run(_run())
        assert result.name == "my-tool"
        assert result.version == "1.0.0"

    def test_resolve_with_version_constraint(self, storage: object) -> None:
        d = ToolDescriptor(
            name="my-tool",
            version="1.2.3",
            scope=ToolScope.TENANT,
            backend=ToolBackendConfig(kind="exec", config={"entry": "echo"}),
            tenant_id="local",
            updated_at=datetime.now(UTC),
        )

        async def _run() -> ToolDescriptor:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d)  # type: ignore[union-attr]
            resolver = ToolResolver(store=storage, tenant_id="local")  # type: ignore[arg-type]
            return await resolver.resolve("my-tool@^1.0.0")

        result = asyncio.run(_run())
        assert result.version == "1.2.3"

    def test_resolve_version_mismatch(self, storage: object) -> None:
        d = ToolDescriptor(
            name="my-tool",
            version="2.0.0",
            scope=ToolScope.TENANT,
            backend=ToolBackendConfig(kind="exec", config={"entry": "echo"}),
            tenant_id="local",
            updated_at=datetime.now(UTC),
        )

        async def _run() -> None:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d)  # type: ignore[union-attr]
            resolver = ToolResolver(store=storage, tenant_id="local")  # type: ignore[arg-type]
            await resolver.resolve("my-tool@^1.0.0")

        with pytest.raises(ToolResolutionError, match="does not satisfy"):
            asyncio.run(_run())

    def test_resolve_not_found(self, storage: object) -> None:
        async def _run() -> None:
            await storage.init()  # type: ignore[union-attr]
            resolver = ToolResolver(store=storage, tenant_id="local")  # type: ignore[arg-type]
            await resolver.resolve("nonexistent")

        with pytest.raises(ToolResolutionError, match="not found"):
            asyncio.run(_run())

    def test_resolve_allowlist_blocked(self, storage: object) -> None:
        d = ToolDescriptor(
            name="restricted-tool",
            version="1.0.0",
            scope=ToolScope.TENANT,
            backend=ToolBackendConfig(kind="exec", config={"entry": "echo"}),
            governance=ToolGovernance(mutating=True, default_grant=False),
            tenant_id="local",
            updated_at=datetime.now(UTC),
        )

        async def _run() -> None:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d)  # type: ignore[union-attr]
            resolver = ToolResolver(
                store=storage,  # type: ignore[arg-type]
                tenant_id="local",
                allowlist=set(),  # Empty allowlist
            )
            await resolver.resolve("restricted-tool")

        with pytest.raises(ToolResolutionError, match="allowlist"):
            asyncio.run(_run())

    def test_resolve_allowlist_granted(self, storage: object) -> None:
        d = ToolDescriptor(
            name="restricted-tool",
            version="1.0.0",
            scope=ToolScope.TENANT,
            backend=ToolBackendConfig(kind="exec", config={"entry": "echo"}),
            governance=ToolGovernance(mutating=True, default_grant=False),
            tenant_id="local",
            updated_at=datetime.now(UTC),
        )

        async def _run() -> ToolDescriptor:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d)  # type: ignore[union-attr]
            resolver = ToolResolver(
                store=storage,  # type: ignore[arg-type]
                tenant_id="local",
                allowlist={"restricted-tool"},
            )
            return await resolver.resolve("restricted-tool")

        result = asyncio.run(_run())
        assert result.name == "restricted-tool"


# ---------------------------------------------------------------------------
# exec backend tests
# ---------------------------------------------------------------------------


class TestExecBackend:
    def test_exec_echo_tool(self) -> None:
        """End-to-end: exec backend runs a shell command with JSON I/O."""
        # Ensure exec backend is registered.
        importlib.import_module("movate.core.skill_backend.exec")

        spec = SkillSpec(
            api_version="movate/v1",
            kind="Skill",
            name="echo-test",
            version="1.0.0",
            schema={"input": {"type": "object"}, "output": {"type": "object"}},
            implementation=SkillImplementation(
                kind=SkillImplementationKind.EXEC,
                entry=(
                    "python3 -c "
                    '"import sys, json; data=json.load(sys.stdin); '
                    'print(json.dumps(data))"'
                ),
            ),
        )
        input_schema: dict = {"type": "object"}
        output_schema: dict = {"type": "object"}
        bundle = SkillBundle(
            spec=spec,
            skill_dir=Path("."),
            input_schema=input_schema,
            output_schema=output_schema,
            input_validator=Draft202012Validator(input_schema),
            output_validator=Draft202012Validator(output_schema),
        )

        ctx = SkillExecutionContext(
            trace_id="test",
            tenant_id="local",
            call_ms_budget=10_000,
        )

        result = asyncio.run(dispatch_skill(bundle, {"hello": "world"}, ctx))
        assert result == {"hello": "world"}


# ---------------------------------------------------------------------------
# Bridge tests
# ---------------------------------------------------------------------------


class TestBridge:
    def test_descriptor_to_skill_bundle(self) -> None:
        d = ToolDescriptor(
            name="jira.create-issue",
            version="1.2.0",
            scope=ToolScope.TENANT,
            description="Create a Jira issue",
            input_schema={"type": "object", "properties": {"project": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"issue_id": {"type": "string"}}},
            backend=ToolBackendConfig(
                kind="mcp",
                config={"entry": "npx -y @movate/mcp-jira", "tool": "create_issue"},
            ),
        )

        bundle = tool_descriptor_to_skill_bundle(d)
        assert bundle.spec.name == "jira-create-issue"  # dots converted to hyphens
        assert bundle.spec.version == "1.2.0"
        assert bundle.spec.implementation.kind.value == "mcp"
        assert bundle.spec.implementation.tool == "create_issue"
        assert bundle.input_schema == d.input_schema
        assert bundle.output_schema == d.output_schema


# ---------------------------------------------------------------------------
# Storage CRUD tests (InMemoryStorage)
# ---------------------------------------------------------------------------


class TestToolDescriptorStorage:
    @pytest.fixture
    def storage(self) -> object:
        return InMemoryStorage()

    def test_save_and_get(self, storage: object) -> None:
        d = ToolDescriptor(
            name="my-tool",
            version="1.0.0",
            scope=ToolScope.TENANT,
            backend=ToolBackendConfig(kind="exec", config={"entry": "echo"}),
            tenant_id="t1",
            updated_at=datetime.now(UTC),
        )

        async def _run() -> ToolDescriptor | None:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d)  # type: ignore[union-attr]
            return await storage.get_tool_descriptor(  # type: ignore[union-attr]
                name="my-tool", version="1.0.0", scope="tenant", tenant_id="t1"
            )

        result = asyncio.run(_run())
        assert result is not None
        assert result.name == "my-tool"

    def test_list_with_tags(self, storage: object) -> None:
        d1 = ToolDescriptor(
            name="tool-a",
            version="1.0.0",
            scope=ToolScope.TENANT,
            tags=["crm"],
            backend=ToolBackendConfig(kind="exec", config={}),
            tenant_id="t1",
        )
        d2 = ToolDescriptor(
            name="tool-b",
            version="1.0.0",
            scope=ToolScope.TENANT,
            tags=["hr"],
            backend=ToolBackendConfig(kind="exec", config={}),
            tenant_id="t1",
        )

        async def _run() -> list:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d1)  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d2)  # type: ignore[union-attr]
            return await storage.list_tool_descriptors(  # type: ignore[union-attr]
                scope=None, tenant_id="t1", tags=["crm"]
            )

        result = asyncio.run(_run())
        assert len(result) == 1
        assert result[0].name == "tool-a"

    def test_delete(self, storage: object) -> None:
        d = ToolDescriptor(
            name="my-tool",
            version="1.0.0",
            scope=ToolScope.TENANT,
            backend=ToolBackendConfig(kind="exec", config={}),
            tenant_id="t1",
        )

        async def _run() -> tuple[bool, object]:
            await storage.init()  # type: ignore[union-attr]
            await storage.save_tool_descriptor(d)  # type: ignore[union-attr]
            deleted = await storage.delete_tool_descriptor(  # type: ignore[union-attr]
                name="my-tool", version="1.0.0", scope="tenant", tenant_id="t1"
            )
            after = await storage.get_tool_descriptor(  # type: ignore[union-attr]
                name="my-tool", version="1.0.0", scope="tenant", tenant_id="t1"
            )
            return deleted, after

        deleted, after = asyncio.run(_run())
        assert deleted is True
        assert after is None
