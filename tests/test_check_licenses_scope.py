"""Tests for the license gate's shipped-dependency scoping.

``scripts/check_licenses.py`` scopes its scan to the transitive closure of
the *shipped* requirement roots (core deps + the ``runtime`` / ``langfuse``
extras). The heavy opt-in extras (``easyocr`` / ``cross-encoder`` / ``ocr``)
drag in a large ML+GPU stack — torch, the NVIDIA CUDA runtime libs,
``python-bidi`` (LGPL) — that a customer explicitly installs; those are out
of scope for the default deliverable and must not fail the gate.

These tests cover:
* requirement-string parsing (name, requested extras, extra-gating marker),
* the false-positive allowlist fixes (bare "Apache", 0BSD, composite exprs),
* the closure: core deps in, opt-in ML/GPU deps out,
* ``scan()`` never surfaces the scoped-out deps as rows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_licenses.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_licenses", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cl = _load_module()


@pytest.mark.unit
class TestRequirementParsing:
    def test_parse_plain(self) -> None:
        assert cl._parse_requirement("pydantic>=2.6,<3") == ("pydantic", frozenset())

    def test_parse_with_extras(self) -> None:
        name, extras = cl._parse_requirement("uvicorn[standard]>=0.29")
        assert name == "uvicorn"
        assert extras == frozenset({"standard"})

    def test_canonical_normalizes_separators(self) -> None:
        assert cl._canonical("Foo_Bar.Baz") == "foo-bar-baz"

    def test_requires_extra_marker(self) -> None:
        assert cl._requires_extra('click>=7.0; extra == "standard"') == "standard"
        assert cl._requires_extra("anyio>=3.0") is None


@pytest.mark.unit
class TestFalsePositiveNormalization:
    def test_bare_apache_maps_to_apache_2(self) -> None:
        assert cl._normalize_license("Apache") == "Apache-2.0"

    def test_0bsd_and_zlib_allowed(self) -> None:
        assert cl._is_permissive_expression("0BSD", cl.ALLOWED_SPDX)
        assert cl._is_permissive_expression("Zlib", cl.ALLOWED_SPDX)

    def test_numpy_style_composite_allowed(self) -> None:
        expr = "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0"
        assert cl._is_permissive_expression(expr, cl.ALLOWED_SPDX)


@pytest.mark.unit
class TestShippedScoping:
    def test_closure_includes_core_and_runtime(self) -> None:
        closure = cl._shipped_closure()
        assert closure is not None
        # Core deps + runtime-extra deps are in scope.
        assert "pydantic" in closure
        assert "httpx" in closure
        assert "fastapi" in closure

    def test_closure_excludes_optin_ml_stack(self) -> None:
        closure = cl._shipped_closure()
        assert closure is not None
        # easyocr / cross-encoder extras (and their transitive ML/GPU deps)
        # are NOT shipped-scoped.
        assert "easyocr" not in closure
        assert "torch" not in closure
        assert "python-bidi" not in closure

    def test_scan_does_not_surface_scoped_out_deps(self) -> None:
        names = {row["name"] for row in cl.scan()}
        # If these were in scope they'd fail the gate (LGPL / proprietary).
        assert "python-bidi" not in names
        assert "torch" not in names
