"""``movate doctor`` — environment + configuration sanity check.

Default mode reports on the local environment (Python, deps, provider
keys, tracer, storage). Pass ``--target <name>`` to add an Azure-side
preflight that walks the deploy path (``az`` login → subscription
→ resource group → ACR → Container Apps → ``/healthz``) — the
first thing to run when ``movate deploy`` is acting up.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate import __version__
from movate.providers.pricing import load_pricing
from movate.tracing import build_tracer

console = Console()

_REQUIRED_DEPS = ("typer", "rich", "pydantic", "yaml", "jinja2", "litellm", "aiosqlite")
_OPTIONAL_DEPS = ("langfuse", "opentelemetry", "asyncpg", "fastapi")

# SPDX license per dep. Curated by hand because Python package metadata
# is famously inconsistent — many packages set the license as free-text
# ("MIT License", "BSD-3", etc.) instead of an SPDX ID, so reading
# importlib.metadata would surface inconsistent strings. This map is
# the canonical answer documented in docs/license-posture.md; the
# CI license-gate (when it lands) reads from the SAME table.
#
# Update both this map AND docs/license-posture.md when adding a dep.
_DEP_LICENSES: dict[str, str] = {
    # Required deps
    "typer": "MIT",
    "rich": "MIT",
    "pydantic": "MIT",
    "yaml": "MIT",
    "jinja2": "BSD-3-Clause",
    "litellm": "MIT",
    "aiosqlite": "MIT",
    # Optional deps
    "langfuse": "MIT",
    "opentelemetry": "Apache-2.0",
    "asyncpg": "Apache-2.0",
    "fastapi": "MIT",
    "httpx": "BSD-3-Clause",
}

# Map each AgentRuntime to (probe-module, extras-install-hint). Used by
# the runtime section of ``movate doctor`` to report what's wired vs.
# what's an `uv add 'movate-cli[...]'` away.
_RUNTIME_PROBES = (
    # litellm is always wired (it's a required dep above).
    ("litellm", "litellm", None),
    ("native_anthropic", "anthropic", "anthropic"),
    ("native_openai", "openai", "openai"),
    ("langchain", "langchain_core", "langchain"),
    # lyzr is httpx-only (no SDK dep) — always wired. Probe httpx
    # so the doctor row is still meaningful (it ships as a required
    # dep but we list it for clarity).
    ("lyzr", "httpx", None),
)
_PROVIDER_KEYS = (
    ("OPENAI_API_KEY", "OpenAI"),
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("AZURE_OPENAI_API_KEY", "Azure OpenAI"),
    ("GEMINI_API_KEY", "Gemini"),
    ("LYZR_API_KEY", "Lyzr Studio"),
)
_TRACING_KEYS = (
    ("MOVATE_TRACER", "explicit override"),
    ("LANGFUSE_SECRET_KEY", "Langfuse secret"),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse public"),
    ("LANGFUSE_HOST", "Langfuse host"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTel endpoint"),
    ("OTEL_SERVICE_NAME", "OTel service.name"),
)


def _ok(label: str) -> str:
    return f"[green]ok[/green] [dim]{label}[/dim]" if label else "[green]ok[/green]"


def _missing(label: str) -> str:
    return f"[yellow]missing[/yellow] [dim]{label}[/dim]" if label else "[yellow]missing[/yellow]"


def doctor(  # noqa: PLR0912 — branch count is inherent to a multi-section diagnostic
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Also run the Azure preflight for a registered target "
            "(az login → subscription → RG → ACR → Container Apps → /healthz). "
            "Use this when `movate deploy` is failing."
        ),
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help=(
            "After the doctor table, print a per-check explanation block: "
            "what each check tests, why it matters, what failure means, and "
            "the copy-pasteable fix command. Useful for new operators trying "
            "to interpret a red row."
        ),
    ),
    licenses: bool = typer.Option(
        False,
        "--licenses",
        help=(
            "Print a license report instead of the standard doctor "
            "output: per-dep SPDX license, resale-safety classification, "
            "and a link to docs/license-posture.md. Use this to confirm "
            "a deployment's dep tree is permissively licensed before "
            "embedding in a customer deliverable."
        ),
    ),
) -> None:
    """Report on the local environment, deps, API keys, and movate state.

    With ``--target <name>``, adds a second table walking the Azure
    deploy path so you see the earliest broken link, not a stack trace
    from ``movate deploy``.

    With ``--licenses``, prints a per-dep SPDX license report instead
    of the standard output — useful before shipping a customer
    deliverable that embeds movate-cli.
    """
    if licenses:
        _render_license_report()
        return

    table = Table(title="movate doctor", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("License", style="dim")  # blank for non-dep rows

    table.add_row("Python", sys.version.split()[0], "")
    table.add_row("movate", __version__, "")
    table.add_row("", "", "")

    # Required deps — now with SPDX license column. Every entry in
    # _REQUIRED_DEPS should have a matching entry in _DEP_LICENSES;
    # a missing entry renders as a yellow "?" prompting the operator
    # to update both maps (see docs/license-posture.md).
    for mod in _REQUIRED_DEPS:
        spec = importlib.util.find_spec(mod)
        status = _ok("") if spec else "[red]missing (install fail)[/red]"
        license_str = _DEP_LICENSES.get(mod, "[yellow]?[/yellow]")
        table.add_row(f"dep: {mod}", status, license_str)

    table.add_row("", "", "")

    # Optional deps
    for mod in _OPTIONAL_DEPS:
        spec = importlib.util.find_spec(mod)
        status = _ok("") if spec else _missing("not installed")
        license_str = _DEP_LICENSES.get(mod, "[yellow]?[/yellow]")
        table.add_row(f"opt: {mod}", status, license_str)

    table.add_row("", "")

    # AgentRuntime probes — which `runtime:` values in agent.yaml will
    # actually resolve to an adapter on THIS install? litellm is always
    # available; the rest depend on whether the matching extra is
    # installed (`uv add 'movate-cli[anthropic]'` etc.).
    for runtime_name, probe_module, extra_name in _RUNTIME_PROBES:
        spec = importlib.util.find_spec(probe_module)
        if spec is not None:
            status = _ok("adapter available")
        elif extra_name is not None:
            status = _missing(f"install with: uv add 'movate-cli[{extra_name}]'")
        else:
            # Should not happen — litellm is in _REQUIRED_DEPS — but the
            # branch keeps mypy happy.
            status = "[red]missing[/red]"  # pragma: no cover
        table.add_row(f"runtime: {runtime_name}", status)

    table.add_row("", "")

    # Provider API keys
    any_key = False
    for env_var, label in _PROVIDER_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        any_key = any_key or present
        table.add_row(env_var, _ok(label) if present else _missing(label))

    if not any_key:
        table.add_row(
            "[yellow]hint[/yellow]",
            "[dim]no provider keys set; use --mock for offline runs[/dim]",
        )

    table.add_row("", "")

    # Tracing keys (separate from agent provider keys — easier to scan)
    for env_var, label in _TRACING_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        table.add_row(env_var, _ok(label) if present else _missing(label))

    # Resolved tracer — what `movate run` would actually use right now.
    try:
        tracer = build_tracer()
        table.add_row("resolved tracer", f"[green]{tracer.name}[/green]")
    except Exception as exc:  # pragma: no cover - diagnostic only
        table.add_row("resolved tracer", f"[red]error: {exc}[/red]")

    table.add_row("", "")

    # Storage
    sqlite_path = Path("~/.movate/local.db").expanduser()
    state = "exists" if sqlite_path.exists() else "will be created on first run"
    table.add_row("storage (sqlite)", f"{sqlite_path} [dim]({state})[/dim]")

    # Pricing
    try:
        pricing = load_pricing()
        models = len(pricing.models)
        table.add_row(
            "pricing",
            f"v{pricing.version} ({models} models, last_verified {pricing.last_verified})",
        )
    except Exception as exc:
        table.add_row("pricing", f"[red]load failed: {exc}[/red]")

    # Project config
    project_yaml = Path("movate.yaml")
    table.add_row(
        "movate.yaml",
        f"[green]found[/green] [dim]({project_yaml.resolve()})[/dim]"
        if project_yaml.exists()
        else _missing("not in cwd; defaults will be used"),
    )

    console.print(table)

    # ------------------------------------------------------------------
    # Optional: per-check explanation block when --explain is set
    # ------------------------------------------------------------------
    if explain:
        _render_explanations()

    # ------------------------------------------------------------------
    # Optional: Azure preflight when --target is set
    # ------------------------------------------------------------------
    if target is not None:
        _render_azure_preflight(target)


def _render_explanations() -> None:
    """Print a per-check explanation block beneath the doctor table.

    Renders every check that has an entry in the explanations registry
    (see ``cli/_doctor_explanations.py``) with what it tests, why it
    matters, what failure means, and the copyable fix command. Operators
    new to the stack use this to interpret a red row without diving into
    the codebase.

    Groups output by section heading (deps / runtimes / keys / tracing
    / storage) so a long scroll is still scannable.
    """
    from movate.cli._doctor_explanations import EXPLANATIONS  # noqa: PLC0415

    sections = [
        ("Required dependencies", [k for k in EXPLANATIONS if k.startswith("dep: ")]),
        ("Optional dependencies", [k for k in EXPLANATIONS if k.startswith("opt: ")]),
        ("Runtime adapters", [k for k in EXPLANATIONS if k.startswith("runtime: ")]),
        ("Provider API keys", [k for k in EXPLANATIONS if k.endswith("_API_KEY")]),
        (
            "Tracing",
            [k for k in EXPLANATIONS if k.startswith(("LANGFUSE_", "OTEL_", "MOVATE_TRACER"))],
        ),
        (
            "Storage & project",
            ["storage (sqlite)", "pricing", "movate.yaml"],
        ),
    ]

    console.print()
    console.print("[bold]═══ Check details (--explain) ═══[/bold]")
    console.print()
    for section_title, check_ids in sections:
        if not check_ids:
            continue
        console.print(f"[bold cyan]▸ {section_title}[/bold cyan]")
        console.print()
        for cid in check_ids:
            entry = EXPLANATIONS[cid]
            console.print(f"  [bold]{cid}[/bold]")
            console.print(f"    [dim]WHAT:[/dim]   {entry.what}")
            console.print(f"    [dim]WHY:[/dim]    {entry.why}")
            console.print(f"    [dim]ON FAIL:[/dim] {entry.failure_impact}")
            if entry.fix:
                console.print(f"    [dim]FIX:[/dim]    [green]{entry.fix}[/green]")
            console.print()


# Allowlist of SPDX licenses that are safe to embed in customer
# deliverables without copyleft / source-availability / competing-services
# obligations. Match the same list in docs/license-posture.md. Keep
# additions deliberate — a new entry here is a policy decision.
_LICENSE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "PostgreSQL",
        "PSF-2.0",
        "MIT OR Apache-2.0",
    }
)


def _render_license_report() -> None:
    """Print a per-dep license report.

    Three columns: dep name, SPDX license, resale-safety verdict. A
    license outside :data:`_LICENSE_ALLOWLIST` renders red ("REVIEW") —
    that's the cue to read ``docs/license-posture.md`` and decide
    whether to keep the dep or replace it.

    Today every dep in the codebase is allowlist-safe, so the report
    is all-green. The CI license-gate (when wired) reads from the
    same allowlist constant.
    """
    table = Table(
        title="movate license posture",
        show_header=True,
        header_style="bold",
        caption="See docs/license-posture.md for the full policy.",
        caption_style="dim",
    )
    table.add_column("Dep")
    table.add_column("SPDX license")
    table.add_column("Resale-safe?")

    all_deps = sorted(_DEP_LICENSES.items())
    n_safe = 0
    n_review = 0
    for dep, license_id in all_deps:
        if license_id in _LICENSE_ALLOWLIST:
            verdict = "[green]✓ permissive[/green]"
            n_safe += 1
        else:
            verdict = "[red]REVIEW[/red]"
            n_review += 1
        table.add_row(dep, license_id, verdict)

    console.print(table)
    if n_review:
        console.print(
            f"\n[red]✗ {n_review} dep(s) need review[/red] — "
            "see docs/license-posture.md for the process."
        )
    else:
        console.print(
            f"\n[green]✓ all {n_safe} deps are permissively licensed[/green] "
            "and safe to embed in customer deliverables."
        )


def _render_azure_preflight(target_name: str) -> None:
    """Print a second table with the Azure-side checks. Resolves the
    target first; missing target is itself a finding (operator pointer
    in the error tells them to run `movate config add-target`)."""
    # Local imports — keep the doctor command's hot-path tight; these
    # are only needed when --target is set.
    from movate.cli._azure_doctor import run_azure_preflight  # noqa: PLC0415
    from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

    try:
        target_name_resolved, target_cfg = resolve_target(target_name)
    except UserConfigError as exc:
        console.print(f"\n[red]✗ azure preflight skipped:[/red] {exc}")
        return

    table = Table(
        title=f"azure preflight → {target_name_resolved}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Check")
    table.add_column("Result")

    for check in run_azure_preflight(target_name_resolved, target_cfg):
        if check.status == "ok":
            badge = _ok(check.detail)
        elif check.status == "missing":
            badge = _missing(check.detail)
        else:
            badge = (
                f"[red]error[/red] [dim]{check.detail}[/dim]"
                if check.detail
                else "[red]error[/red]"
            )
        table.add_row(check.name, badge)

    console.print()
    console.print(table)
