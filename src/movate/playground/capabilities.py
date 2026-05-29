"""Runtime capability discovery for the playground (pure logic).

The playground is **capability-aware**: at chat start it asks the
runtime what it can do (``GET /api/v1/capabilities``) and adapts the
chat experience accordingly, so a single playground build auto-upgrades
as new runtime features land — no playground release required.

The three feature axes the playground cares about today:

* **sessions** — does the runtime offer *server-managed* multi-turn
  memory (``POST /api/v1/sessions`` + ``/sessions/{id}/messages``)? If
  yes, conversation memory lives on the server (ADR 045 D10). If no
  (the common case today), the playground re-sends prior turns as
  context to the stateless run endpoint — client-managed memory.
* **run_streaming** — does the runs endpoint stream tokens over SSE
  (ADR 045 D11 / the existing ``POST /agents/{name}/runs/stream``)?
  If yes, tokens render live; else the response is buffered.
* **feedback_api** — is there a first-class feedback endpoint
  (ADR 045 D14 — ``POST /runs/{id}/feedback``)? If yes, route 👍/👎
  there; else fall back to the runtime's existing feedback persistence.

This module is **pure logic** — no Chainlit, no httpx-at-import — so it
is unit-testable in isolation. The Chainlit app fetches the raw JSON and
hands it to :func:`parse_capabilities`; everything downstream branches on
the returned :class:`RuntimeCapabilities` dataclass.

Conservative-by-default: when the endpoint 404s (a runtime that predates
capability discovery) or the payload is malformed, every flag is off and
every limit takes a sane default — i.e. today's single-shot,
client-managed, buffered behavior. The playground never *assumes* a
feature it could not confirm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Default upload ceilings used when the runtime does not advertise its
# own limits. Mirrors the conservative ``cl.AskFileMessage`` caps the
# pre-enhancement playground hard-coded for KB upload.
DEFAULT_MAX_UPLOAD_MB: int = 20
DEFAULT_MAX_UPLOAD_COUNT: int = 10


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Feature/limit snapshot of the runtime the playground talks to.

    Built by :func:`parse_capabilities` from the
    ``GET /api/v1/capabilities`` payload (or the all-off default when the
    endpoint is absent). Immutable — fetched once per chat session and
    read everywhere the UI needs to branch.
    """

    sessions: bool = False
    """True when the runtime offers server-managed conversation sessions
    (``POST /api/v1/sessions``). Selects :class:`SessionBackend` over
    :class:`ClientManagedBackend`."""

    run_streaming: bool = False
    """True when the runs endpoint can stream tokens over SSE. Enables
    live-token rendering; otherwise the response is buffered."""

    feedback_api: bool = False
    """True when a first-class ``POST /runs/{id}/feedback`` endpoint is
    advertised. Routes feedback there; otherwise the legacy path."""

    max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB
    """Per-file upload ceiling (MB). Taken from
    ``limits.max_kb_upload_mb`` when present, else the default."""

    max_upload_count: int = DEFAULT_MAX_UPLOAD_COUNT
    """Max files per upload. Taken from ``limits.max_kb_upload_count``
    when present, else the default."""

    raw: dict[str, Any] | None = None
    """The raw capabilities payload, kept for diagnostics / future
    feature gating. ``None`` when the endpoint was unavailable."""


def default_capabilities() -> RuntimeCapabilities:
    """Return the all-off, all-default capability set.

    This is what the playground uses when the capabilities endpoint is
    unreachable / 404s — i.e. today's behavior: single-shot run path,
    client-managed conversation context, buffered (non-streamed)
    responses, legacy feedback persistence, default upload caps.
    """
    return RuntimeCapabilities()


def _coerce_bool(value: Any) -> bool:
    """Best-effort truthiness for a capability flag.

    Tolerates the shapes a capabilities endpoint might use for a flag:
    a JSON ``true``/``false``, a ``1``/``0``, or a string like
    ``"true"``/``"enabled"``. Anything unrecognised reads False — we
    never *opt in* to a feature on an ambiguous value.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on", "enabled"}
    return False


def _flag(features: Any, *names: str) -> bool:
    """Read a feature flag that may live under any of ``names``.

    ``features`` may be a dict ``{"sessions": true, ...}`` OR a list of
    enabled feature slugs ``["sessions", "streaming"]`` — both are common
    capability-endpoint shapes. Any of ``names`` matching counts as on,
    so we tolerate alias drift (e.g. ``"sessions"`` vs ``"stateful_sessions"``).
    """
    if isinstance(features, dict):
        return any(_coerce_bool(features.get(n)) for n in names)
    if isinstance(features, (list, tuple, set)):
        present = {str(item).strip().lower() for item in features}
        return any(n.lower() in present for n in names)
    return False


def _int_limit(limits: Any, name: str, default: int) -> int:
    """Read a positive integer limit from the ``limits`` block.

    Falls back to ``default`` when absent, non-numeric, or non-positive.
    A zero/negative advertised limit is treated as "unset" rather than
    "disallow uploads" — the latter would silently break the upload
    affordance on a misconfigured runtime.
    """
    if not isinstance(limits, dict):
        return default
    raw = limits.get(name)
    if isinstance(raw, bool):  # bool is an int subclass — exclude it
        return default
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    return default


def parse_capabilities(payload: dict[str, Any] | None) -> RuntimeCapabilities:
    """Build a :class:`RuntimeCapabilities` from the endpoint payload.

    Expected (forward-looking) shape — every part optional::

        {
          "features": {"sessions": true, "run_streaming": true,
                       "feedback_api": true},
          "limits": {"max_kb_upload_mb": 50, "max_kb_upload_count": 25}
        }

    ``features`` may alternatively be a list of enabled slugs. When
    ``payload`` is ``None`` (endpoint unavailable), returns
    :func:`default_capabilities` — every flag off, every limit default.

    This function NEVER raises on a malformed payload: a runtime that
    ships a half-baked capabilities document must degrade to today's
    behavior, not crash the playground.
    """
    if not isinstance(payload, dict):
        return default_capabilities()

    features = payload.get("features")
    limits = payload.get("limits")

    return RuntimeCapabilities(
        sessions=_flag(features, "sessions", "stateful_sessions"),
        run_streaming=_flag(features, "run_streaming", "streaming", "sse"),
        feedback_api=_flag(features, "feedback_api", "feedback"),
        max_upload_mb=_int_limit(limits, "max_kb_upload_mb", DEFAULT_MAX_UPLOAD_MB),
        max_upload_count=_int_limit(limits, "max_kb_upload_count", DEFAULT_MAX_UPLOAD_COUNT),
        raw=payload,
    )
