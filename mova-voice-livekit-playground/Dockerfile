# Mova-iO Voice Playground (LiveKit) — thin proxy in front of Lyzr's hosted
# voice service. Unlike the sibling `mova-voice-playground`, this image needs
# NO movate.voice source (the agent runs on LiveKit Cloud), so the build context
# is just this directory and the image stays tiny (~120 MB).
#
#   az acr build -r <acr> -t mova-voice-livekit-playground:<tag> \
#       -f mova-voice-livekit-playground/Dockerfile mova-voice-livekit-playground
FROM python:3.11-slim

RUN pip install --no-cache-dir uv==0.5.11

RUN useradd --create-home --shell /bin/bash --uid 10001 mdk
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Only three runtime deps: FastAPI, an ASGI server, and httpx to forward to Lyzr.
RUN uv pip install --system --no-cache \
    "fastapi>=0.110,<1" "uvicorn[standard]>=0.27,<1" "httpx>=0.27,<1"

COPY server.py index.html ./
COPY static/ ./static/

RUN chown -R mdk:mdk /app
USER mdk

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
