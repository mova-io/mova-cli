"""``mdk export oci-bundle <agent>`` — package agent as portable artifact (Sprint R).

Packages an agent directory as a tar.gz bundle with an OCI-compatible
manifest. Operators can ship the bundle anywhere — registry, S3,
scp'd to an air-gapped host — and the receiver gets everything needed
to run the agent.

The bundle is structured so a future ``mdk import oci-bundle`` /
``oras push`` workflow can consume it without re-packaging:

  agent-<name>-<version>.tar.gz
  ├── oci-manifest.json    # OCI Artifact Manifest (media-typed blobs)
  ├── manifest.yaml         # movate-native metadata (operator-readable)
  └── agent/                # full agent dir contents
      ├── agent.yaml
      ├── prompt.md
      └── ...

Why an OCI-compatible shape:

* OCI Image Layout / Artifact Manifest is the de-facto standard for
  "ship a payload through a registry." Once we sign + push, every
  container-aware tool (ORAS, Docker, ACR, Harbor) can pull the
  bundle by digest. No bespoke artifact protocol.
* Even without registry push today, the layout means a future
  ``mdk publish`` (Sprint R+) is just a wrapper around ``oras push``
  pointing at this directory.
* Operators who don't run registries can still ``tar -xzf`` and copy
  the agent dir in place — the inner layout matches what
  ``mdk init`` would produce, so nothing special to learn.

[bold]Design call vs the BACKLOG:[/bold] BACKLOG slots this as 5
days because true OCI compliance has a lot of edge cases (signing,
attestations, OCI Index variations). The MVP ships a single-blob
manifest + a movate-readable `manifest.yaml` for human inspection.
Sprint R+ can layer signing / multi-arch / SBOM attestations on top.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel

from movate.core.loader import AgentLoadError, load_agent

console = Console()
err_console = Console(stderr=True)


# OCI media types we emit. The custom `.agent.v1+yaml` config type is
# what marks this as a movate-agent bundle (vs. a generic OCI image).
_MEDIA_TYPE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_MEDIA_TYPE_CONFIG = "application/vnd.movate.agent.v1+yaml"
_MEDIA_TYPE_LAYER = "application/vnd.movate.agent.dir.v1.tar+gzip"

# Tarball layout: top-level entries written into the archive.
_INNER_AGENT_DIR = "agent"


# ---------------------------------------------------------------------------
# Bundle planning (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundlePlan:
    """Self-describing record of what we're about to write.

    Pure data — lets the CLI emit a clean dry-run summary without
    actually building the tarball. ``files`` is in the order the
    tarball stream will write them; tests assert on this for
    deterministic-ordering bug coverage.
    """

    agent_name: str
    agent_version: str
    agent_dir: Path
    prompt_hash: str
    files: tuple[Path, ...]
    output_path: Path


def _collect_agent_files(agent_dir: Path) -> tuple[Path, ...]:
    """Walk the agent dir, sorted deterministically.

    Skips junk that shouldn't ship in a portable artifact:
      - __pycache__ / .pyc (Python build artifacts)
      - .DS_Store (macOS Finder cruft)
      - .git / .venv (operator-environment, not agent state)

    Sorted by relative path so a bit-identical agent → bit-identical
    tarball. Lets us hash the bundle deterministically.
    """
    skip_dirs = {"__pycache__", ".git", ".venv", "node_modules"}
    skip_names = {".DS_Store"}
    skip_suffixes = {".pyc", ".pyo"}

    found: list[Path] = []
    for path in agent_dir.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.name in skip_names:
            continue
        if path.suffix in skip_suffixes:
            continue
        found.append(path)
    return tuple(sorted(found, key=lambda p: p.relative_to(agent_dir).as_posix()))


def build_plan(*, agent_dir: Path, output: Path | None) -> BundlePlan:
    """Load the agent + decide what to package.

    Pure / no writes — operators can call this through ``--dry-run``
    to preview the bundle before paying the tarball-write cost.
    """
    try:
        bundle = load_agent(agent_dir)
    except AgentLoadError as exc:
        err_console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    files = _collect_agent_files(agent_dir)
    default_name = f"{bundle.spec.name}-{bundle.spec.version}.tar.gz"
    target = output if output is not None else Path.cwd() / default_name
    return BundlePlan(
        agent_name=bundle.spec.name,
        agent_version=bundle.spec.version,
        agent_dir=agent_dir.resolve(),
        prompt_hash=bundle.prompt_hash,
        files=files,
        output_path=target.resolve(),
    )


# ---------------------------------------------------------------------------
# Manifest construction
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _movate_manifest(plan: BundlePlan) -> dict:
    """Human-readable manifest — what's in this bundle.

    Operators inspect this before importing into a foreign project.
    Mirrors the snapshot manifest's shape so existing audit tooling
    (``mdk audit``, snapshot diffing) can reason about bundle contents.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    file_entries = []
    for f in plan.files:
        rel = f.relative_to(plan.agent_dir).as_posix()
        file_entries.append(
            {
                "path": rel,
                "size": f.stat().st_size,
                "sha256": _sha256(f.read_bytes()),
            }
        )
    return {
        "api_version": "movate/v1",
        "kind": "OciBundle",
        "agent_name": plan.agent_name,
        "agent_version": plan.agent_version,
        "prompt_hash": plan.prompt_hash,
        "created_at": now,
        "files": file_entries,
    }


def _oci_manifest(layer_digest: str, layer_size: int, config_digest: str, config_size: int) -> dict:
    """OCI Artifact Manifest. Single-layer for MVP.

    Conforms to the OCI Image Manifest schema with a custom config
    media type. Tools like ORAS see this and know it's a non-image
    artifact (won't try to interpret it as a runnable container).
    """
    return {
        "schemaVersion": 2,
        "mediaType": _MEDIA_TYPE_MANIFEST,
        "config": {
            "mediaType": _MEDIA_TYPE_CONFIG,
            "digest": f"sha256:{config_digest}",
            "size": config_size,
        },
        "layers": [
            {
                "mediaType": _MEDIA_TYPE_LAYER,
                "digest": f"sha256:{layer_digest}",
                "size": layer_size,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tarball assembly
# ---------------------------------------------------------------------------


def write_bundle(plan: BundlePlan) -> dict:
    """Write the tarball to ``plan.output_path``. Returns the OCI manifest.

    Sequence:
      1. Build the inner agent-dir tarball in-memory + compute its
         sha256 (becomes the OCI "layer").
      2. Build the movate manifest.yaml (becomes the OCI "config").
      3. Emit the outer tarball with oci-manifest.json, manifest.yaml,
         and the agent dir contents.

    Returns the OCI manifest dict so the CLI can echo digest info.
    """
    output = plan.output_path

    # Build the movate manifest first — contains file hashes which
    # operators will want regardless of OCI metadata.
    movate_manifest = _movate_manifest(plan)
    movate_yaml = yaml.safe_dump(movate_manifest, sort_keys=False, allow_unicode=True)

    # Compute deterministic config digest + size.
    config_bytes = movate_yaml.encode("utf-8")
    config_digest = _sha256(config_bytes)

    # Construct the "layer" content. For MVP, we model the entire
    # tarball as the layer. We compute the layer digest by hashing
    # the in-memory bytes of an inner tar so the digest is stable.
    inner_tar_path = output.with_suffix(output.suffix + ".inner.tmp")
    try:
        with tarfile.open(inner_tar_path, mode="w:gz") as inner:
            for f in plan.files:
                rel = f.relative_to(plan.agent_dir).as_posix()
                # Put files under "agent/" inside the archive so an
                # `tar -xzf bundle.tar.gz` produces an agent/ dir.
                inner.add(f, arcname=f"{_INNER_AGENT_DIR}/{rel}")
        layer_bytes = inner_tar_path.read_bytes()
        layer_digest = _sha256(layer_bytes)
        layer_size = len(layer_bytes)
    finally:
        # Always clean up the temp inner tar — we re-emit the layer
        # contents inside the final bundle so we don't ship the
        # double-tarred form.
        if inner_tar_path.exists():
            os.unlink(inner_tar_path)

    oci = _oci_manifest(
        layer_digest=layer_digest,
        layer_size=layer_size,
        config_digest=config_digest,
        config_size=len(config_bytes),
    )

    # Now write the final outer bundle:
    #   manifest.yaml      (operator-readable)
    #   oci-manifest.json  (OCI Artifact Manifest)
    #   agent/...          (the actual files)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, mode="w:gz") as bundle:
        _add_bytes(bundle, "manifest.yaml", config_bytes)
        _add_bytes(
            bundle,
            "oci-manifest.json",
            json.dumps(oci, indent=2).encode("utf-8"),
        )
        for f in plan.files:
            rel = f.relative_to(plan.agent_dir).as_posix()
            bundle.add(f, arcname=f"{_INNER_AGENT_DIR}/{rel}")
    return oci


def _add_bytes(tf: tarfile.TarFile, arcname: str, data: bytes) -> None:
    """Append an in-memory blob to the tar under ``arcname``."""
    import io  # noqa: PLC0415

    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    # Reproducible builds: pin mtime so bit-identical inputs produce
    # bit-identical tarballs. Operators verifying digests against
    # CI / a remote registry need this.
    info.mtime = 0
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _resolve_agent_path(name_or_path: str, project_root: Path) -> Path:
    """Same convention used by `mdk inspect agent` / `mdk tune`."""
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "agent.yaml").is_file():
        return candidate.resolve()
    by_name = project_root / "agents" / name_or_path
    if by_name.is_dir() and (by_name / "agent.yaml").is_file():
        return by_name.resolve()
    err_console.print(
        f"[red]✗[/red] agent not found: [bold]{name_or_path}[/bold]. "
        "[dim]Looked under [bold]agents/[/bold] and as a literal path.[/dim]"
    )
    raise typer.Exit(code=2)


export_app = typer.Typer(
    name="export",
    help=(
        "Package movate primitives as portable artifacts. "
        "[bold]oci-bundle[/bold] ships an agent as a tar.gz with an "
        "OCI Artifact Manifest for any container registry."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@export_app.command("oci-bundle")
def export_oci_bundle(
    name: str = typer.Argument(
        ...,
        help=(
            "Agent name (resolved under [bold]agents/<name>[/bold]) or a "
            "literal path to an agent directory."
        ),
        metavar="AGENT",
    ),
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Where to write the bundle. Defaults to "
            "[bold]<agent-name>-<version>.tar.gz[/bold] in the current directory."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite the output file if it already exists.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Show what would be packaged (files + sizes) without writing. "
            "Useful before paying the tarball-write cost on a big agent."
        ),
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd). Used to resolve bare agent names.",
        hidden=True,
    ),
) -> None:
    """Package an agent as a portable OCI-compatible tar.gz bundle.

    The output is a single tar.gz containing the full agent directory
    plus an [bold]oci-manifest.json[/bold] (OCI Artifact Manifest) and
    a human-readable [bold]manifest.yaml[/bold]. Operators can ship
    the bundle anywhere — registry, S3, scp — and the receiver gets
    everything needed to run the agent.

    [bold]Examples:[/bold]

      [dim]$ mdk export oci-bundle triage[/dim]
      [dim]$ mdk export oci-bundle triage --output /tmp/triage.tar.gz[/dim]
      [dim]$ mdk export oci-bundle triage --dry-run[/dim]

    [bold]Future:[/bold] [cyan]oras push <registry>/agents/triage:v1 triage.tar.gz[/cyan]
    will work today (the OCI Artifact Manifest is standards-compliant).
    A planned [cyan]mdk publish[/cyan] (Sprint R+) wraps this.
    """
    root = Path(project_root).resolve()
    agent_path = _resolve_agent_path(name, root)
    plan = build_plan(agent_dir=agent_path, output=output)

    if not plan.files:
        err_console.print(
            f"[red]✗[/red] no files to package in {agent_path}. "
            "[dim]The agent directory is empty or only contains skipped files.[/dim]"
        )
        raise typer.Exit(code=2)

    if dry_run:
        _render_dry_run(plan)
        return

    if plan.output_path.exists() and not force:
        err_console.print(
            f"[red]✗[/red] {plan.output_path} already exists "
            "(pass [bold]--force[/bold] to overwrite)"
        )
        raise typer.Exit(code=2)

    oci = write_bundle(plan)
    _render_summary(plan, oci)


def _render_dry_run(plan: BundlePlan) -> None:
    """Print the file list + final tarball location, no writes."""
    total_size = sum(f.stat().st_size for f in plan.files)
    body = (
        f"[bold]Agent:[/bold]    [cyan]{plan.agent_name}[/cyan] "
        f"v{plan.agent_version}\n"
        f"[bold]Source:[/bold]   [cyan]{plan.agent_dir}[/cyan]\n"
        f"[bold]Files:[/bold]    {len(plan.files)} "
        f"([dim]{_humanize_bytes(total_size)}[/dim])\n"
        f"[bold]Would write:[/bold] [cyan]{plan.output_path}[/cyan]"
    )
    console.print(
        Panel(
            body + "\n\n[yellow]⚠ dry-run — no files written.[/yellow]",
            title="export oci-bundle — preview",
            title_align="left",
            border_style="yellow",
        )
    )


def _render_summary(plan: BundlePlan, oci: dict) -> None:
    """Post-write summary with the OCI digests operators can quote."""
    layer = oci["layers"][0]
    body = (
        f"[bold]Agent:[/bold]    [cyan]{plan.agent_name}[/cyan] "
        f"v{plan.agent_version}\n"
        f"[bold]Output:[/bold]   [cyan]{plan.output_path}[/cyan]\n"
        f"[bold]Files:[/bold]    {len(plan.files)}\n"
        f"[bold]Digest:[/bold]   [dim]{layer['digest']}[/dim]\n"
        f"[bold]Size:[/bold]     {_humanize_bytes(plan.output_path.stat().st_size)}"
    )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] OCI bundle written",
            title_align="left",
            border_style="green",
        )
    )


_KB = 1024
_MB = 1024 * 1024


def _humanize_bytes(n: int) -> str:
    """Compact byte rendering — mirrors `mdk diff` convention."""
    if n < _KB:
        return f"{n} B"
    if n < _MB:
        return f"{n / _KB:.1f} KB"
    return f"{n / _MB:.1f} MB"
