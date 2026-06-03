# mdk-voice web demo — container image for Azure Container Apps (or any
# Docker-compatible host). Stays small (~150MB) by using the slim Python base
# and installing only the demo's actual runtime deps — no test/lint toolchain.
#
# Builds from the *repo root* so the local mdk_voice source can be installed
# in editable mode: `docker build -f examples/web_demo/Dockerfile .`
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
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache \
    ".[openai,deepgram,cartesia,elevenlabs]" \
    "fastapi>=0.110,<1" "uvicorn[standard]>=0.27,<1" "websockets>=12,<14" \
    "pydub>=0.25,<1" \
    "azure-storage-blob>=12.19,<13"
# ── B4 call recording: azure-storage-blob is included in the demo image but
# NOT in the package's base extras (CLAUDE.md rule 6 — recording is a
# demo-level concern). Lazy-imported inside examples/web_demo/recording.py
# so the package itself stays SDK-free.

# Bring in only what the demo actually needs at runtime — keeps the image lean
# and the attack surface small.
COPY examples/web_demo/agent.py \
     examples/web_demo/server.py \
     examples/web_demo/twilio_bridge.py \
     examples/web_demo/index.html \
     examples/web_demo/doctor.py \
     examples/web_demo/recording.py \
     ./web_demo/
# Static assets (Movate logos, future brand/marketing files). Mounted by
# server.py at /static/ when present. Copies ALL files under static/ so
# adding new assets doesn't need a Dockerfile change.
COPY examples/web_demo/static/ ./web_demo/static/

RUN chown -R mdk:mdk /app
USER mdk

# The demo listens on $PORT (Azure Container Apps injects PORT=8000 by
# default; the env var lets us run the same image locally with `-e PORT=8765`).
ENV PORT=8000
EXPOSE 8000

# Run the server. CMD is execed by uvicorn directly so signals propagate
# (SIGTERM → graceful WS close, important for ACA's rolling-update story).
CMD ["sh", "-c", "uvicorn web_demo.server:app --host 0.0.0.0 --port ${PORT}"]
