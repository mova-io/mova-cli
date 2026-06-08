# Mova-iO Voice Playground — generic standalone Lyzr voice adapter. Container
# image for Azure Container Apps (or any Docker-compatible host). Stays small
# (~150MB) by using the slim Python base + only the demo's actual runtime deps.
#
# Builds from the *repo root* so the package's movate.voice source installs:
#   az acr build -r <acr> -t mova-voice-playground:<tag> -f mova-voice-playground/Dockerfile .
FROM python:3.11-slim

# uv: a fast pip/venv replacement. ~3x faster install than pip; cuts the build
# step from ~90s to ~30s on a cold cache. No runtime cost.
RUN pip install --no-cache-dir uv==0.5.11

# Non-root user — basic hardening for a public-facing demo. /app owned by it.
RUN useradd --create-home --shell /bin/bash --uid 10001 mdk
WORKDIR /app
ENV PATH="/home/mdk/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install the package and the demo's HTTP/WS deps. We use --system because
# the container itself *is* the venv; no need for a nested one.
#
# ADR 066 — the version is git-derived, but the build context excludes .git, so
# the host passes the computed CalVer as a build-arg; the hatch metadata hook
# (hatch_build.py + scripts/calver_version.py) reads it via MOVATE_BUILD_VERSION.
# Both files MUST be present for the build backend to resolve the dynamic version.
ARG MOVATE_BUILD_VERSION=""
COPY pyproject.toml README.md hatch_build.py ./
COPY scripts/calver_version.py ./scripts/
COPY src/ ./src/
RUN MOVATE_BUILD_VERSION="${MOVATE_BUILD_VERSION}" uv pip install --system --no-cache \
    ".[voice]" \
    "fastapi>=0.110,<1" "uvicorn[standard]>=0.27,<1" "websockets>=12,<14" \
    "pydub>=0.25,<1" \
    "azure-storage-blob>=12.19,<13" \
    "livekit>=0.17,<1" "livekit-api>=0.7,<1"
# ADR 067 consolidation note: the legacy mdk-voice per-provider extras
# (`[openai,deepgram,cartesia,elevenlabs]`) collapse into the single mdk[voice]
# extra in movate-cli's pyproject — same SDK pins, single name.
# ── B4 call recording: azure-storage-blob is included in the demo image but
# NOT in the package's base extras (CLAUDE.md rule 6 — recording is a
# demo-level concern). Lazy-imported inside mova-voice-playground/recording.py
# so the package itself stays SDK-free.

# Bring in only what the demo actually needs at runtime — keeps the image lean
# and the attack surface small. The container package dir stays `web_demo/`
# (server.py path-inserts its own dir + the CMD imports `web_demo.server`).
COPY mova-voice-playground/agent.py \
     mova-voice-playground/server.py \
     mova-voice-playground/twilio_bridge.py \
     mova-voice-playground/livekit_bridge.py \
     mova-voice-playground/index.html \
     mova-voice-playground/doctor.py \
     mova-voice-playground/recording.py \
     ./web_demo/
# Static assets (Mova-iO logos, future brand/marketing files). Mounted by
# server.py at /static/ when present. Copies ALL files under static/ so
# adding new assets doesn't need a Dockerfile change.
COPY mova-voice-playground/static/ ./web_demo/static/

RUN chown -R mdk:mdk /app
USER mdk

# The demo listens on $PORT (Azure Container Apps injects PORT=8000 by
# default; the env var lets us run the same image locally with `-e PORT=8765`).
ENV PORT=8000
EXPOSE 8000

# Run the server. CMD is execed by uvicorn directly so signals propagate
# (SIGTERM → graceful WS close, important for ACA's rolling-update story).
CMD ["sh", "-c", "uvicorn web_demo.server:app --host 0.0.0.0 --port ${PORT}"]
