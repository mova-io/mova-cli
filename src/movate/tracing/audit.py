"""Control-plane audit telemetry (item 35).

Security/ops-relevant control-plane *mutations* — minting, revoking and
rotating API keys, and promoting / rolling back a canary — need a structured
"who did what, when" trail. :func:`record_audit_event` emits one audit record
to **two** places operators already have, in priority order:

1. **Structured logs (the reliable channel).** A dedicated stdlib logger,
   ``movate.audit``, at INFO — captured by whatever log pipeline the runtime
   already ships to (on a deployed runtime, that's Azure Log Analytics). The
   structured payload rides on the record's ``extra`` under a single ``audit``
   key so a JSON formatter surfaces it as one object; the human-readable
   message keeps the codebase's ``key=value`` log style so plain-text logs are
   still grep-able. This path uses ONLY stdlib ``logging`` — no boundary issue,
   safe to call from anywhere.

2. **An event on the active OpenTelemetry span (best-effort).** When tracing is
   active (the ``otel`` extra is installed and a span is recording), the same
   audit fields are attached as a span event so the action correlates with the
   surrounding trace (→ App Insights). The OTel API is imported lazily exactly
   like :mod:`movate.tracing.propagation` / :mod:`movate.tracing.otel`, so this
   is a complete no-op when the extra is absent or no span is active, and it
   NEVER raises — audit logging must never break the request.

**No-secret guarantee.** Callers pass only *identifiers* (a key id, a subject,
an agent name + version) as ``actor`` / ``target`` / ``**fields``. The API key
*value*, secrets, and any plaintext credential MUST NEVER be passed here. Span
attributes are restricted to low-cardinality primitives (coerced to ``str``).
"""

from __future__ import annotations

import logging
from typing import Any

# Dedicated logger so operators can route/retain the audit trail independently
# of the noisier per-request runtime logs (e.g. a separate sink/retention in
# Azure Log Analytics). ``__name__``-based loggers stay on their own modules.
audit_logger = logging.getLogger("movate.audit")

# Import the OTel trace API lazily so this module loads even when the optional
# ``otel`` extra isn't installed (mirrors ``tracing/propagation.py`` /
# ``tracing/otel.py``). When absent, the span-event path is a silent no-op.
_otel_trace: Any = None
_OTEL_AVAILABLE = False
try:
    import opentelemetry.trace as _otel_trace_module

    _otel_trace = _otel_trace_module
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - covered by the no-otel no-op tests
    pass


def record_audit_event(
    action: str,
    *,
    actor: str,
    tenant_id: str | None = None,
    target: str | None = None,
    **fields: object,
) -> None:
    """Record a control-plane audit event to the logs and the active span.

    Always emits a structured ``movate.audit`` log record at INFO (the reliable
    channel). Best-effort, additionally attaches the same fields as an event on
    the currently-recording OTel span when tracing is active.

    Args:
        action: A stable, low-cardinality verb for the audited mutation, e.g.
            ``"api_key.mint"`` / ``"canary.promote"``.
        actor: The identifier of who performed the action — a key id or auth
            subject. NEVER the API key *value* or any secret.
        tenant_id: The tenant the action was scoped to, if known.
        target: The identifier of what was acted on (a key id, or an agent
            ``name@version``). NEVER a secret.
        **fields: Extra low-cardinality, non-secret identifiers to record.

    Never raises. The span-event path is a complete no-op when the ``otel``
    extra is absent or no span is recording.
    """
    payload: dict[str, object] = {
        "action": action,
        "actor": actor,
        "tenant_id": tenant_id,
        "target": target,
        **fields,
    }

    # 1) Reliable channel — stdlib logging. The structured object rides on
    # ``extra={"audit": ...}`` (a JSON formatter surfaces it as one object);
    # the message keeps the repo's ``key=value`` style for plain-text grep.
    audit_logger.info(
        "audit action=%s actor=%s tenant_id=%s target=%s",
        action,
        actor,
        tenant_id,
        target,
        extra={"audit": payload},
    )

    # 2) Best-effort — add an event to the active span for trace correlation.
    # Lazily-imported, fully guarded: a no-op when OTel is absent / no span is
    # recording, and it must NEVER raise (audit must not break the request).
    if not _OTEL_AVAILABLE or _otel_trace is None:
        return
    try:
        span = _otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return
        # OTel attributes accept primitives only; coerce + drop None so the
        # event stays low-cardinality and never carries a complex/secret value.
        attributes = {k: _attr_value(v) for k, v in payload.items() if v is not None}
        span.add_event("audit", attributes=attributes)
    except Exception:  # pragma: no cover - audit must never break the request
        return


def _attr_value(value: object) -> str | int | float | bool:
    """Coerce an audit field to a primitive OTel span-attribute value.

    Keeps attributes low-cardinality and never lets a complex (or unexpectedly
    rich) value reach the span — anything that isn't already a primitive is
    stringified.
    """
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)
