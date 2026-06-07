"""ToolResolver — resolves ``name@version`` to a concrete ToolDescriptor.

Resolution algorithm (ADR 052 D3):

1. Walk scope precedence: project -> tenant -> movate (most-specific wins).
2. Check version constraint (semver, reuses the SkillRef pattern).
3. Check allowlist (per-agent grants) when governance requires it.
4. Return the resolved descriptor or raise ``ToolResolutionError``.

The resolver is a build-time / load-time step that turns a
``name@version`` reference into a concrete descriptor; from there the
runtime converts it to a ``SkillBundle`` for the unchanged dispatch path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from movate.core.tool_registry.models import ToolDescriptor, ToolScope

if TYPE_CHECKING:
    pass


class ToolResolutionError(Exception):
    """Raised when a tool reference cannot be resolved."""


class ToolStore(Protocol):
    """Minimal read interface the resolver needs from storage.

    Satisfied by both ``StorageProvider`` (runtime) and the in-memory
    test double, keeping the resolver decoupled from concrete backends.
    """

    async def get_tool_descriptor(
        self,
        name: str,
        version: str | None,
        scope: str,
        tenant_id: str,
    ) -> ToolDescriptor | None: ...

    async def list_tool_descriptors(
        self,
        scope: str | None,
        tenant_id: str,
        tags: list[str] | None,
    ) -> list[ToolDescriptor]: ...


# Caret-range pattern for version constraints.
_CARET_RE = re.compile(r"\^(\d+(?:\.\d+)*)")
_BARE_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def _normalize_constraint(constraint: str) -> str:
    """Normalize a constraint to a pip-style specifier.

    * ``^MAJOR.MINOR`` -> ``>=MAJOR.MINOR,<(MAJOR+1).0.0``
    * Bare version -> ``==version``
    * Everything else passes through.
    """
    stripped = constraint.strip()
    if _BARE_VERSION_RE.match(stripped):
        return f"=={stripped}"

    def _replace(m: re.Match[str]) -> str:
        parts = m.group(1).split(".")
        major = int(parts[0])
        lo = m.group(1)
        hi = f"{major + 1}.0.0"
        return f">={lo},<{hi}"

    return _CARET_RE.sub(_replace, stripped)


def _check_version(
    name: str,
    installed: str,
    constraint: str,
) -> None:
    """Check that ``installed`` satisfies ``constraint``.

    Raises ``ToolResolutionError`` on mismatch.
    """
    if constraint == "*":
        return

    normalized = _normalize_constraint(constraint)

    from packaging.specifiers import InvalidSpecifier, SpecifierSet  # noqa: PLC0415
    from packaging.version import Version  # noqa: PLC0415

    try:
        spec_set = SpecifierSet(normalized)
    except InvalidSpecifier as exc:
        raise ToolResolutionError(
            f"tool {name!r}: invalid version constraint {constraint!r} -- {exc}"
        ) from exc

    try:
        parsed = Version(installed)
    except Exception as exc:
        raise ToolResolutionError(
            f"tool {name!r}: version {installed!r} is not valid semver: {exc}"
        ) from exc

    if parsed not in spec_set:
        raise ToolResolutionError(
            f"tool {name!r} version {installed!r} does not satisfy constraint {constraint!r}"
        )


def parse_tool_ref(ref: str) -> tuple[str, str]:
    """Parse ``name@version`` into ``(name, constraint)``.

    If no ``@`` is present, returns ``(name, '*')`` (any version).
    """
    if "@" in ref:
        name, constraint = ref.split("@", 1)
        return name.strip(), constraint.strip()
    return ref.strip(), "*"


class ToolResolver:
    """Resolves tool references against the registry.

    Scope precedence: project -> tenant -> movate. The most-specific
    scope wins: a project tool shadows a tenant tool of the same name.
    """

    def __init__(
        self,
        store: ToolStore,
        tenant_id: str,
        *,
        allowlist: set[str] | None = None,
    ) -> None:
        self._store = store
        self._tenant_id = tenant_id
        # Per-agent allowlist of tool names. None means "all tools allowed"
        # (the default when governance.default_grant is True for all tools).
        self._allowlist = allowlist

    async def resolve(
        self,
        ref: str,
    ) -> ToolDescriptor:
        """Resolve a ``name@version`` reference to a ``ToolDescriptor``.

        Walks scope precedence (project -> tenant -> movate), checks the
        version constraint, checks the allowlist, and returns the best
        matching descriptor.

        Raises ``ToolResolutionError`` if the tool cannot be found, the
        version constraint is not satisfied, or the tool is not on the
        allowlist.
        """
        name, constraint = parse_tool_ref(ref)

        # Walk scope precedence.
        scopes = [ToolScope.PROJECT, ToolScope.TENANT, ToolScope.MOVATE]
        descriptor: ToolDescriptor | None = None

        for scope in scopes:
            candidate = await self._store.get_tool_descriptor(
                name=name,
                version=None,  # Get latest; version check below.
                scope=scope.value,
                tenant_id=self._tenant_id,
            )
            if candidate is not None:
                descriptor = candidate
                break

        if descriptor is None:
            raise ToolResolutionError(
                f"tool {name!r} not found in any scope "
                f"(project, tenant, movate) for tenant {self._tenant_id!r}"
            )

        # Version constraint check.
        _check_version(name, descriptor.version, constraint)

        # Allowlist check.
        if not descriptor.governance.default_grant and (
            self._allowlist is None or name not in self._allowlist
        ):
            raise ToolResolutionError(
                f"tool {name!r} requires an explicit allowlist grant "
                f"(governance.default_grant=false) but is not in the "
                f"agent's tool allowlist"
            )

        return descriptor

    async def resolve_many(
        self,
        refs: list[str],
    ) -> list[ToolDescriptor]:
        """Resolve a list of tool references."""
        result: list[ToolDescriptor] = []
        for ref in refs:
            descriptor = await self.resolve(ref)
            result.append(descriptor)
        return result
