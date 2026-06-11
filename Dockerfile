# Multi-stage image for the movate runtime.
#
# Two final targets share the same base + app layers; only the
# default CMD differs:
#
#   docker build --target runtime -t movate-runtime .   # serves HTTP
#   docker build --target worker  -t movate-worker  .   # drains queue
#
# In Azure, ACR builds ONE image (target=runtime by default) and the
# Container Apps override `args` to invoke `movate serve` or
# `movate worker`. Splitting into two targets here is for local dev
# convenience — `docker run movate-worker` Just Works without remembering
# the args.

# ---------------------------------------------------------------------------
# Stage 1: base — system + Python + uv
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # uv writes its venv here; predictable path so the runtime stage
    # can copy it without globbing.
    UV_PROJECT_ENVIRONMENT=/opt/movate/.venv \
    PATH=/opt/movate/.venv/bin:$PATH

# Pin uv to a known-good version for reproducible builds. Bumping
# requires updating uv.lock too; the docs explain that path.
COPY --from=ghcr.io/astral-sh/uv:0.5.20 /uv /usr/local/bin/uv

WORKDIR /opt/movate

# ---------------------------------------------------------------------------
# Stage 2: deps — install Python deps. Cached separately from app code so
# code-only changes don't bust the (slow) pip layer.
# ---------------------------------------------------------------------------
FROM base AS deps

COPY pyproject.toml uv.lock ./
# --all-extras installs runtime + langfuse + otel for full observability.
# --no-dev keeps the image lean (no pytest, no mypy, no ruff).
# --no-install-project skips building the movate wheel here — that needs
# README.md + src/, which we deliberately don't copy until the next stage
# to keep this slow layer cached across source-only changes. The app
# stage finalizes the install once those files are present.
RUN uv sync --all-extras --no-extra airflow --no-dev --frozen --no-install-project

# ---------------------------------------------------------------------------
# Stage 3: app — copy the source + README, then complete the sync to
# install the movate project itself into the venv built above.
# ---------------------------------------------------------------------------
FROM deps AS app

# ADR 066 — the version is git-derived, but the Docker context excludes .git, so
# the host passes the computed CalVer as a build-arg; the hatch metadata hook
# reads it via MOVATE_BUILD_VERSION. hatch_build.py + scripts/calver_version.py
# must be present for the build backend to resolve the dynamic version.
ARG MOVATE_BUILD_VERSION=""
COPY src/ ./src/
COPY README.md ./
COPY pyproject.toml uv.lock hatch_build.py ./
COPY scripts/calver_version.py ./scripts/
RUN MOVATE_BUILD_VERSION="${MOVATE_BUILD_VERSION}" \
    uv sync --all-extras --no-extra airflow --no-dev --frozen

# Bake the default templates so `movate init` works inside the
# container if an operator shells in. Production runs ignore this.
COPY src/movate/templates/ /opt/movate/.venv/lib/python3.11/site-packages/movate/templates/

# Operator-provided agents/ directory. ACA can mount a volume here
# for dynamic multi-agent loading (post-v1.0), but the default
# pattern bakes the repo's agents/ into the image so the runtime
# ships with a known catalog. Empty if the repo has no agents/ yet.
#
# The COPY uses the directory itself + `--parents`-free trick: COPYing
# a directory creates the destination if it doesn't exist, so we don't
# need a separate mkdir. If `agents/` is missing in the build context,
# Docker errors out — that's intentional, since shipping zero agents
# is almost always a mistake. To deploy with no agents (volume-mount
# pattern), add an empty `agents/.keep` file.
COPY agents/ /app/agents/
ENV MOVATE_AGENTS_PATH=/app/agents

# Operator-provided workflows/ directory — same pattern as agents/ above. The
# temporal worker (`movate worker --backend temporal`) scans MOVATE_WORKFLOWS_PATH
# for `runtime: temporal` workflow.yaml files to register; WITHOUT this the image
# ships zero workflows and the worker logs "workflows_root_missing path=workflows
# … workflows=0" — it connects to Temporal but hosts nothing to run (observed
# 2026-06-08, the refund-approval durable-HITL demo never registered). Baking the
# repo's workflows/ here ships the known catalog (e.g. refund-approval); a
# volume-mount / --from-storage (ADR 088) deployment can still override the path.
COPY workflows/ /app/workflows/
ENV MOVATE_WORKFLOWS_PATH=/app/workflows

# Certification suite (driver + scenarios) — lets an in-env runner (the ACA
# certification Job) execute `python -m certification.run_suite --target dev`
# from inside the environment: the shared Postgres is reachable (lights the
# side-effects capability the laptop run must SKIP) and the OTLP collector
# receives the mdk.certification.scenario metric behind the Grafana matrix.
# Pure data+driver code, adds no runtime deps; PYTHONPATH=/app makes it
# importable without changing WORKDIR.
COPY certification/ /app/certification/

# Project governance policy for the baked catalog (ADR 093/096). The config
# loader (`load_project_config()`) reads the project base file from the
# process CWD — which is this WORKDIR (/opt/movate), NOT /app — so the policy
# must land here for `movate serve` / `movate worker` (and the Temporal
# worker's `configure_activities`, #822) to pick it up. Baked under the
# loader's canonical base-file name `project.yaml`; the source file keeps the
# `policy.yaml` name because it documents the governance gates shipped with
# the expense-approval certification deployable (its header explains scope:
# the policy applies image-wide, and its constraints are satisfied by every
# bundled agent so gates EVALUATE without blocking).
COPY workflows/expense-approval/policy.yaml ./project.yaml

# Default tracer goes to stdout — Container Apps captures stdout to
# Log Analytics. Operators flip MOVATE_TRACER=otel via env to switch
# to OTLP.
ENV MOVATE_TRACER=stdout

# Non-root user for the app (defense in depth — Container Apps doesn't
# enforce a non-root requirement, but doing it ourselves means an
# image breakout has less to work with).
RUN useradd --create-home --home-dir /home/movate --shell /bin/bash movate \
    && chown -R movate:movate /opt/movate /app
USER movate

# ---------------------------------------------------------------------------
# Stage 4a: runtime — HTTP API, listens on 8000
# ---------------------------------------------------------------------------
FROM app AS runtime

EXPOSE 8000

# ENTRYPOINT picks the binary; CMD picks the verb + flags.
# ACA overrides both via `command` + `args` in the Container App spec.
ENTRYPOINT ["movate"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------
# Stage 4b: worker — drains the job queue, no ingress
# ---------------------------------------------------------------------------
FROM app AS worker

ENTRYPOINT ["movate"]
CMD ["worker"]

# ---------------------------------------------------------------------------
# Stage 5: playground — hosts the Chainlit playground (ADR 053 D2).
#
# Same codebase + CLI as the runtime, but a DIFFERENT extras set: it needs the
# `[playground]` extra (chainlit + its async-ORM deps) and MUST NOT carry
# `[airflow]` — the two are declared conflicting in pyproject.toml (chainlit's
# data layer needs sqlalchemy>=2.0; airflow pins 1.4), so `uv sync --all-extras`
# can't co-resolve them (ADR 053 D2 + Risks). We therefore give the playground
# its OWN deps + app layers built from `base` (NOT layered on the runtime `app`
# stage, which installs a conflicting extras set) and exclude airflow via
# `--no-extra airflow`. The runtime/worker stages above are untouched.
#
# Build:  docker build --target playground -t movate-playground .
# Run  :  mdk playground serve --host 0.0.0.0 --port 8765 --runtime-url <api>
# ---------------------------------------------------------------------------
FROM base AS playground-deps

COPY pyproject.toml uv.lock ./
# All extras EXCEPT airflow (see stage header for the conflict rationale) —
# this pulls in `[playground]` (chainlit) while keeping the resolve clean.
RUN uv sync --all-extras --no-extra airflow --no-dev --frozen --no-install-project

FROM playground-deps AS playground

# ADR 066 — see the `app` stage: git-derived version via MOVATE_BUILD_VERSION
# build-arg + the hatch hook files (the build context excludes .git).
ARG MOVATE_BUILD_VERSION=""
COPY src/ ./src/
COPY README.md ./
COPY pyproject.toml uv.lock hatch_build.py ./
COPY scripts/calver_version.py ./scripts/
RUN MOVATE_BUILD_VERSION="${MOVATE_BUILD_VERSION}" \
    uv sync --all-extras --no-extra airflow --no-dev --frozen

# Bake the default templates so `movate init` works if an operator shells in.
COPY src/movate/templates/ /opt/movate/.venv/lib/python3.11/site-packages/movate/templates/

# The playground is an HTTP client of the runtime — it carries no agents/
# catalog of its own. We still copy agents/ so the COPY context matches the
# runtime stages (build-context parity) and `movate` subcommands that probe the
# path don't trip; the playground command never reads it.
COPY agents/ /app/agents/
ENV MOVATE_AGENTS_PATH=/app/agents

# Non-root user (defense in depth — matches the runtime/worker stages).
RUN useradd --create-home --home-dir /home/movate --shell /bin/bash movate \
    && chown -R movate:movate /opt/movate /app
USER movate

EXPOSE 8765

# ENTRYPOINT is the `mdk` CLI (an alias of `movate`); CMD launches Chainlit.
# ACA overrides CMD via `command` + `args` in containerapp-playground.bicep
# (it injects --runtime-url / --headless / --no-targets), so this CMD is just
# the sensible local-`docker run` default.
ENTRYPOINT ["mdk"]
CMD ["playground", "serve", "--host", "0.0.0.0", "--port", "8765", "--headless"]
