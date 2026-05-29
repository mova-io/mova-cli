"""Unified agent-creation dispatcher.

Backs ``POST /api/v1/projects/{project_id}/agents`` — a single additive
endpoint that routes to one of five existing creation paths based on
a discriminated-union JSON body OR a multipart upload.

This module hosts the composition logic so the route handler in
``app.py`` stays focused on auth + Content-Type sniffing + response
shaping. The five source paths COMPOSE existing handlers — they never
duplicate persistence / validation logic:

* ``source: "bundle"`` → multipart pipeline (``_collect_bundle_files``
  + ``persist_bundle`` + ``_dual_write_agent_to_registry``).
* ``source: "spec"`` → spec JSON → bundle bytes (this module's
  :func:`spec_to_bundle_files`) → ``persist_bundle``.
* ``source: "wizard"`` → ``wizard_to_bundle_files`` → ``persist_bundle``
  (same path the existing ``POST /agents/from-wizard`` uses).
* ``source: "llm"`` → 202 + SSE pipeline. Composes scaffold-preview +
  Eval Generator + Judge Engineer + Unified KB Ingest when those are
  available; degrades to ``stage_skipped`` SSE events when they are
  not.
* ``source: "catalog"`` → catalog lookup → clone bundle → apply
  overrides → ``persist_bundle``. Requires the catalog read API to
  be reachable; 503s cleanly otherwise.

The ``attach_agent_to_project`` step at the end of every sync path
goes through the storage Protocol (ADR 014 boundary). When the
projects-storage backend hasn't shipped that method yet, the helper
records ``attached=False`` on the response and the endpoint returns
200 — the agent is persisted regardless.

SSE event taxonomy for ``source: "llm"`` (ADR 042 alignment):

* ``stage_started`` — ``{"stage": "scaffold"|"kb-seed"|"evals"|"judge"|"smoke"}``
* ``stage_progress`` — ``{"stage": ..., "message": "..."}``
* ``stage_done`` — ``{"stage": ..., "ok": true, ...}``
* ``stage_skipped`` — ``{"stage": ..., "reason": "..."}`` (upstream
  unavailable)
* ``stage_error`` — ``{"stage": ..., "code": "...", "message": "..."}``
* ``done`` — ``{"agent_name": ..., "preview_score": ..., "project_id": ...}``
* ``error`` — terminal failure outside any single stage.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from movate.runtime.agent_creation import (
    AgentCreationError,
    persist_bundle,
)

if TYPE_CHECKING:
    from movate.runtime.schemas import (
        AgentCreateCatalogRequest,
        AgentCreateLlmRequest,
        AgentCreateSpecRequest,
    )
    from movate.storage.base import StorageProvider


# ---------------------------------------------------------------------------
# Spec → bundle bytes (source: "spec")
# ---------------------------------------------------------------------------


def spec_to_bundle_files(req: AgentCreateSpecRequest) -> dict[str, bytes]:
    """Translate an ``AgentCreateSpecRequest`` into the canonical
    ``{path: bytes}`` dict that :func:`persist_bundle` accepts.

    Mirrors the wizard pipeline shape: the spec dict is YAML-dumped
    to ``agent.yaml``, the prompt body becomes ``prompt.md``, and
    explicit ``schemas`` (when supplied) land at the canonical
    ``schema/input.json`` + ``schema/output.json`` paths. When
    ``schemas`` is omitted, callers are expected to ship inline
    ``schema:`` shorthand on the spec — ``load_agent()`` will catch
    a missing-schema agent at the validation gate with a clean 422.

    Name reconciliation: when ``req.name`` doesn't match
    ``spec['name']`` (both present), ``req.name`` wins and the spec
    is patched in-flight — the bundle's on-disk name is the path
    param's canonical answer.
    """
    spec_data: dict[str, Any] = dict(req.spec)  # copy — don't mutate caller's dict
    spec_name = spec_data.get("name")
    if isinstance(spec_name, str) and spec_name and spec_name != req.name:
        # Patch to the request's name; the route handler already
        # promised callers the URL is canonical.
        spec_data["name"] = req.name
    elif not spec_name:
        spec_data["name"] = req.name

    # Defensive: ensure api_version / kind are set so load_agent()
    # doesn't reject the bundle for a missing literal.
    spec_data.setdefault("api_version", "movate/v1")
    spec_data.setdefault("kind", "Agent")

    files: dict[str, bytes] = {
        "agent.yaml": yaml.safe_dump(spec_data, sort_keys=False).encode("utf-8"),
        "prompt.md": req.prompt.encode("utf-8"),
    }
    if req.schemas:
        # Explicit JSON Schema strings — write under the canonical
        # paths the loader's path-form resolver expects.
        if "input" in req.schemas:
            files["schema/input.json"] = req.schemas["input"].encode("utf-8")
        if "output" in req.schemas:
            files["schema/output.json"] = req.schemas["output"].encode("utf-8")
    return files


# ---------------------------------------------------------------------------
# Project attachment helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentResult:
    """Return of :func:`attach_to_project` — sentinel-style so the
    route handler can echo ``attached`` on the response without
    swallowing real errors.

    ``attached`` is ``True`` when the storage backend implemented
    ``attach_agent_to_project`` and the call succeeded;
    ``False`` when the method isn't available (graceful degrade
    against the projects-storage PR landing later).
    """

    attached: bool
    reason: str | None


async def attach_to_project(
    storage: StorageProvider,
    *,
    project_id: str,
    agent_name: str,
    tenant_id: str,
) -> AttachmentResult:
    """Attach a freshly-persisted agent to a project via the
    storage layer.

    The storage Protocol may not yet implement
    ``attach_agent_to_project`` — that ships in the parallel
    projects-storage PR. When it isn't present we return
    ``attached=False`` rather than raising; the endpoint still
    succeeds (the agent IS persisted), and the UI can call
    the projects endpoint later once it ships.

    Tenant scoping: the project itself is tenant-owned (the caller
    resolves ``project_id`` within ``tenant_id`` before this point),
    so the storage Protocol's ``attach_agent_to_project`` is a thin
    ``(project_id, agent_name)`` junction insert — the tenant is
    derived from the project row, not re-passed here.
    """
    attach_method = getattr(storage, "attach_agent_to_project", None)
    if attach_method is None:
        return AttachmentResult(
            attached=False,
            reason="projects-storage layer not yet available on this storage backend",
        )
    try:
        await attach_method(
            project_id=project_id,
            agent_name=agent_name,
        )
    except LookupError as exc:
        # Project doesn't exist — surface up as 404 via AgentCreationError.
        raise AgentCreationError(
            f"project {project_id!r} does not exist: {exc}",
            status_code=404,
        ) from exc
    return AttachmentResult(attached=True, reason=None)


# ---------------------------------------------------------------------------
# Catalog clone (source: "catalog")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogCloneFiles:
    """Output of :func:`clone_from_catalog` when the catalog API is
    reachable. Holds the canonical bundle bytes ready for
    :func:`persist_bundle`.
    """

    files: dict[str, bytes]
    source_slug: str
    source_version: str


async def clone_from_catalog(
    storage: StorageProvider,
    *,
    req: AgentCreateCatalogRequest,
    tenant_id: str,
) -> CatalogCloneFiles:
    """Look up a catalog entry, unpack it, apply overrides, and
    return the canonical bundle bytes.

    Resolution order (delegates to storage when the catalog read
    API ships):

    1. ``storage.get_catalog_entry(slug, version=, tenant_id=...)``
       — the bundle-returning lookup this clone path expects, returning
       ``{files: {...}, version: ...}``.
    2. Missing (or signature-incompatible) method on storage → raise
       :class:`AgentCreationError` with ``status_code=503`` so the
       endpoint surfaces a "catalog read API not deployed" message
       rather than 500.

    The bundle-returning ``get_catalog_entry(slug, version=, ...)``
    shape this path needs is distinct from the catalog-storage Protocol's
    metadata-only ``get_catalog_entry(slug, source=, ...)`` (ADR 041 PR1
    / #548). Until the bundle-returning clone API ships, callers see a
    clean 503 — we detect the mismatch by checking the method accepts a
    ``version`` parameter rather than blindly invoking it.

    Overrides merge: ``req.overrides`` is a partial dict that's
    shallow-merged into the cloned ``agent.yaml`` — e.g.
    ``{"model": {"provider": "..."}}`` overrides only the model
    block, leaving the rest of the spec intact.

    Rename: when ``req.rename_to`` is set we patch the cloned
    ``agent.yaml`` name BEFORE persisting — the result is a NEW
    agent decoupled from the source per ADR 041 D6 (catalog clones
    don't auto-sync).
    """
    import inspect  # noqa: PLC0415

    catalog_method = getattr(storage, "get_catalog_entry", None)
    _catalog_unavailable = AgentCreationError(
        "catalog clone read API not yet available on this storage backend; "
        "source=catalog requires the bundle-returning catalog-clone API to be deployed",
        status_code=503,
    )
    if catalog_method is None:
        raise _catalog_unavailable
    # The metadata-only Protocol method (#548) takes ``source`` and returns a
    # CatalogEntry, not the bundle dict this clone path needs. Treat that — and
    # anything else that doesn't accept ``version`` — as "clone API absent".
    try:
        _sig = inspect.signature(catalog_method)
    except (TypeError, ValueError):
        raise _catalog_unavailable from None
    if "version" not in _sig.parameters:
        raise _catalog_unavailable

    try:
        entry = await catalog_method(
            slug=req.slug,
            version=req.version,
            tenant_id=tenant_id,
        )
    except LookupError as exc:
        raise AgentCreationError(
            f"catalog entry {req.slug!r} not found: {exc}",
            status_code=404,
        ) from exc

    # Expected shape from the catalog-storage PR — when it's not yet
    # quite finalised we treat the response as a best-effort dict.
    files: dict[str, bytes] = dict(entry.get("files", {}))
    if "agent.yaml" not in files:
        raise AgentCreationError(
            f"catalog entry {req.slug!r} missing agent.yaml; the catalog bundle is malformed",
            status_code=502,
        )

    # Apply overrides + rename in-flight (single YAML edit).
    if req.overrides or req.rename_to:
        try:
            spec_data: dict[str, Any] = yaml.safe_load(files["agent.yaml"].decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise AgentCreationError(
                f"catalog entry {req.slug!r} has unparseable agent.yaml: {exc}",
                status_code=502,
            ) from exc

        if req.overrides:
            spec_data = _deep_merge(spec_data, req.overrides)
        if req.rename_to:
            spec_data["name"] = req.rename_to

        files["agent.yaml"] = yaml.safe_dump(spec_data, sort_keys=False).encode("utf-8")

    return CatalogCloneFiles(
        files=files,
        source_slug=req.slug,
        source_version=str(entry.get("version", req.version or "")),
    )


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Shallow-deep merge: each top-level key in ``overrides`` replaces
    the same key in ``base``, but nested dicts merge one level deep.

    Intentionally NOT recursive past one level — agent.yaml's nested
    blocks (model, schema) are small + flat, and a full deep merge
    invites silent override semantics that are hard to debug.
    """
    out = dict(base)
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update(val)
            out[key] = merged
        else:
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# LLM authoring pipeline — SSE event taxonomy + degrade helpers
# ---------------------------------------------------------------------------


# Stage names — the canonical taxonomy mirrors ADR 042's Bundle Composer
# event vocabulary so the Mova iO UI can render a single progress widget
# for both the unified-create flow and the standalone Composer flow.
STAGE_SCAFFOLD = "scaffold"
STAGE_KB_SEED = "kb-seed"
STAGE_EVALS = "evals"
STAGE_JUDGE = "judge"
STAGE_SMOKE = "smoke"


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """Format one SSE frame matching the runtime's canonical wire shape.

    Identical to ``app._sse_frame`` (kept module-local so this module
    has no upward dependency on app.py — both reference the SSE spec
    in the same way).
    """
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


async def llm_authoring_stream(
    *,
    req: AgentCreateLlmRequest,
    project_id: str,
    job_id: str,
    storage: StorageProvider,
    agents_path: Path | None,
    tenant_id: str,
) -> AsyncIterator[str]:
    """Drive the SSE event stream for the ``source: "llm"`` path.

    Stages, in order:

    1. ``scaffold`` — call into the scaffold-preview pipeline (PR
       #524). When missing → ``stage_skipped`` with reason; the
       overall job fails because there's no agent to persist.
    2. ``kb-seed`` (optional) — when ``auto_seed_kb=true``, call
       the unified KB ingest endpoint with ``kind=generated``.
       Missing upstream → ``stage_skipped``.
    3. ``evals`` (optional) — when ``include_evals=true``, call
       the Eval Generator (PR #541). Missing upstream →
       ``stage_skipped``.
    4. ``judge`` (optional) — when ``include_judge=true``, call
       the Judge Engineer (PR #540). Missing upstream →
       ``stage_skipped``.
    5. ``smoke`` — always: run a ``--mock`` smoke eval, emit
       ``preview_score`` on success.

    On each stage, emits a ``stage_started`` → ``stage_progress``
    (possibly) → ``stage_done`` triplet. ``stage_skipped`` replaces
    the ``stage_done`` when the upstream isn't deployed. ``stage_error``
    replaces it on any caught exception.

    The terminal frame is ``done`` (success) or ``error`` (failure
    outside any single stage).
    """
    # ---- 1. scaffold (REQUIRED) ----
    yield sse_frame("stage_started", {"stage": STAGE_SCAFFOLD, "job_id": job_id})
    scaffold_fn = _resolve_optional_pipeline("scaffold_preview")
    if scaffold_fn is None:
        # No scaffold preview available — degrade by emitting a clear
        # skipped event and terminating with an error. Without scaffold
        # there's no agent.yaml to persist.
        yield sse_frame(
            "stage_skipped",
            {
                "stage": STAGE_SCAFFOLD,
                "reason": (
                    "scaffold-preview endpoint (PR #524) not yet deployed; "
                    "source=llm cannot author an agent without it"
                ),
            },
        )
        yield sse_frame(
            "error",
            {
                "message": "scaffold-preview pipeline unavailable",
                "code": "upstream_unavailable",
            },
        )
        return

    try:
        scaffold_result = await scaffold_fn(
            description=req.description,
            shape=req.shape,
            model=req.model,
            rename_to=req.rename_to,
            budget_usd=req.budget_usd,
            tenant_id=tenant_id,
        )
    except Exception as exc:
        yield sse_frame(
            "stage_error",
            {
                "stage": STAGE_SCAFFOLD,
                "code": "scaffold_failed",
                "message": str(exc) or exc.__class__.__name__,
            },
        )
        yield sse_frame(
            "error",
            {"message": str(exc) or exc.__class__.__name__, "code": "internal_error"},
        )
        return

    agent_name = scaffold_result.get("name", "")
    files = scaffold_result.get("files", {})
    yield sse_frame(
        "stage_done",
        {"stage": STAGE_SCAFFOLD, "ok": True, "agent_name": agent_name},
    )

    # ---- Persist the scaffold bundle ----
    if agents_path is None:
        yield sse_frame(
            "error",
            {
                "message": "runtime has no agents_path configured",
                "code": "no_agents_path",
            },
        )
        return
    try:
        persist_bundle(files, agents_path=agents_path)
    except AgentCreationError as exc:
        yield sse_frame(
            "error",
            {"message": str(exc), "code": "persist_failed"},
        )
        return

    # ---- 2-4: optional stages (kb-seed / evals / judge) ----
    if req.auto_seed_kb:
        async for frame in _run_optional_stage(
            stage=STAGE_KB_SEED,
            pipeline_name="kb_ingest_generated",
            missing_reason=(
                "unified KB ingest endpoint (PR #537) not yet deployed; skipping seed corpus"
            ),
            error_code="kb_seed_failed",
            agent_name=agent_name,
            tenant_id=tenant_id,
        ):
            yield frame
    if req.include_evals:
        async for frame in _run_optional_stage(
            stage=STAGE_EVALS,
            pipeline_name="eval_generator",
            missing_reason=(
                "Eval Generator (PR #541) not yet deployed; skipping eval set generation"
            ),
            error_code="eval_gen_failed",
            agent_name=agent_name,
            tenant_id=tenant_id,
        ):
            yield frame
    if req.include_judge:
        async for frame in _run_optional_stage(
            stage=STAGE_JUDGE,
            pipeline_name="judge_engineer",
            missing_reason=("Judge Engineer (PR #540) not yet deployed; skipping judge generation"),
            error_code="judge_failed",
            agent_name=agent_name,
            tenant_id=tenant_id,
        ):
            yield frame

    # ---- 5. smoke (ALWAYS — also produces preview_score on success) ----
    preview_score: float | None = None
    async for frame, score in _run_smoke_stage(agent_name=agent_name, tenant_id=tenant_id):
        if score is not None:
            preview_score = score
        yield frame

    # ---- Attach to project + terminal frame ----
    attachment = await attach_to_project(
        storage,
        project_id=project_id,
        agent_name=agent_name,
        tenant_id=tenant_id,
    )
    yield sse_frame(
        "done",
        {
            "agent_name": agent_name,
            "project_id": project_id,
            "preview_score": preview_score,
            "attached": attachment.attached,
            "attach_reason": attachment.reason,
        },
    )


async def _run_optional_stage(
    *,
    stage: str,
    pipeline_name: str,
    missing_reason: str,
    error_code: str,
    agent_name: str,
    tenant_id: str,
) -> AsyncIterator[str]:
    """Run one optional pipeline stage and yield its SSE frames.

    Emits the ``stage_started`` → ``stage_done`` triplet on success,
    ``stage_started`` → ``stage_skipped`` when the upstream isn't
    deployed, ``stage_started`` → ``stage_error`` on a caught
    exception. Never raises — a failed optional stage doesn't
    terminate the overall job.
    """
    yield sse_frame("stage_started", {"stage": stage})
    fn = _resolve_optional_pipeline(pipeline_name)
    if fn is None:
        yield sse_frame(
            "stage_skipped",
            {"stage": stage, "reason": missing_reason},
        )
        return
    try:
        await fn(agent_name=agent_name, tenant_id=tenant_id)
        yield sse_frame("stage_done", {"stage": stage, "ok": True})
    except Exception as exc:
        yield sse_frame(
            "stage_error",
            {"stage": stage, "code": error_code, "message": str(exc)},
        )


async def _run_smoke_stage(
    *,
    agent_name: str,
    tenant_id: str,
) -> AsyncIterator[tuple[str, float | None]]:
    """Run the always-on smoke stage and yield ``(frame, score)``
    pairs. ``score`` is the ``preview_score`` on the success path
    (consumed by the terminal ``done`` frame) and ``None`` on
    every other frame.
    """
    yield sse_frame("stage_started", {"stage": STAGE_SMOKE}), None
    fn = _resolve_optional_pipeline("smoke_eval_mock")
    if fn is None:
        yield (
            sse_frame(
                "stage_skipped",
                {
                    "stage": STAGE_SMOKE,
                    "reason": (
                        "smoke-eval --mock executor not yet wired; preview_score unavailable"
                    ),
                },
            ),
            None,
        )
        return
    try:
        score = await fn(agent_name=agent_name, tenant_id=tenant_id)
        yield (
            sse_frame(
                "stage_done",
                {"stage": STAGE_SMOKE, "ok": True, "preview_score": score},
            ),
            score,
        )
    except Exception as exc:
        yield (
            sse_frame(
                "stage_error",
                {
                    "stage": STAGE_SMOKE,
                    "code": "smoke_failed",
                    "message": str(exc),
                },
            ),
            None,
        )


def _resolve_optional_pipeline(name: str) -> Any:
    """Look up an optional pipeline function on the
    :mod:`movate.runtime.pipelines` namespace.

    Returns the callable when present, ``None`` when the upstream PR
    that ships that function hasn't landed. The lookup goes through
    ``getattr`` (not a hard import) so a missing module / missing
    attr both degrade to ``None`` rather than ``ImportError``.

    Names match the upstream PR conventions:

    * ``scaffold_preview`` — PR #524 (``/agents/preview``).
    * ``kb_ingest_generated`` — PR #537 (unified KB ingest).
    * ``eval_generator`` — PR #541.
    * ``judge_engineer`` — PR #540.
    * ``smoke_eval_mock`` — internal helper (``--mock`` executor).
    """
    try:
        from movate.runtime import pipelines  # noqa: PLC0415
    except ImportError:
        return None
    return getattr(pipelines, name, None)
