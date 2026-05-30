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
from movate.core.paths import project_state_dir
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
    "pillow": "HPND",  # Historical Permission Notice and Disclaimer (permissive)
    "pdf2image": "MIT",
    "pytesseract": "Apache-2.0",
    "tesseract": "Apache-2.0",  # system binary
    # EasyOCR dep ([easyocr] extra — alternative pure-Python OCR backend)
    "easyocr": "Apache-2.0",
}

# One-line role description per dep — surfaced in ``mdk doctor`` so
# operators glancing at the table can answer "wait, why do we depend
# on this?" without leaving the terminal. Keep entries terse (<=50
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
_RUNTIME_PROBES: tuple[tuple[str, str, str | None], ...] = (
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
    ("MOVATE_TRACE_SINK", "sink selector"),
    ("MOVATE_TRACER", "explicit override"),
    ("LANGFUSE_SECRET_KEY", "Langfuse secret"),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse public"),
    ("LANGFUSE_HOST", "Langfuse host"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTel endpoint"),
    ("OTEL_EXPORTER_OTLP_HEADERS", "OTel headers"),
    ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTel protocol"),
    ("OTEL_SERVICE_NAME", "OTel service.name"),
    # ADR 039 Phase 2 — opt-in dual export to Movate's central Collector.
    # Off by default; the dedicated phase-2 row below summarizes overall
    # state and the per-var rows here let operators see at a glance which
    # half is set.
    ("MDK_TELEMETRY_ENDPOINT", "Movate central Collector OTLP endpoint (Phase 2)"),
    ("MDK_TELEMETRY_CUSTOMER_ID", "opaque customer id (hash) for Phase 2"),
)


def _ok(label: str) -> str:
    return f"[green]ok[/green] [dim]{label}[/dim]" if label else "[green]ok[/green]"


def _missing(label: str) -> str:
    return f"[yellow]missing[/yellow] [dim]{label}[/dim]" if label else "[yellow]missing[/yellow]"


def _is_runtime_key_shadowed(var: str) -> bool:
    """True when ``var`` is set from the shell AND ~/.movate/credentials
    holds a DIFFERENT non-empty value for it.

    This is the exact failure that bit operators repeatedly: a freshly
    saved runtime key in ``~/.movate/credentials`` is silently shadowed
    by a stale ``export MDK_<TARGET>_KEY=...`` left in a shell profile.
    Because the shell value is set before the CLI starts, autoload never
    overwrites it (narrowest-beats-widest), so the saved key never takes
    effect — every live call 401s with no obvious cause.

    Reuses :func:`movate.credentials.key_source` for source attribution
    and :class:`CredentialsStore` for the file value rather than
    reimplementing either — kept local to ``doctor.py`` so the
    credentials/loader seam stays untouched (a separate concern may
    evolve it). The ``mdk fix unshadow-runtime-keys`` remediation
    targets exactly this predicate.
    """
    from movate.credentials import key_source  # noqa: PLC0415
    from movate.credentials.store import CredentialsStore  # noqa: PLC0415

    if key_source(var) != "shell":
        return False
    current = os.environ.get(var, "").strip()
    file_value = (CredentialsStore().get(var) or "").strip()
    # Shadow only when the file actually holds a competing value that
    # differs from the live shell value. A shell-only export with no
    # saved counterpart is NOT a shadow — there's nothing being hidden.
    return bool(file_value) and file_value != current


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
    # Staleness check (ADR 026 D5): compare the INSTALLED mdk against its
    # source of truth — a co-located editable repo checkout's version when
    # present, else how many days old the installed CalVer is. Warns (not
    # errors) when behind, with the reinstall command. Converts the silent
    # day-stale-install class of bug into a self-fixing prompt.
    stale_result, stale_purpose = _check_version_staleness()
    _add("mdk up-to-date", stale_result, stale_purpose, "")
    # Project-pinned minimum mdk version (Monday-demo polish). Compares the
    # installed __version__ against `mdk_version_min:` in project.yaml (or
    # the MDK_VERSION_MIN env override). Silent skip when neither source is
    # set — only customer projects that opt in get the row.
    binary_staleness = _check_mdk_binary_staleness()
    if binary_staleness is not None:
        result, purpose = binary_staleness
        _add("mdk-binary-staleness", result, purpose, "")
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
    for runtime_name, probe_module, runtime_extra in _RUNTIME_PROBES:
        spec = importlib.util.find_spec(probe_module)
        if spec is not None:
            status = _ok("adapter available")
        elif runtime_extra is not None:
            status = _missing(f"install with: uv add 'movate-cli[{runtime_extra}]'")
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

    # ADR 039 Phase 2 — opt-in dual OTLP export to Movate's central
    # Collector. Off by default; when MDK_TELEMETRY_ENDPOINT is set we
    # surface the configured endpoint + a short prefix of the customer-id
    # hash (NEVER the full ID; the hash already obscures the customer
    # name, and we still avoid printing it in full so a screenshot of
    # `mdk doctor` doesn't leak it).
    _render_phase2_telemetry_section(_add)

    _add("", "")

    # Runtime bearer keys (MDK_<TARGET>_KEY) — presence, source, and the
    # shell-shadow condition that `mdk fix unshadow-runtime-keys`
    # remediates. Closes the doctor(diagnose) ↔ fix(remediate) loop.
    _render_runtime_keys_section(_add)

    # Storage
    sqlite_path = Path("~/.movate/local.db").expanduser()
    state = "exists" if sqlite_path.exists() else "will be created on first run"
    _add("storage (sqlite)", f"{sqlite_path} [dim]({state})[/dim]")

    # DB connection-ceiling capacity (ADR 034 D1). Warns when the worst-case
    # KEDA-autoscaled fleet would exceed Postgres max_connections. Informational
    # + graceful when a DB / the values aren't reachable — never crashes doctor.
    _render_pool_capacity_section(_add)

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


def _render_phase2_telemetry_section(_add: Any) -> None:
    """ADR 039 Phase 2 telemetry doctor check.

    A single row that reports the state of the **opt-in** dual-export
    feature:

    * ``MDK_TELEMETRY_ENDPOINT`` unset → ``ok (off)``: Phase 2 is disabled
      and the row is informational. This is the default for every customer
      deployment.
    * Endpoint set + ``MDK_TELEMETRY_CUSTOMER_ID`` set → ``ok``: prints the
      endpoint + an 8-char prefix of the customer-id hash (never the full
      ID — a screenshot of doctor output should not leak a customer ID even
      though the value is already hashed by the operator).
    * Endpoint set + customer-id unset → ⚠ ``missing``: the runtime will
      log a warning at next provider init and skip Phase 2 (primary export
      unaffected). Surfaces the misconfig before the operator hits it.

    The check intentionally does **not** probe the Movate endpoint's
    reachability — that lives behind a cross-tenant network and probing
    from a customer host would be misleading. The runtime fail-soft path
    handles unreachability silently.
    """
    from movate.tracing.dual_export import (  # noqa: PLC0415 - lazy by design
        ENV_TELEMETRY_CUSTOMER_ID,
        ENV_TELEMETRY_ENDPOINT,
        telemetry_customer_id,
        telemetry_endpoint,
    )

    endpoint = telemetry_endpoint()
    if not endpoint:
        _add(
            "phase 2 telemetry",
            _ok("off")
            + " [dim](MDK_TELEMETRY_ENDPOINT unset — default; per-tenant export only)[/dim]",
        )
        return

    customer = telemetry_customer_id()
    if not customer:
        _add(
            "phase 2 telemetry",
            _missing(
                f"endpoint set ({endpoint}) but {ENV_TELEMETRY_CUSTOMER_ID} is unset — "
                "dual export disabled. Set both, or unset the endpoint to silence this."
            ),
        )
        return

    # Never print the full customer-id, even though it's a hash. 8 chars is
    # enough to recognize "this is the value I configured" without
    # publishing the whole identifier into a doctor screenshot.
    prefix = customer[:8]
    _add(
        "phase 2 telemetry",
        _ok(f"{endpoint} [dim]customer={prefix}…[/dim]"),
    )
    _add(
        "",
        f"[dim]{ENV_TELEMETRY_ENDPOINT} sends a minimized copy of metrics + spans to "
        "Movate's Collector (no prompts, no PII; see docs/observability.md).[/dim]",
    )


def _render_runtime_keys_section(_add: Any) -> None:
    """Render the "Runtime keys" section: one row per key-auth target.

    Enumerates every target in ``~/.movate/config.yaml`` via
    :func:`load_user_config` (``cfg.targets`` — the same dict
    ``resolve_target`` reads). Only ``auth == "key"`` targets are
    checked; ``oidc`` targets mint a short-lived JWT and have no
    ``MDK_<TARGET>_KEY`` to diagnose, so they're skipped silently.

    Per target, the row reports its ``key_env`` var by:

    * **set + shadowed** → ⚠ (counts as ``missing``). The live value is
      a stale shell export hiding a different value saved in
      ``~/.movate/credentials``. Remediation points at
      ``mdk fix unshadow-runtime-keys --apply`` (auto) and the manual
      ``unset <VAR>``.
    * **set, not shadowed** → ✓ ``ok``, annotated with the source
      (``shell`` / ``dotenv`` / ``credentials_file``). The key value is
      NEVER printed — only its source, matching how the provider-key
      rows above report set/unset rather than the secret.
    * **unset** → ⚠ ``missing`` with the
      ``mdk auth save-runtime-key <target>`` hint.

    No configured targets (or no config file at all) → a clean no-op:
    the section header isn't even emitted, so the table stays quiet for
    operators who only run agents locally.
    """
    from movate.core.user_config import UserConfigError, load_user_config  # noqa: PLC0415
    from movate.credentials import key_source  # noqa: PLC0415

    try:
        cfg = load_user_config()
    except UserConfigError:
        # Malformed config is surfaced elsewhere (resolve_target / the
        # azure preflight). The runtime-key section degrades to a no-op
        # rather than crashing the whole doctor table.
        return

    # Only key-auth targets carry a bearer var worth diagnosing.
    key_targets = [(name, tcfg) for name, tcfg in sorted(cfg.targets.items()) if tcfg.auth == "key"]
    if not key_targets:
        return

    _add("", "")
    for name, tcfg in key_targets:
        var = tcfg.key_env
        source = key_source(var)
        if source == "unset":
            _add(
                var,
                _missing(
                    f"target {name!r} has no key — run mdk auth save-runtime-key {name} <key>"
                ),
            )
            continue
        if _is_runtime_key_shadowed(var):
            # ⚠: a stale shell export shadows the saved key. Point at the
            # auto-fix AND the manual unset so operators have both paths.
            _add(
                var,
                f"[yellow]shadowed[/yellow] [dim]target {name!r}: shell export hides a "
                f"different value in ~/.movate/credentials — run "
                f"[bold]mdk fix unshadow-runtime-keys --apply[/bold] "
                f"(or manually: unset {var})[/dim]",
            )
            continue
        # Set + not shadowed — report the source, never the value.
        _add(var, _ok(f"target {name!r} (source: {source})"))


def _render_pool_capacity_section(_add: Any) -> None:
    """Render the DB connection-ceiling capacity check (ADR 034 D1).

    Computes whether the worst-case KEDA-autoscaled fleet
    (``pods x pool_max``) fits under Postgres ``max_connections`` with headroom,
    per the sizing formula ``pods x pool_max <= max_connections - headroom``.

    Inputs degrade gracefully: ``pool_max`` + ``max_connections`` are probed from
    a live Postgres (``MOVATE_DB_URL``) when reachable; otherwise they fall back
    to documented env overrides (``MOVATE_DB_POOL_MAX_SIZE`` /
    ``MOVATE_DB_MAX_CONNECTIONS`` / ``MOVATE_KEDA_MAX_REPLICAS`` /
    ``MOVATE_DB_CONNECTION_HEADROOM``) and then assumed defaults that match the
    shipped infra. A result built on assumed inputs renders as a dim
    informational row, never a false-positive warning — and any failure inside
    here is swallowed so the capacity check can never crash ``mdk doctor``.

    Row semantics:

    * green ``ok`` — fits under the ceiling with observed/env inputs.
    * yellow ``warn`` — over the ceiling: the exhaustion risk; remediation names
      the three fixes (lower pool_max, cap replicas, add PgBouncer → ADR 034 D1).
    * dim ``info`` — fits, but on assumed inputs (advisory; confirm the real ones).
    """
    try:
        import asyncio as _asyncio  # noqa: PLC0415

        from movate.cli._pool_capacity import (  # noqa: PLC0415
            SIZING_FORMULA,
            compute_capacity_verdict,
            probe_postgres_inputs,
        )

        try:
            observed_pool_max, observed_max_conns = _asyncio.run(probe_postgres_inputs())
        except Exception:
            observed_pool_max, observed_max_conns = (None, None)

        verdict = compute_capacity_verdict(
            observed_pool_max=observed_pool_max,
            observed_max_connections=observed_max_conns,
        )
    except Exception as exc:  # pragma: no cover - defensive; check must not crash doctor
        _add(
            "db pool capacity",
            f"[dim]skipped: capacity check failed ({str(exc)[:60]})[/dim]",
        )
        return

    formula = f"[dim](formula: {SIZING_FORMULA})[/dim]"
    if verdict.status == "warn":
        _add(
            "db pool capacity",
            f"[yellow]⚠ {escape(verdict.summary)}[/yellow] [dim]— {escape(verdict.remediation)} "
            f"{SIZING_FORMULA}[/dim]",
        )
    elif verdict.status == "info":
        _add(
            "db pool capacity",
            f"[dim]> {escape(verdict.summary)} {formula}[/dim]",
        )
    else:
        _add(
            "db pool capacity",
            _ok(escape(verdict.summary)) + f" {formula}",
        )


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


# CalVer staleness thresholds (ADR 026 D5). An install N days behind today
# is "getting stale" (advisory); past STALE_DAYS it's flagged as a warning.
_VERSION_STALE_DAYS = 14


def _parse_calver_date(version: str) -> Any:
    """Parse the ``YYYY.M.D`` date prefix of a CalVer ``YYYY.M.D.N`` string.

    Returns a :class:`datetime.date`, or ``None`` when the string isn't
    CalVer-shaped (e.g. a legacy ``v0.x`` SemVer tag) — the caller then
    skips the day-based staleness path.
    """
    import datetime as _dt  # noqa: PLC0415

    parts = version.split(".")
    # CalVer is YYYY.M.D.N — need at least the three date segments.
    calver_date_segments = 3
    if len(parts) < calver_date_segments:
        return None
    try:
        return _dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        return None


def _editable_repo_version() -> str | None:
    """Version string of a co-located editable repo checkout, or ``None``.

    The installed ``mdk`` source-of-truth (ADR 026 D5): when ``mdk`` runs
    FROM (or alongside) an editable ``movate-cli`` checkout, the repo's
    ``src/movate/__init__.py`` carries the latest hand-bumped version. We
    locate it from this module's own file path (``movate/cli/doctor.py`` →
    repo root two levels up from ``movate/``) and confirm the repo manifest
    is movate-cli's (not some other project that happens to vendor a copy).

    Returns the repo ``__version__`` only when it differs from the
    INSTALLED ``__version__`` — when they match (the common case: the repo
    IS what's installed), there's nothing to compare, so ``None``.
    """
    import re as _re  # noqa: PLC0415

    try:
        # doctor.py → cli/ → movate/ → src/ → <repo root>
        module_file = Path(__file__).resolve()
        src_movate = module_file.parent.parent  # .../src/movate
        repo_root = src_movate.parent.parent  # .../<repo>
        pyproject = repo_root / "pyproject.toml"
        init_py = src_movate / "__init__.py"
        if not (pyproject.is_file() and init_py.is_file()):
            return None
        if 'name = "movate-cli"' not in pyproject.read_text():
            return None
        match = _re.search(r'^__version__\s*=\s*"([^"]+)"', init_py.read_text(), _re.MULTILINE)
        if not match:
            return None
        repo_version = match.group(1)
        return repo_version if repo_version != __version__ else None
    except OSError:
        return None


def _check_version_staleness() -> tuple[str, str]:
    """Compare the installed ``mdk`` against its source of truth (ADR 026 D5).

    Returns ``(result_markup, purpose)`` for a doctor table row:

    * Editable repo checkout present AND newer than the installed build →
      WARN with the reinstall command (``uv tool install --force .``). This
      is the "I edited the repo / pulled main but forgot to reinstall the
      tool" footgun — the installed binary silently lags the source.
    * No newer repo → fall back to "last-updated N days ago" from the
      installed CalVer date. Past :data:`_VERSION_STALE_DAYS` it warns with
      the upgrade command; within the window it's a green "current" note.
    * Non-CalVer / unknown date → a neutral note (never errors).

    Cheap + best-effort; any failure degrades to a neutral note rather than
    breaking ``mdk doctor``.
    """
    import datetime as _dt  # noqa: PLC0415

    repo_version = _editable_repo_version()
    if repo_version is not None:
        return (
            f"[yellow]behind repo ({repo_version})[/yellow] — reinstall: uv tool install --force .",
            "installed build lags the editable checkout",
        )

    installed_date = _parse_calver_date(__version__)
    if installed_date is None:
        return ("[dim]version not date-based[/dim]", "staleness check needs a CalVer build")

    days = (_dt.date.today() - installed_date).days
    if days < 0:
        # Clock skew / future-dated build — don't cry wolf.
        return (_ok("current"), "installed build is up to date")
    if days >= _VERSION_STALE_DAYS:
        return (
            f"[yellow]last updated {days}d ago[/yellow] — "
            f"upgrade: uv tool install --force movate-cli",
            "installed build is getting old",
        )
    label = "today" if days == 0 else f"{days}d ago"
    return (_ok(f"current (built {label})"), "installed build is up to date")


def _parse_calver_tuple(version: str) -> tuple[int, ...] | None:
    """Parse a CalVer ``YYYY.M.D.N`` into a comparable int tuple.

    Returns ``None`` on any non-CalVer / unparseable input so callers can
    skip the comparison rather than raise. Used by the project-pinned
    minimum-version check to avoid false positives on legacy SemVer tags.
    """
    parts = version.strip().split(".")
    try:
        return tuple(int(p) for p in parts)
    except (ValueError, TypeError):
        return None


def _check_mdk_binary_staleness() -> tuple[str, str] | None:
    """Compare installed ``mdk`` against the project-pinned minimum.

    Sources, in order: ``MDK_VERSION_MIN`` env var (operator override),
    then ``project.yaml`` ``mdk_version_min:`` field. The env wins when
    both are set so an operator can pin a stricter floor without editing
    the project file.

    Returns ``(result_markup, purpose)`` for the doctor row, or ``None``
    when neither source is set — the caller then skips the row entirely
    so projects that don't opt in stay quiet.

    Failure modes (all degrade to ``None`` rather than crashing doctor):

    * project config malformed or unreadable
    * pinned value isn't CalVer-shaped (legacy SemVer / typo)
    * installed ``__version__`` isn't CalVer-shaped
    """
    pinned = os.environ.get("MDK_VERSION_MIN", "").strip()
    if not pinned:
        try:
            from movate.core.config import load_project_config  # noqa: PLC0415

            cfg = load_project_config()
            pinned = (cfg.mdk_version_min or "").strip()
        except Exception:
            # No project / malformed config — silently skip, the project-
            # config-parses check above already surfaces parse failures.
            return None
    if not pinned:
        return None

    pinned_tuple = _parse_calver_tuple(pinned)
    installed_tuple = _parse_calver_tuple(__version__)
    if pinned_tuple is None or installed_tuple is None:
        # Can't compare apples-to-apples; degrade to a neutral note
        # rather than crying wolf on a legacy SemVer install.
        return ("[dim]skipped (non-CalVer version)[/dim]", f"project expects >= {pinned}")

    if installed_tuple >= pinned_tuple:
        return (
            _ok(f"{__version__} >= {pinned}"),
            "installed mdk satisfies the project's minimum",
        )
    return (
        f"[yellow]⚠ mdk binary is stale: installed = {__version__}, "
        f"project expects >= {pinned}[/yellow] — "
        f"update: uv tool install --editable '.[runtime,playground]' --force",
        "installed mdk is older than the project's minimum",
    )


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
            [
                k
                for k in EXPLANATIONS
                if k.startswith(
                    (
                        "LANGFUSE_",
                        "OTEL_",
                        "MOVATE_TRACER",
                        "MOVATE_TRACE_SINK",
                        # ADR 039 Phase 2 — opt-in dual export envs.
                        "MDK_TELEMETRY_",
                    )
                )
            ],
        ),
        (
            "Storage & project",
            [
                "storage (sqlite)",
                "pricing",
                "db pool capacity",
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
        declared_skills = [str(s) for s in bundle.spec.skills]
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
        project_state_dir(agent_dir) / "baseline.json",
        project_state_dir(agent_dir.parent.parent) / bundle.spec.name / "baseline.json",
    ]
    if any(c.is_file() for c in baseline_candidates):
        _row("eval baseline", "[green]✓ committed[/green]", "")
    else:
        _row(
            "eval baseline",
            "[yellow]missing[/yellow]",
            "run `mdk eval --output-baseline .mdk/baseline.json`",
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
