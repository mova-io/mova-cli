"""Static assets for the ``mdk graph serve`` knowledge-graph viewer.

This package contains only browser static files (HTML/CSS/JS + vendored,
MIT-licensed sigma.js / graphology / graphology-layout-forceatlas2 UMD
builds). The ``__init__.py`` exists so hatchling ships the directory as a
real package (see ``pyproject.toml`` ``[tool.hatch.build.targets.wheel]``),
guaranteeing the assets are present in the installed wheel. The vendored
asset licenses are recorded in ``VENDOR_LICENSES.md``.

Nothing here is importable Python — the assets are loaded at runtime by
:mod:`movate.cli.graph` via :data:`ASSETS_DIR`.
"""

from __future__ import annotations

from pathlib import Path

#: Directory holding the viewer's static assets, resolved relative to this
#: module so it works for both editable installs and built wheels.
ASSETS_DIR = Path(__file__).resolve().parent

__all__ = ["ASSETS_DIR"]
