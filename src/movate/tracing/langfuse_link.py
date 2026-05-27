"""Compute a Langfuse trace deep-link from the environment (ADR 031 D1).

This is a *pure* helper — it does NOT import the ``langfuse`` package and has
no SDK dependency. It only reads the same env vars the Langfuse client is
configured from (``LANGFUSE_HOST`` / ``LANGFUSE_BASE_URL``, optionally
``LANGFUSE_PROJECT_ID``) and string-formats a URL. It lives in the tracing
layer so the CLI edges (``mdk run`` / ``runs show`` / ``explain`` / ``trace`` /
deploy next-steps) can surface a one-click link to the dashboard without
reaching into the SDK.

No-op contract: when Langfuse isn't configured (no host AND no Langfuse keys),
or there is no trace id, :func:`langfuse_trace_url` returns ``None`` — callers
simply omit the link. It never raises.
"""

from __future__ import annotations

import os

# Langfuse Cloud default host (matches ``langfuse._build_client_from_env``).
_DEFAULT_HOST = "https://cloud.langfuse.com"


def _configured_host() -> str | None:
    """Return the configured Langfuse host, or ``None`` if Langfuse is off.

    We only return a host when Langfuse is plausibly in use:

    * an explicit ``LANGFUSE_HOST`` / ``LANGFUSE_BASE_URL`` is set, OR
    * a Langfuse secret key is set (implicit opt-in, same signal
      ``build_tracer`` auto-detects on) — in which case we fall back to the
      Cloud default host the client would use.

    When neither holds, Langfuse isn't configured and we return ``None`` so
    the caller omits the trace link entirely (the no-op contract).
    """
    explicit = (
        os.environ.get("LANGFUSE_HOST", "").strip()
        or os.environ.get("LANGFUSE_BASE_URL", "").strip()
    )
    if explicit:
        return explicit.rstrip("/")
    # No explicit host, but a secret key means the client defaults to Cloud.
    if os.environ.get("LANGFUSE_SECRET_KEY", "").strip():
        return _DEFAULT_HOST
    return None


def langfuse_trace_url(trace_id: str | None) -> str | None:
    """Build the Langfuse dashboard URL for ``trace_id``, or ``None``.

    Returns ``None`` (no link, no error) when:

    * ``trace_id`` is empty / ``None`` (tracing was off for the run), or
    * Langfuse isn't configured (no host and no secret key).

    URL shape:

    * ``LANGFUSE_PROJECT_ID`` set → the project-scoped deep link
      ``{host}/project/{project_id}/traces/{trace_id}`` (lands directly on the
      trace within the project).
    * otherwise → ``{host}/trace/{trace_id}`` (the host-level shortcut the
      Langfuse UI resolves to the trace without needing the project id).
    """
    tid = (trace_id or "").strip()
    if not tid:
        return None
    host = _configured_host()
    if not host:
        return None
    project_id = os.environ.get("LANGFUSE_PROJECT_ID", "").strip()
    if project_id:
        return f"{host}/project/{project_id}/traces/{tid}"
    return f"{host}/trace/{tid}"
