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
from collections.abc import Iterable
from importlib import metadata

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


def scan() -> list[dict[str, str]]:
    """Return one row per installed dist with name, version, license, status."""
    rows: list[dict[str, str]] = []
    for dist in metadata.distributions():
        name = dist.metadata.get("Name", "").lower()
        if not name:
            continue
        if name in DEV_ONLY or name in SELF_PACKAGES:
            continue
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
