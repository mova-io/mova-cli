# mypy: ignore-errors  — build-time tooling; hatchling ships no type stubs.
"""Hatchling custom metadata hook — set the version from git CalVer (ADR 066).

The version is computed from git history at build/install time (see
``scripts/calver_version.py``) instead of being read from a committed
``version = "..."`` line. This is what removes the version from every branch's
working tree, so sibling PRs never conflict on it.

``[project] dynamic = ["version"]`` + ``[tool.hatch.metadata.hooks.custom]`` in
``pyproject.toml`` wire hatchling to this hook; ``update()`` populates the
``version`` field before the sdist/wheel metadata is frozen. The built artifact
therefore carries a pinned, reproducible CalVer while the repo carries none.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from hatchling.metadata.plugin.interface import MetadataHookInterface


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict[str, Any]) -> None:
        # scripts/ isn't an importable package; add it to the path so the build
        # can reuse the same pure CalVer computor the tests cover.
        scripts_dir = os.path.join(self.root, "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from calver_version import compute_calver  # noqa: PLC0415

        metadata["version"] = compute_calver(self.root)
