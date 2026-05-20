"""Doctor check explanations — what / why / failure-impact / fix.

Companion to ``mdk doctor``. When the operator passes ``--explain``,
each check renders with a small block of human-readable context so
they can interpret what they're looking at without ssh-ing into the
codebase. Especially useful for operators new to the stack who hit a
red ``missing`` and want to know whether it actually matters.

Each entry is a :class:`CheckExplanation`. Keep the prose terse — this
runs in the terminal, not a docs site.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckExplanation:
    """Operator-facing context for one doctor row.

    Fields:
      * ``what`` — what the check measures (one short sentence)
      * ``why`` — why it matters (one short sentence)
      * ``failure_impact`` — what breaks when this is red (specific)
      * ``fix`` — copyable command or one-liner; empty if the check
        can't fail (e.g. fixed-fact like "movate version")
    """

    what: str
    why: str
    failure_impact: str
    fix: str = ""


# Required deps — each one is a hard requirement; a missing one means
# the install is broken. Fix is always "reinstall".

_REQUIRED_DEP_EXPLANATIONS: dict[str, CheckExplanation] = {
    "typer": CheckExplanation(
        what="Required Python dep — Typer CLI framework.",
        why="Every `mdk` / `movate` subcommand is built on Typer.",
        failure_impact="The CLI won't run at all; you'd see an ImportError before the first command.",
        fix="uv tool install --editable . --force",
    ),
    "rich": CheckExplanation(
        what="Required Python dep — Rich terminal-rendering library.",
        why="Powers tables, panels, progress bars, and colored output across the CLI.",
        failure_impact="The CLI imports fail at startup — same as a missing Typer.",
        fix="uv tool install --editable . --force",
    ),
    "pydantic": CheckExplanation(
        what="Required Python dep — Pydantic data-validation library.",
        why="Every `agent.yaml`, `policy.yaml`, and request/response shape is validated through a Pydantic model.",
        failure_impact="Schema loading + every typed contract in the CLI breaks.",
        fix="uv tool install --editable . --force",
    ),
    "yaml": CheckExplanation(
        what="Required Python dep — PyYAML.",
        why="Loads `agent.yaml`, `policy.yaml` / `movate.yaml`, judge configs.",
        failure_impact="Any config-file load throws an ImportError before validation.",
        fix="uv tool install --editable . --force",
    ),
    "jinja2": CheckExplanation(
        what="Required Python dep — Jinja2 template engine.",
        why="Renders agent prompts (`{{ input.field }}` substitution).",
        failure_impact="`mdk run` fails at prompt-render time.",
        fix="uv tool install --editable . --force",
    ),
    "litellm": CheckExplanation(
        what="Required Python dep — LiteLLM (multi-provider model adapter).",
        why="Routes every agent's model call (OpenAI / Anthropic / Azure / Gemini) through one client.",
        failure_impact="Any agent with `runtime: litellm` (the default) fails at inference time.",
        fix="uv tool install --editable . --force",
    ),
    "aiosqlite": CheckExplanation(
        what="Required Python dep — async SQLite driver.",
        why="Local-mode storage backend (`~/.movate/local.db`). Deployed runtimes use Postgres via asyncpg.",
        failure_impact="`mdk run` can't persist RunRecords or read failures locally.",
        fix="uv tool install --editable . --force",
    ),
}


# Optional deps — each gates a feature. Missing = that feature unavailable.

_OPTIONAL_DEP_EXPLANATIONS: dict[str, CheckExplanation] = {
    "langfuse": CheckExplanation(
        what="Optional Python dep — Langfuse SDK (tracing/observability platform).",
        why="`MDK_TRACER=langfuse` ships every run's prompt + response + cost to Langfuse for review.",
        failure_impact="Setting `MDK_TRACER=langfuse` falls back to the stdout tracer. No data loss.",
        fix="uv pip install 'movate-cli[langfuse]'",
    ),
    "opentelemetry": CheckExplanation(
        what="Optional Python dep — OpenTelemetry SDK + OTLP exporter.",
        why="`MDK_TRACER=otel` sends span data to any OTLP backend (Honeycomb, Datadog, Jaeger, etc.).",
        failure_impact="OTel tracing falls back to stdout. No data loss; just no spans in your observability backend.",
        fix="uv pip install 'movate-cli[otel]'",
    ),
    "asyncpg": CheckExplanation(
        what="Optional Python dep — async Postgres driver.",
        why="Powers the Postgres storage backend used by deployed runtimes (`mdk serve` + `mdk worker`).",
        failure_impact="Postgres-backed storage unavailable. Local sqlite still works; `mdk serve` won't.",
        fix="uv pip install 'movate-cli[runtime]'",
    ),
    "fastapi": CheckExplanation(
        what="Optional Python dep — FastAPI web framework.",
        why="The HTTP runtime (`mdk serve`) is a FastAPI app. Workers don't need it.",
        failure_impact="`mdk serve` won't start. Local CLI commands unaffected.",
        fix="uv pip install 'movate-cli[runtime]'",
    ),
}


# Runtime adapters — each one represents a way an `agent.yaml: runtime:`
# value resolves. Missing = an agent declaring that runtime fails at
# load time with a clear "runtime not registered" error.

_RUNTIME_EXPLANATIONS: dict[str, CheckExplanation] = {
    "litellm": CheckExplanation(
        what="The default runtime — agents call models via LiteLLM.",
        why="Provider-portable: same agent.yaml works against OpenAI, Anthropic, Azure OpenAI, Gemini, ... by changing one string.",
        failure_impact="Can't happen — LiteLLM is a required dep.",
    ),
    "native_anthropic": CheckExplanation(
        what="Native Anthropic SDK adapter — invokes `anthropic` Python SDK directly.",
        why="Unlocks tool-use, computer-use, prompt caching, thinking blocks, vision, MCP integrations.",
        failure_impact="Agents with `runtime: native_anthropic` fail `mdk validate` with 'runtime not registered'.",
        fix="uv pip install 'movate-cli[anthropic]'",
    ),
    "native_openai": CheckExplanation(
        what="Native OpenAI SDK adapter — invokes `openai` Python SDK directly.",
        why="Unlocks Assistants API, strict structured outputs, parallel function-calling, vision-with-tools.",
        failure_impact="Agents with `runtime: native_openai` fail `mdk validate` with 'runtime not registered'.",
        fix="uv pip install 'movate-cli[openai]'",
    ),
    "langchain": CheckExplanation(
        what="LangChain adapter — agents whose `provider:` is a Python entry-point returning a LangChain Runnable.",
        why="Drop a LangChain LCEL chain into MDK without re-writing it; inherits MDK's auth, eval, deploy.",
        failure_impact="Agents with `runtime: langchain` fail `mdk validate` with 'runtime not registered'.",
        fix="uv pip install 'movate-cli[langchain]'",
    ),
    "lyzr": CheckExplanation(
        what="Lyzr Studio adapter — invokes Lyzr-hosted agents via HTTPS.",
        why="Read-only bridge for evaluating / benchmarking Lyzr-hosted customer agents from MDK.",
        failure_impact="Agents with `runtime: lyzr` get an AuthError at runtime if LYZR_API_KEY isn't set.",
        fix="export LYZR_API_KEY=sk-default-...   # from Lyzr Studio → Agent → API Key",
    ),
}


# Provider API keys — each one enables one model vendor.

_PROVIDER_KEY_EXPLANATIONS: dict[str, CheckExplanation] = {
    "OPENAI_API_KEY": CheckExplanation(
        what="Authentication for OpenAI models (gpt-4o-mini, gpt-5, o1, ...).",
        why="Required when any agent's `provider:` starts with `openai/` or `azure/`.",
        failure_impact="OpenAI calls fail with AuthError. Agents fall through to `model.fallback`; if all fallbacks are also OpenAI-family, the run fails.",
        fix="export OPENAI_API_KEY=sk-...   # from https://platform.openai.com/api-keys",
    ),
    "ANTHROPIC_API_KEY": CheckExplanation(
        what="Authentication for Anthropic Claude models.",
        why="Required when any agent's `provider:` starts with `anthropic/`.",
        failure_impact="Claude calls fail with AuthError. If used as a fallback chain target, the chain truncates.",
        fix="export ANTHROPIC_API_KEY=sk-ant-...   # from https://console.anthropic.com/settings/keys",
    ),
    "AZURE_OPENAI_API_KEY": CheckExplanation(
        what="Authentication for Azure OpenAI Service (Microsoft's hosted OpenAI deployment).",
        why="Required when any agent's `provider:` starts with `azure/`.",
        failure_impact="Azure OpenAI calls fail with AuthError. Use OpenAI directly as a fallback if available.",
        fix="export AZURE_OPENAI_API_KEY=...   # from your Azure OpenAI resource's Keys page",
    ),
    "GEMINI_API_KEY": CheckExplanation(
        what="Authentication for Google Gemini models.",
        why="Required when any agent's `provider:` starts with `gemini/`.",
        failure_impact="Gemini calls fail with AuthError.",
        fix="export GEMINI_API_KEY=...   # from https://aistudio.google.com/apikey",
    ),
    "LYZR_API_KEY": CheckExplanation(
        what="Authentication for Lyzr Studio agents (used with `runtime: lyzr` agents).",
        why="Required only when invoking agents migrated from Lyzr via the `mdk import lyzr` bridge.",
        failure_impact="`runtime: lyzr` agents fail with AuthError. Non-Lyzr agents unaffected.",
        fix="export LYZR_API_KEY=sk-default-...   # from Lyzr Studio → Agent → API Key",
    ),
}


# Tracing env vars.

_TRACING_EXPLANATIONS: dict[str, CheckExplanation] = {
    "MOVATE_TRACER": CheckExplanation(
        what="Explicit tracer selection: `stdout` | `langfuse` | `otel` | `composite`.",
        why="Override of the auto-detect rule. Default is stdout when no other tracer is configured.",
        failure_impact="No effect when unset — auto-detect kicks in.",
        fix="export MDK_TRACER=langfuse   # or otel, composite, stdout",
    ),
    "LANGFUSE_SECRET_KEY": CheckExplanation(
        what="Server-side authentication for Langfuse.",
        why="Required when `MDK_TRACER=langfuse`. Pairs with LANGFUSE_PUBLIC_KEY.",
        failure_impact="Setting MDK_TRACER=langfuse without this key falls back silently to stdout.",
        fix="export LANGFUSE_SECRET_KEY=sk-lf-...   # from langfuse.com → Settings → API Keys",
    ),
    "LANGFUSE_PUBLIC_KEY": CheckExplanation(
        what="Public-key auth for Langfuse.",
        why="Required alongside LANGFUSE_SECRET_KEY when using the langfuse tracer.",
        failure_impact="Setting MDK_TRACER=langfuse without this falls back to stdout.",
        fix="export LANGFUSE_PUBLIC_KEY=pk-lf-...",
    ),
    "LANGFUSE_HOST": CheckExplanation(
        what="Langfuse server URL.",
        why="Override the default `cloud.langfuse.com` host. Set for self-hosted Langfuse.",
        failure_impact="Unset = use Langfuse cloud. Usually fine.",
        fix="export LANGFUSE_HOST=https://langfuse.your-domain.com",
    ),
    "OTEL_EXPORTER_OTLP_ENDPOINT": CheckExplanation(
        what="OTLP receiver URL — where spans get sent.",
        why="Required when `MDK_TRACER=otel`. Could be Jaeger, Honeycomb, Datadog, etc.",
        failure_impact="MDK_TRACER=otel without this falls back to stdout.",
        fix="export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io",
    ),
    "OTEL_SERVICE_NAME": CheckExplanation(
        what="Logical service name attached to every emitted span.",
        why="Lets your observability backend group spans by service.",
        failure_impact="OTel spans get a default service.name (`unknown_service`). Hard to find in dashboards.",
        fix="export OTEL_SERVICE_NAME=mdk-prod",
    ),
}


# Storage + project config.

_STORAGE_AND_PROJECT_EXPLANATIONS: dict[str, CheckExplanation] = {
    "storage (sqlite)": CheckExplanation(
        what="Local SQLite database at ~/.movate/local.db.",
        why="Persists RunRecords + FailureRecords + EvalRecords + BenchRecords for local-mode runs.",
        failure_impact="Database creation fails (permissions, disk full). Runs still execute; nothing gets persisted.",
        fix="Check ~/.movate is writable. `mkdir -p ~/.movate` if missing.",
    ),
    "pricing": CheckExplanation(
        what="Canonical price-per-1k-tokens table.",
        why="Powers `cost_usd` on every Metrics record + cost forecasts on `mdk validate`.",
        failure_impact="Cost reporting reads as $0.00 for unknown models. Doesn't block runs.",
        fix="The pricing table ships with the CLI — failure means a broken install.",
    ),
    "project.yaml": CheckExplanation(
        what="Project-level config file — canonical filename (May 2026+). Carries layered defaults, policy, runtime gates, skills allowlist, eval/bench config.",
        why="Loaded by every `mdk` command via `load_project_config`. Per-agent `agent.yaml` always wins per-key; entries here only fill gaps. Absent = permissive defaults.",
        failure_impact="No config = no project policy enforced. Agents run without provider / cost / runtime restrictions.",
        fix="`mdk init <name>` scaffolds a canonical, self-documenting `project.yaml`. To migrate from a legacy filename: rename + delete the old file.",
    ),
    "policy.yaml": CheckExplanation(
        what="Legacy v1.x name for the project-level config (renamed to `project.yaml` in May 2026).",
        why="Still loaded for back-compat; emits a one-shot deprecation warning. Operators should rename to `project.yaml`.",
        failure_impact="Same as project.yaml — no policy enforced.",
        fix="`mv policy.yaml project.yaml` (the loader will pick up the new name; no other changes required).",
    ),
    "movate.yaml": CheckExplanation(
        what="Original v0.x name for the project-level config. Still loaded for back-compat; renamed first to `policy.yaml` (v1.x), now to `project.yaml` (May 2026+).",
        why="Loader accepts the legacy name + emits a one-shot deprecation warning. New projects scaffolded via `mdk init` use `project.yaml`.",
        failure_impact="Same as project.yaml.",
        fix="`mv movate.yaml project.yaml`.",
    ),
    "project config parses": CheckExplanation(
        what="The project config file (project.yaml / policy.yaml / movate.yaml) parses successfully as ProjectConfig.",
        why="A malformed config blocks every project-aware command: `mdk validate`, `mdk add`, `mdk eval`, `mdk deploy`. Catching it at doctor time prevents the failure mode where every command in a session errors with the same cryptic Pydantic ValidationError.",
        failure_impact="Project-wide commands fail until the config is fixed.",
        fix="Run `mdk validate` for the full error. Common causes: unknown top-level field, wrong type on `defaults.model.params.*`, malformed YAML.",
    ),
    "agents/": CheckExplanation(
        what="Standard project subdirectory for agent definitions.",
        why="`mdk add <template>` scaffolds new agents here. `mdk run <name>` resolves bare names under this directory.",
        failure_impact="`mdk add` fails until you create it; `mdk run` can't resolve bare names.",
        fix="`mkdir agents && touch agents/.gitkeep` — or re-run `mdk init <name>` which scaffolds it automatically.",
    ),
    "skills/": CheckExplanation(
        what="Standard project subdirectory for reusable skill definitions (`skill.yaml` + `impl.py`).",
        why="Agents that declare `skills: [foo]` resolve the skill at `<project>/skills/foo/skill.yaml`. Auto-scaffolded by `mdk add` when an agent declares skills.",
        failure_impact="Agents that declare skills fail to load with `SkillLoadError: empty registry`.",
        fix="`mkdir skills && touch skills/.gitkeep` — or re-run `mdk init <name>`. `mdk add` auto-creates skill dirs for declared skills.",
    ),
    "contexts/": CheckExplanation(
        what="Standard project subdirectory for reusable Markdown contexts (prepended to prompts at render time).",
        why="Agents that declare `contexts: [foo]` resolve to `<project>/contexts/foo.md`. Per-agent overrides at `agents/<name>/contexts/foo.md` win when names collide.",
        failure_impact="Agents that declare contexts fail to load with `ContextLoadError: not registered`.",
        fix="`mkdir contexts && touch contexts/.gitkeep` — or re-run `mdk init <name>`. Drop hand-authored `.md` files in here as the shared knowledge base for prompts.",
    ),
    "kb/": CheckExplanation(
        what="Standard project subdirectory for knowledge assets (JSON corpora, documents, future embeddings).",
        why="Skills like `kb-lookup` resolve their data via `movate.core.kb_loader.resolve_kb_file(name)`, which checks `<project>/kb/<name>` first before falling back to a bundled default. Drop your real corpus here to override the demo data.",
        failure_impact="Skills using `resolve_kb_file` fall back to bundled defaults (usually a demo corpus). Not a hard failure, but operators expect their KB to be used.",
        fix="`mkdir kb && touch kb/.gitkeep` — or re-run `mdk init <name>`. See `kb/README.md` for filename conventions per skill.",
    ),
}


# KB parsing deps — gates `mdk kb ingest` for each document format.
# Missing = that format silently falls back to "unsupported" at ingest time.

_KB_DEP_EXPLANATIONS: dict[str, CheckExplanation] = {
    "pypdf": CheckExplanation(
        what="KB dep — pypdf Python PDF parser.",
        why=(
            "`mdk kb ingest` extracts text from machine-readable PDFs directly "
            "(no OCR needed). pypdf reads the PDF text layer and feeds it into "
            "the chunker + embedder pipeline. Fast, zero system deps."
        ),
        failure_impact=(
            "Machine-readable PDFs are skipped at ingest time. Scanned PDFs "
            "still work via the OCR path (pdf2image + pytesseract/easyocr), "
            "but text-layer PDFs lose their content."
        ),
        fix="uv add 'movate-cli[kb]'",
    ),
    "python-docx": CheckExplanation(
        what="KB dep — python-docx DOCX parser.",
        why=(
            "`mdk kb ingest` extracts text from Word documents (.docx). "
            "Reads paragraphs, headers, and table cells. Common format for "
            "HR policies, SOPs, and compliance docs."
        ),
        failure_impact="`.docx` files are skipped at ingest time. PDF/MD/HTML paths unaffected.",
        fix="uv add 'movate-cli[kb]'",
    ),
    "beautifulsoup4": CheckExplanation(
        what="KB dep — BeautifulSoup4 HTML parser.",
        why=(
            "`mdk kb ingest` extracts text from `.html` files (knowledge bases "
            "exported from Confluence, Notion, Zendesk, etc.). BS4 strips nav, "
            "scripts, and boilerplate; `readability-lxml` cleans article body."
        ),
        failure_impact="`.html` files are skipped at ingest time. PDF/MD/DOCX paths unaffected.",
        fix="uv add 'movate-cli[kb]'",
    ),
}


# OCR deps — gates scanning of image-backed documents.
# The four Python packages and one system binary are independent layers;
# install only what your document mix needs.
#
# Typical setups:
#   Text PDFs only        → pypdf alone (no OCR needed)
#   Scanned PDFs          → [ocr] extra + tesseract binary
#   Standalone images     → [ocr] extra (Pillow) + tesseract OR [easyocr]
#   Noisy/multilingual    → [easyocr] (no system binary, GPU-optional)
#   Full pipeline         → [kb] + [ocr] + [easyocr] + tesseract binary

_OCR_DEP_EXPLANATIONS: dict[str, CheckExplanation] = {
    "pillow": CheckExplanation(
        what="OCR dep — Pillow (Python Imaging Library fork).",
        why=(
            "Decodes standalone image files (PNG, JPG, JPEG, TIFF, BMP) into "
            "PIL.Image objects for OCR. Also used inside the PDF OCR path once "
            "pdf2image rasterizes a page. `MOVATE_OCR_BACKEND` (tesseract or "
            "easyocr) receives the PIL.Image — Pillow is the common intake layer "
            "for BOTH backends. Without Pillow, standalone image KB files are "
            "silently skipped at ingest time."
        ),
        failure_impact=(
            "PNG/JPG/TIFF/BMP files dropped at `mdk kb ingest` with "
            "'unsupported extension' warning. PDF OCR path also breaks "
            "(pdf2image returns PIL images; they can't be decoded further)."
        ),
        fix="uv add 'movate-cli[ocr]'",
    ),
    "pdf2image": CheckExplanation(
        what="OCR dep — pdf2image (Poppler wrapper).",
        why=(
            "Rasterizes PDF pages to PIL.Image at 300 DPI so Tesseract or "
            "EasyOCR can read them. Required only for scanned PDFs — PDFs with "
            "a text layer are parsed directly by pypdf without rasterization. "
            "Requires Poppler system binaries (`pdftoppm`)."
        ),
        failure_impact=(
            "Scanned / image-only PDFs silently fall back to empty text at "
            "ingest time. Machine-readable PDFs (text layer) unaffected."
        ),
        fix="uv add 'movate-cli[ocr]'  # then: brew install poppler  /  apt-get install poppler-utils",
    ),
    "pytesseract": CheckExplanation(
        what="OCR dep — pytesseract (Tesseract Python wrapper).",
        why=(
            "Thin binding around the Tesseract binary. Used when "
            "`MOVATE_OCR_BACKEND=tesseract` (the default). Works well for "
            "clean, high-contrast printed text. Supports 100+ languages via "
            "`MOVATE_OCR_LANG=eng+fra` (Tesseract 3-letter ISO codes)."
        ),
        failure_impact=(
            "The default OCR backend unavailable. `mdk kb ingest` skips "
            "scanned PDFs and image files. Switch to EasyOCR "
            "(`MOVATE_OCR_BACKEND=easyocr`) as a drop-in alternative."
        ),
        fix="uv add 'movate-cli[ocr]'  # also needs: brew install tesseract  /  apt-get install tesseract-ocr",
    ),
    "tesseract": CheckExplanation(
        what="OCR dep — Tesseract system binary (NOT a Python package).",
        why=(
            "The actual OCR engine that pytesseract calls. Apache-2.0 licensed; "
            "maintained by Google. Must be installed separately from the Python "
            "packages via your OS package manager. Without it, `pytesseract` "
            "raises a TesseractNotFoundError at ingest time."
        ),
        failure_impact=(
            "Every OCR call via the default backend fails with TesseractNotFoundError. "
            "EasyOCR (`MOVATE_OCR_BACKEND=easyocr`) is unaffected — it uses "
            "PyTorch internally, no system binary."
        ),
        fix="brew install tesseract   # macOS\napt-get install tesseract-ocr   # Debian/Ubuntu",
    ),
    "easyocr": CheckExplanation(
        what="OCR dep — EasyOCR (pure-Python, torch-based OCR backend).",
        why=(
            "Alternative to Tesseract activated via `MOVATE_OCR_BACKEND=easyocr`. "
            "Advantages: no system binary, better accuracy on noisy scans, "
            "handwriting, and non-Latin scripts (Arabic, Japanese, Korean, etc.). "
            "GPU-optional: set `MOVATE_EASYOCR_GPU=1` to use a CUDA GPU; "
            "CPU-mode is the default and works fine for batch ingest. "
            "Downloads model weights (~50 MB) on first use."
        ),
        failure_impact=(
            "`MOVATE_OCR_BACKEND=easyocr` raises ImportError at ingest time. "
            "Falls back gracefully to `None` (skips the file) rather than "
            "crashing the ingest run. Tesseract backend unaffected."
        ),
        fix="uv add 'movate-cli[easyocr]'",
    ),
}


# Aggregate registry. Public so the doctor command can look up
# explanations by check identifier.

EXPLANATIONS: dict[str, CheckExplanation] = {
    **{f"dep: {k}": v for k, v in _REQUIRED_DEP_EXPLANATIONS.items()},
    **{f"opt: {k}": v for k, v in _OPTIONAL_DEP_EXPLANATIONS.items()},
    **{f"kb: {k}": v for k, v in _KB_DEP_EXPLANATIONS.items()},
    **{f"ocr: {k}": v for k, v in _OCR_DEP_EXPLANATIONS.items()},
    **{f"runtime: {k}": v for k, v in _RUNTIME_EXPLANATIONS.items()},
    **_PROVIDER_KEY_EXPLANATIONS,
    **_TRACING_EXPLANATIONS,
    **_STORAGE_AND_PROJECT_EXPLANATIONS,
}
