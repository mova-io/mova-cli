#!/usr/bin/env python3
"""License gate — fails CI if any installed dep has a non-permissive license.

Walks the runtime dependency tree via ``importlib.metadata`` and checks
each package's declared license against the allowlist. Exits 1 on the
first violation; the failed PR shows the dep name, its license, and a
pointer to ``docs/license-posture.md`` so the operator knows what to do.

Why this gate exists
--------------------

movate-cli is embedded in Movate customer deliverables. Any dep with a
copyleft (GPL/LGPL/AGPL), SSPL, BSL, or Elastic License 2.0 obligation
would propagate to those customer products — potentially forcing
source-disclosure, blocking SaaS resale, or imposing
competing-services restrictions. This script blocks such deps at PR time
so the issue is caught before merge, not before a customer engagement.

Single source of truth
----------------------

The allowlist below mirrors the table in ``docs/license-posture.md``
and the ``_LICENSE_ALLOWLIST`` constant in ``src/movate/cli/doctor.py``.
Add a license here only after writing an ADR explaining the addition
(per the policy in ``docs/license-posture.md``).

Usage
-----

    $ python scripts/check_licenses.py              # full report
    $ python scripts/check_licenses.py --strict     # exit 1 on any violation
    $ python scripts/check_licenses.py --json       # machine-readable output

CI runs this with ``--strict``. Local devs can run without to see the
full inventory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

# --- Allowlist ---------------------------------------------------------------
# Mirror of docs/license-posture.md "approved licenses" table. Keep in sync.

ALLOWED_SPDX = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "Apache 2.0",  # tolerated synonym; most packages use the dashed form
        "Apache Software License",  # legacy classifier
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSD",  # often unspecified variant; assume 3-clause (verify on suspicion)
        "ISC",
        "0BSD",  # BSD Zero Clause — public-domain-equivalent, OSI-approved
        "Zlib",  # zlib/libpng license — permissive, OSI-approved
        "PostgreSQL",
        "PSF-2.0",
        "Python Software Foundation License",
        "Python-2.0",  # PSF aliasing
        "MPL-2.0",  # Mozilla Public License 2.0 — file-level copyleft only;
        # the "weak copyleft" boundary doesn't propagate to using
        # code that just imports MPL modules. Approved for embed.
        "MIT-CMU",  # used by some pyparsing variants; permissive
        "Unlicense",
        "CC0-1.0",
    }
)

# License families we explicitly EXCLUDE. Listed here for the gate's
# error message — the script doesn't need to know all of them by name
# (anything not in ALLOWED_SPDX is rejected), but surfacing the family
# in the error helps the operator understand why.

EXCLUDED_FAMILIES = {
    "GPL": "copyleft — propagates to anything that links it",
    "LGPL": "weak copyleft — restrictions on derived works",
    "AGPL": "network copyleft — triggers on hosting as a service",
    "SSPL": "Server Side Public License — kills SaaS resale",
    "BSL": "Business Source License — competing-services restrictions",
    "Elastic License": "restrictive — competing-services / SaaS limits",
    "Commons Clause": "restrictive — bars commercial use",
    "RPL": "Reciprocal Public License — strong copyleft",
}

# Packages that are dev-only (NOT shipped in customer deliverables).
# These don't need to be in the allowlist — they're filtered out of the
# gate. Names should match what ``importlib.metadata`` returns.

DEV_ONLY = frozenset(
    {
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "pytest-mock",
        "ruff",
        "mypy",
        "types-pyyaml",
        "types-jsonschema",
        "coverage",
        "iniconfig",
        "pluggy",
        "pathspec",
        "exceptiongroup",
        "execnet",
        "tomli",
        # Build / publish toolchain — present in dev envs, never ships
        "build",
        "twine",
        "id",
        "keyring",
        "nh3",
        "readme_renderer",
        "rfc3986",
        "docutils",
        "backports.tarfile",
        "jaraco.classes",
        "jaraco.context",
        "jaraco.functools",
        "more-itertools",
        "pyproject_hooks",
        # Doc / deck generation (used by scripts/ but not by the runtime)
        "python-pptx",
        "xlsxwriter",
        "pillow",
        "lxml",
    }
)


# Packages that are the project itself or local-only (filter them out).

SELF_PACKAGES = frozenset({"movate-cli", "movate", "mdk"})

# Extras that ship in customer deliverables. The gate scopes to the
# transitive closure of the core deps PLUS these extras. The heavy opt-in
# extras (easyocr / cross-encoder / ocr) pull in a large ML/GPU stack
# (torch, the NVIDIA CUDA runtime libs, python-bidi, …) that an operator
# explicitly chooses to install — those licenses are out of scope for the
# default deliverable, so they are intentionally NOT shipped-scoped here.
SHIPPED_EXTRAS = frozenset({"runtime", "langfuse"})

# Heuristic: license metadata > this many chars almost always means the
# package embedded its full LICENSE.txt in the metadata. Strip down to
# just the first line to recover the SPDX id.

_MAX_LICENSE_LENGTH = 80


_ALIASES = {
    # MIT
    "MIT": "MIT",
    "MIT License": "MIT",
    # Apache 2.0 and its many spellings
    "Apache 2.0": "Apache-2.0",
    "Apache 2": "Apache-2.0",
    "Apache": "Apache-2.0",  # bare classifier (e.g. huggingface_hub)
    "Apache-2.0": "Apache-2.0",
    "Apache Software": "Apache-2.0",
    "Apache Software License": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "Apache License Version 2.0": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    # BSD
    "BSD-3-Clause": "BSD-3-Clause",
    "BSD 3-Clause": "BSD-3-Clause",
    "BSD-3": "BSD-3-Clause",
    "3-Clause BSD": "BSD-3-Clause",
    "Modified BSD": "BSD-3-Clause",
    "New BSD": "BSD-3-Clause",
    "BSD-2-Clause": "BSD-2-Clause",
    "BSD 2-Clause": "BSD-2-Clause",
    "BSD": "BSD-3-Clause",
    # ISC
    "ISC": "ISC",
    "ISC License": "ISC",
    "ISC License (ISCL)": "ISC",
    # MPL
    "MPL 2.0": "MPL-2.0",
    "MPL-2.0": "MPL-2.0",
    "Mozilla Public License 2.0": "MPL-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    # PSF / Python
    "Python Software Foundation License": "PSF-2.0",
    "PSF": "PSF-2.0",
    "PSF-2.0": "PSF-2.0",
    "Python-2.0": "PSF-2.0",
    # Misc
    "Public Domain": "Unlicense",
    "The Unlicense": "Unlicense",
    "Unlicense": "Unlicense",
    "CNRI-Python": "PSF-2.0",  # CNRI Python is the Python Software Foundation predecessor
}


def _normalize_license(raw: str | None) -> str:
    """Coerce a free-text license field into a comparable SPDX token.

    Real-world package metadata is messy:
      * Some packages set ``License-Expression`` to a clean SPDX id.
      * Many use the old free-text ``License`` field with strings like
        ``"Apache License, Version 2.0"`` or ``"Modified BSD"``.
      * A few EMBED THE FULL LICENSE TEXT in the metadata, with copyright
        notices and conditions inlined — we want just the SPDX id.
      * Composite expressions like ``"Apache-2.0 AND MIT"`` or
        ``"MIT OR Apache-2.0"`` are increasingly common (PEP 639).

    We strip license boilerplate, normalize aliases via :data:`_ALIASES`,
    and pass compound expressions through unchanged so the allowlist
    check (which understands ``AND`` / ``OR``) can evaluate them.
    """
    if not raw:
        return ""
    # Truncate license text — if the string is long or contains "Copyright"
    # or "Permission", it's the full license body. The first line is the
    # actual license name.
    if len(raw) > _MAX_LICENSE_LENGTH or "Copyright" in raw or "Permission is hereby" in raw:
        raw = raw.split("\n", 1)[0]
    s = raw.strip().strip(".")
    # Trim a trailing " License" / " license" suffix
    s = re.sub(r"\s+licen[cs]e$", "", s, flags=re.IGNORECASE)
    return _ALIASES.get(s, s)


def _is_permissive_expression(expr: str, allowed: frozenset[str]) -> bool:
    """Evaluate a (possibly composite) SPDX-style expression.

    Handles three shapes:
      * ``"MIT"`` — single license; check against ``allowed``.
      * ``"Apache-2.0 AND MIT"`` — composite via AND; user must comply
        with both. Safe if all parts are permissive.
      * ``"MIT OR Apache-2.0"`` — user can choose either. Safe if any
        part is permissive (which == all parts permissive in practice
        for our allowlist scope).

    We DON'T parse parentheses for arbitrary grouping. The compound
    expressions we see in practice are either flat AND or flat OR; if
    a parenthesized hybrid like ``"MPL-2.0 AND (Apache-2.0 OR MIT)"``
    shows up, we flatten by splitting on both operators and checking
    every component — slightly over-permissive but safe given our
    actual allowlist (we ONLY accept permissive parts).
    """
    # Single SPDX id — fast path.
    if expr in allowed:
        return True
    # Composite: split on AND / OR / parens; check every non-empty part.
    parts = re.split(r"\s*(?:AND|OR|\(|\))\s*", expr)
    parts = [_ALIASES.get(p.strip(), p.strip()) for p in parts if p.strip()]
    if not parts:
        return False
    return all(part in allowed for part in parts)


def _read_license(dist: metadata.Distribution) -> str:
    """Best-effort read of the package's declared license.

    Newer packages set ``License-Expression`` directly (PEP 639); older
    ones use ``License`` (free text); the oldest use the ``Classifier``
    list. We try in that order and surface whichever has content.
    """
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return _normalize_license(expr)
    free = md.get("License")
    if free:
        return _normalize_license(free)
    # Fall back to classifiers (e.g. "License :: OSI Approved :: MIT License")
    for classifier in md.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            tail = classifier.rsplit("::", 1)[-1].strip()
            normed = _normalize_license(tail)
            if normed:
                return normed
    return ""


def _excluded_family(license_id: str) -> tuple[str, str] | None:
    """If the license matches a known copyleft / restricted family, return
    (family_name, reason). Otherwise None."""
    for family, reason in EXCLUDED_FAMILIES.items():
        if family.lower() in license_id.lower():
            return family, reason
    return None


def _canonical(name: str) -> str:
    """PEP 503 canonical package name (lowercase; runs of -_. → single -)."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_REQ_EXTRAS_RE = re.compile(r"\[([^\]]+)\]")
_REQ_EXTRA_MARKER_RE = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")


def _parse_requirement(req: str) -> tuple[str, frozenset[str]]:
    """``(canonical name, requested-extras)`` from a requirement string like
    ``uvicorn[standard]>=0.29``."""
    m = _REQ_NAME_RE.match(req)
    name = _canonical(m.group(1)) if m else ""
    # Extras live in the bracket BEFORE any version specifier / marker.
    head = re.split(r"[;<>=!~ ]", req, maxsplit=1)[0]
    em = _REQ_EXTRAS_RE.search(head)
    extras = frozenset(_canonical(e) for e in em.group(1).split(",")) if em else frozenset()
    return name, extras


def _requires_extra(req: str) -> str | None:
    """The extra that gates a ``Requires-Dist`` entry (``; extra == "x"``), if any."""
    m = _REQ_EXTRA_MARKER_RE.search(req)
    return _canonical(m.group(1)) if m else None


def _read_shipped_roots() -> list[str] | None:
    """Requirement strings that ship: core ``dependencies`` plus the
    :data:`SHIPPED_EXTRAS` extras, read from ``pyproject.toml``. Returns
    ``None`` if pyproject can't be located/parsed (caller scans everything)."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project", {})
    roots: list[str] = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in SHIPPED_EXTRAS:
        roots.extend(optional.get(extra, []))
    return roots or None


def _shipped_closure() -> set[str] | None:
    """Canonical names of every installed package reachable from the shipped
    roots — following requested extras (e.g. ``uvicorn[standard]``) and
    skipping extra-gated requirements we didn't ask for. ``None`` means
    "couldn't scope; scan everything" (safe over-inclusive fallback)."""
    roots = _read_shipped_roots()
    if not roots:
        return None
    queue: list[tuple[str, frozenset[str]]] = [_parse_requirement(r) for r in roots]
    closure: set[str] = set()
    visited: set[tuple[str, frozenset[str]]] = set()
    while queue:
        name, extras = queue.pop()
        if not name:
            continue
        state = (name, extras)
        if state in visited:
            continue
        visited.add(state)
        closure.add(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for req in dist.requires or []:
            gate = _requires_extra(req)
            if gate is not None and gate not in extras:
                continue  # optional dep for an extra we didn't request
            queue.append(_parse_requirement(req))
    return closure


def scan() -> list[dict[str, str]]:
    """Return one row per shipped dist with name, version, license, status.

    Scopes to the transitive closure of the shipped requirement roots (see
    :func:`_shipped_closure`) so opt-in extras a customer explicitly installs
    (the easyocr / cross-encoder ML+GPU stack) aren't policed as if they were
    part of the default deliverable.
    """
    rows: list[dict[str, str]] = []
    closure = _shipped_closure()
    for dist in metadata.distributions():
        name = dist.metadata.get("Name", "").lower()
        if not name:
            continue
        if name in DEV_ONLY or name in SELF_PACKAGES:
            continue
        if closure is not None and _canonical(name) not in closure:
            continue  # not part of the shipped dependency closure
        license_id = _read_license(dist)
        if _is_permissive_expression(license_id, ALLOWED_SPDX):
            status = "OK"
        elif _excluded_family(license_id) is not None:
            status = "EXCLUDED"
        else:
            status = "REVIEW"
        rows.append(
            {
                "name": name,
                "version": dist.metadata.get("Version", "?"),
                "license": license_id or "(unknown)",
                "status": status,
            }
        )
    rows.sort(key=lambda r: r["name"])
    return rows


def _render_text(rows: Iterable[dict[str, str]]) -> str:
    lines: list[str] = []
    lines.append(f"{'PACKAGE':<35} {'VERSION':<14} {'LICENSE':<22} STATUS")
    lines.append("-" * 80)
    for row in rows:
        lines.append(f"{row['name']:<35} {row['version']:<14} {row['license']:<22} {row['status']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any dep is REVIEW or EXCLUDED. CI gate mode.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human table.",
    )
    args = parser.parse_args()

    rows = scan()

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(_render_text(rows))

    bad = [r for r in rows if r["status"] != "OK"]
    if bad:
        print(file=sys.stderr)
        print(f"✗ {len(bad)} dep(s) are NOT in the allowlist:", file=sys.stderr)
        for row in bad:
            print(
                f"  {row['name']} {row['version']} → {row['license']} ({row['status']})",
                file=sys.stderr,
            )
        print(file=sys.stderr)
        print(
            "  see docs/license-posture.md for the policy and the process",
            file=sys.stderr,
        )
        print(
            "  for proposing a non-allowlist license (ADR + Deva sign-off).",
            file=sys.stderr,
        )
        if args.strict:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
