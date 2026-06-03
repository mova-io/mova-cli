"""Capability discovery — the runtime's honest self-description.

``build_capabilities(app, ctx)`` produces the :class:`CapabilitiesView`
served at ``GET /api/v1/capabilities``. The whole value of the endpoint is
that it reflects *reality*: a client (e.g. Mova iO fanning out to many
heterogeneous customer runtimes) can learn exactly what THIS build supports
without trial-and-error against every endpoint.

So nothing here is a static promise. Each field is derived from the deployed
runtime at request time:

* **features** — a registry of ``name → predicate``; each predicate inspects
  the live FastAPI route table (is the path registered on this app?) or
  probes whether a module imports. A feature flips ``False`` the instant its
  route/module is absent from the build — see ``tests/test_runtime_capabilities_v1.py``
  for the register/deregister proof.
* **models.available / default** — the shared model catalog the runtime
  already knows (``movate.providers.model_catalog``), the same source
  ``mdk models`` / ``GET /api/v1/models`` use.
* **models.byok_configured** — provider NAMES only, from the per-tenant BYOK
  key store (ADR 018). The encrypted key value is NEVER read or surfaced here.
* **scopes_supported** — the canonical scope vocabulary (``movate.core.auth``).
* **limits** — the runtime's live rate-limit + batch config.
* **extras_installed** — which optional ``pyproject`` extras are importable
  in this image (marker-module probe, try/except).

This module lives in ``runtime/`` (not ``core/``) because the feature
predicates need the app's route table — ``cli ⊥ runtime`` keeps the builder
on the runtime side. The process-stable parts (features, scopes, extras, the
model catalog) are computed once and cached on ``app.state``; only the
tenant-scoped ``byok_configured`` is recomputed per request.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from movate.core.auth import ALL_SCOPES
from movate.runtime.schemas import (
    CapabilitiesView,
    CapabilityLimitsView,
    CapabilityModelsView,
    CapabilityResourceView,
    CapabilityVoiceView,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from movate.runtime.middleware import AuthContext

# Cache key on ``app.state`` for the process-stable snapshot (everything
# except the per-tenant BYOK list + the served_at timestamp). Recomputed
# only on a fresh process / redeploy.
_STATE_CACHE_ATTR = "_capabilities_static"

API_VERSION = "v1"


# ----------------------------------------------------------------------
# Feature detection — predicates over the LIVE app, never a static dict.
# ----------------------------------------------------------------------


def _registered_paths(app: FastAPI) -> frozenset[str]:
    """Every routed path on ``app`` (the FastAPI route table).

    Only :class:`~fastapi.routing.APIRoute` entries carry a ``.path``
    we care about; Mount/WebSocket/static routes are skipped. Returned
    as a frozenset for O(1) membership in the predicates.
    """
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
    return frozenset(paths)


def _has_route(app: FastAPI, *candidates: str) -> bool:
    """True if ANY of ``candidates`` is a registered path on ``app``.

    Paths are matched against the route table verbatim (FastAPI stores the
    declared template, e.g. ``/api/v1/agents/{name}/runs/stream``). Several
    candidates let a feature be satisfied by either an unversioned or a
    ``/api/v1`` mounting of the same surface.
    """
    registered = _registered_paths(app)
    return any(c in registered for c in candidates)


def _module_importable(module: str) -> bool:
    """True if ``module`` can be imported in this image (no side effects).

    Uses :func:`importlib.util.find_spec` so we don't actually execute the
    module — just check it's installed/resolvable. Swallows the rare
    ``ModuleNotFoundError`` from a half-broken parent package so a probe
    can never crash the endpoint.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# name → predicate(app) -> bool. ADD a feature here; never hand-edit a
# static bool. A predicate MUST derive its answer from the deployed
# runtime (route table or import probe), so the flag tracks the code.
_FEATURE_PREDICATES: dict[str, Callable[[FastAPI], bool]] = {
    # SSE token streaming for a run.
    "sse_events": lambda app: _has_route(
        app,
        "/agents/{name}/runs/stream",
        "/api/v1/agents/{name}/runs/stream",
    ),
    # Inbound webhook fire endpoint for triggers (ADR: triggers).
    "webhooks": lambda app: _has_route(
        app,
        "/triggers/{trigger_id}/events",
        "/api/v1/triggers/{trigger_id}/events",
    ),
    # Trigger management surface.
    "triggers": lambda app: _has_route(app, "/triggers", "/api/v1/triggers"),
    # Workflow run + signal API.
    "workflows_api": lambda app: _has_route(app, "/workflow-runs", "/api/v1/workflow-runs"),
    # Per-tenant BYOK provider-key management (ADR 018).
    "provider_keys": lambda app: _has_route(app, "/provider-keys", "/api/v1/provider-keys"),
    # Bulk async inference (item 17).
    "batch_runs": lambda app: _has_route(
        app, "/agents/{name}/batch", "/api/v1/agents/{name}/batch"
    ),
    # Eval / scorecard surface.
    "evals": lambda app: _has_route(app, "/evals", "/api/v1/evals"),
    # KB semantic search over an agent corpus.
    "kb_search": lambda app: _has_route(
        app, "/agents/{name}/kb/search", "/api/v1/agents/{name}/kb/search"
    ),
    # Champion/challenger canary rollout (ADR 016).
    "canary": lambda app: _has_route(app, "/agents/{name}/canary", "/api/v1/agents/{name}/canary"),
    # Conversational threads.
    "threads": lambda app: _has_route(app, "/threads", "/api/v1/threads"),
    # Cron-style scheduled jobs.
    "schedules": lambda app: _has_route(app, "/schedules", "/api/v1/schedules"),
    # Run replay / time-travel (ADR 045 D13) — re-execute a historical run's
    # recorded input against a chosen agent version (``mdk replay --target``).
    # Route-detected, so the flag tracks whether the endpoint is deployed here.
    "run_replay": lambda app: _has_route(
        app, "/runs/{run_id}/replay", "/api/v1/runs/{run_id}/replay"
    ),
    # Optional OIDC JWT bearer acceptance (ADR 012) — import-detected: the
    # auth path only validates JWTs when PyJWT is importable AND an issuer is
    # configured. We surface the *capability* (the dep is present); the
    # actual on/off still gates on MOVATE_OIDC_ISSUER at request time.
    "oidc_auth": lambda app: _module_importable("jwt"),
    # Langfuse tracing sink (opt-in extra).
    "langfuse_tracing": lambda app: _module_importable("langfuse"),
    # Voice transport — the WS /voice route is registered on this build (ADR
    # 048 D4). WebSocket routes don't appear in ``_registered_paths`` (it only
    # scans APIRoute), so detect by the route-builder's app-state factory hook
    # (``voice_stt_factory``) set when the route is wired.
    "voice": lambda app: getattr(app.state, "voice_stt_factory", None) is not None,
    # Realtime (speech↔speech) voice — advertised ONLY when a realtime provider
    # is actually configured on this runtime (ADR 048 D2b / ADR 050 D12). The
    # route exists for every build, but realtime is opt-in: the factory is
    # ``None`` until a deployment lights it up (MDK_VOICE_REALTIME / app.state),
    # so this flips True exactly when ``?mode=realtime`` will work here.
    "voice_realtime": lambda app: getattr(app.state, "voice_realtime_factory", None) is not None,
}


def _route_method_pairs(app: FastAPI) -> frozenset[tuple[str, str]]:
    """``{(METHOD, path)}`` for every :class:`APIRoute` on ``app``.

    The method-aware companion to :func:`_registered_paths` — resource-operation
    detection needs to know not just *that* a path is registered but *which*
    verbs it answers (``GET /skills`` vs ``POST /skills`` are different
    operations on the same path).
    """
    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if isinstance(path, str) and methods:
            for method in methods:
                pairs.add((method.upper(), path))
    return frozenset(pairs)


# Managed-resource map: family → base path + the lifecycle operations we probe
# for, each as ``(operation, METHOD, path-template)``. Probed against the live
# route table (NOT a static promise), so the matrix tracks the deployed surface:
# skills reports ``create``-only until ADR 060 lands its CRUD, and contexts is
# omitted entirely until its API ships, then appears automatically.
_RESOURCE_BASE: dict[str, str] = {
    "agents": "/api/v1/agents",
    "projects": "/api/v1/projects",
    "skills": "/api/v1/skills",
    "contexts": "/api/v1/contexts",
    "kb": "/api/v1/agents/{name}/kb",
}
_RESOURCE_OPERATIONS: dict[str, list[tuple[str, str, str]]] = {
    "agents": [
        ("list", "GET", "/api/v1/agents"),
        ("create", "POST", "/api/v1/agents"),
        ("get", "GET", "/api/v1/agents/{name}"),
        ("update", "PUT", "/api/v1/agents/{name}"),
        ("delete", "DELETE", "/api/v1/agents/{name}"),
    ],
    "projects": [
        ("list", "GET", "/api/v1/projects"),
        ("create", "POST", "/api/v1/projects"),
        ("get", "GET", "/api/v1/projects/{project_id}"),
        ("update", "PUT", "/api/v1/projects/{project_id}"),
        ("delete", "DELETE", "/api/v1/projects/{project_id}"),
    ],
    "skills": [
        ("list", "GET", "/api/v1/skills"),
        ("create", "POST", "/api/v1/skills"),
        ("get", "GET", "/api/v1/skills/{name}"),
        ("update", "PUT", "/api/v1/skills/{name}"),
        ("delete", "DELETE", "/api/v1/skills/{name}"),
    ],
    "contexts": [
        ("list", "GET", "/api/v1/contexts"),
        ("create", "POST", "/api/v1/contexts"),
        ("get", "GET", "/api/v1/contexts/{name}"),
        ("update", "PUT", "/api/v1/contexts/{name}"),
        ("delete", "DELETE", "/api/v1/contexts/{name}"),
    ],
    "kb": [
        ("ingest", "POST", "/api/v1/agents/{name}/kb"),
        ("get", "GET", "/api/v1/agents/{name}/kb"),
        ("search", "POST", "/api/v1/agents/{name}/kb/search"),
        ("stats", "GET", "/api/v1/agents/{name}/kb/stats"),
        ("delete", "DELETE", "/api/v1/agents/{name}/kb"),
    ],
}
_RESOURCE_WRITE_OPS = frozenset({"create", "ingest"})
_RESOURCE_READ_OPS = frozenset({"list", "get", "search", "stats"})


def detect_resources(app: FastAPI) -> list[CapabilityResourceView]:
    """The manageable resource surface, derived from the live route table.

    For each family in :data:`_RESOURCE_OPERATIONS`, report exactly the
    operations whose ``(METHOD, path)`` is registered on ``app``. A family with
    no registered operation is omitted (e.g. contexts before ADR 060). A family
    is ``managed`` when it has a full lifecycle here — a write op + a read op +
    ``delete`` — so a create-only resource (skills today) reports ``managed:
    false``. Never raises: a malformed route degrades to omission, not a crash.
    """
    pairs = _route_method_pairs(app)
    out: list[CapabilityResourceView] = []
    for name, ops in _RESOURCE_OPERATIONS.items():
        present = sorted(op for (op, method, path) in ops if (method, path) in pairs)
        if not present:
            continue
        present_set = set(present)
        managed = (
            bool(_RESOURCE_WRITE_OPS & present_set)
            and bool(_RESOURCE_READ_OPS & present_set)
            and "delete" in present_set
        )
        out.append(
            CapabilityResourceView(
                name=name,
                path=_RESOURCE_BASE[name],
                operations=present,
                managed=managed,
            )
        )
    return out


def detect_features(app: FastAPI) -> dict[str, bool]:
    """Evaluate every feature predicate against the live ``app``.

    Returns a name→bool map sorted by key for a stable wire shape. A
    predicate that raises is treated as ``False`` (fail-closed: better to
    under-claim a capability than to crash discovery), and logged would be
    overkill for a pure introspection call.
    """
    flags: dict[str, bool] = {}
    for name, predicate in _FEATURE_PREDICATES.items():
        try:
            flags[name] = bool(predicate(app))
        except Exception:
            flags[name] = False
    return dict(sorted(flags.items()))


# ----------------------------------------------------------------------
# Extras detection — marker module per known pyproject extra.
# ----------------------------------------------------------------------

# extra name (as in pyproject [project.optional-dependencies]) → the import
# that proves it's installed. Probed via find_spec (no execution).
_EXTRA_MARKERS: dict[str, str] = {
    "runtime": "fastapi",
    "langfuse": "langfuse",
    "keychain": "keyring",
    "cross-encoder": "sentence_transformers",
    "ocr": "pdf2image",
    "otel": "opentelemetry",
    "anthropic": "anthropic",
    "openai": "openai",
    "langchain": "langchain_core",
    "playground": "chainlit",
    # ADR 048 D9: the voice extra marker is the pipeline module — present only
    # when mdk[voice] is installed (lazy import confirmed importable).
    "voice": "movate.voice.pipeline",
}


def detect_extras() -> list[str]:
    """Which optional ``pyproject`` extras are importable in this image.

    Sorted list of extra names. Process-stable (the installed package set
    doesn't change mid-process), so this is part of the cached snapshot.
    """
    return sorted(name for name, marker in _EXTRA_MARKERS.items() if _module_importable(marker))


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


def _default_model() -> str | None:
    """The runtime's fleet-default model id, or ``None`` if unconfigured.

    There is no runtime-wide default model in v1.x (each agent declares its
    own ``model``), so we only surface a default when an operator has pinned
    one via ``MOVATE_DEFAULT_MODEL`` / ``MDK_DEFAULT_MODEL`` (env). Unset →
    ``None`` (honest: we don't invent one).
    """
    value = os.environ.get("MOVATE_DEFAULT_MODEL") or os.environ.get("MDK_DEFAULT_MODEL")
    value = (value or "").strip()
    return value or None


def _catalog_model_ids() -> list[str]:
    """Sorted model ids from the shared catalog (same source as ``mdk models``)."""
    from movate.providers.model_catalog import model_catalog  # noqa: PLC0415

    return [info.model_id for info in model_catalog()]


# ----------------------------------------------------------------------
# Voice capability detection (ADR 048/050 D4)
# ----------------------------------------------------------------------

# Mapping: provider name → the env var whose presence means the provider is
# keyed on this runtime. Separate lists for STT and TTS because a provider
# may cover only one role (e.g. Deepgram is STT-only, Cartesia TTS-only) or
# both (Azure Speech, OpenAI).
_STT_PROVIDER_KEYS: dict[str, str] = {
    "deepgram": "DEEPGRAM_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_SPEECH_KEY",
}

_TTS_PROVIDER_KEYS: dict[str, str] = {
    "cartesia": "CARTESIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "azure": "AZURE_SPEECH_KEY",
}


def _configured_stt_providers() -> list[str]:
    """STT provider names whose credential env var is set, sorted."""
    return sorted(
        name for name, var in _STT_PROVIDER_KEYS.items() if os.environ.get(var, "").strip()
    )


def _configured_tts_providers() -> list[str]:
    """TTS provider names whose credential env var is set, sorted."""
    return sorted(
        name for name, var in _TTS_PROVIDER_KEYS.items() if os.environ.get(var, "").strip()
    )


def build_voice_capabilities(app: FastAPI) -> CapabilityVoiceView:
    """Build the voice capability block for ``GET /api/v1/capabilities``.

    ``enabled`` is ``True`` when the voice WS route is registered (the
    ``voice_stt_factory`` app-state hook is set) AND at least one STT + one
    TTS provider has its key in the environment. Either condition alone is
    insufficient: a keyed but unregistered route (mdk[voice] not installed)
    and a registered but keyless runtime both return ``enabled=False`` cleanly.

    Modes: ``pipeline`` is always included when the route is registered;
    ``realtime`` is added only when a ``voice_realtime_factory`` is set (the
    opt-in premium path, ADR 048 D2b / ADR 050 D12).

    ADR 050 D4 — additive; no existing field is changed.
    """
    route_registered = getattr(app.state, "voice_stt_factory", None) is not None
    realtime_registered = getattr(app.state, "voice_realtime_factory", None) is not None

    stt_providers = _configured_stt_providers()
    tts_providers = _configured_tts_providers()

    enabled = route_registered and bool(stt_providers) and bool(tts_providers)

    modes: list[str] = []
    if route_registered:
        modes.append("pipeline")
    if realtime_registered:
        modes.append("realtime")

    return CapabilityVoiceView(
        enabled=enabled,
        modes=modes,
        stt_providers=stt_providers,
        tts_providers=tts_providers,
    )


# ----------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------


def _limits(app: FastAPI) -> CapabilityLimitsView:
    """This runtime's effective limits, from live config.

    Reads the resolved rate-limit ints stashed on ``app.state`` at build
    time (``capability_rate_limit_per_min`` / ``capability_tenant_rate_limit_per_min``),
    falling back to ``None`` (= disabled) when absent. ``max_batch_size`` is
    the server-enforced batch row cap (``MDK_BATCH_MAX_ROWS``), read live.
    """
    from movate.runtime.app import _batch_max_rows  # noqa: PLC0415

    per_key = getattr(app.state, "capability_rate_limit_per_min", None)
    per_tenant = getattr(app.state, "capability_tenant_rate_limit_per_min", None)
    return CapabilityLimitsView(
        rate_limit_per_min=per_key if isinstance(per_key, int) and per_key > 0 else None,
        tenant_rate_limit_per_min=(
            per_tenant if isinstance(per_tenant, int) and per_tenant > 0 else None
        ),
        max_batch_size=_batch_max_rows(),
    )


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _StaticSnapshot:
    """The process-stable capability fields, memoized on ``app.state``.

    Everything here only changes on a redeploy (a fresh process), so it's
    computed once. The per-tenant ``byok_configured`` list and the
    ``served_at`` timestamp are deliberately NOT part of this — they're
    resolved per request in :func:`build_capabilities`.
    """

    features: dict[str, bool]
    scopes_supported: list[str]
    extras_installed: list[str]
    model_ids: list[str]
    default_model: str | None


def minimal_capabilities() -> CapabilitiesView:
    """The unauthenticated subset: version + api_version only.

    Served to a caller without a valid ``read`` scope (or no bearer at all)
    so an orchestrator can fingerprint a runtime's version before it holds
    a key. Every richer field is ``None``/omitted and ``minimal=True``.
    """
    import movate  # noqa: PLC0415

    return CapabilitiesView(
        mdk_version=movate.__version__,
        api_version=API_VERSION,
        served_at=datetime.now(UTC),
        minimal=True,
    )


def _static_snapshot(app: FastAPI) -> _StaticSnapshot:
    """Compute (or fetch the cached) process-stable capability fields.

    Features, scopes, extras, and the model catalog only change on a
    redeploy, so they're computed once and memoized on ``app.state``. The
    per-tenant BYOK list and ``served_at`` are intentionally NOT cached —
    they're filled in per request by :func:`build_capabilities`.
    """
    cached = getattr(app.state, _STATE_CACHE_ATTR, None)
    if isinstance(cached, _StaticSnapshot):
        return cached

    snapshot = _StaticSnapshot(
        features=detect_features(app),
        scopes_supported=sorted(ALL_SCOPES),
        extras_installed=detect_extras(),
        model_ids=_catalog_model_ids(),
        default_model=_default_model(),
    )
    setattr(app.state, _STATE_CACHE_ATTR, snapshot)
    return snapshot


async def build_capabilities(app: FastAPI, ctx: AuthContext) -> CapabilitiesView:
    """Build the FULL capability matrix for an authenticated (``read``) caller.

    The process-stable parts come from the cached snapshot; the
    tenant-scoped ``byok_configured`` list is resolved per request from the
    BYOK key store for ``ctx.tenant_id`` (NAMES only — the ciphertext is
    never touched). ``served_at`` is stamped now.

    Never mutates anything — pure read + assemble.
    """
    import movate  # noqa: PLC0415

    snapshot = _static_snapshot(app)

    byok = await _byok_providers(app, tenant_id=ctx.tenant_id)

    models = CapabilityModelsView(
        available=list(snapshot.model_ids),
        default=snapshot.default_model,
        byok_configured=byok,
    )

    return CapabilitiesView(
        mdk_version=movate.__version__,
        api_version=API_VERSION,
        served_at=datetime.now(UTC),
        minimal=False,
        models=models,
        features=dict(snapshot.features),
        scopes_supported=list(snapshot.scopes_supported),
        limits=_limits(app),
        extras_installed=list(snapshot.extras_installed),
        voice=build_voice_capabilities(app),
        resources=detect_resources(app),
    )


async def _byok_providers(app: FastAPI, *, tenant_id: str) -> list[str]:
    """Provider NAMES the tenant has a BYOK key for — NEVER the values.

    Reads the per-tenant provider-key store (ADR 018, metadata only) and
    returns the sorted provider names. If the store call fails for any
    reason (backend down, method unimplemented on a minimal storage stub),
    we degrade to an empty list rather than failing discovery — capability
    discovery must never leak storage errors, and a transient store blip
    shouldn't 500 a read-only probe.

    The ``TenantProviderKey`` records carry an encrypted ``ciphertext``; we
    read ONLY ``record.provider`` here. The key value is never decrypted,
    returned, or logged.
    """
    storage = getattr(app.state, "storage", None)
    if storage is None:
        return []
    try:
        records = await storage.list_tenant_provider_keys(tenant_id=tenant_id)
    except Exception:
        return []
    return sorted({record.provider for record in records})


__all__ = [
    "API_VERSION",
    "build_capabilities",
    "build_voice_capabilities",
    "detect_extras",
    "detect_features",
    "detect_resources",
    "minimal_capabilities",
]
