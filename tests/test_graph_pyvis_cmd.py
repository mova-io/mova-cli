"""Tests for ``mdk graph export`` (the standalone PyVis HTML exporter).

Two tiers, so the suite is meaningful whether or not the opt-in ``graph-pyvis``
extra is installed:

* **Always runs** — the command is registered, and the missing-extra path
  prints a friendly ``install movate-cli[graph-pyvis]`` hint and exits cleanly
  (we simulate the absence by forcing the ``import pyvis`` to raise).
* **Render tier (needs pyvis)** — the full happy path: a mocked API fetch feeds
  a real PyVis render; we assert a file is written, that it is self-contained
  with ``cdn_resources`` in-line, and — the security-critical assertion — that
  the bearer key used to fetch the graph is NEVER written into the HTML.
"""

from __future__ import annotations

import builtins

import pytest
import typer
from typer.testing import CliRunner

import movate.cli.graph_pyvis_cmd as graph_cmd
from movate.cli.graph_pyvis_cmd import graph_app
from movate.core.user_config import TargetConfig

runner = CliRunner()

# Mount `graph_app` under a parent app exactly as `cli/main.py` does. A Typer
# app with a single command collapses the subcommand name when it's the root,
# so we exercise it the way it's actually wired: `... graph export <file>`.
_parent = typer.Typer()
_parent.add_typer(graph_app, name="graph")


def _invoke(args: list[str]):
    return runner.invoke(_parent, ["graph", *args])


# The render tier needs the real PyVis extra. We DON'T `importorskip` at module
# scope — the registration + missing-extra tests must run regardless of whether
# the extra is installed. Render-tier tests are individually skipped instead.
try:
    import pyvis  # noqa: F401

    _HAS_PYVIS = True
except ImportError:
    _HAS_PYVIS = False

_needs_pyvis = pytest.mark.skipif(not _HAS_PYVIS, reason="graph-pyvis extra not installed")

# A recognizable secret we assert never lands in the exported artifact.
_SECRET_KEY = "mvt_live_SUPERSECRET_donotleak_123456"

_SAMPLE_GRAPH = {
    "attributes": {"name": "test graph"},
    "nodes": [
        {
            "key": "n1",
            "attributes": {
                "label": "SAML SSO",
                "type": "Feature",
                "source_provenance": {"url": "https://docs.example.com/saml"},
            },
        },
        {"key": "n2", "attributes": {"label": "Security Policy", "type": "Policy"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2", "attributes": {"label": "governed-by", "weight": 0.7}}
    ],
}


@pytest.fixture
def _stub_target(monkeypatch):
    """Resolve a fake target + bearer without touching real config/env."""
    cfg = TargetConfig(url="https://runtime.example.com", key_env="MOVATE_TEST_KEY")
    monkeypatch.setattr(graph_cmd, "resolve_target", lambda name: ("prod", cfg))
    monkeypatch.setattr(graph_cmd, "resolve_bearer_token", lambda target: _SECRET_KEY)
    return cfg


@pytest.fixture
def _stub_fetch(monkeypatch):
    """Stub the remote graph fetch so no network is touched.

    Asserts the bearer token is actually passed to the fetch (through-the-API,
    server-side auth) so we know the key *is* used — just never written out.
    """
    captured: dict = {}

    async def _fake_fetch(*, base_url, token, project, limit, depth):
        captured["token"] = token
        captured["project"] = project
        captured["base_url"] = base_url
        return _SAMPLE_GRAPH

    monkeypatch.setattr(graph_cmd, "_fetch_graph", _fake_fetch)
    return captured


# ---------------------------------------------------------------------------
# Always-on: registration + missing-extra path
# ---------------------------------------------------------------------------


def test_export_command_is_registered():
    result = _invoke(["--help"])
    assert result.exit_code == 0
    assert "export" in result.stdout


def test_missing_pyvis_extra_prints_install_hint_and_exits_clean(monkeypatch, _stub_target):
    """When pyvis is absent the command must hint at the extra and exit 2 —
    not raise a raw ModuleNotFoundError."""
    real_import = builtins.__import__

    def _no_pyvis(name, *args, **kwargs):
        if name == "pyvis" or name.startswith("pyvis."):
            raise ImportError("No module named 'pyvis'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pyvis)

    result = _invoke(["export", "out.html"])
    assert result.exit_code == 2
    assert "graph-pyvis" in result.stdout
    assert "pyvis" in result.stdout


def test_missing_pyvis_fails_fast_before_any_fetch(monkeypatch, _stub_target):
    """The extra check happens before network work — so a missing extra never
    triggers a fetch (or leaks a key over the wire)."""
    real_import = builtins.__import__

    def _no_pyvis(name, *args, **kwargs):
        if name == "pyvis" or name.startswith("pyvis."):
            raise ImportError("nope")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pyvis)

    fetched = {"called": False}

    async def _boom(**kwargs):
        fetched["called"] = True
        return _SAMPLE_GRAPH

    monkeypatch.setattr(graph_cmd, "_fetch_graph", _boom)

    result = _invoke(["export", "out.html"])
    assert result.exit_code == 2
    assert fetched["called"] is False


# ---------------------------------------------------------------------------
# Render tier — requires the real PyVis extra
# ---------------------------------------------------------------------------


@_needs_pyvis
def test_export_generates_standalone_html_file(tmp_path, _stub_target, _stub_fetch):
    out = tmp_path / "kg.html"
    result = _invoke(["export", str(out), "--project", "proj-42"])
    assert result.exit_code == 0, result.stdout
    assert out.is_file()
    html = out.read_text(encoding="utf-8")
    # It's a real HTML doc with the graph nodes embedded.
    assert "<html" in html.lower()
    assert "SAML SSO" in html
    # The token was used for the fetch (server-side auth) ...
    assert _stub_fetch["token"] == _SECRET_KEY
    assert _stub_fetch["project"] == "proj-42"


@_needs_pyvis
def test_exported_html_does_not_contain_bearer_key(tmp_path, _stub_target, _stub_fetch):
    """SECURITY: the artifact is data-only — the bearer key must never appear."""
    out = tmp_path / "kg.html"
    result = _invoke(["export", str(out)])
    assert result.exit_code == 0, result.stdout
    html = out.read_text(encoding="utf-8")
    assert _SECRET_KEY not in html
    assert "Bearer" not in html or _SECRET_KEY not in html
    assert "Authorization" not in html


@_needs_pyvis
def test_exported_html_is_self_contained_inline_resources(tmp_path, _stub_target, _stub_fetch):
    """Air-gapped + shareable: vis.js is inlined (no CDN <script src=...>)."""
    out = tmp_path / "kg.html"
    result = _invoke(["export", str(out)])
    assert result.exit_code == 0, result.stdout
    html = out.read_text(encoding="utf-8")
    # in_line resources inline the vis.js library body into the file.
    assert "vis-network" in html or "vis.min.js" not in html
    # No remote CDN script/style references for the vis assets.
    assert "https://cdnjs.cloudflare.com" not in html
    assert "unpkg.com" not in html


@_needs_pyvis
def test_write_html_uses_inline_cdn_resources(tmp_path, monkeypatch):
    """White-box: assert the PyVis Network is constructed with
    cdn_resources='in_line' (the air-gapped property)."""
    from pyvis.network import Network

    import movate.core.graph.networkx_format as nxf

    captured = {}
    real_init = Network.__init__

    def _spy_init(self, *args, **kwargs):
        captured["cdn_resources"] = kwargs.get("cdn_resources")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(Network, "__init__", _spy_init)

    out = tmp_path / "kg.html"
    graph_cmd._write_html(_SAMPLE_GRAPH, output=out)
    assert captured["cdn_resources"] == "in_line"
    # And the adapter actually ran (file has the node).
    assert "SAML SSO" in out.read_text(encoding="utf-8")
    # Sanity: legend was injected.
    assert "Node types" in out.read_text(encoding="utf-8")
    # Keep the import referenced (adapter is the source of the node attrs).
    assert nxf.graphology_to_networkx({"nodes": [], "edges": []}).number_of_nodes() == 0
