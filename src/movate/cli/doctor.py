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
import shutil
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from movate import __version__
from movate.providers.pricing import load_pricing
from movate.tracing import build_tracer


def _ocr_install_hint(extra: str) -> str:
    """Return the right install command for an OCR extra.

    Two contexts:

    * **Source-repo developer** (``pyproject.toml`` in cwd is movate-cli
      itself) — ``uv add 'movate-cli[ocr]'`` fails with the self-dependency
      error. Use ``uv sync --extra <extra>`` instead.
    * **Operator project** (movate-cli installed as an external dep or tool)
      — ``uv add 'movate-cli[<extra>]'`` is the right command.

    Detection: look for a ``pyproject.toml`` in cwd that declares
    ``name = "movate-cli"`` (the source repo's own manifest).
    """
    try:
        toml_path = Path("pyproject.toml")
        if toml_path.is_file() and 'name = "movate-cli"' in toml_path.read_text():
            return f"uv sync --extra {extra}"
    except OSError:
        pass
    return f"uv add 'movate-cli[{extra}]'"

console = Console()

_REQUIRED_DEPS = ("typer", "rich", "pydantic", "yaml", "jinja2", "litellm", "aiosqlite")
_OPTIONAL_DEPS = ("langfuse", "opentelemetry", "asyncpg", "fastapi")

# KB parsing deps — required for document ingestion pipelines.
# Listed separately from _REQUIRED_DEPS because they're KB-specific and
# installed as part of the movate-cli[kb] extra (or the full install).
# pypdf, docx (python-docx), and bs4 (beautifulsoup4) cover PDF, DOCX,
# and HTML ingest respectively. Operators without these see "missing" but
# the core agent runtime still works — they just can't run mdk kb ingest.
_KB_DEPS: tuple[tuple[str, str], ...] = (
    # (probe_module, display_name) — display_name used as the "kb: X" label.
    # These are CORE deps (always installed with movate-cli). If they show as
    # missing the install is broken — the fix is a full reinstall, not an extra.
    ("pypdf", "pypdf"),
    ("docx", "python-docx"),
    ("bs4", "beautifulsoup4"),
)

# OCR deps — optional [ocr] and [easyocr] extras, plus Pillow for
# standalone image files (PNG/JPG/TIFF).
#
# Each entry is (probe_module, display_name, install_hint) so the
# doctor table can point operators at the right `uv add` command per dep:
#
#   [ocr]      — Pillow (image decoder) + pdf2image (Poppler rasterizer)
#                + pytesseract (Tesseract Python wrapper)
#   [easyocr]  — EasyOCR (pure-Python OCR; no system binary required)
#
# All four feed the same _ocr_tesseract / _ocr_easyocr dispatcher in
# movate.kb.parsers. The Tesseract system-binary check is done separately
# via shutil.which (it's not a Python package).
_OCR_DEPS: tuple[tuple[str, str, str], ...] = (
    # Pillow — decodes PNG/JPG/TIFF/BMP → PIL.Image before OCR.
    # Required for standalone image KB files; also used inside the PDF OCR
    # path once pdf2image rasterizes a page.
    # Install hint is resolved at runtime by _ocr_install_hint() so it shows
    # the right command whether you're inside the source repo or an operator
    # project (uv sync --extra vs uv add 'movate-cli[...]').
    ("PIL", "pillow", "ocr"),
    # pdf2image — wraps Poppler's `pdftoppm` to rasterize PDF pages.
    # Required only for scanned / mixed PDFs (text PDFs parsed by pypdf).
    ("pdf2image", "pdf2image", "ocr"),
    # pytesseract — thin Python wrapper around the Tesseract binary.
    # `MOVATE_OCR_BACKEND=tesseract` (the default) uses this path.
    ("pytesseract", "pytesseract", "ocr"),
    # EasyOCR — pure-Python OCR (torch-based); no system binary needed.
    # `MOVATE_OCR_BACKEND=easyocr` uses this path. Better on noisy scans,
    # handwriting, and non-Latin scripts. GPU-optional (MOVATE_EASYOCR_GPU=1).
    ("easyocr", "easyocr", "easyocr"),
)

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
    # KB parsing deps
    "pypdf": "BSD-3-Clause",
    "python-docx": "MIT",
    "beautifulsoup4": "MIT",
    # OCR deps ([ocr] extra — Pillow + pdf2image + pytesseract)
    "pillow": "HPND",          # Historical Permission Notice and Disclaimer (permissive)
    "pdf2image": "MIT",
    "pytesseract": "Apache-2.0",
    "tesseract": "Apache-2.0",  # system binary
    # EasyOCR dep ([easyocr] extra — alternative pure-Python OCR backend)
    "easyocr": "Apache-2.0",
}

# One-line role description per dep — surfaced in ``mdk doctor`` so
# operators glancing at the table can answer "wait, why do we depend
# on this?" without leaving the terminal. Keep entries terse (≤50
# chars) — Rich wraps but a one-liner reads cleanly. Detailed defense
# of each choice lives in docs/stack-defense.md.
_DEP_PURPOSE: dict[str, str] = {
    # Required deps
    "typer": "CLI argument parsing",
    "rich": "Terminal tables + colors",
    "pydantic": "Schema validation + parsing",
    "yaml": "YAML config parsing (agent.yaml, movate.yaml)",
    "jinja2": "Prompt templating",
    "litellm": "Multi-provider LLM SDK",
    "aiosqlite": "Async sqlite driver (local storage)",
    # Optional deps
    "langfuse": "LLM trace + cost observability",
    "opentelemetry": "OTel spans for distributed tracing",
    "asyncpg": "Async Postgres driver (deployed runtime)",
    "fastapi": "HTTP runtime + Teams bot webhook",
    "httpx": "Async HTTP client (LiteLLM, Lyzr, etc.)",
    # KB parsing deps
    "pypdf": "PDF text extraction (mdk kb ingest)",
    "python-docx": "DOCX text extraction (mdk kb ingest)",
    "beautifulsoup4": "HTML text extraction (mdk kb ingest)",
    # OCR deps — [ocr] extra (Pillow + pdf2image + pytesseract)
    "pillow": "Decode PNG/JPG/TIFF/BMP KB files → OCR-ready PIL.Image",
    "pdf2image": "PDF→image rasterizer for scanned PDFs (Poppler)",
    "pytesseract": "Python wrapper for Tesseract OCR engine",
    "tesseract": "Tesseract OCR engine binary (system install)",
    # EasyOCR dep — [easyocr] extra
    "easyocr": "Pure-Python OCR backend; no system binary, GPU-optional",
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
    no_fix_prompt: bool = typer.Option(
        False,
        "--no-fix-prompt",
        help=(
            "Skip the interactive 'run mdk fix?' prompt that fires when "
            "doctor surfaces fixable issues. Useful for CI runs that "
            "don't want a hanging prompt."
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
    # Purpose sits LEFT of License so operators reading top-to-bottom hit
    # the "what is this for?" answer before the "is it permissive?"
    # answer — most operators only care about license posture during the
    # `--licenses` audit, but want to recognize what a dep does at a
    # glance every time they run doctor. Blank for non-dep rows.
    table.add_column("Purpose", style="dim", overflow="fold")
    table.add_column("License", style="dim")

    # Tally check statuses as we build the table so the greppable
    # summary line at the bottom can report counts without scraping
    # internal Rich row state. ``_classify_result`` turns the markup
    # string into one of {"ok", "missing", "error"} or returns None
    # for non-check rows (section headers, raw values like Python
    # version, blank separators).
    counts = {"ok": 0, "missing": 0, "error": 0}

    def _add(check: str, result: str, *extra: str) -> None:
        table.add_row(check, result, *extra)
        kind = _classify_result(check, result)
        if kind is not None:
            counts[kind] += 1

    _add("Python", sys.version.split()[0], "", "")
    _add("movate", __version__, "", "")
    _add("", "", "", "")

    # Required deps — Purpose + SPDX license columns. Every entry in
    # _REQUIRED_DEPS should have a matching entry in both _DEP_LICENSES
    # and _DEP_PURPOSE; a missing entry renders as a yellow "?"
    # prompting the operator to update the maps (see
    # docs/license-posture.md and docs/stack-defense.md).
    for mod in _REQUIRED_DEPS:
        spec = importlib.util.find_spec(mod)
        status = _ok("") if spec else "[red]missing (install fail)[/red]"
        purpose = _DEP_PURPOSE.get(mod, "[yellow]?[/yellow]")
        license_str = _DEP_LICENSES.get(mod, "[yellow]?[/yellow]")
        _add(f"dep: {mod}", status, purpose, license_str)

    _add("", "", "", "")

    # Optional deps
    for mod in _OPTIONAL_DEPS:
        spec = importlib.util.find_spec(mod)
        status = _ok("") if spec else _missing("not installed")
        purpose = _DEP_PURPOSE.get(mod, "[yellow]?[/yellow]")
        license_str = _DEP_LICENSES.get(mod, "[yellow]?[/yellow]")
        _add(f"opt: {mod}", status, purpose, license_str)

    _add("", "", "", "")

    # KB parsing deps — PDF, DOCX, HTML ingest for `mdk kb ingest`.
    # These are CORE deps (always installed); missing = broken install.
    # Probe by the importable module name; display uses the canonical
    # PyPI package name for recognition.
    for probe_mod, display_name in _KB_DEPS:
        spec = importlib.util.find_spec(probe_mod)
        # Core deps: missing means the whole install is broken, not just
        # one extra. Point at a full reinstall rather than a specific package.
        status = _ok("") if spec else _missing("reinstall: uv tool install movate-cli --force")
        purpose = _DEP_PURPOSE.get(display_name, "[yellow]?[/yellow]")
        license_str = _DEP_LICENSES.get(display_name, "[yellow]?[/yellow]")
        _add(f"kb: {display_name}", status, purpose, license_str)

    _add("", "", "", "")

    # OCR deps — Pillow (image decoder) + [ocr] extra (pdf2image +
    # pytesseract) + [easyocr] extra (EasyOCR) — plus the Tesseract
    # system binary.
    #
    # The three Python extras gate three different document-type paths:
    #   Pillow       → standalone image files (PNG/JPG/TIFF/BMP)
    #   pdf2image    → scanned / mixed PDFs (rasterize → OCR)
    #   pytesseract  → MOVATE_OCR_BACKEND=tesseract (default)
    #   easyocr      → MOVATE_OCR_BACKEND=easyocr (no system binary)
    #
    # All paths converge at movate.kb.parsers; install whichever combo
    # matches your document mix. Full pipeline: all four + tesseract binary.
    for probe_mod, display_name, extra_name in _OCR_DEPS:
        spec = importlib.util.find_spec(probe_mod)
        # _ocr_install_hint detects whether we're in the source repo (→ uv sync
        # --extra) or an operator project (→ uv add 'movate-cli[extra]') so the
        # fix command always works regardless of where `mdk doctor` is run from.
        status = _ok("") if spec else _missing(_ocr_install_hint(extra_name))
        purpose = _DEP_PURPOSE.get(display_name, "[yellow]?[/yellow]")
        license_str = _DEP_LICENSES.get(display_name, "[yellow]?[/yellow]")
        _add(f"ocr: {display_name}", status, purpose, license_str)

    # System binary: Tesseract engine.  Neither pdf2image nor pytesseract
    # bundles the Tesseract binary — it must be installed separately.
    # shutil.which() finds it if it's on PATH.
    tess_bin = shutil.which("tesseract")
    if tess_bin:
        try:
            import subprocess  # noqa: PLC0415

            ver_out = subprocess.check_output(
                ["tesseract", "--version"],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            )
            tess_ver = ver_out.splitlines()[0] if ver_out else "found"
        except Exception:
            tess_ver = "found"
        tess_status = _ok(tess_ver)
    else:
        tess_status = _missing("brew install tesseract  /  apt-get install tesseract-ocr")
    _add(
        "ocr: tesseract",
        tess_status,
        _DEP_PURPOSE.get("tesseract", ""),
        _DEP_LICENSES.get("tesseract", "Apache-2.0"),
    )

    _add("", "")

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
        _add(f"runtime: {runtime_name}", status)

    _add("", "")

    # Provider API keys
    any_key = False
    for env_var, label in _PROVIDER_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        any_key = any_key or present
        _add(env_var, _ok(label) if present else _missing(label))

    if not any_key:
        _add(
            "[yellow]hint[/yellow]",
            "[dim]no provider keys set; use --mock for offline runs[/dim]",
        )

    _add("", "")

    # Tracing keys (separate from agent provider keys — easier to scan)
    for env_var, label in _TRACING_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        _add(env_var, _ok(label) if present else _missing(label))

    # Resolved tracer — what `movate run` would actually use right now.
    try:
        tracer = build_tracer()
        _add("resolved tracer", f"[green]{tracer.name}[/green]")
    except Exception as exc:  # pragma: no cover - diagnostic only
        _add("resolved tracer", f"[red]error: {exc}[/red]")

    _add("", "")

    # Storage
    sqlite_path = Path("~/.movate/local.db").expanduser()
    state = "exists" if sqlite_path.exists() else "will be created on first run"
    _add("storage (sqlite)", f"{sqlite_path} [dim]({state})[/dim]")

    # Memory store — shows the active backend and file path so operators
    # know which backend their `mdk memory` commands are reading/writing.
    mem_backend = os.environ.get("MOVATE_MEMORY_BACKEND", "memory").lower()
    if mem_backend == "sqlite":
        mem_label = "sqlite (MOVATE_MEMORY_BACKEND=sqlite)"
        mem_file_default = "~/.movate/memory.db"
    else:
        mem_label = "json-file (default)"
        mem_file_default = "~/.movate/memory.json"
    mem_file_env = os.environ.get("MOVATE_MEMORY_FILE", "")
    mem_file = mem_file_env if mem_file_env else mem_file_default
    _add(
        "memory store",
        f"{mem_label} [dim]→ {mem_file}[/dim]",
    )

    # Pricing
    try:
        pricing = load_pricing()
        models = len(pricing.models)
        _add(
            "pricing",
            f"v{pricing.version} ({models} models, last_verified {pricing.last_verified})",
        )
    except Exception as exc:
        _add("pricing", f"[red]load failed: {exc}[/red]")

    # Project config — recognizes all 3 accepted filenames + reports
    # which is canonical. Catches the "I migrated to project.yaml but
    # forgot to delete movate.yaml" footgun.
    project_yaml = _detect_project_config_row(_add)

    # Project layout directories. The May-2026 MVP scaffold ships
    # `agents/`, `skills/`, `contexts/`, `kb/`. Their absence isn't
    # always an error (operator may not need every subdir yet), but
    # surface them so the operator knows what's there + what isn't.
    if project_yaml is not None:
        # We're in a project — check each conventional subdir.
        project_root = project_yaml.parent
        for subdir, what_for in (
            ("agents", "agent definitions"),
            ("skills", "reusable skill defs (`skill.yaml` + impl.py)"),
            ("contexts", "reusable Markdown contexts"),
            ("kb", "knowledge assets (corpora, docs)"),
        ):
            sub_path = project_root / subdir
            if sub_path.is_dir():
                _add(
                    f"{subdir}/",
                    _ok("present") + f" [dim]({what_for})[/dim]",
                )
            else:
                _add(
                    f"{subdir}/",
                    _missing("not in project") + f" [dim]({what_for})[/dim]",
                )

        # Project config parses as ProjectConfig — catches malformed
        # YAML or schema drift (e.g. an unknown top-level field after
        # a manual edit gone wrong).
        _add_project_yaml_parse_check(_add, project_root)

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

    # Greppable single-line summary at the very end. Mirrors audit /
    # eval so CI tooling has one consistent prefix across all three
    # diagnostic commands. Counts are tallied during row adds (see
    # ``_add`` above) — no internal Rich state scraping.
    _print_doctor_summary_line(counts)

    # Empty-project hint: inside a movate project with zero agents,
    # point the operator at the natural next step. Catches new users
    # who run `mdk init --project` followed by `mdk doctor` and don't
    # see anything actionable in the table.
    if target is None and project_yaml is not None and project_yaml.exists():
        _maybe_offer_empty_project_hint(project_yaml.parent.resolve())

    # Interactive handoff to `mdk fix`. Closes the diagnose→fix loop
    # without making operators re-read `mdk fix --list` themselves.
    # Gated on: TTY (not CI / log capture), no --target (Azure preflight
    # context is a separate concern), and at least one fixable issue.
    if target is None and (counts["missing"] > 0 or counts["error"] > 0):
        _maybe_offer_fix(no_prompt=no_fix_prompt)


def _detect_project_config_row(
    _add: Any,
) -> Path | None:
    """Render the project-config row (one of project.yaml /
    policy.yaml / movate.yaml) + warn on the multi-file footgun.

    Returns the Path to the *first found* config file (used by the
    caller to walk project-root subdirs). Returns None if no config
    file is present — operator is outside a project; defaults apply.

    Rich semantics:

    * Exactly one canonical name → `[green]found[/green] (path)`.
    * Canonical + legacy both present → `[yellow]found[/yellow]` on
      the canonical with a "also legacy: ..." hint. Operator should
      delete the legacy file to avoid confusion.
    * Only a legacy name present → `[yellow]found (legacy)[/yellow]`
      with the rename suggestion.
    """
    from movate.core.config import PROJECT_MARKER_FILES  # noqa: PLC0415

    found = [Path(name) for name in PROJECT_MARKER_FILES if Path(name).exists()]
    if not found:
        _add(
            "project config",
            _missing("not in cwd; defaults will be used"),
        )
        return None

    # First entry of PROJECT_MARKER_FILES is the canonical name.
    canonical_name = PROJECT_MARKER_FILES[0]
    primary = found[0]
    extras = found[1:]

    if primary.name == canonical_name and not extras:
        _add(
            primary.name,
            f"[green]found[/green] [dim]({primary.resolve()})[/dim]",
        )
    elif primary.name == canonical_name and extras:
        extras_str = ", ".join(e.name for e in extras)
        _add(
            primary.name,
            f"[yellow]found[/yellow] [dim]({primary.resolve()}); "
            f"also legacy: {extras_str} — delete to avoid confusion[/dim]",
        )
    else:
        # Only legacy file(s) present — primary IS a legacy file.
        _add(
            primary.name,
            f"[yellow]found (legacy)[/yellow] [dim]({primary.resolve()}) — "
            f"rename to `{canonical_name}` (legacy still loads through "
            f"v1.x with a deprecation warning)[/dim]",
        )
    return primary


def _add_project_yaml_parse_check(_add: Any, project_root: Path) -> None:
    """Validate the project's config file parses as ProjectConfig.

    Catches: malformed YAML, unknown fields the schema rejects (after
    the May-2026 `extra="forbid"` flip on ProjectConfig), bad value
    types. A green row means `mdk validate` / `mdk eval` / `mdk add`
    won't trip on the project config at runtime.
    """
    from movate.core.config import load_project_config  # noqa: PLC0415

    try:
        # load_project_config walks for the right file relative to cwd;
        # we expect cwd == project_root in the doctor flow.
        cfg = load_project_config()
        _add(
            "project config parses",
            _ok("valid") + f" [dim](agents_dir={cfg.agents_dir}, "
            f"skills_dir={cfg.skills_dir}, contexts_dir={cfg.contexts_dir})[/dim]",
        )
    except Exception as exc:
        # Truncate so a long pydantic ValidationError doesn't blow up
        # the table rendering. Operator runs `mdk validate` for the
        # full error.
        snippet = str(exc).splitlines()[0][:100]
        _add(
            "project config parses",
            f"[red]invalid[/red] [dim]({snippet}; "
            f"run [bold]mdk validate[/bold] for the full error)[/dim]",
        )


def _maybe_offer_empty_project_hint(project_root: Path) -> None:
    """Surface a 'no agents yet — add some' hint for new projects.

    Pure UX nudge: when the project root has no agents/ at all OR has
    an empty agents/ directory, we print a dim Rich line pointing at
    `mdk add --list`. Doesn't fire if there are already agents — that
    would be noise for the common case.
    """
    agents_dir = project_root / "agents"
    if not agents_dir.is_dir():
        return  # Old project layout — skip silently rather than confuse.
    # `.gitkeep` is the standard placeholder we drop on init; ignore
    # it when counting "real" agent dirs.
    real_agents = [p for p in agents_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if real_agents:
        return
    console.print()
    console.print(
        "[dim]→ no agents yet in [bold]agents/[/bold]. "
        "Run [bold]mdk add --list[/bold] to browse role templates, "
        "or [bold]mdk add rag-qa ticket-triager[/bold] to bootstrap a "
        "support workspace.[/dim]"
    )


def _maybe_offer_fix(*, no_prompt: bool) -> None:
    """If `mdk fix` would do something useful, offer to run it.

    Runs the registry in dry-run mode first to find out what's fixable
    here-and-now, then asks the operator whether to commit. Skips the
    prompt entirely when stdin isn't a TTY (CI / log capture) or when
    --no-fix-prompt was set.

    Failures inside this helper never raise — the diagnose path is the
    primary value of `mdk doctor`; the interactive handoff is a
    convenience layer on top.
    """
    # Local imports keep the cold-path doctor module free of these
    # heavyweight subsystems. Most invocations don't fire this code.
    import sys  # noqa: PLC0415

    from movate.fixes.registry import diagnose_and_fix  # noqa: PLC0415

    # Probe what's fixable from this project root.
    try:
        results = diagnose_and_fix(Path.cwd(), dry_run=True)
    except Exception as exc:  # pragma: no cover - defensive
        console.print(f"[dim]→ skipped: fix probe failed: {exc}[/dim]")
        return

    # Only count fixes that would actually do something. "would_apply"
    # status means dry-run saw a change that would be made; "not_needed"
    # means no work needed.
    fixable = [r for r in results if r.status.value == "would_apply"]
    if not fixable:
        return

    # Surface what fix would do — operators want to know before saying yes.
    console.print()
    console.print(
        f"[yellow]⚠[/yellow] [bold]mdk fix[/bold] can auto-resolve "
        f"[bold]{len(fixable)}[/bold] of the issue(s) above:"
    )
    for r in fixable:
        message = r.message or r.fix_id
        console.print(f"  [cyan]→[/cyan] [dim]{r.fix_id}[/dim]: {message}")
    console.print()

    # Skip the prompt path when there's no TTY or operator opted out.
    if no_prompt or not sys.stdin.isatty():
        console.print(
            "[dim]To apply: run [bold]mdk fix --apply[/bold]. "
            "Suppress this prompt with [bold]--no-fix-prompt[/bold].[/dim]"
        )
        return

    # Prompt + dispatch. typer.confirm handles the TTY interaction and
    # returns False on Ctrl-C / EOF / "n".
    try:
        run_now = typer.confirm("Run `mdk fix --apply` now?", default=False)
    except typer.Abort:
        run_now = False

    if not run_now:
        console.print("[dim]→ skipped. Run [bold]mdk fix --apply[/bold] when ready.[/dim]")
        return

    # Re-run the same registry, this time committing. We don't call the
    # CLI command — that would re-render its own Panel etc. Just run
    # the registry directly and surface the results.
    apply_results = diagnose_and_fix(Path.cwd(), dry_run=False)
    applied = [r for r in apply_results if r.status.value == "applied"]
    failed = [r for r in apply_results if r.status.value == "failed"]
    if applied:
        console.print(
            f"[green]✓[/green] applied {len(applied)} fix(es). "
            "Re-run [bold]mdk doctor[/bold] to confirm."
        )
    if failed:
        for r in failed:
            console.print(f"[red]✗[/red] {r.fix_id} failed: {r.message or 'no detail'}")


def _classify_result(check: str, result: str) -> str | None:
    """Bucket a doctor table row into ``ok`` / ``missing`` / ``error``.

    Returns ``None`` for non-check rows — section separators (empty
    check + empty result) and informational rows (Python version,
    movate version, sqlite path, pricing version, hint). The summary
    line only counts rows that represent a pass/fail signal.
    """
    # Section separators contribute nothing.
    if not check and not result:
        return None
    # Informational rows have a label but report a raw value, not an
    # ok/missing/error verdict — exclude them so the summary line
    # reflects pass/fail signal density, not row count.
    if check in {"Python", "movate", "storage (sqlite)", "pricing", "[yellow]hint[/yellow]"}:
        return None
    lower = result.lower()
    # Order matters: "missing (install fail)" is RED — required dep
    # absent — and must classify as ``error``, not ``missing``,
    # otherwise CI dashboards under-count broken installs.
    if "error" in lower or "missing (install fail)" in lower or "load failed" in lower:
        return "error"
    if "[green]ok" in lower or "[green]found" in lower or "[green]" in lower:
        return "ok"
    if "missing" in lower or "[yellow]" in lower:
        return "missing"
    return "ok"


def _print_doctor_summary_line(counts: dict[str, int]) -> None:
    """Emit ``mdk_doctor_summary: checks=N ok=N missing=N error=N`` line.

    Reads the tally built up during row adds. Single line, dim style,
    same key=value shape as ``mdk_audit_summary`` and
    ``mdk_eval_summary`` so CI grep stays trivial.
    """
    total = counts["ok"] + counts["missing"] + counts["error"]
    console.print(
        f"[dim]mdk_doctor_summary: checks={total} ok={counts['ok']} "
        f"missing={counts['missing']} error={counts['error']}[/dim]"
    )


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
        # KB parsing + OCR — separate section so operators building document
        # ingestion pipelines get focused guidance without wading through the
        # core-dep explanations.
        (
            "KB parsing & OCR",
            [k for k in EXPLANATIONS if k.startswith(("kb: ", "ocr: "))],
        ),
        ("Runtime adapters", [k for k in EXPLANATIONS if k.startswith("runtime: ")]),
        ("Provider API keys", [k for k in EXPLANATIONS if k.endswith("_API_KEY")]),
        (
            "Tracing",
            [k for k in EXPLANATIONS if k.startswith(("LANGFUSE_", "OTEL_", "MOVATE_TRACER"))],
        ),
        (
            "Storage & project",
            [
                "storage (sqlite)",
                "pricing",
                # Project-config check renders under one of three
                # filenames; the explanation file registers all three
                # but only the present one renders.
                "project.yaml",
                "policy.yaml",
                "movate.yaml",
                # New layout checks (May-2026 MVP).
                "project config parses",
                "agents/",
                "skills/",
                "contexts/",
                "kb/",
            ],
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
            console.print(f"    [dim]WHAT:[/dim]   {escape(entry.what)}")
            console.print(f"    [dim]WHY:[/dim]    {escape(entry.why)}")
            console.print(f"    [dim]ON FAIL:[/dim] {escape(entry.failure_impact)}")
            if entry.fix:
                console.print(f"    [dim]FIX:[/dim]    [green]{escape(entry.fix)}[/green]")
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
        # HPND — Historical Permission Notice and Disclaimer. OSI-approved
        # permissive license used by Pillow. Functionally equivalent to MIT;
        # no copyleft / source-availability obligations. Safe for embedding
        # in commercial deliverables. See: spdx.org/licenses/HPND.html
        "HPND",
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


# ---------------------------------------------------------------------------
# Sub-app — adds `mdk doctor agent <name>` while preserving `mdk doctor`
# as the default env-check command.
# ---------------------------------------------------------------------------


doctor_app = typer.Typer(
    name="doctor",
    help=(
        "Environment + configuration sanity check. Default: project-wide "
        "doctor. Subcommand [bold]agent <name>[/bold] focuses on one agent."
    ),
    invoke_without_command=True,
    rich_markup_mode="rich",
)


@doctor_app.callback(invoke_without_command=True)
def _doctor_callback(
    ctx: typer.Context,
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
    explain: bool = typer.Option(False, "--explain", help="Per-check explanation block."),
    licenses: bool = typer.Option(False, "--licenses", help="Print a per-dep SPDX license report."),
    no_fix_prompt: bool = typer.Option(
        False,
        "--no-fix-prompt",
        help="Skip the interactive `mdk fix?` prompt when issues are found.",
    ),
) -> None:
    """Default-mode dispatch.

    When ``mdk doctor`` is called WITHOUT a subcommand, run the
    project-wide env check (the original ``doctor()`` function). When
    a subcommand IS given (e.g. ``mdk doctor agent rag-qa``), this
    callback is a no-op and Typer dispatches to the subcommand.
    """
    if ctx.invoked_subcommand is not None:
        return
    doctor(
        target=target,
        explain=explain,
        licenses=licenses,
        no_fix_prompt=no_fix_prompt,
    )


@doctor_app.command("agent")
def doctor_agent(
    name: str = typer.Argument(
        ...,
        help="Agent name (resolved under [bold]agents/<name>[/bold]) "
        "or a literal path to an agent directory.",
        metavar="AGENT",
    ),
    project_root: Path = typer.Option(
        None,
        "--project-root",
        help="Override the project root. Defaults to walking up from cwd.",
    ),
) -> None:
    """Agent-specific health check: validates, prices, smoke-tests.

    Runs eight checks against one agent:

      1. agent.yaml + schemas load
      2. prompt template renders against the first dataset row
      3. model is in the packaged pricing table
      4. all declared skills resolve in the skill registry
      5. all declared contexts exist on disk
      6. evals/dataset.jsonl has at least one row
      7. eval baseline is committed (or hint to create one)
      8. last run from storage (if any) — recency + cost

    Ends with a greppable ``mdk_doctor_agent_summary:`` line for CI
    parity with the project-wide doctor's ``mdk_doctor_summary:``.
    """
    _run_agent_doctor(name=name, explicit_project_root=project_root)


# ---------------------------------------------------------------------------
# Agent-specific doctor (Bundle B item 2)
# ---------------------------------------------------------------------------


def _resolve_agent_dir(name: str, explicit_project_root: Path | None) -> Path | None:
    """Find the agent directory for ``mdk doctor agent <name>``.

    Three resolution paths, in order:

    1. ``name`` is itself an absolute or relative path pointing at a
       directory — use it directly.
    2. ``explicit_project_root`` was passed — look under
       ``<root>/agents/<name>/``.
    3. Walk up from cwd looking for ``movate.yaml``, then look under
       ``<root>/agents/<name>/``.

    Returns ``None`` if no resolution succeeds.
    """
    # Path-literal form
    direct = Path(name)
    if direct.is_dir() and (direct / "agent.yaml").is_file():
        return direct.resolve()

    # Explicit project root
    if explicit_project_root is not None:
        candidate = (explicit_project_root / "agents" / name).resolve()
        if candidate.is_dir():
            return candidate

    # Walk-up resolution — accepts any of project.yaml / policy.yaml /
    # movate.yaml via the shared `is_project_root` helper. Without
    # this, `mdk init` writes `project.yaml` (post-May-2026) and the
    # old movate.yaml-only walk would silently fail to find the
    # agent's project root.
    from movate.core.config import is_project_root  # noqa: PLC0415

    current = Path.cwd().resolve()
    while True:
        if is_project_root(current):
            candidate = current / "agents" / name
            if candidate.is_dir():
                return candidate.resolve()
            break
        if current.parent == current:
            break
        current = current.parent

    return None


def _run_agent_doctor(  # noqa: PLR0912 — multi-section diagnostic
    *, name: str, explicit_project_root: Path | None
) -> None:
    """Render the per-agent doctor table.

    Implementation lives separate from the Typer command so tests can
    invoke it directly without `runner.invoke(...)` (and skip the Typer
    machinery + Rich rendering complications).
    """
    # Local imports — keep the cold-path doctor module light.
    from movate.core.loader import AgentLoadError, load_agent  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415

    err = Console(stderr=True)

    agent_dir = _resolve_agent_dir(name, explicit_project_root)
    if agent_dir is None:
        # Typo suggestion: fuzzy-match against agents that DO exist in
        # the current project so `mdk doctor agent ragqa` surfaces
        # `did you mean rag-qa?` instead of a flat error.
        from movate.cli._resolve import suggest_similar_agent  # noqa: PLC0415

        suggestion = suggest_similar_agent(name)
        hint_text = (
            f" Did you mean [bold]{suggestion}[/bold]?"
            if suggestion
            else " Pass a directory path, or run from inside a movate "
            "project where [bold]agents/<name>/[/bold] exists."
        )
        err.print(
            f"[red]✗[/red] could not resolve agent [bold]{name}[/bold].[dim]{hint_text}[/dim]"
        )
        raise typer.Exit(code=2)

    table = Table(
        title=f"movate doctor → agent: [cyan]{name}[/cyan]",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail", style="dim", overflow="fold")

    counts = {"ok": 0, "missing": 0, "error": 0}

    def _row(check: str, status: str, detail: str = "") -> None:
        table.add_row(check, status, detail)
        if "[green]" in status:
            counts["ok"] += 1
        elif "[red]" in status:
            counts["error"] += 1
        elif "[yellow]" in status:
            counts["missing"] += 1

    # Check 1: agent loads
    bundle = None
    try:
        bundle = load_agent(agent_dir)
        _row("load", "[green]✓ ok[/green]", f"name={bundle.spec.name}")
    except AgentLoadError as exc:
        _row(
            "load",
            "[red]✗ failed[/red]",
            str(exc).splitlines()[0][:120],
        )

    # Bail early — the remaining checks need the bundle to exist.
    if bundle is None:
        console.print(table)
        _print_agent_doctor_summary(name=name, counts=counts)
        raise typer.Exit(code=1)

    # Check 2: prompt renders against the first dataset row
    dataset_path = agent_dir / "evals" / "dataset.jsonl"
    if dataset_path.is_file() and dataset_path.read_text().strip():
        import json as _json  # noqa: PLC0415

        first_line = dataset_path.read_text().splitlines()[0]
        try:
            first_input = _json.loads(first_line)["input"]
            bundle.input_validator.validate(first_input)
            bundle.render_prompt(first_input)
            _row(
                "prompt renders",
                "[green]✓ ok[/green]",
                "against first dataset row",
            )
        except Exception as exc:
            _row("prompt renders", "[red]✗ failed[/red]", str(exc)[:120])
    else:
        _row(
            "prompt renders",
            "[yellow]skipped[/yellow]",
            "no dataset rows to render against",
        )

    # Check 3: model is in pricing table
    try:
        pricing = load_pricing()
        provider_str = bundle.spec.model.provider
        if provider_str in pricing.models:
            _row("pricing", "[green]✓ ok[/green]", provider_str)
        else:
            _row(
                "pricing",
                "[yellow]not listed[/yellow]",
                f"{provider_str} not in pricing table — cost will report unknown",
            )
    except Exception as exc:
        _row("pricing", "[red]✗ failed[/red]", str(exc)[:120])

    # Check 4: skills resolve — name-by-name so a failure points at
    # WHICH skill is missing, not just "count mismatch".
    if bundle.spec.skills:
        declared_skills = list(bundle.spec.skills)
        # Each entry in `bundle.skills` is a SkillBundle whose .spec
        # has .name; cross-reference with what the agent.yaml declared.
        resolved_names = [getattr(s.spec, "name", "?") for s in bundle.skills]
        missing_skills = [n for n in declared_skills if n not in resolved_names]
        if not missing_skills:
            _row(
                "skills resolve",
                "[green]✓ ok[/green]",
                f"{len(declared_skills)} declared, all resolved: " + ", ".join(declared_skills),
            )
        else:
            _row(
                "skills resolve",
                "[red]✗ failed[/red]",
                f"missing skill(s): {', '.join(missing_skills)} "
                f"(add to <project>/skills/<name>/ or remove from "
                f"agent.yaml: skills)",
            )
    else:
        _row("skills resolve", "[green]✓ none declared[/green]", "")

    # Check 5: contexts resolve — name-by-name + flag whether each
    # came from agent-local (overrides project-level on collision)
    # or shared project-level. Helps operators verify the override
    # they intended actually fired.
    if bundle.spec.contexts:
        declared_contexts = list(bundle.spec.contexts)
        resolved_names = [name for name, _body in bundle.contexts]
        missing_contexts = [n for n in declared_contexts if n not in resolved_names]
        if not missing_contexts:
            # Classify each resolved context by where it came from.
            # Use `ctx_name` (not `name`) to avoid shadowing the
            # outer-scope `name` arg (agent name) that the summary
            # line + table title both consume.
            agent_local_dir = agent_dir / "contexts"
            tier_labels = []
            for ctx_name in declared_contexts:
                if (agent_local_dir / f"{ctx_name}.md").is_file():
                    tier_labels.append(f"{ctx_name} (agent-local)")
                else:
                    tier_labels.append(f"{ctx_name} (shared)")
            _row(
                "contexts resolve",
                "[green]✓ ok[/green]",
                f"{len(declared_contexts)} resolved: " + ", ".join(tier_labels),
            )
        else:
            _row(
                "contexts resolve",
                "[red]✗ failed[/red]",
                f"missing context(s): {', '.join(missing_contexts)} "
                f"(add to <project>/contexts/<name>.md or "
                f"agents/<this-agent>/contexts/<name>.md)",
            )
    else:
        _row("contexts resolve", "[green]✓ none declared[/green]", "")

    # Check 6: dataset rows
    if dataset_path.is_file():
        rows = [line for line in dataset_path.read_text().splitlines() if line.strip()]
        if rows:
            _row(
                "dataset rows",
                "[green]✓ ok[/green]",
                f"{len(rows)} row(s)",
            )
        else:
            _row(
                "dataset rows",
                "[yellow]empty[/yellow]",
                "evals/dataset.jsonl is empty — eval will skip",
            )
    else:
        _row(
            "dataset rows",
            "[yellow]missing[/yellow]",
            "no evals/dataset.jsonl — mdk audit will flag",
        )

    # Check 7: eval baseline
    baseline_candidates = [
        agent_dir / ".movate" / "baseline.json",
        agent_dir.parent.parent / ".movate" / bundle.spec.name / "baseline.json",
    ]
    if any(c.is_file() for c in baseline_candidates):
        _row("eval baseline", "[green]✓ committed[/green]", "")
    else:
        _row(
            "eval baseline",
            "[yellow]missing[/yellow]",
            "run `mdk eval --output-baseline .movate/baseline.json`",
        )

    # Check 8: last run from storage
    # Skip this if storage isn't accessible. It's a soft-check that gives
    # production-aware context without being a hard requirement.
    try:
        import asyncio as _asyncio  # noqa: PLC0415

        from movate.storage import build_storage  # noqa: PLC0415

        async def _last_run() -> tuple[str, str]:
            storage = build_storage()
            try:
                await storage.init()
                runs = await storage.list_runs(
                    tenant_id="local",
                    agent=bundle.spec.name,
                    limit=1,
                )
                if not runs:
                    return "no runs", ""
                run = runs[0]
                cost = getattr(run.metrics, "cost_usd", 0) or 0
                latency = getattr(run.metrics, "latency_ms", 0) or 0
                return (
                    f"{run.run_id[:8]}",
                    f"${cost:.4f} · {latency:.0f}ms",
                )
            finally:
                await storage.close()

        run_id, detail = _asyncio.run(_last_run())
        if run_id == "no runs":
            _row(
                "last run",
                "[yellow]none[/yellow]",
                "no runs recorded for this agent yet",
            )
        else:
            _row("last run", f"[green]✓ {run_id}[/green]", detail)
    except Exception as exc:
        _row(
            "last run",
            "[yellow]skipped[/yellow]",
            f"storage probe failed: {str(exc)[:80]}",
        )

    console.print(table)
    _print_agent_doctor_summary(name=name, counts=counts)


def _print_agent_doctor_summary(name: str, counts: dict[str, int]) -> None:
    """Emit the greppable summary line for `mdk doctor agent`."""
    total = counts["ok"] + counts["missing"] + counts["error"]
    console.print(
        f"[dim]mdk_doctor_agent_summary: "
        f"agent={name} "
        f"checks={total} ok={counts['ok']} "
        f"missing={counts['missing']} error={counts['error']}[/dim]"
    )
