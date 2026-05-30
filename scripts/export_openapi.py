"""Export the runtime's OpenAPI spec to ``docs/openapi.json`` (a checked-in artifact).

Builds the FastAPI app via the runtime factory
(:func:`movate.runtime.app.build_app`) against an in-memory storage double —
no database, no network, no heavy state — and dumps ``app.openapi()``. This
gives integrators / front-end a stable spec to generate clients from without
standing up a running server.

The ``info.version`` field is pinned to a stable placeholder so the committed
artifact does NOT churn on every CalVer version bump — the spec's *shape* is
what integrators care about, and tying it to the per-commit version would make
the ``--check`` gate fail after every bump.

Usage
-----

::

    # Write docs/openapi.json
    python scripts/export_openapi.py

    # Preview without writing
    python scripts/export_openapi.py --print

    # CI freshness gate — exit non-zero if the committed spec is stale
    python scripts/export_openapi.py --check

Why this exists
---------------

The canonical spec is served at ``/api/v1/openapi.json`` by a running runtime,
but client-gen + contract tests want a stable, reviewable artifact in the repo.
Wire ``--check`` into CI so an API change that isn't reflected in the committed
spec fails the build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Default output path, relative to the repo root (this file's parent's parent).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "openapi.json"

# Pinned so the artifact is stable across per-commit CalVer bumps. The real
# runtime still advertises its actual version at runtime; this is only the
# checked-in export.
_PINNED_VERSION = "v1"


def _build_spec() -> dict[str, Any]:
    """Build the app against an in-memory storage double and return its spec.

    ``app.openapi()`` only needs the route definitions, which are registered
    at ``build_app`` time — so an un-``init``-ed in-memory storage is enough;
    we never touch the DB. Imports are local so ``--help`` stays cheap.
    """
    from movate.runtime.app import build_app  # noqa: PLC0415
    from movate.testing import InMemoryStorage  # noqa: PLC0415

    app = build_app(InMemoryStorage())
    spec: dict[str, Any] = app.openapi()
    # Pin the version for a stable, churn-free artifact (see module docstring).
    spec.setdefault("info", {})["version"] = _PINNED_VERSION
    return spec


def _render(spec: dict[str, Any]) -> str:
    """Serialize ``spec`` deterministically (sorted keys, trailing newline)."""
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the runtime OpenAPI spec to docs/openapi.json.",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Output path (default: {_DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print to stdout instead of writing the file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the output file is stale (or missing). Writes nothing.",
    )
    args = parser.parse_args()

    rendered = _render(_build_spec())

    if args.print_only:
        sys.stdout.write(rendered)
        return 0

    output: Path = args.output

    if args.check:
        current = output.read_text() if output.exists() else None
        if current == rendered:
            print(f"OK: {output} is up to date")
            return 0
        print(
            f"STALE: {output} differs from the generated spec — "
            "run scripts/export_openapi.py and commit the result",
            file=sys.stderr,
        )
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
