"""Sprint R — `mdk export oci-bundle` tests.

Three layers:

1. **Helpers** — _collect_agent_files filters junk + sorts deterministically;
   _sha256 is stable.
2. **Bundle plan + write** — build_plan / write_bundle produce a
   tarball with the expected layout (manifest.yaml, oci-manifest.json,
   agent/ subtree).
3. **CLI** — happy path writes a real bundle; --dry-run doesn't write;
   --force overwrites; missing agent / existing output handled.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.export_oci_cmd import (
    _collect_agent_files,
    _sha256,
    build_plan,
    write_bundle,
)
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold_agent(dst: Path, name: str = "demo") -> Path:
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Project with one agent. No DB needed — export is filesystem-only.

    Deliberately NO movate.yaml — load_project_config rejects unknown
    keys (extra='forbid') and our test only needs the agent dir; the
    loader falls back to defaults when no project config is present.
    """
    _scaffold_agent(tmp_path / "agents" / "demo", name="demo")
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollectAgentFiles:
    def test_includes_agent_yaml_and_prompt(self, project: Path) -> None:
        files = _collect_agent_files(project / "agents" / "demo")
        names = {f.name for f in files}
        assert "agent.yaml" in names
        assert "prompt.md" in names

    def test_skips_pycache(self, project: Path) -> None:
        agent_dir = project / "agents" / "demo"
        pycache = agent_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "compiled.pyc").write_text("junk")
        files = _collect_agent_files(agent_dir)
        # pycache contents filtered out
        assert not any("__pycache__" in str(f) for f in files)

    def test_skips_ds_store(self, project: Path) -> None:
        agent_dir = project / "agents" / "demo"
        (agent_dir / ".DS_Store").write_text("mac cruft")
        files = _collect_agent_files(agent_dir)
        assert not any(f.name == ".DS_Store" for f in files)

    def test_sorted_deterministically(self, project: Path) -> None:
        """Same agent → identical file order across runs (lets us hash
        the bundle reproducibly)."""
        agent_dir = project / "agents" / "demo"
        a = _collect_agent_files(agent_dir)
        b = _collect_agent_files(agent_dir)
        assert a == b


@pytest.mark.unit
def test_sha256_is_stable() -> None:
    """Identical input → identical digest. (Sanity-check ours wraps
    hashlib correctly; this is the building block for reproducible
    bundles.)"""
    assert _sha256(b"hello") == _sha256(b"hello")


# ---------------------------------------------------------------------------
# Bundle plan + write
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_plan_captures_agent_metadata(project: Path) -> None:
    plan = build_plan(agent_dir=project / "agents" / "demo", output=None)
    assert plan.agent_name == "demo"
    assert plan.agent_version  # non-empty (from template)
    assert plan.prompt_hash  # 64-hex
    assert len(plan.files) > 0


@pytest.mark.unit
def test_build_plan_default_output_in_cwd(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default output is <name>-<version>.tar.gz in the cwd."""
    monkeypatch.chdir(tmp_path)
    plan = build_plan(agent_dir=project / "agents" / "demo", output=None)
    assert plan.output_path.name.startswith("demo-")
    assert plan.output_path.name.endswith(".tar.gz")


@pytest.mark.unit
def test_write_bundle_creates_tarball(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "demo-bundle.tar.gz"
    plan = build_plan(agent_dir=project / "agents" / "demo", output=out)
    oci = write_bundle(plan)
    assert out.is_file()
    # OCI manifest has the expected single-layer shape
    assert oci["schemaVersion"] == 2
    assert len(oci["layers"]) == 1
    assert oci["layers"][0]["digest"].startswith("sha256:")


@pytest.mark.unit
def test_bundle_tarball_contains_expected_entries(project: Path, tmp_path: Path) -> None:
    """The outer tar.gz contains manifest.yaml + oci-manifest.json +
    agent/ subtree."""
    out = tmp_path / "demo-bundle.tar.gz"
    plan = build_plan(agent_dir=project / "agents" / "demo", output=out)
    write_bundle(plan)

    with tarfile.open(out, "r:gz") as tf:
        members = tf.getnames()

    assert "manifest.yaml" in members
    assert "oci-manifest.json" in members
    # Agent files are under `agent/`
    agent_files = [m for m in members if m.startswith("agent/")]
    assert any(m == "agent/agent.yaml" for m in agent_files)


@pytest.mark.unit
def test_bundle_manifest_yaml_is_valid(project: Path, tmp_path: Path) -> None:
    """The movate manifest inside the tarball parses + has expected keys."""
    out = tmp_path / "demo-bundle.tar.gz"
    plan = build_plan(agent_dir=project / "agents" / "demo", output=out)
    write_bundle(plan)

    with tarfile.open(out, "r:gz") as tf:
        extracted = tf.extractfile("manifest.yaml")
        assert extracted is not None
        data = yaml.safe_load(extracted.read())
    assert data["api_version"] == "movate/v1"
    assert data["kind"] == "OciBundle"
    assert data["agent_name"] == "demo"
    # File entries have path + size + sha256
    assert len(data["files"]) > 0
    first = data["files"][0]
    assert {"path", "size", "sha256"} <= set(first.keys())


@pytest.mark.unit
def test_bundle_oci_manifest_is_valid_json(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "demo-bundle.tar.gz"
    plan = build_plan(agent_dir=project / "agents" / "demo", output=out)
    write_bundle(plan)
    with tarfile.open(out, "r:gz") as tf:
        extracted = tf.extractfile("oci-manifest.json")
        assert extracted is not None
        manifest = json.loads(extracted.read())
    # Conforms to the OCI Image Manifest schema (loosely)
    assert manifest["schemaVersion"] == 2
    assert manifest["mediaType"].startswith("application/vnd.oci.image.manifest")
    assert manifest["config"]["mediaType"] == "application/vnd.movate.agent.v1+yaml"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_export_writes_bundle(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.tar.gz"
    result = runner.invoke(
        app,
        [
            "export",
            "oci-bundle",
            "demo",
            "--output",
            str(out),
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert out.is_file()


@pytest.mark.unit
def test_cli_export_dry_run_does_not_write(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.tar.gz"
    result = runner.invoke(
        app,
        [
            "export",
            "oci-bundle",
            "demo",
            "--output",
            str(out),
            "--dry-run",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    assert not out.exists()


@pytest.mark.unit
def test_cli_export_refuses_existing_without_force(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.tar.gz"
    out.write_bytes(b"don't lose me")
    result = runner.invoke(
        app,
        [
            "export",
            "oci-bundle",
            "demo",
            "--output",
            str(out),
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2
    assert out.read_bytes() == b"don't lose me"


@pytest.mark.unit
def test_cli_export_force_overwrites(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.tar.gz"
    out.write_bytes(b"old content")
    result = runner.invoke(
        app,
        [
            "export",
            "oci-bundle",
            "demo",
            "--output",
            str(out),
            "--force",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    # File is now a real tarball (not the old "old content" bytes)
    with tarfile.open(out, "r:gz") as tf:
        assert "manifest.yaml" in tf.getnames()


@pytest.mark.unit
def test_cli_export_missing_agent_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        ["export", "oci-bundle", "ghost", "--project-root", str(project)],
    )
    assert result.exit_code == 2
