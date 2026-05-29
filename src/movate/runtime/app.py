"""FastAPI app factory.

``build_app(storage)`` is the single entry point — tests build one per
test case with an :class:`InMemoryStorage`; ``movate serve`` builds
one with a :class:`SqliteProvider`. Storage is passed in (not built
inside) so the same factory works for every backend without env-var
gymnastics.

v0.5 stage 3a endpoints:

* ``GET /healthz`` — unauthed liveness check.
* ``POST /run`` — queue a job, return ``{"job_id", "status": "queued"}``.
* ``GET /jobs/{id}`` — poll a job; tenant-scoped (a tenant can never
  see another tenant's job, even with a valid key in the wrong env).

Deferred to stage 3b: ``GET /agents`` (needs an agent registry layer)
and ``movate serve`` CLI binding (uvicorn integration).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml
from fastapi import APIRouter, Depends, FastAPI, File, Header, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import movate
from movate.core.auth import (
    ALL_SCOPES,
    KEY_DEFAULT_ROTATION_GRACE_SECONDS,
    KEY_DEFAULT_TTL_DAYS,
    LEGACY_DEFAULT_SCOPES,
    mint_api_key,
    rotate_key_record,
)
from movate.core.cache import build_cache
from movate.core.canary import aggregate_side, choose_version
from movate.core.events import EventKind, EventListView, EventView
from movate.core.graph import query as graph_query
from movate.core.graph.models import GraphologyDoc, NodeDetail, NodeSearchHit
from movate.core.graph.query import GraphMode
from movate.core.loader import AgentBundle
from movate.core.models import (
    AgentBundleRecord,
    ApiKeyEnv,
    BatchRecord,
    BenchRecord,
    CanaryConfig,
    CatalogEntry,
    CatalogRatingsSummary,
    CatalogSource,
    EvalRecord,
    EvalSchedule,
    JobKind,
    JobRecord,
    JobSchedule,
    JobStatus,
    Project,
    ProjectMemberRole,
    TenantProviderKey,
    Trigger,
    WorkflowBundleRecord,
    WorkflowStatus,
)
from movate.core.provider_keys import (
    ProviderKeyError,
    mint_tenant_provider_key,
    normalize_provider,
)
from movate.core.rate_limit import InProcessRateLimiter, NoOpRateLimiter, RateLimiter
from movate.core.reporting import (
    Report,
    _filter_evals_by_since,
    _filter_runs_by_since,
    build_report,
)
from movate.core.triggers import (
    DELIVERY_ID_HEADER,
    DELIVERY_ID_MAX_LEN,
    SIGNATURE_HEADER,
    build_triggered_job,
    mint_trigger,
    verify_signature,
)
from movate.core.webhooks import (
    WebhookAttemptListView,
    WebhookAttemptView,
    WebhookCreatedView,
    WebhookCreateRequest,
    WebhookListView,
    WebhookSubscription,
    WebhookUpdateRequest,
    WebhookView,
)
from movate.core.workflow.spec import WorkflowSpecLoadError, load_workflow_spec
from movate.runtime.agent_creation import (
    AgentCreationError,
    persist_bundle,
    soft_delete_agent,
    split_skills_from_bundle,
    unzip_bundle,
    wizard_to_bundle_files,
)
from movate.runtime.agent_resolver import (
    PublishResult,
    bundle_files_from_dir,
    import_filesystem_agents,
    publish_agent_bundle,
    resolve_agent_bundle,
)
from movate.runtime.errors import ErrorCode, auth_required, conflict, http_error, not_found
from movate.runtime.events import emit_event
from movate.runtime.hardening import (
    PayloadSizeLimitMiddleware,
    RequestIdMiddleware,
    resolve_max_request_bytes,
)
from movate.runtime.middleware import (
    AuthContext,
    make_auth_dependency,
    require_scope,
)
from movate.runtime.registry import scan_agents
from movate.runtime.request_context import (
    REQUEST_ID_HEADER,
    install_request_id_logging,
)
from movate.runtime.schemas import (
    AgentCatalogItemView,
    AgentCatalogView,
    AgentCommitView,
    AgentCreateAccepted,
    AgentCreateCatalogRequest,
    AgentCreatedView,
    AgentCreateLlmRequest,
    AgentCreateSpecRequest,
    AgentCreateWizardRequest,
    AgentDatasetInfo,
    AgentDatasetUploadView,
    AgentDeletedView,
    AgentDetailView,
    AgentHistoryView,
    AgentListView,
    AgentMetricsView,
    AgentPublishedView,
    AgentPublishSubmission,
    AgentRevertedView,
    AgentRevertSubmission,
    AgentRunSubmission,
    AgentUpdatedView,
    AgentValidationCostForecast,
    AgentValidationIssue,
    AgentValidationView,
    AgentVersionsView,
    AgentVersionView,
    AgentView,
    AnalyzeAcceptedView,
    AnalyzeRequest,
    ApiKeyBulkRevokedView,
    ApiKeyListView,
    ApiKeyMintedView,
    ApiKeyMintRequest,
    ApiKeyRevokedView,
    ApiKeyRotatedView,
    ApiKeyRotateRequest,
    ApiKeyView,
    AskRequest,
    AuthWhoamiView,
    BatchAcceptedView,
    BatchInlineSubmission,
    BatchListItemView,
    BatchListView,
    BatchStatusCounts,
    BatchStatusView,
    BenchAcceptedView,
    BenchListView,
    BenchModelView,
    BenchResultView,
    BenchSubmission,
    CanaryCompareView,
    CanaryPromotedView,
    CanaryPromoteRequest,
    CanarySetRequest,
    CanarySideView,
    CanaryView,
    CatalogEntryDetailView,
    CatalogEntryListResponse,
    CatalogEntryVersionView,
    CatalogEntryView,
    CatalogPublishVersionRequest,
    CatalogRatingRequest,
    CatalogRatingsSummaryView,
    CatalogSubmitRequest,
    CatalogSyncRequest,
    CatalogSyncResponse,
    EvalAcceptedView,
    EvalListView,
    EvalScheduleListView,
    EvalScheduleSubmission,
    EvalScheduleView,
    EvalScorecardView,
    EvalSubmission,
    FeedbackListView,
    FeedbackSubmission,
    FeedbackView,
    GraphologyView,
    GraphQueryRequest,
    GraphSearchResult,
    GraphSearchView,
    GroundedAnswerView,
    HarvestedCaseView,
    HarvestView,
    HealthView,
    JobCancelView,
    JobListView,
    JobScheduleListView,
    JobScheduleSubmission,
    JobScheduleView,
    JobView,
    KbChunkView,
    KbDeletedView,
    KbIngestFileResult,
    KbIngestView,
    KbListView,
    KbReindexSubmission,
    KbReindexView,
    KbSearchResultView,
    KbSearchSubmission,
    KbSearchView,
    KbStatsSourceView,
    KbStatsView,
    ModelCatalogView,
    ModelInfoView,
    NodeDetailView,
    ObservabilityHealthView,
    ObservabilityInsightListView,
    ObservabilityInsightView,
    PricingEntryView,
    PricingView,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectMemberAddRequest,
    ProjectMemberListView,
    ProjectMemberPatchRequest,
    ProjectMemberView,
    ProjectUpdateRequest,
    ProjectView,
    ProviderKeyListView,
    ProviderKeySetRequest,
    ProviderKeyView,
    ReadyView,
    ReportView,
    RunAccepted,
    RunExplainLlmCallView,
    RunExplainView,
    RunSubmission,
    RunTraceView,
    RunView,
    SkillCreatedView,
    ThreadCreateSubmission,
    ThreadListView,
    ThreadMessageSubmission,
    ThreadView,
    TriggerCreatedView,
    TriggerCreateRequest,
    TriggerListView,
    TriggerView,
    TroubleshootRequest,
    UnifiedAgentCreatedView,
    WizardAgentSubmission,
    WorkflowCreatedView,
    WorkflowCreateRequest,
    WorkflowDeletedView,
    WorkflowDetailView,
    WorkflowListResponse,
    WorkflowPublishedView,
    WorkflowRevertedView,
    WorkflowRevertSubmission,
    WorkflowRunListView,
    WorkflowRunView,
    WorkflowSignalRequest,
    WorkflowUpdatedView,
    WorkflowValidationIssue,
    WorkflowValidationView,
    WorkflowVersionsView,
    WorkflowVersionView,
    WorkflowView,
)
from movate.runtime.skill_creation import (
    SkillCreationError,
    persist_skill_bundle,
)
from movate.runtime.unified_create import (
    attach_to_project,
    clone_from_catalog,
    llm_authoring_stream,
    spec_to_bundle_files,
)
from movate.runtime.workflow_persistence import PublishResult as WorkflowPublishResult
from movate.runtime.workflow_persistence import (
    WorkflowPersistenceError,
    persist_workflow_bundle,
    publish_workflow_bundle,
    soft_delete_workflow,
)
from movate.runtime.workflow_persistence import (
    bundle_files_from_dir as workflow_bundle_files_from_dir,
)
from movate.runtime.workflow_persistence import (
    mint_revert_version as mint_workflow_revert_version,
)
from movate.runtime.workflow_persistence import (
    unzip_bundle as unzip_workflow_bundle,
)
from movate.storage.base import StorageProvider
from movate.tracing import (
    dec_sse_connections,
    inc_sse_connections,
    inject_current_trace_context,
    record_audit_event,
)

if TYPE_CHECKING:
    from movate.providers.model_catalog import ModelInfo


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    """Format one Server-Sent Events frame.

    Single source of truth for the wire shape so the endpoint and any
    future caller stay byte-identical: an ``event:`` line, a ``data:``
    line carrying compact JSON, terminated by the mandatory blank line
    (``\\n\\n``). Compact separators keep token frames small — most
    carry a one- or two-token ``text`` delta.
    """
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


# ---------------------------------------------------------------------------
# ADR 035 D3 — SSE event-stream tuning.
#
# The polling interval is the cost dial: at one query per active
# connection per ``_EVENTS_SSE_POLL_INTERVAL_S`` we trade tail latency
# (median push lag ≈ half the interval) for storage load (queries/sec ≈
# active_connections / interval). 500ms keeps the perceived push lag
# under ~half a second while costing two queries per active subscriber
# per second — acceptable for D3 in front of an outbox that's read-light
# vs. write-heavy. A Postgres ``LISTEN/NOTIFY`` upgrade is the documented
# replacement when scale demands it.
#
# The heartbeat keeps proxies (Azure Front Door / App Gateway / nginx)
# from closing an idle connection. 15s is well under the typical 30-60s
# idle-timeout floor and small enough that a client's reconnect logic
# kicks in quickly when the connection actually dies.
#
# The per-tenant connection cap is advisory: it prevents one runaway
# client from owning every worker slot. 50 is a soft ceiling — well
# above any expected legitimate fan-out (a single browser tab uses one)
# and well below the uvicorn worker concurrency. Operators override via
# ``MDK_EVENTS_SSE_MAX_PER_TENANT``.
# ---------------------------------------------------------------------------
_EVENTS_SSE_POLL_INTERVAL_S: float = 0.5
_EVENTS_SSE_HEARTBEAT_INTERVAL_S: float = 15.0
_EVENTS_SSE_MAX_PER_TENANT_DEFAULT: int = 50
# Outbox page size for each storage poll (replay AND live). Bounded so a
# long backlog doesn't materialise in memory all at once; matches the
# ``list_events.limit`` cap the storage layer documents (1000). 100 hits
# the sweet spot for a typical fan-out (a busy tenant sees < 100 events
# between 500ms polls).
_EVENTS_SSE_PAGE_SIZE: int = 100


def _events_sse_max_per_tenant() -> int:
    """Resolve the per-tenant connection cap from env, else the default.

    Env wins (operator override at deploy-time); falls back to the
    in-code default. A bad value (non-int / <=0) silently falls back so
    a misconfigured deploy doesn't turn the cap off entirely.
    """
    raw = os.environ.get("MDK_EVENTS_SSE_MAX_PER_TENANT", "").strip()
    if not raw:
        return _EVENTS_SSE_MAX_PER_TENANT_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        return _EVENTS_SSE_MAX_PER_TENANT_DEFAULT
    return n if n > 0 else _EVENTS_SSE_MAX_PER_TENANT_DEFAULT


def _events_sse_event_frame(view: EventView) -> str:
    """Format one outbox-event SSE frame: ``id: <id>\\ndata: <json>\\n\\n``.

    Single source of truth shared by the replay + live phases so a
    rename of the wire shape can't drift between them. The ``id:`` line
    is mandatory — it's what SSE clients echo back as ``Last-Event-ID``
    on a reconnect to resume from the right cursor.
    """
    payload = view.model_dump(mode="json")
    return f"id: {view.id}\ndata: {json.dumps(payload, separators=(',', ':'), default=str)}\n\n"


async def _events_sse_generator(
    *,
    store: StorageProvider,
    target_tenant: str,
    kind: str | None,
    subject: str | None,
    since: datetime | None,
    last_event_id: str | None,
    poll_interval_s: float,
    heartbeat_interval_s: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    """Yield SSE frames from the events outbox until the client disconnects.

    Extracted as a top-level async generator (not a closure inside the
    endpoint) so unit tests can drive it directly with an injected
    ``is_disconnected`` predicate — TestClient can't gracefully shut
    down a long-poll SSE stream, but a direct-drive test can flip a flag
    and assert the generator unwinds cleanly. Same code path runs in
    production: the endpoint wraps this in a ``StreamingResponse``.

    Behavior is the contract documented on the endpoint:

    * **Replay** when ``since`` (timestamp) OR ``last_event_id`` (cursor)
      is set — page through the outbox in 100-row batches, emit each
      event, advance the cursor, then fall through to live mode.
      ``last_event_id`` wins when both are set (id is exact).
    * **Live** otherwise — snap a UTC-now anchor, poll the outbox every
      ``poll_interval_s`` for newer events, emit them, advance the
      cursor.
    * **Heartbeat** — every ``heartbeat_interval_s`` (measured against
      the wall clock between polls), emit a ``:keepalive\\n\\n`` SSE
      comment line so idle proxies don't drop the connection.
    * **Disconnect** — between iterations, the predicate is awaited;
      ``True`` exits the generator cleanly. ``asyncio.CancelledError``
      from the server-side cancel is re-raised after no extra work.

    Tenant-scoping is the caller's responsibility (``target_tenant`` is
    plumbed into every ``list_events`` call). ``tenant_id NOT NULL`` is
    the storage-layer invariant.
    """
    # Replay cursor: prefer Last-Event-ID (id-keyed) over `since`
    # (timestamp-keyed); id is exact, timestamps can tie.
    after_id: str | None = last_event_id
    replay_since: datetime | None = since if (since is not None and after_id is None) else None
    # Live cursor: high-water mark on the last emitted id. Seeded from
    # Last-Event-ID if present; initialised on the first live tick
    # otherwise. ``live_since`` is the "events recorded after now"
    # anchor when there's no id cursor yet.
    live_after_id: str | None = after_id
    live_since: datetime = datetime.now(UTC)

    last_heartbeat = asyncio.get_event_loop().time()

    try:
        # ---- Phase 1: replay (skipped on the live-only path) ----
        if replay_since is not None or after_id is not None:
            while True:
                if await is_disconnected():
                    return
                batch = await store.list_events(
                    target_tenant,
                    since=replay_since,
                    kind=kind,
                    subject=subject,
                    limit=_EVENTS_SSE_PAGE_SIZE,
                    after_id=after_id,
                )
                if not batch:
                    break
                for ev in batch:
                    yield _events_sse_event_frame(EventView.from_record(ev))
                    after_id = ev.id
                    live_after_id = ev.id
                # Less than a full page → backlog drained; flip to live.
                if len(batch) < _EVENTS_SSE_PAGE_SIZE:
                    break

        # ---- Phase 2: live ----
        while True:
            if await is_disconnected():
                return

            # Heartbeat: SSE comment so idle proxies don't drop us.
            now = asyncio.get_event_loop().time()
            if now - last_heartbeat >= heartbeat_interval_s:
                yield ":keepalive\n\n"
                last_heartbeat = now

            batch = await store.list_events(
                target_tenant,
                since=None if live_after_id is not None else live_since,
                kind=kind,
                subject=subject,
                limit=_EVENTS_SSE_PAGE_SIZE,
                after_id=live_after_id,
            )
            for ev in batch:
                yield _events_sse_event_frame(EventView.from_record(ev))
                live_after_id = ev.id

            # Cooperative sleep — keeps the loop free + lets the
            # disconnect check + heartbeat tick at a predictable cadence.
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        # Client disconnect / server shutdown — unwind cleanly. Re-raise
        # so the ASGI server's cancellation contract is honored.
        raise


async def _sse_run_stream(
    *,
    executor: Any,
    bundle: AgentBundle,
    run_request: Any,
    store: StorageProvider,
    tenant_id: str,
) -> Any:
    """Bridge the Executor's *sync* ``on_token`` callback to an *async*
    SSE generator and yield SSE frames.

    The tricky bit: ``Executor.execute`` invokes ``on_token`` inline
    from inside its own coroutine, but an SSE response must be driven by
    an async generator that the server pulls from. We decouple the two
    with an :class:`asyncio.Queue`:

    * A background task runs ``execute(..., on_token=...)``. The callback
      is a sync lambda that ``put_nowait``-s each token delta onto the
      queue (the queue is unbounded, so the callback never blocks the
      executor coroutine). When ``execute`` returns (or raises), the task
      pushes a terminal marker.
    * This generator ``await queue.get()``-loops, translating each
      marker into an SSE frame: ``token`` per delta, then a single
      ``done`` (success / safety) or ``error`` (executor error status
      or raised exception) frame.

    No orphaned tasks / leaks: the ``finally`` cancels the executor task
    if it's still running (e.g. the client disconnected mid-stream and
    the server threw ``GeneratorExit`` into us) and awaits it so the
    coroutine is fully unwound. On the normal path the task has already
    completed before we exit the loop.

    Persistence is unchanged: ``execute`` writes the ``RunRecord`` (or a
    ``FailureRecord`` on error) exactly as a non-streamed run, so a
    follow-up ``GET /runs/{run_id}`` returns the run after the stream
    closes.
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def _drive() -> None:
        try:
            response = await executor.execute(
                bundle,
                run_request,
                on_token=lambda delta: queue.put_nowait(("token", delta)),
                tenant_id_override=tenant_id,
            )
            await queue.put(("result", response))
        except Exception as exc:  # surface ANY failure as an SSE error frame
            await queue.put(("exc", exc))

    task = asyncio.create_task(_drive())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "token":
                # Only emit non-empty deltas — the provider's final
                # usage-only chunk carries an empty string.
                if payload:
                    yield _sse_frame("token", {"text": payload})
                continue
            if kind == "result":
                response = payload
                if response.status == "error":
                    err = response.error
                    yield _sse_frame(
                        "error",
                        {
                            "message": err.message if err is not None else "run failed",
                            "code": err.type if err is not None else "error",
                        },
                    )
                    return
                # Success / safety_blocked → fetch the persisted record so
                # the terminal frame carries the canonical RunView shape.
                record = await store.get_run(response.run_id, tenant_id=tenant_id)
                if record is not None:
                    view = RunView.from_record(record)
                    yield _sse_frame(
                        "done",
                        {
                            "run_id": view.run_id,
                            "status": view.status.value,
                            "metrics": view.metrics.model_dump(mode="json"),
                            "output": view.output,
                        },
                    )
                else:
                    # No RunRecord (only happens if the status was
                    # non-error but persistence was skipped) — fall back
                    # to the RunResponse so the client still gets a
                    # terminal frame with the run_id + output.
                    yield _sse_frame(
                        "done",
                        {
                            "run_id": response.run_id,
                            "status": response.status,
                            "metrics": response.metrics.model_dump(mode="json"),
                            "output": response.data or None,
                        },
                    )
                return
            if kind == "exc":
                exc = payload
                yield _sse_frame(
                    "error",
                    {"message": str(exc) or exc.__class__.__name__, "code": "internal_error"},
                )
                return
    finally:
        # Cancel + reap the executor task so nothing is orphaned. On the
        # happy path it's already done; on client disconnect (GeneratorExit
        # thrown into this generator) it may still be running. Suppress both
        # CancelledError (from our cancel) and any straggler exception —
        # cleanup must never raise out of the generator's finally.
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ----------------------------------------------------------------------
# Knowledge-graph query helpers (ADR 046). These translate the runtime's
# request shape onto the pure ``core.graph`` operations. ``project_id`` /
# ``project`` is the agent (one graph per agent); a node-id-only endpoint
# (detail / neighbors / search) optionally narrows to one agent or scans
# the tenant's agents to find the node's owner.
# ----------------------------------------------------------------------

# Hard cap on how many of a tenant's agents a node-by-id lookup will scan
# when no ``project`` is supplied. Bounds the cross-agent search so one
# request can't fan out unboundedly on a fleet with thousands of agents.
_GRAPH_AGENT_SCAN_LIMIT = 200


def _graph_mode(raw: str | None) -> GraphMode:
    """Parse a ``mode`` query param into a :class:`GraphMode`.

    Unknown / missing → ``knowledge`` (the only persisted graph today),
    so a typo degrades to the default view rather than erroring."""
    if raw == GraphMode.TOPOLOGY.value:
        return GraphMode.TOPOLOGY
    return GraphMode.KNOWLEDGE


async def _graph_agents_for_tenant(store: StorageProvider, *, tenant_id: str) -> list[str]:
    """Distinct agent names registered for ``tenant_id``.

    Used by the node-by-id / search endpoints when no ``project`` is
    given: ask the registry for the tenant's agents (latest version per
    name) so the cross-agent scope is bounded. Tenant-scoped at the
    storage layer; never lists another tenant's agents."""
    bundles = await store.list_agents(tenant_id=tenant_id, limit=_GRAPH_AGENT_SCAN_LIMIT)
    return [b.name for b in bundles]


async def _resolve_node_agent(
    store: StorageProvider,
    *,
    node_id: str,
    tenant_id: str,
    project: str | None,
    agents_hint: list[str] | None = None,
) -> str | None:
    """Find which agent owns ``node_id`` within ``tenant_id``.

    ``get_entity`` is tenant-scoped (returns None for a foreign tenant),
    so this never leaks cross-tenant ids. When ``project`` is given we
    only accept the node if it belongs to that agent; otherwise we read
    the node's own ``agent`` field (cheap — one tenant-scoped lookup)."""
    entity = await store.get_entity(node_id, tenant_id=tenant_id)
    if entity is None:
        return None
    if project is not None and entity.agent != project:
        return None
    return entity.agent


async def _resolve_node_detail(
    store: StorageProvider,
    *,
    node_id: str,
    tenant_id: str,
    project: str | None,
) -> NodeDetail | None:
    """Build a :class:`NodeDetail` for ``node_id`` (or None if absent).

    Resolves the owning agent first (tenant-scoped, no leak), then defers
    to the pure ``core.graph`` detail builder for provenance + neighbor
    counting."""
    agent = await _resolve_node_agent(store, node_id=node_id, tenant_id=tenant_id, project=project)
    if agent is None:
        return None
    return await graph_query.node_detail(store, agent=agent, tenant_id=tenant_id, node_id=node_id)


async def _search_graph_nodes(
    store: StorageProvider,
    *,
    tenant_id: str,
    q: str,
    project: str | None,
    type: str | None,
    limit: int | None,
) -> list[NodeSearchHit]:
    """Label search across one agent (``project``) or the tenant's agents.

    When ``project`` is set, a single-agent search; otherwise it scans the
    tenant's registered agents and merges hits up to the node budget. Pure
    matching lives in ``core.graph.search_nodes`` — this only fans out the
    agent scope and caps the merged result."""
    cap = graph_query.clamp_cap(limit)
    if project is not None:
        return await graph_query.search_nodes(
            store, agent=project, tenant_id=tenant_id, q=q, type=type, limit=cap
        )
    agents = await _graph_agents_for_tenant(store, tenant_id=tenant_id)
    merged: list[NodeSearchHit] = []
    for agent in agents:
        if len(merged) >= cap:
            break
        hits = await graph_query.search_nodes(
            store,
            agent=agent,
            tenant_id=tenant_id,
            q=q,
            type=type,
            limit=cap - len(merged),
        )
        merged.extend(hits)
    return merged[:cap]


async def _sse_graph_growth_stream(
    *,
    store: StorageProvider,
    agent: str,
    tenant_id: str,
    mode: GraphMode,
    cap: int,
) -> Any:
    """SSE growth generator: replay the current graph as add events.

    Loads a capped window (the same windowing the GET endpoint uses) and
    emits one ``node.added`` frame per node, then one ``edge.added`` frame
    per edge, each carrying a single-element graphology document so the
    client merges every frame with the same zero-transform
    ``graph.import(...)``. Closes with a ``done`` frame carrying the
    totals.

    Snapshot-as-stream today (ADR 046 D3): a live-tail that pushes future
    ingest writes is the additive follow-on; the frame contract here is
    forward-compatible with it."""
    doc: GraphologyDoc = await graph_query.windowed_subgraph(
        store,
        agent=agent,
        tenant_id=tenant_id,
        mode=mode,
        limit=cap,
    )
    for node in doc.nodes:
        frame_doc = GraphologyDoc(nodes=[node], edges=[])
        yield _sse_frame("node.added", frame_doc.model_dump(mode="json"))
    for edge in doc.edges:
        frame_doc = GraphologyDoc(nodes=[], edges=[edge])
        yield _sse_frame("edge.added", frame_doc.model_dump(mode="json"))
    yield _sse_frame("done", {"nodes": len(doc.nodes), "edges": len(doc.edges)})


def _default_sibling_path(
    explicit: Path | None,
    agents_path: Path | None,
    *,
    name: str,
    at_parent: bool = False,
) -> Path | None:
    """Resolve an optional path that defaults to a sibling of ``agents_path``.

    Used to wire ``skills_path`` (defaults to ``<agents_path>/skills``) and
    ``workflows_path`` (defaults to ``<agents_path>/../workflows`` —
    ``at_parent=True``). Returns ``None`` only when no explicit path was
    given AND ``agents_path`` is also missing (typical for a unit-test
    runtime that doesn't persist anything to disk).
    """
    if explicit is not None:
        return explicit
    if agents_path is None:
        return None
    if at_parent:
        return agents_path.parent / name
    return agents_path / name


def _github_is_enabled() -> bool:
    """Whether the GitHub integration is turned on.

    Mirrors :func:`movate.integrations.github.is_enabled` — duplicated
    here so ``build_app`` doesn't import the integrations subpackage
    just to read an env var (the integrations module's lazy-import
    contract is "no import unless you actually want the client")."""
    raw = os.environ.get("MDK_GITHUB_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes")


def _resolve_cors_origins(explicit: list[str] | None) -> list[str]:
    """Pick the effective CORS allow-list, in priority order:

    1. ``explicit`` (passed via ``build_app(cors_allowed_origins=...)``
       — primarily for tests).
    2. ``MDK_CORS_ALLOWED_ORIGINS`` env var (comma-separated, e.g.
       ``"http://localhost:4200,https://mova-io.movate.com"``).
    3. ``MOVATE_CORS_ALLOWED_ORIGINS`` env var (legacy alias).
    4. Empty list — no CORS middleware mounted (server-to-server or
       same-origin only; browser clients from other hosts will fail).

    A single ``"*"`` entry enables permissive CORS — fine for local
    dev, NEVER do this in staging/prod because ``allow_credentials=True``
    with ``*`` is rejected by browsers per the CORS spec.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get("MDK_CORS_ALLOWED_ORIGINS") or os.environ.get(
        "MOVATE_CORS_ALLOWED_ORIGINS", ""
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _resolve_tenant_rate_limit(explicit: int | None) -> int | None:
    """Resolve the per-tenant aggregate rate limit (item 25), in
    priority order:

    1. ``explicit`` (the ``build_app(tenant_rate_limit_per_minute=...)``
       kwarg) — wins whenever it is not ``None``. Tests pass it directly.
    2. ``MDK_TENANT_RATE_LIMIT_PER_MINUTE`` env var (an integer; e.g.
       ``"600"``). A non-integer / blank value is treated as unset so a
       typo can't silently disable a configured limit elsewhere — it
       falls through to OFF.
    3. ``None`` — per-tenant limiting OFF (the default; the runtime keeps
       today's per-key-only behavior, byte-for-byte).

    Returns the resolved limit (``int``) or ``None`` for OFF. A
    non-positive resolved value is left as-is for the caller to map to a
    :class:`NoOpRateLimiter` (mirrors ``rate_limit_per_minute<=0``)."""
    if explicit is not None:
        return explicit
    raw = os.environ.get("MDK_TENANT_RATE_LIMIT_PER_MINUTE", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "ignoring non-integer MDK_TENANT_RATE_LIMIT_PER_MINUTE=%r; "
            "per-tenant rate limiting stays OFF",
            raw,
        )
        return None


async def _collect_bundle_files(
    *,
    agent_yaml: UploadFile | None,
    prompt: UploadFile | None,
    input_schema: UploadFile | None,
    output_schema: UploadFile | None,
    dataset: UploadFile | None,
    bundle: UploadFile | None,
    contexts: list[UploadFile],
    kb: list[UploadFile],
) -> dict[str, bytes]:
    """Convert the multipart form fields into a
    ``{canonical_path: bytes}`` dict :func:`persist_bundle` accepts.

    Enforces the two-mode contract: EITHER ``bundle`` OR the four
    individual files, never both, never neither. 400 with a clear
    pointer at the conflict on either error.

    ``contexts`` is always optional — zero or more ``contexts/<name>.md``
    files uploaded via the repeating ``contexts`` multipart field. They
    are merged into the bundle regardless of which mode is used.
    """
    individual = [agent_yaml, prompt, input_schema, output_schema, dataset]
    has_individual = any(f is not None for f in individual)

    if bundle is not None and has_individual:
        raise AgentCreationError(
            "supply EITHER a zipped 'bundle' OR individual files "
            "(agent_yaml + prompt + input_schema + output_schema + "
            "optional dataset), not both",
            status_code=400,
        )
    if bundle is None and not has_individual:
        raise AgentCreationError(
            "no files in the multipart form; supply either a zipped "
            "'bundle' or the individual canonical files",
            status_code=400,
        )

    if bundle is not None:
        files = unzip_bundle(await bundle.read())
    else:
        # Individual-files mode. Re-check the required fields are present
        # — the FastAPI param defaults make them all optional at the route
        # level, but the bundle contract requires the canonical 4.
        required = {
            "agent.yaml": agent_yaml,
            "prompt.md": prompt,
            "schema/input.json": input_schema,
            "schema/output.json": output_schema,
        }
        missing = [name for name, f in required.items() if f is None]
        if missing:
            raise AgentCreationError(
                f"individual-files mode requires {sorted(required)}; missing: {sorted(missing)}",
                status_code=400,
            )

        files = {}
        for canonical_path, upload in required.items():
            assert upload is not None  # narrowed by the missing-check above
            files[canonical_path] = await upload.read()
        if dataset is not None:
            files["evals/dataset.jsonl"] = await dataset.read()

    # Context files — optional, repeating field. Each upload is stored
    # under contexts/<basename> so the loader's two-tier resolution finds
    # them inside the agent dir without a shared project volume.
    for ctx_upload in contexts:
        raw_name = (ctx_upload.filename or "").lstrip("/")
        # Safety: only the basename, prefixed with contexts/. Reject
        # any name with path separators that could escape the dir.
        basename = Path(raw_name).name
        if not basename or ".." in basename:
            continue
        canonical = f"contexts/{basename}"
        files[canonical] = await ctx_upload.read()

    # KB corpus files — optional, repeating field. Stored under
    # kb/<basename> so resolve_kb_file() finds them via its agent-local
    # tier when the skill runs inside a deployed container.
    for kb_upload in kb:
        raw_name = (kb_upload.filename or "").lstrip("/")
        basename = Path(raw_name).name
        if not basename or ".." in basename:
            continue
        canonical = f"kb/{basename}"
        files[canonical] = await kb_upload.read()

    return files


async def _dual_write_agent_to_registry(
    storage: StorageProvider,
    agent_dir: Path,
    *,
    tenant_id: str,
    version: str,
    created_by: str | None,
) -> PublishResult | None:
    """Publish a freshly-persisted agent dir into the durable registry.

    ADR 014 D2/D5 + **ADR 021 D2**: ``POST/PUT /api/v1/agents`` keep
    writing to the filesystem (``persist_bundle`` — the local-serve path
    + back-compat), AND publish the bundle into the durable registry so
    every pod (the async worker, other replicas) resolves it. The FS
    write is the source of truth for local ``mdk serve``, the registry
    row for the deployed multi-pod runtime.

    Delegates to :func:`publish_agent_bundle`, which is **content-aware**
    (ADR 021): it writes a NEW immutable ``(name, tenant, version)`` row
    only when the bundle's ``content_hash`` differs from the latest
    published version — so a re-deploy of *changed* content updates what
    runs, while an *unchanged* re-deploy is a no-op (no duplicate history
    row, no swallowed duplicate-PK error). When the declared version
    collides with a different-content history entry, a distinct
    ``<version>+<hash8>`` registry version is derived so ``latest`` is the
    new content.

    Returns the :class:`PublishResult` (so the caller can report the
    published version + whether anything changed), or ``None`` if the
    registry write failed. Best-effort: a registry failure must NOT fail
    the publish (the FS copy is still live and the worker's
    filesystem-fallback / a later import seed picks it up) — it's logged,
    not raised.
    """
    try:
        files = bundle_files_from_dir(agent_dir)
        result = await publish_agent_bundle(
            storage,
            name=agent_dir.name,
            tenant_id=tenant_id,
            version=version,
            files=files,
            created_by=created_by,
        )
    except Exception:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "agent_registry_publish_failed name=%s tenant_id=%s version=%s",
            agent_dir.name,
            tenant_id,
            version,
            exc_info=True,
        )
        return None

    # ADR 035 D1 — emit ``agent.published`` only on a content-changed
    # publish (``result.published is True``). A no-op re-deploy of
    # byte-identical content does NOT emit (it's not a new state on the
    # registry — same audit-meaningful discipline as the registry
    # itself, which skips the duplicate history row). Fire-and-forget.
    if result.published:
        emit_event(
            storage,
            tenant_id=tenant_id,
            kind=EventKind.AGENT_PUBLISHED,
            subject=agent_dir.name,
            data={
                "version": result.version,
                "content_hash": result.content_hash,
                "previous_version": result.previous_version,
                "created_by": created_by,
            },
        )
    return result


async def _unified_create_persist_and_attach(
    request: Request,
    files: dict[str, bytes],
    *,
    project_id: str,
    source: str,
    ctx: AuthContext,
    agents_path: Path,
) -> UnifiedAgentCreatedView:
    """Shared tail for spec / wizard / catalog sources.

    Persists ``files`` to ``agents_path``, refreshes the in-memory
    registry, dual-writes to the durable registry, and attaches to
    ``project_id``. Returns the unified response view.

    Pure factoring — every byte that lands on disk goes through the
    same persist / publish / scan path the canonical endpoints use.
    """
    result = persist_bundle(files, agents_path=agents_path)
    request.app.state.agents = scan_agents(agents_path)
    store: StorageProvider = request.app.state.storage
    published = await _dual_write_agent_to_registry(
        store,
        result.agent_dir,
        tenant_id=ctx.tenant_id,
        version=result.bundle.spec.version,
        created_by=ctx.api_key_id,
    )
    attachment = await attach_to_project(
        store,
        project_id=project_id,
        agent_name=result.bundle.spec.name,
        tenant_id=ctx.tenant_id,
    )
    return UnifiedAgentCreatedView(
        source=source,  # type: ignore[arg-type]
        project_id=project_id,
        agent_name=result.bundle.spec.name,
        version=result.bundle.spec.version,
        description=result.bundle.spec.description,
        agent_dir=result.agent_dir.name,
        files_persisted=result.files_persisted,
        published_version=published.version if published is not None else None,
        changed=published.published if published is not None else True,
        attached=attachment.attached,
    )


async def _unified_create_spec(
    request: Request,
    body_dict: dict[str, Any],
    project_id: str,
    ctx: AuthContext,
    agents_path: Path,
) -> UnifiedAgentCreatedView:
    """Handle ``source: "spec"`` — translate spec JSON → bundle bytes."""
    try:
        req = AgentCreateSpecRequest.model_validate(body_dict)
    except Exception as exc:
        raise AgentCreationError(
            f"invalid 'spec' request body: {exc}",
            status_code=422,
        ) from exc
    files = spec_to_bundle_files(req)
    return await _unified_create_persist_and_attach(
        request,
        files,
        project_id=project_id,
        source="spec",
        ctx=ctx,
        agents_path=agents_path,
    )


async def _unified_create_wizard(
    request: Request,
    body_dict: dict[str, Any],
    project_id: str,
    ctx: AuthContext,
    agents_path: Path,
) -> UnifiedAgentCreatedView:
    """Handle ``source: "wizard"`` — re-parse the wizard_form through
    the canonical WizardAgentSubmission model and run the same
    translation the existing /agents/from-wizard endpoint uses."""
    try:
        req = AgentCreateWizardRequest.model_validate(body_dict)
        wizard_submission = WizardAgentSubmission.model_validate(req.wizard_form)
    except Exception as exc:
        raise AgentCreationError(
            f"invalid 'wizard' request body: {exc}",
            status_code=422,
        ) from exc
    files = wizard_to_bundle_files(wizard_submission)
    return await _unified_create_persist_and_attach(
        request,
        files,
        project_id=project_id,
        source="wizard",
        ctx=ctx,
        agents_path=agents_path,
    )


async def _unified_create_catalog(
    request: Request,
    body_dict: dict[str, Any],
    project_id: str,
    ctx: AuthContext,
    agents_path: Path,
) -> UnifiedAgentCreatedView:
    """Handle ``source: "catalog"`` — clone-and-decouple from the
    agent catalog."""
    try:
        req = AgentCreateCatalogRequest.model_validate(body_dict)
    except Exception as exc:
        raise AgentCreationError(
            f"invalid 'catalog' request body: {exc}",
            status_code=422,
        ) from exc
    store: StorageProvider = request.app.state.storage
    cloned = await clone_from_catalog(store, req=req, tenant_id=ctx.tenant_id)
    return await _unified_create_persist_and_attach(
        request,
        cloned.files,
        project_id=project_id,
        source="catalog",
        ctx=ctx,
        agents_path=agents_path,
    )


async def _unified_create_llm(
    request: Request,
    body_dict: dict[str, Any],
    project_id: str,
    ctx: AuthContext,
    agents_path: Path,
) -> Any:
    """Handle ``source: "llm"`` — async pipeline, returns 202 +
    stream_url. The SSE pipeline COMPOSES scaffold-preview / KB
    ingest / eval-gen / judge-engineer; missing upstreams emit
    ``stage_skipped`` events rather than failing the whole job.

    We mint a ``job_id`` synchronously and return it immediately
    so the caller can subscribe to the SSE stream. The stream
    itself is served by ``GET /api/v1/projects/{project_id}/agents/
    create-stream/{job_id}`` (see route below).
    """
    try:
        req = AgentCreateLlmRequest.model_validate(body_dict)
    except Exception as exc:
        raise AgentCreationError(
            f"invalid 'llm' request body: {exc}",
            status_code=422,
        ) from exc
    job_id = str(uuid4())
    # Cache the request on app.state so the SSE GET can pick it up.
    # In a multi-replica deploy this would go through storage; for the
    # local-serve + single-pod case the in-memory cache is enough.
    pending: dict[str, dict[str, Any]] = (
        getattr(request.app.state, "_pending_llm_create", None) or {}
    )
    pending[job_id] = {
        "req": req,
        "project_id": project_id,
        "tenant_id": ctx.tenant_id,
        "agents_path": str(agents_path),
    }
    request.app.state._pending_llm_create = pending
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(
        status_code=202,
        content=AgentCreateAccepted(
            job_id=job_id,
            status_url=f"{base_url}/jobs/{job_id}",
            stream_url=(f"{base_url}/api/v1/projects/{project_id}/agents/create-stream/{job_id}"),
        ).model_dump(mode="json"),
    )


def _normalize_if_match(raw: str) -> str:
    """Strip RFC 7232 ETag decoration from an ``If-Match`` value.

    Tolerates the wire forms clients/proxies emit: a leading weak
    validator ``W/`` and surrounding double quotes (``W/"0.2.0"`` →
    ``0.2.0``). We don't honor the wildcard ``*`` specially — for this
    registry an explicit version/hash is the useful precondition, and a
    literal ``*`` simply won't match a real version (so it 409s, which
    is the safe direction). Returns the bare value to compare against
    the current version or content_hash.
    """
    value = raw.strip()
    if value[:2] in ("W/", "w/"):
        value = value[2:].strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


async def _check_agent_if_match(
    storage: StorageProvider,
    name: str,
    *,
    tenant_id: str,
    if_match: str,
) -> None:
    """Enforce the ``If-Match`` optimistic-concurrency precondition (ADR 014 D3).

    Looks up the registry's CURRENT latest bundle for ``(name,
    tenant_id)`` and 409s unless the caller's ``If-Match`` matches that
    version's ``version`` OR its ``content_hash`` (either is an accepted
    precondition token). A missing registry row is treated as a match —
    the durable registry may be empty for a local ``mdk serve`` whose
    agent only lives on the filesystem, and a precondition can't be stale
    against a history that doesn't exist; the FS 404-guard already ran.

    Only called when the client opted in by sending the header — absent
    ``If-Match`` this is never invoked, preserving last-write-wins.
    """
    current = await storage.get_agent_bundle(name, tenant_id=tenant_id)
    if current is None:
        return
    expected = _normalize_if_match(if_match)
    if expected in (current.version, current.content_hash):
        return
    raise conflict(
        f"agent {name!r} was updated concurrently: If-Match {expected!r} no longer "
        f"matches the current version {current.version!r} — re-fetch and retry",
    )


def _mint_revert_version(to_version: str, existing: set[str]) -> str:
    """Derive a new, collision-free registry version for a revert (ADR 014 D3).

    The registry's ``(tenant_id, name, version)`` row is a primary key,
    so a revert can't re-use ``to_version``'s string verbatim — it must
    publish a *new* row. We keep the new version human-traceable by
    suffixing the target with SemVer build metadata (``+revert.N``,
    RFC-style ``+`` build tag), bumping ``N`` until it doesn't collide
    with any version already in the history. The registry resolves
    "latest" by ``created_at`` (not by parsing the version), so the
    suffix only needs to be unique + legible — it carries the provenance
    "this is a re-publish of ``to_version``" without pretending to be a
    semantic bump the operator didn't author.
    """
    # Strip any prior ``+revert.*`` so reverting a reverted version stays
    # ``<base>+revert.N`` rather than nesting (``...+revert.1+revert.1``).
    base = to_version.split("+revert.", 1)[0]
    n = 1
    candidate = f"{base}+revert.{n}"
    while candidate in existing:
        n += 1
        candidate = f"{base}+revert.{n}"
    return candidate


def _agent_creation_error_code(status_code: int) -> str:
    """Map HTTP status to a stable error code the Angular client can
    branch on. Keeps the wire contract independent of the human-readable
    message (which may change as we improve the diagnostics).
    """
    return {
        400: "bad_request",
        404: "not_found",
        409: "already_exists",
        422: "invalid_bundle",
        502: "upstream_unavailable",
        503: "agent_persistence_unavailable",
    }.get(status_code, "internal_error")


def _render_agent_validation(bundle: AgentBundle) -> AgentValidationView:
    """Build the ``AgentValidationView`` for
    ``POST /api/v1/agents/{name}/validate``.

    Runs the prompt linter + cost forecast against the bundle. The
    bundle itself was already validated structurally at load time
    (via ``load_agent()``) — by the time it's in the registry, it
    parsed cleanly. This endpoint surfaces the SOFT checks the CLI
    surfaces via ``mdk validate``: prompt-template hygiene and an
    eval-cost forecast.

    Pure function — no I/O beyond what the linter + forecaster
    already do. Safe to call repeatedly; cheap.
    """
    from movate.core.cost_forecast import estimate_eval_cost  # noqa: PLC0415
    from movate.core.prompt_linter import lint_prompt  # noqa: PLC0415
    from movate.providers.pricing import load_pricing  # noqa: PLC0415

    # Severity is a typing.Literal["error", "warning"] (NOT an Enum) —
    # compare against the bare strings.
    issues = lint_prompt(bundle)
    errors = [
        AgentValidationIssue(
            code=i.code,
            severity=i.severity,
            message=i.message,
            hint=i.hint,
        )
        for i in issues
        if i.severity == "error"
    ]
    warnings = [
        AgentValidationIssue(
            code=i.code,
            severity=i.severity,
            message=i.message,
            hint=i.hint,
        )
        for i in issues
        if i.severity == "warning"
    ]

    # Cost forecast — None when the agent has no dataset, or when the
    # pricing table doesn't know the agent's model. Wrap defensively
    # so a missing pricing.yaml doesn't 500 the endpoint.
    forecast_view: AgentValidationCostForecast | None = None
    try:
        forecast = estimate_eval_cost(bundle, pricing=load_pricing())
        if forecast is not None:
            forecast_view = AgentValidationCostForecast(
                model_provider=forecast.model_provider,
                cases=forecast.cases,
                input_tokens_per_call=forecast.input_tokens_per_call,
                output_tokens_per_call=forecast.output_tokens_per_call,
                cost_per_call_usd=forecast.cost_per_call_usd,
                total_cost_usd=forecast.total_cost_usd,
            )
    except Exception:  # pragma: no cover — defensive
        # Pricing-table load failure shouldn't sink validate.
        forecast_view = None

    return AgentValidationView(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        cost_forecast=forecast_view,
    )


def _eval_record_to_view(record: EvalRecord) -> EvalScorecardView:
    """Map an :class:`EvalRecord` to the wire view. Pulled out so
    the kickoff endpoint, retrieval endpoint, and list endpoint all
    use the same field-mapping logic.
    """
    return EvalScorecardView(
        eval_id=record.eval_id,
        agent=record.agent,
        agent_version=record.agent_version,
        dataset_hash=record.dataset_hash,
        judge_method=record.judge_method.value,
        judge_provider=record.judge_provider,
        runs_per_case=record.runs_per_case,
        gate_mode=record.gate_mode,
        threshold=record.threshold,
        mean_score=record.mean_score,
        pass_rate=record.pass_rate,
        sample_count=record.sample_count,
        total_cost_usd=record.total_cost_usd,
        created_at=record.created_at.isoformat(),
    )


def _bench_record_to_view(record: BenchRecord) -> BenchResultView:
    """Map a :class:`BenchRecord` to the wire view. Shared by the
    retrieval + list endpoints so both map fields identically (mirrors
    ``_eval_record_to_view``).
    """
    return BenchResultView(
        bench_id=record.bench_id,
        agent=record.agent,
        agent_version=record.agent_version,
        input=record.input,
        judge_method=record.judge_method.value if record.judge_method else None,
        judge_provider=record.judge_provider,
        runs_per_model=record.runs_per_model,
        gate_mode=record.gate_mode,
        models=[
            BenchModelView(
                provider=m.provider,
                score=m.score,
                judge_skipped=m.judge_skipped,
                cost_mean_usd=m.cost_mean_usd,
                cost_total_usd=m.cost_total_usd,
                latency_p50_ms=m.latency_p50_ms,
                latency_p95_ms=m.latency_p95_ms,
                error_count=m.error_count,
                sample_output=m.sample_output,
            )
            for m in record.models
        ],
        created_at=record.created_at.isoformat(),
    )


def _model_info_to_view(info: ModelInfo) -> ModelInfoView:
    """Map a :class:`movate.providers.model_catalog.ModelInfo` to the wire
    view. Shared by the catalog-list and single-model endpoints so both
    map fields identically.
    """
    return ModelInfoView(
        model_id=info.model_id,
        provider=info.provider,
        context_window=info.context_window,
        input_per_1m=info.input_per_1m,
        output_per_1m=info.output_per_1m,
        cached_input_per_1m=info.cached_input_per_1m,
        supports_tools=info.supports_tools,
        supports_vision=info.supports_vision,
        notes=info.notes,
        in_pricing_table=info.in_pricing_table,
    )


def _render_agent_detail(bundle: AgentBundle) -> AgentDetailView:
    """Build the ``AgentDetailView`` for ``GET /api/v1/agents/{name}``.

    Reads dataset stats lazily (computed only if the dataset file
    actually exists; ``None`` otherwise). Lists canonical files that
    physically exist on disk — the UI's "files in this agent" view
    should reflect reality, not the abstract canonical layout.

    Pure function — no I/O beyond reading the dataset bytes for
    digest/count + listing the bundle dir. Trivially testable.
    """
    import hashlib  # noqa: PLC0415

    spec = bundle.spec
    agent_dir = bundle.agent_dir

    # Dataset info — read once, compute digest + line count.
    dataset_info: AgentDatasetInfo | None = None
    if spec.evals.dataset:
        ds_path = (agent_dir / spec.evals.dataset).resolve()
        if ds_path.exists() and ds_path.is_file():
            raw = ds_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:12]
            # Count non-empty lines — what mdk eval would walk.
            count = sum(1 for line in raw.decode().splitlines() if line.strip())
            dataset_info = AgentDatasetInfo(
                path=spec.evals.dataset,
                case_count=count,
                sha256_prefix=digest,
                size_bytes=len(raw),
            )

    # Canonical files that ACTUALLY exist on disk. Walks one level
    # deep (matches scan_agents' depth convention).
    candidate_files = [
        "agent.yaml",
        "prompt.md",
        "schema/input.json",
        "schema/output.json",
        "evals/dataset.jsonl",
    ]
    files = sorted(f for f in candidate_files if (agent_dir / f).exists())

    # Prompt body — read from disk so the response is self-contained.
    # Same path the AgentBundle.render_prompt() goes through, but we
    # want the raw template (no Jinja substitution).
    prompt_path = (agent_dir / spec.prompt).resolve()
    prompt_body = prompt_path.read_text() if prompt_path.exists() else ""

    return AgentDetailView(
        name=spec.name,
        version=spec.version,
        description=spec.description,
        owner=spec.owner,
        role=spec.role,
        persona=spec.persona,
        capabilities=list(spec.capabilities),
        tags=list(spec.tags),
        model_provider=spec.model.provider,
        model_params=dict(spec.model.params) if spec.model.params else {},
        model_fallback=[fb.provider for fb in spec.model.fallback] if spec.model.fallback else [],
        runtime=spec.runtime.value,
        prompt=prompt_body,
        prompt_hash=bundle.prompt_hash,
        input_schema=bundle.input_schema,
        output_schema=bundle.output_schema,
        skills=list(spec.skills),
        contexts=list(spec.contexts),
        dataset=dataset_info,
        timeout_call_ms=spec.timeouts.call_ms,
        timeout_total_ms=spec.timeouts.total_ms,
        max_cost_usd_per_run=spec.budget.max_cost_usd_per_run,
        agent_dir=agent_dir.name,
        files=files,
    )


# ---------------------------------------------------------------------------
# Workflow API parity helpers (ADR 037 D1) — the workflow-side analogues of
# the agent helpers above. Kept module-level (not under build_app) so the
# test suite can import them directly.
# ---------------------------------------------------------------------------


async def _collect_workflow_bundle_files(
    *,
    body: WorkflowCreateRequest | None,
    workflow_yaml: UploadFile | None,
    state_schema: UploadFile | None,
    dataset: UploadFile | None,
    bundle: UploadFile | None,
) -> dict[str, bytes]:
    """Normalize the three create-modes (JSON / multipart fields / zipped
    bundle) into a single ``{rel_path: bytes}`` dict.

    Exactly one mode must be supplied; multiple modes → 400. Mirrors
    :func:`_collect_bundle_files` for agents.
    """
    modes_set = sum(
        1
        for v in (
            body,
            workflow_yaml,
            bundle,
        )
        if v is not None
    )
    if modes_set == 0:
        raise http_error(
            ErrorCode.BAD_REQUEST,
            status_code=400,
            message=(
                "POST/PUT /api/v1/workflows requires one of: a JSON body, an "
                "individual workflow_yaml multipart field, or a zipped bundle "
                "field"
            ),
        )
    if modes_set > 1:
        raise http_error(
            ErrorCode.BAD_REQUEST,
            status_code=400,
            message=(
                "POST/PUT /api/v1/workflows accepts exactly one of: JSON body, "
                "individual fields, or zipped bundle — pick ONE"
            ),
        )

    if bundle is not None:
        return unzip_workflow_bundle(await bundle.read())

    if body is not None:
        files: dict[str, bytes] = {"workflow.yaml": body.workflow_yaml.encode("utf-8")}
        for rel, content in body.files.items():
            files[rel] = content.encode("utf-8")
        return files

    # Individual-fields mode.
    assert workflow_yaml is not None  # narrowed by the modes_set guard above
    files = {"workflow.yaml": await workflow_yaml.read()}
    if state_schema is not None:
        files["schema/state.json"] = await state_schema.read()
    if dataset is not None:
        files["evals/dataset.jsonl"] = await dataset.read()
    return files


async def _dual_write_workflow_to_registry(
    storage: StorageProvider,
    workflow_dir: Path,
    *,
    tenant_id: str,
    version: str,
    created_by: str | None,
) -> WorkflowPublishResult | None:
    """Publish a freshly-persisted workflow dir into the durable registry.

    Mirrors :func:`_dual_write_agent_to_registry`. Best-effort: a registry
    failure must not fail the persist — the FS write is still live for
    local serve. Always returns ``None`` on failure (logged) so the caller
    can render the response cleanly.
    """
    try:
        files = workflow_bundle_files_from_dir(workflow_dir)
        return await publish_workflow_bundle(
            storage,
            name=workflow_dir.name,
            tenant_id=tenant_id,
            version=version,
            files=files,
            created_by=created_by,
        )
    except Exception:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "workflow_registry_publish_failed name=%s tenant_id=%s version=%s",
            workflow_dir.name,
            tenant_id,
            version,
            exc_info=True,
        )
        return None


async def _check_workflow_if_match(
    storage: StorageProvider,
    name: str,
    *,
    tenant_id: str,
    if_match: str,
) -> None:
    """Enforce ``If-Match`` optimistic concurrency on workflow PUT.

    Mirrors :func:`_check_agent_if_match`: matches either the current
    latest version's ``version`` OR its ``content_hash``. A missing
    registry row is treated as a match (the on-disk bundle exists but the
    registry is empty — local-serve case).
    """
    current = await storage.get_workflow_bundle(name, tenant_id=tenant_id)
    if current is None:
        return
    expected = _normalize_if_match(if_match)
    if expected in (current.version, current.content_hash):
        return
    raise conflict(
        f"workflow {name!r} was updated concurrently: If-Match {expected!r} "
        f"no longer matches the current version {current.version!r} — re-fetch "
        f"and retry",
    )


async def _do_validate_workflow(
    *,
    request: Request,
    name: str,
    body: WorkflowCreateRequest | None,
    workflow_yaml: UploadFile | None,
    state_schema: UploadFile | None,
    bundle: UploadFile | None,
    ctx: AuthContext,
) -> WorkflowValidationView:
    """Shared validate body — runs the parse/compile path on either the
    supplied bundle (any mode) or the on-disk bundle when nothing is
    supplied (multipart-only convenience for "is the current bundle still
    valid?")."""
    _ = ctx.tenant_id  # future per-tenant isolation
    workflows_path: Path | None = request.app.state.workflows_path
    any_input = (
        body is not None
        or workflow_yaml is not None
        or state_schema is not None
        or bundle is not None
    )
    if not any_input:
        if workflows_path is None or not (workflows_path / name).is_dir():
            raise not_found("workflow", name)
        try:
            load_workflow_spec(workflows_path / name)
        except WorkflowSpecLoadError as exc:
            return _validation_failed(str(exc))
        return WorkflowValidationView(passed=True, errors=[], warnings=[])
    try:
        files = await _collect_workflow_bundle_files(
            body=body,
            workflow_yaml=workflow_yaml,
            state_schema=state_schema,
            dataset=None,
            bundle=bundle,
        )
    except WorkflowPersistenceError as exc:
        return _validation_failed(str(exc))

    import tempfile as _tempfile  # noqa: PLC0415

    from movate.runtime.workflow_persistence import _write_files  # noqa: PLC0415

    staging = Path(_tempfile.mkdtemp(prefix=f".validate-{name}-"))
    try:
        _write_files(staging, files)
        try:
            load_workflow_spec(staging)
        except WorkflowSpecLoadError as exc:
            return _validation_failed(str(exc))
    finally:
        import shutil as _shutil  # noqa: PLC0415

        _shutil.rmtree(staging, ignore_errors=True)
    return WorkflowValidationView(passed=True, errors=[], warnings=[])


async def _do_update_workflow(
    *,
    request: Request,
    name: str,
    body: WorkflowCreateRequest | None,
    workflow_yaml: UploadFile | None,
    state_schema: UploadFile | None,
    dataset: UploadFile | None,
    bundle: UploadFile | None,
    if_match: str | None,
    ctx: AuthContext,
) -> WorkflowUpdatedView:
    """Shared PUT body for the multipart + JSON-body workflow update
    endpoints. Single point of truth so the two routes never diverge.
    """
    workflows_path: Path | None = request.app.state.workflows_path
    if workflows_path is None:
        raise WorkflowPersistenceError(
            "runtime was built without a workflows_path; "
            "PUT /api/v1/workflows/{name} is unavailable",
            status_code=503,
        )
    store: StorageProvider = request.app.state.storage
    existing = await store.get_workflow_bundle(name, tenant_id=ctx.tenant_id)
    on_disk = (workflows_path / name).is_dir()
    if existing is None and not on_disk:
        raise not_found("workflow", name)
    previous_version = existing.version if existing is not None else "<unknown>"
    if if_match is not None:
        await _check_workflow_if_match(store, name, tenant_id=ctx.tenant_id, if_match=if_match)
    files = await _collect_workflow_bundle_files(
        body=body,
        workflow_yaml=workflow_yaml,
        state_schema=state_schema,
        dataset=dataset,
        bundle=bundle,
    )
    try:
        yaml_name = yaml.safe_load(files["workflow.yaml"])["name"]
    except Exception as exc:
        raise http_error(
            ErrorCode.BAD_REQUEST,
            status_code=422,
            message=f"workflow.yaml could not be parsed: {exc}",
        ) from exc
    if yaml_name != name:
        raise http_error(
            ErrorCode.BAD_REQUEST,
            status_code=422,
            message=(
                f"workflow.yaml name {yaml_name!r} does not match the URL path parameter {name!r}"
            ),
        )
    result = persist_workflow_bundle(files, workflows_path=workflows_path, on_conflict="replace")
    published = await _dual_write_workflow_to_registry(
        store,
        result.workflow_dir,
        tenant_id=ctx.tenant_id,
        version=result.spec.version,
        created_by=ctx.api_key_id,
    )
    return WorkflowUpdatedView(
        name=result.spec.name,
        version=result.spec.version,
        description=result.spec.description,
        workflow_dir=result.workflow_dir.name,
        files_persisted=result.files_persisted,
        previous_version=previous_version,
        published_version=published.version if published is not None else None,
        changed=published.published if published is not None else True,
    )


async def _resolve_published_version(
    storage: StorageProvider,
    name: str,
    *,
    tenant_id: str,
) -> str | None:
    """Return the version flagged ``published=True`` for ``(name, tenant)``,
    or ``None`` when nothing is published.

    Walks the version history newest-first; at most one row has the flag set
    so this terminates quickly. Pure-helper — exposed here so list / publish /
    delete handlers can label the response uniformly.
    """
    history = await storage.list_workflow_versions(name, tenant_id=tenant_id, limit=1000)
    for row in history:
        if row.published:
            return row.version
    return None


async def _demote_workflow_published(
    storage: StorageProvider,
    name: str,
    *,
    tenant_id: str,
) -> None:
    """No-op hook for now — the DELETE handler invokes this so a future
    "explicit unpublish-all" Protocol method can land without touching
    every call site.

    ADR 037 D1 design call: a soft-delete preserves the entire version
    history (the contract the API documents — "DELETE preserves versions").
    A registry row's ``published`` flag is intentionally NOT cleared on
    delete: the row remains the immutable record of "this is what was
    published when soft-delete happened." After a soft-delete, the
    workflow disappears from the **filesystem** catalog (the API
    surfaces "are there registry rows" as the existence signal, but a
    versionless GET still returns the registry's latest). Operators
    who want to fully remove a workflow + its history use a separate
    operator-level path (out of scope for this PR — durable revoke is
    its own ADR seam).

    Kept as a function (not inlined) so the DELETE handler reads cleanly
    and a future "really unpublish" Protocol primitive can replace this
    body without disturbing the route.
    """
    _ = storage, name, tenant_id  # documented no-op


def _peek_workflow_yaml_tags(files: dict[str, str]) -> tuple[list[str], str]:
    """Extract ``tags`` + ``description`` from a bundle's workflow.yaml.

    Lightweight YAML parse used by the catalog list view so it can render
    metadata without re-loading + compiling the full spec for every row.
    Returns ``([], "")`` on any parse failure — the list view degrades
    gracefully rather than 500-ing on a corrupt row.
    """
    raw = files.get("workflow.yaml")
    if not raw:
        return [], ""
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError:
        return [], ""
    if not isinstance(doc, dict):
        return [], ""
    tags = doc.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    description = doc.get("description") or ""
    if not isinstance(description, str):
        description = ""
    return [str(t) for t in tags], description


def _render_workflow_detail(record: WorkflowBundleRecord) -> WorkflowDetailView:
    """Build the ``WorkflowDetailView`` from a registry record.

    Pure function — parses the bundle's ``workflow.yaml`` for the spec
    metadata + nodes/edges; tolerates malformed YAML (returns empty
    nodes/edges rather than 500). The full Pydantic validation happened at
    publish time, so a registry row is by construction parseable; the
    defensive branches exist for forward-compat (a future schema bump that
    a stale handler hasn't learned to read).
    """
    files_sorted = sorted(record.files.keys())
    raw = record.files.get("workflow.yaml", "")
    description = ""
    owner = ""
    tags: list[str] = []
    entrypoint = ""
    state_schema_path = ""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError:
        doc = None
    if isinstance(doc, dict):
        description = str(doc.get("description") or "")
        owner = str(doc.get("owner") or "")
        raw_tags = doc.get("tags") or []
        if isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags]
        entrypoint = str(doc.get("entrypoint") or "")
        state_schema_path = str(doc.get("state_schema") or "")
        raw_nodes = doc.get("nodes") or []
        if isinstance(raw_nodes, list):
            nodes = [n if isinstance(n, dict) else {"value": n} for n in raw_nodes]
        raw_edges = doc.get("edges") or []
        if isinstance(raw_edges, list):
            edges = [e if isinstance(e, dict) else {"value": e} for e in raw_edges]

    return WorkflowDetailView(
        name=record.name,
        version=record.version,
        description=description,
        owner=owner,
        tags=tags,
        entrypoint=entrypoint,
        state_schema_path=state_schema_path,
        nodes=nodes,
        edges=edges,
        content_hash=record.content_hash,
        created_by=record.created_by,
        created_at=record.created_at,
        published_version=record.version if record.published else None,
        is_published=record.published,
        files=files_sorted,
    )


def _validation_failed(message: str) -> WorkflowValidationView:
    """Build a ``WorkflowValidationView`` from a single error message."""
    return WorkflowValidationView(
        passed=False,
        errors=[
            WorkflowValidationIssue(
                code="invalid_workflow_spec",
                severity="error",
                message=message,
            )
        ],
        warnings=[],
    )


# How many prior turns to inject into a threaded message's input
# under ``conversation_history`` (PR-R). 20 turns at ~500 tokens each
# is ~10k tokens of context — comfortable for modern models, leaves
# room for the current input + prompt + output. Operators wanting a
# different window can pre-supply ``conversation_history`` in the
# request body (the endpoint preserves caller-supplied values).
_THREAD_HISTORY_TURNS = 20

# Char-based budget cap on the injected history (PR-U). 40000 chars
# ≈ 10k tokens by the 4-chars-per-token rule of thumb. When the
# turn-count cap above pulls more bytes than this, we drop OLDEST
# turns first so the most recent context survives. Without this
# cap, a thread with verbose turns could blow past the model's
# context window even though the turn count is under the limit.
#
# Belt-and-braces: real callers who hit this often should be
# pre-summarizing older turns via the caller-supplied-wins path
# rather than relying on raw truncation. The cap just stops the
# pathological case (single 50KB turn) from breaking everyone else.
_THREAD_HISTORY_CHAR_BUDGET = 40000


def _batch_max_rows() -> int:
    """Cap on rows per ``POST /api/v1/agents/{name}/batch`` (item 17).

    A single batch submission enqueues ONE job per dataset row; without a
    ceiling a single request could flood the shared queue (and starve other
    tenants' single runs). Default 10000 — generous for realistic eval / bulk
    datasets while bounding the blast radius of one request. Operators tune it
    per-deployment via ``MDK_BATCH_MAX_ROWS`` (a non-positive / unparseable
    value falls back to the default). Read per-request so a deploy can change
    it without a code change; the cost is one ``os.environ`` lookup per submit.
    """
    raw = os.environ.get("MDK_BATCH_MAX_ROWS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return _BATCH_MAX_ROWS_DEFAULT
        if parsed > 0:
            return parsed
    return _BATCH_MAX_ROWS_DEFAULT


_BATCH_MAX_ROWS_DEFAULT = 10_000

# ADR 032 D2: hard cap on rows the aggregate monitor endpoints
# (``GET /api/v1/report`` + ``/agents/{name}/metrics``) read per call. The
# rollup is over the most-recent N runs/evals so a tenant with a huge history
# can't make the read unbounded; mirrors the CLI's ``mdk report`` fetch cap.
_REPORT_FETCH_CAP = 10_000

# item 37: submission idempotency. The OPTIONAL header an async-submit caller
# may send so a retry (network blip / timeout) returns the SAME job instead of
# double-enqueuing. When present, the submit path dedups on
# ``(tenant_id, idempotency_key)`` (per-tenant via the AuthContext) so a repeat
# returns the original job without re-enqueuing. Absent → byte-for-byte today's
# always-enqueue behavior. Capped to bound storage; an empty value is treated
# as absent. The header name follows the de-facto industry convention
# (Stripe/IETF ``Idempotency-Key``).
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LEN = 200


def _parse_jsonl_rows(raw: bytes) -> list[dict[str, Any]]:
    """Parse a JSONL byte payload into a list of input-row dicts.

    Every non-empty line must be a JSON object — same contract as the
    dataset-upload endpoint. A malformed line or a non-object value raises
    ``HTTPException`` (422) naming the line so the caller can fix it. Blank
    lines are skipped so trailing newlines are harmless.
    """
    rows: list[dict[str, Any]] = []
    for lineno, raw_line in enumerate(raw.decode().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=422,
                message=f"batch dataset line {lineno} is not valid JSON: {exc}",
            ) from exc
        if not isinstance(obj, dict):
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=422,
                message=(
                    f"batch dataset line {lineno} must be a JSON object, got {type(obj).__name__}"
                ),
            )
        rows.append(obj)
    return rows


async def _parse_batch_dataset(request: Request) -> tuple[list[dict[str, Any]], str | None]:
    """Extract the batch dataset rows + optional notify_email from a request.

    Dispatches on ``Content-Type`` so one endpoint serves both shapes:

    * ``multipart/form-data`` → a ``file`` field carrying a **JSONL** dataset
      (one JSON object per line). The form may also carry ``notify_email``.
    * otherwise (JSON body) → ``{"inputs": [ {...}, ... ], "notify_email"?: ...}``
      validated against :class:`BatchInlineSubmission`.

    Returns ``(rows, notify_email)``. Raises ``HTTPException`` (422) on a
    malformed dataset / missing file / unparseable body — never silently
    coerces, so a typo fails the submit loud rather than enqueuing garbage.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        # ``request.form()`` returns Starlette's UploadFile (FastAPI's
        # ``UploadFile`` is a *subclass*, so an isinstance against the FastAPI
        # re-export would miss it). Check the Starlette base instead.
        from starlette.datastructures import UploadFile as _StarletteUploadFile  # noqa: PLC0415

        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, _StarletteUploadFile):
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=422,
                message="multipart batch upload requires a 'file' field holding a JSONL dataset",
            )
        raw = await upload.read()
        rows = _parse_jsonl_rows(raw)
        notify_field = form.get("notify_email")
        notify_email = notify_field if isinstance(notify_field, str) and notify_field else None
        return rows, notify_email

    # JSON body path — {"inputs": [...], "notify_email"?: ...}.
    from pydantic import ValidationError  # noqa: PLC0415

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise http_error(
            ErrorCode.BAD_REQUEST,
            status_code=422,
            message=f"batch body is not valid JSON: {exc}",
        ) from exc
    try:
        submission = BatchInlineSubmission.model_validate(body)
    except ValidationError as exc:
        raise http_error(
            ErrorCode.BAD_REQUEST,
            status_code=422,
            message=f"batch body must be {{'inputs': [ {{...}}, ... ]}}: {exc.errors()}",
        ) from exc
    return submission.inputs, submission.notify_email


def _read_idempotency_key(request: Request) -> str | None:
    """Read + normalize the OPTIONAL ``Idempotency-Key`` header (item 37).

    Strips surrounding whitespace; returns ``None`` (→ treated as absent, i.e.
    today's always-enqueue behavior) when the header is missing, empty after
    stripping, or longer than :data:`IDEMPOTENCY_KEY_MAX_LEN` (so an arbitrary
    header can't bloat the dedup store).
    """
    raw = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    key = raw.strip() if raw else None
    if not key or len(key) > IDEMPOTENCY_KEY_MAX_LEN:
        return None
    return key


async def _idempotent_submit_guard(
    request: Request, store: StorageProvider, ctx: AuthContext
) -> str | None:
    """Return the ``job_id`` a prior submission with this key enqueued, or ``None``.

    Pre-create check for the async (queued, 202) submit endpoints (item 37): if
    the caller sent an ``Idempotency-Key`` we've already seen for this tenant,
    the endpoint returns that SAME job instead of enqueuing a second one. No
    header (or an unusable one) → ``None`` → the endpoint creates a job as
    today. After creating, the endpoint calls
    :meth:`StorageProvider.record_run_submission` (race-safe) to bind the key.
    """
    key = _read_idempotency_key(request)
    if key is None:
        return None
    return await store.get_run_submission(ctx.tenant_id, key)


def _apply_history_char_budget(
    turns: list[dict[str, Any]],
    *,
    budget: int = _THREAD_HISTORY_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """Trim the OLDEST turns from ``turns`` until total char count
    fits within ``budget``.

    Most-recent turns survive — they're the highest-value context
    for the next message. Returns a NEW list (input untouched).

    Char count = ``len(json.dumps(turn))`` for each turn. Approximate
    by ~4 chars per token; the default 40000-char budget ≈ 10k tokens.

    Empty input or budget>=total → return input unchanged. Single-turn
    overflow → return a one-element list with that turn (we don't
    drop the most recent turn to fit budget — better to overflow
    than send empty history). Operators with consistently huge turns
    should pre-summarize via the caller-supplied-wins path.
    """
    import json  # noqa: PLC0415 — lazy: most requests don't hit the budget

    if not turns:
        return turns
    sizes = [len(json.dumps(t, default=str)) for t in turns]
    total = sum(sizes)
    if total <= budget:
        return list(turns)
    # Drop oldest first. Keep the most recent N that fit; always
    # keep at least the last turn even if it alone exceeds budget.
    kept_reverse: list[dict[str, Any]] = []
    remaining = budget
    for turn, size in zip(reversed(turns), reversed(sizes), strict=True):
        if not kept_reverse or remaining - size >= 0:
            kept_reverse.append(turn)
            remaining -= size
        else:
            break
    return list(reversed(kept_reverse))


def build_app(
    storage: StorageProvider,
    *,
    agents: list[AgentBundle] | None = None,
    agents_path: Path | None = None,
    skills_path: Path | None = None,
    workflows_path: Path | None = None,
    rate_limit_per_minute: int | None = 60,
    tenant_rate_limit_per_minute: int | None = None,
    cors_allowed_origins: list[str] | None = None,
    github_client: object | None = None,
    import_tenant_id: str | None = None,
    max_request_bytes: int | None = None,
) -> FastAPI:
    """Build the FastAPI app bound to ``storage`` + ``agents``.

    ``agents`` is the registry returned by :func:`scan_agents`. Scan
    happens once at app build time so each ``GET /agents`` is a
    constant-time list lookup, not a fresh disk walk. Pass ``None``
    (the default) for tests that don't care about the registry.

    ``rate_limit_per_minute`` is the per-API-key token-bucket
    capacity (and the steady-state allowed request rate). Default
    60. Pass ``None`` to disable rate limiting entirely (uses a
    :class:`NoOpRateLimiter` that always allows).

    ``tenant_rate_limit_per_minute`` (item 25) is a SECOND, aggregate
    ceiling applied across ALL of a tenant's API keys — so a tenant
    can't sidestep the per-key limit by minting more keys (each key
    gets its own per-key bucket; the per-tenant bucket is shared by
    every key of that tenant). Default ``None`` → OFF (a
    :class:`NoOpRateLimiter`, behavior byte-for-byte the per-key-only
    path); env-overridable via ``MDK_TENANT_RATE_LIMIT_PER_MINUTE``
    (the explicit kwarg wins when not ``None``). When enabled, a
    request is allowed only if BOTH the per-key and per-tenant buckets
    allow; the 429 ``Retry-After`` is the max of the two waits. Like
    the per-key limiter, per-tenant state is in-process per replica
    (effective tenant limit ≈ ``limit * replica_count`` in v1.x);
    Redis-backed shared state is the documented future seam.

    ``max_request_bytes`` (ADR 033 D6) caps the request body size; an
    over-large body is rejected with a ``413`` envelope before any
    handler reads it. ``None`` (the default) → resolved from
    ``MDK_MAX_REQUEST_BYTES`` else the 25 MiB default; pass ``0`` to
    disable the guard. Additive — only introduces the new 413 path.

    The app's ``state`` carries collaborators so handlers can read
    them without closing over the factory's locals — keeps
    testability clean (override ``app.state.storage`` /
    ``state.agents`` / ``state.rate_limiter`` to swap mid-test if
    you really need to).
    """
    # One-time filesystem → registry import (ADR 014 D5), wired as a
    # lifespan startup step. On boot, if an import tenant is configured,
    # seed any filesystem-scanned agents not yet in the durable registry
    # so a deployed runtime's pre-baked agents become registry-resolvable
    # by every pod (incl. the worker). Idempotent — already-present
    # (name, version) rows are skipped — so it's safe on every boot.
    # Guarded: skipped entirely when no import tenant is set (local
    # ``mdk serve`` without durable storage), and never raises (a seed
    # failure must not block the runtime from coming up — the FS fallback
    # still serves those agents).
    from contextlib import asynccontextmanager  # noqa: PLC0415

    @asynccontextmanager
    async def _lifespan(app_: FastAPI) -> AsyncIterator[None]:
        # Belt-and-suspenders MDK_* ↔ MOVATE_* env bridge (#67). The CLI
        # entrypoint (movate.cli.main) already runs this at startup, but a
        # runtime booted via a direct ASGI/uvicorn factory, embedded, or in
        # tests never hits that path — so re-run it here, BEFORE any storage
        # or seed env var is read, to guarantee the bridge holds. Idempotent
        # (a no-op when already synced). Imported from movate.core (NOT
        # movate.cli) so the execution plane never imports the control plane
        # — see docs/architecture-principles.md.
        from movate.core.env_aliases import sync_env_aliases  # noqa: PLC0415

        sync_env_aliases()

        import_tenant: str | None = app_.state.import_tenant_id
        fs_agents: list[AgentBundle] = app_.state.agents
        if import_tenant and fs_agents:
            try:
                count = await import_filesystem_agents(storage, fs_agents, tenant_id=import_tenant)
                if count:
                    import logging  # noqa: PLC0415

                    logging.getLogger(__name__).info(
                        "agent_registry_seeded count=%d tenant_id=%s",
                        count,
                        import_tenant,
                    )
            except Exception:
                import logging  # noqa: PLC0415

                logging.getLogger(__name__).warning(
                    "agent_registry_seed_failed tenant_id=%s",
                    import_tenant,
                    exc_info=True,
                )
        yield

    app = FastAPI(
        title="movate",
        version=movate.__version__,
        description="Declarative platform for building and running AI agents.",
        lifespan=_lifespan,
    )
    app.state.storage = storage
    app.state.agents = agents or []
    # Tenant to seed the durable registry with filesystem agents on first
    # boot (ADR 014 D5 one-time import). ``None`` (the default + local
    # ``mdk serve``) skips the import entirely — the resolver's
    # filesystem-fallback already serves those agents, so seeding is a
    # convenience for deployed multi-pod runtimes that want FS agents to
    # become registry-resolvable. Env fallback lets a deploy set it
    # without a code change.
    app.state.import_tenant_id = import_tenant_id or os.environ.get("MDK_AGENTS_IMPORT_TENANT_ID")
    # Where new agents (POST /api/v1/agents, item 76) land on disk.
    # None means the endpoint returns 503 — the runtime was built
    # without an agents_path and can't persist. mdk serve always
    # passes its --agents-path here; tests pass tmp_path.
    app.state.agents_path = agents_path
    # Where new skills (POST /api/v1/skills) land. Defaults to
    # ``<agents_path>/skills/`` so the agent loader's project-root
    # fallback (``agent_dir.parent`` when no project marker is found)
    # resolves to the same directory. Explicit skills_path overrides —
    # used by tests and operators who keep skills on a sibling volume.
    # ADR 037 D1: where new workflows (POST /api/v1/workflows) land on disk.
    # Defaults to a sibling ``workflows/`` next to ``agents_path`` so the
    # bundled-deploy convention "agents/, workflows/, skills/" Just Works.
    # ``None`` (no agents_path either) means the workflow CRUD endpoints
    # return 503 — same back-compat as agents (test configurations may pass
    # neither; mdk serve always passes both).
    app.state.skills_path = _default_sibling_path(skills_path, agents_path, name="skills")
    app.state.workflows_path = _default_sibling_path(
        workflows_path, agents_path, name="workflows", at_parent=True
    )
    # GitHub integration (item 78 / ADR 007). Built lazily when
    # ``MDK_GITHUB_ENABLED=1`` so the typical runtime (no GitHub) pays
    # no cost. Tests pass a pre-built mock through ``github_client``.
    # ``None`` means the endpoint returns 503.
    if github_client is not None:
        app.state.github_client = github_client
    elif _github_is_enabled():
        try:
            from movate.integrations.github import (  # noqa: PLC0415
                GitHubClient,
                GitHubConfig,
            )

            app.state.github_client = GitHubClient(GitHubConfig.from_env())
        except Exception as exc:
            # A bad config shouldn't take the whole runtime down —
            # surface as "not configured" at the endpoint. Logged loud
            # so operators see what broke at boot.
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).warning("github_integration_init_failed reason=%s", exc)
            app.state.github_client = None
    else:
        app.state.github_client = None

    # ------------------------------------------------------------------
    # Layer-1 API hardening middlewares (ADR 033). Starlette applies
    # middlewares in REVERSE registration order (last added = outermost),
    # so the desired wrapping outer→inner is:
    #     RequestId  →  CORS  →  PayloadSizeLimit  →  app
    # Register accordingly: payload guard FIRST (innermost), CORS next,
    # request-id LAST (outermost) so it wraps everything — including the
    # CORS layer, a 413 from the payload guard, a 429 from the limiter, and
    # any 5xx — meaning EVERY response carries ``X-Request-Id``.
    #
    # D6 — payload size guard (innermost of the three). Rejects an
    # over-large body with the 413 envelope before any handler reads it.
    # Mounted unconditionally; a resolved ``0`` (disabled, via
    # ``MDK_MAX_REQUEST_BYTES=0`` or an explicit ``max_request_bytes=0``)
    # makes the middleware a pure pass-through (no body buffering).
    max_body = resolve_max_request_bytes(max_request_bytes)
    app.state.max_request_bytes = max_body
    app.add_middleware(PayloadSizeLimitMiddleware, max_bytes=max_body)

    # CORS — required for browser-side callers (the Mova iO Angular
    # app). Allow-list resolved from the explicit kwarg, then env vars,
    # then empty (= middleware not mounted). The wildcard ``"*"`` is
    # supported but only fully works with ``allow_credentials=False``
    # — browsers reject ``*`` + credentials per the CORS spec. For
    # bearer-token auth (which we use) credentials don't need to ride
    # on cookies, so ``allow_credentials=False`` is the correct default.
    # Operators with cookie-based session auth (future) flip credentials
    # on AND pin the origin list to exact hosts.
    origins = _resolve_cors_origins(cors_allowed_origins)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
            allow_headers=["*"],
            # X-RateLimit-* + Retry-After need to be readable by browser
            # JS so the Angular client can show a "you'll be rate-limited
            # in N seconds" hint. Without expose_headers, CORS strips them.
            expose_headers=[
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                # Per-tenant aggregate budget (item 25) — additive; lets
                # the browser client tell which ceiling it's near / hit.
                "X-RateLimit-Tenant-Limit",
                "X-RateLimit-Tenant-Remaining",
                "X-RateLimit-Tenant-Reset",
                "Retry-After",
                # Per-request correlation id (ADR 033 D2) — let browser JS
                # read it so a client can surface / report it on errors.
                REQUEST_ID_HEADER,
            ],
        )

    # D2 — request correlation (OUTERMOST). Added last so it wraps the CORS
    # layer and the payload guard: binds the per-request id to the logging /
    # error-envelope context and stamps ``X-Request-Id`` on every response.
    # Install the matching logging filter so log lines carry the same id (a
    # no-op-safe, idempotent attach mirroring ADR 024's trace correlation).
    install_request_id_logging()
    app.add_middleware(RequestIdMiddleware)

    # Build the rate limiter once at app construction so bucket state
    # persists across requests. NoOp when disabled, but the middleware
    # still calls .check() — keeps the header path uniform.
    limiter: RateLimiter
    if rate_limit_per_minute is None or rate_limit_per_minute <= 0:
        limiter = NoOpRateLimiter()
    else:
        limiter = InProcessRateLimiter(limit_per_minute=rate_limit_per_minute)
    app.state.rate_limiter = limiter

    # Per-tenant aggregate rate limiter (item 25) — a SECOND bucket keyed
    # by tenant_id, capping total throughput across all of a tenant's
    # keys. Additive + OFF by default: the explicit kwarg wins, else fall
    # back to ``MDK_TENANT_RATE_LIMIT_PER_MINUTE``, else None → NoOp (no
    # behavior change vs the per-key-only path). Built once here so the
    # bucket state persists across requests for this replica's lifetime.
    tenant_limit = _resolve_tenant_rate_limit(tenant_rate_limit_per_minute)
    tenant_limiter: RateLimiter
    if tenant_limit is None or tenant_limit <= 0:
        tenant_limiter = NoOpRateLimiter()
    else:
        tenant_limiter = InProcessRateLimiter(limit_per_minute=tenant_limit)
    app.state.tenant_rate_limiter = tenant_limiter

    # Build the LLM response cache once at app construction so entries
    # persist across requests within this replica (mirrors the rate
    # limiter's lifecycle). NoOp / OFF unless MOVATE_LLM_CACHE selects
    # a backend — unset → zero behavior change. In-process per-replica
    # in v1.x; shared backends slot in behind the CacheProvider later.
    app.state.llm_cache = build_cache()

    # ADR 035 D3 — per-tenant SSE subscriber accounting. A simple
    # ``{tenant_id: count}`` dict, lock-guarded for the increment/cap
    # check + decrement edges so a burst of opens can't race past the
    # ceiling. In-process per-replica (mirrors the rate-limiter
    # lifecycle); cross-replica accounting is the same future Redis
    # seam. Initialised here so the dict identity persists across
    # requests for the replica's lifetime.
    app.state.events_sse_connections = {}
    app.state.events_sse_lock = asyncio.Lock()

    auth_dep = make_auth_dependency(
        storage, rate_limiter=limiter, tenant_rate_limiter=tenant_limiter
    )

    # ``require_scope(auth_dep, ...)`` (ADR 013 L2) layers a per-endpoint
    # scope check on top of ``auth_dep``. Passing the SAME ``auth_dep``
    # object means the scope checker and the handler's own
    # ``Depends(auth_dep)`` share FastAPI's per-request dependency cache —
    # the bearer is parsed, the key looked up, and the rate limiter charged
    # exactly once per request. Bind it once here so the call sites stay
    # terse.
    def _scope(*needed: str) -> Any:
        return Depends(require_scope(auth_dep, *needed))

    # ------------------------------------------------------------------
    # /healthz — unauthed liveness probe
    # ------------------------------------------------------------------
    @app.get("/healthz", response_model=HealthView, tags=["meta"])
    async def healthz() -> HealthView:
        """Liveness probe. Cheap on purpose — never hits storage.

        ACA's liveness probe restarts a pod if this fails. We
        deliberately don't gate on DB connectivity here because a DB
        blip would otherwise trigger a pod restart that doesn't help
        (the new pod will hit the same dead DB). Use ``/ready`` for
        readiness; let liveness stay simple.
        """
        return HealthView(status="ok", version=movate.__version__)

    # ------------------------------------------------------------------
    # /api/v1/openapi.json — versioned alias (item 120)
    # ------------------------------------------------------------------
    # FastAPI emits the OpenAPI spec at the unversioned /openapi.json;
    # we keep that for backward compat AND expose a versioned alias so
    # client-gen tooling that expects every v1 path under /api/v1/* can
    # point at a consistent prefix. The alias returns the SAME spec —
    # not a v1-filtered subset — because the spec already self-describes
    # via the per-route ``/api/v1/...`` paths.
    @app.get(
        "/api/v1/openapi.json",
        include_in_schema=False,
        tags=["meta"],
    )
    async def openapi_v1_alias() -> JSONResponse:
        return JSONResponse(content=app.openapi())

    # ------------------------------------------------------------------
    # /ready — unauthed readiness probe with deep checks
    # ------------------------------------------------------------------
    @app.get(
        "/ready",
        response_model=ReadyView,
        tags=["meta"],
        responses={503: {"model": ReadyView}},
    )
    async def ready(request: Request) -> Response:
        """Readiness probe with deep checks.

        ACA's readiness probe stops routing traffic to a pod when
        this fails (but doesn't restart it — that's liveness's job).
        We check the dependencies whose failure would make every
        request 5xx: storage backend connectivity, primarily.

        Returns 200 with ``{"status": "ready", "checks": {...}}`` on
        the happy path; 503 with ``{"status": "not_ready", "checks":
        {"storage": "<error>"}}`` if any check fails. The HTTP
        status is what ACA reads; the JSON body is for human triage
        and curl-by-hand debugging.
        """
        store: StorageProvider = request.app.state.storage
        checks: dict[str, str] = {}
        # Storage ping — covers DB-down, pool-exhausted, network-blip,
        # sqlite-file-missing. Any backend error here means real
        # queries will fail too, so the pod shouldn't get traffic.
        try:
            await store.ping()
            checks["storage"] = "ok"
        except Exception as exc:
            # Surface the exception class + a truncated message. We
            # don't want to leak DSNs or other internals, but the
            # class name + short message is operator-actionable.
            checks["storage"] = f"{type(exc).__name__}: {str(exc)[:120]}"

        # Surface which backend was selected + whether it's durable
        # across container restarts. Drives `mdk doctor target` and
        # makes "Postgres intended, SQLite actually picked" debuggable
        # from a single HTTP call.
        from movate.storage import selected_backend  # noqa: PLC0415

        backend_info = selected_backend()
        storage_backend = backend_info[0] if backend_info else None
        storage_durable = backend_info[2] if backend_info else None

        all_ok = all(v == "ok" for v in checks.values())
        body = ReadyView(
            status="ready" if all_ok else "not_ready",
            version=movate.__version__,
            checks=checks,
            storage_backend=storage_backend,
            storage_durable=storage_durable,
        )
        return JSONResponse(
            status_code=200 if all_ok else 503,
            content=body.model_dump(),
        )

    # ------------------------------------------------------------------
    # GET /agents — registry discovery
    # ------------------------------------------------------------------
    @app.get(
        "/agents",
        response_model=AgentListView,
        tags=["meta"],
        dependencies=[_scope("read")],
    )
    async def list_agents(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentListView:
        """List agents available on this runtime.

        Auth-required for consistency (every non-healthz endpoint
        gates on a key); discovery is per-runtime, not per-tenant in
        v0.5 — every authenticated tenant sees the same catalog.
        Per-tenant agent visibility lands when a customer asks for it.

        Returns metadata only (name, version, description). The full
        agent definition lives on disk; this endpoint is for ``what
        can I call?``, not for fetching prompts or schemas.
        """
        _ = ctx  # auth gate; tenant attribution lives in logs/spans
        agents: list[AgentBundle] = request.app.state.agents
        return AgentListView(
            agents=[
                AgentView(
                    name=b.spec.name,
                    version=b.spec.version,
                    description=b.spec.description,
                )
                for b in agents
            ]
        )

    # ------------------------------------------------------------------
    # POST /run — queue a job
    # ------------------------------------------------------------------
    @app.post(
        "/run",
        response_model=RunAccepted,
        tags=["jobs"],
        status_code=202,
        dependencies=[_scope("run")],
    )
    async def submit_run(
        body: RunSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunAccepted:
        """Queue a job for the worker to claim.

        Returns ``202 Accepted`` (not ``201 Created``) — the resource
        being created is the *job*, but it's not yet executed; clients
        poll ``/jobs/{id}`` until terminal. The 202 status code makes
        that distinction wire-visible.

        item 37: an OPTIONAL ``Idempotency-Key`` header makes a retry
        (network blip / timeout) return the SAME job instead of double-
        enqueuing. Absent → byte-for-byte today's always-enqueue path.
        """
        store: StorageProvider = request.app.state.storage

        # item 37 — submission idempotency. Pre-create check: a prior submit
        # with this key for this tenant returns the SAME job; do NOT enqueue
        # again. No header → prior is None → today's path.
        prior_job_id = await _idempotent_submit_guard(request, store, ctx)
        if prior_job_id is not None:
            return RunAccepted(job_id=prior_job_id, status=JobStatus.QUEUED, deduplicated=True)

        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=body.kind,
            target=body.target,
            status=JobStatus.QUEUED,
            input=body.input,
            api_key_id=ctx.api_key_id,
            notify_email=body.notify_email,
            # ADR 019: capture the originating trace so the worker continues it.
            trace_context=inject_current_trace_context(),
        )
        await store.save_job(job)

        # item 37 — bind the key AFTER create so the recorded job_id is real.
        # Race-safe: if a concurrent retry won, record returns False and we
        # prefer its stored job_id (one canonical response; under a true
        # simultaneous race we may have enqueued one extra job).
        key = _read_idempotency_key(request)
        if key is not None:
            recorded = await store.record_run_submission(ctx.tenant_id, key, job.job_id)
            if not recorded:
                winning_job_id = await store.get_run_submission(ctx.tenant_id, key)
                if winning_job_id is not None and winning_job_id != job.job_id:
                    return RunAccepted(
                        job_id=winning_job_id, status=JobStatus.QUEUED, deduplicated=True
                    )
        return RunAccepted(job_id=job.job_id, status=job.status)

    # ------------------------------------------------------------------
    # GET /jobs/{id} — poll
    # ------------------------------------------------------------------
    @app.get(
        "/jobs",
        response_model=JobListView,
        tags=["jobs"],
        dependencies=[_scope("read")],
    )
    async def list_jobs(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> JobListView:
        """Return this tenant's recent jobs, newest first.

        Always tenant-scoped — there's no cross-tenant variant on
        this endpoint. ``status`` filters to one terminal/transient
        state; omit for "all states". ``limit`` is hard-capped at 100
        to keep the response bounded; deeper history goes through
        ``movate logs`` against the local sqlite (operator path)."""
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_jobs(
            tenant_id=ctx.tenant_id,
            status=status,
            limit=capped_limit,
        )
        views = [JobView.from_record(r) for r in records]
        return JobListView(jobs=views, count=len(views))

    @app.get(
        "/jobs/{job_id}",
        response_model=JobView,
        tags=["jobs"],
        dependencies=[_scope("read")],
    )
    async def get_job(
        job_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> JobView:
        """Return job state. Tenant-scoped at the SQL layer
        (``get_job(..., tenant_id=...)`` filters in WHERE) so a
        cross-tenant lookup returns ``None`` and we 404 — never 403,
        which would leak the existence of the id."""
        store: StorageProvider = request.app.state.storage
        record = await store.get_job(job_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("job", job_id)
        return JobView.from_record(record)

    @app.get(
        "/runs/{run_id}",
        response_model=RunView,
        tags=["runs"],
        dependencies=[_scope("read")],
    )
    async def get_run(
        run_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunView:
        """Return a single run including its ``output``.

        Companion to ``GET /jobs/{id}`` — ``JobView`` only carries the
        ``result_run_id`` pointer, not the actual agent output. Callers
        that want to *see* what the agent produced fetch the job, read
        ``result_run_id``, then hit this endpoint. Same tenant-scoping
        story as jobs: 404 on cross-tenant access (never 403, which
        would leak that the id exists)."""
        store: StorageProvider = request.app.state.storage
        record = await store.get_run(run_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("run", run_id)
        return RunView.from_record(record)

    # ------------------------------------------------------------------
    # Run feedback (Chainlit playground / operators rating outputs) —
    # 0.8.2.11. Two endpoints: POST creates / updates a feedback row;
    # GET lists feedback for a run so the UI can re-open prior ratings.
    #
    # Lives on the pre-v1 unversioned path because clients tend to
    # treat feedback as part of the run resource (same tenancy +
    # auth shape as ``GET /runs/{id}``).
    # ------------------------------------------------------------------

    @app.post(
        "/runs/{run_id}/feedback",
        response_model=FeedbackView,
        status_code=201,
        tags=["runs", "feedback"],
        dependencies=[_scope("run")],
    )
    async def post_run_feedback(
        run_id: str,
        submission: FeedbackSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> FeedbackView:
        """Create (or update) an operator feedback row for ``run_id``.

        Auth: the authenticated tenant must own the underlying run —
        404 on cross-tenant attempts (mirrors ``GET /runs/{id}``).

        ``user_id`` precedence: when the auth context carries an
        identity (sub claim / Azure AD object_id), it wins over any
        ``user_id`` the client supplied. When auth is anonymous
        (dev mode), the client-supplied ``user_id`` is used; if
        neither is set, the row is rejected with 422.

        Feedback is persisted via ``StorageProvider.save_feedback``
        with upsert semantics (same ``feedback_id`` overwrites). When
        Langfuse is configured AND the run has a trace, the score is
        also pushed to Langfuse via ``langfuse.score()`` and the
        returned id is stored alongside the row.
        """
        from movate.core.models import FeedbackRecord  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        record = await store.get_run(run_id, tenant_id=ctx.tenant_id)
        if record is None:
            # Tenant-scoped 404 — never leak that the run exists for
            # another tenant. Mirrors GET /runs/{id} above.
            raise not_found("run", run_id)

        # User identity: auth context wins. Falls back to client-
        # supplied user_id only when the context has no identity
        # (e.g. dev mode with auth disabled).
        ctx_identity = getattr(ctx, "user_id", None) or getattr(ctx, "subject", None)
        user_id = ctx_identity or submission.user_id
        if not user_id:
            from fastapi import HTTPException  # noqa: PLC0415

            raise HTTPException(
                status_code=422,
                detail=(
                    "feedback requires a user_id — either authenticate or pass "
                    "``user_id`` in the request body (dev mode only)."
                ),
            )

        feedback = FeedbackRecord(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            agent=record.agent,
            user_id=user_id,
            score=submission.score,
            dimensions=submission.dimensions,
            comment=submission.comment,
        )

        # Best-effort Langfuse mirror — when the tracer is the Langfuse
        # variant, push the feedback as a trace-level score. Never let
        # a Langfuse failure block the feedback save (the row is the
        # source of truth; Langfuse is the analytics cross-link).
        tracer = getattr(request.app.state, "tracer", None)
        if tracer is not None:
            push = getattr(tracer, "push_run_feedback_score", None)
            if callable(push):
                try:
                    langfuse_score_id = await push(record, feedback)
                    if langfuse_score_id:
                        feedback.langfuse_score_id = langfuse_score_id
                except Exception:
                    # Langfuse client failure: log + proceed. We don't
                    # have a logger reference here at this layer; the
                    # tracer's own diagnostics surface it.
                    pass

        await store.save_feedback(feedback)
        return FeedbackView.from_record(feedback)

    @app.get(
        "/runs/{run_id}/feedback",
        response_model=FeedbackListView,
        tags=["runs", "feedback"],
        dependencies=[_scope("read")],
    )
    async def list_run_feedback(
        run_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        limit: int = 100,
    ) -> FeedbackListView:
        """List feedback for ``run_id``, newest-first. Tenant-scoped:
        404 if the run doesn't belong to the authenticated tenant.
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_run(run_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("run", run_id)
        rows = await store.list_feedback(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            limit=int(limit),
        )
        views = [FeedbackView.from_record(r) for r in rows]
        return FeedbackListView(feedback=views, count=len(views))

    # ------------------------------------------------------------------
    # /api/v1/* — versioned API surface for the Mova iO Angular front
    # end (BACKLOG Group G item 52).
    #
    # Routing convention:
    #   * Pre-v1 endpoints above (/healthz, /ready, /agents, /run,
    #     /jobs/*, /runs/*) stay UNVERSIONED for back-compat — they
    #     shipped before the versioning policy was set, and existing
    #     `mdk submit` callers + the Teams bot depend on the URLs.
    #   * NEW resource-oriented endpoints land here, under /api/v1.
    #   * Breaking changes bump to /api/v2 (new router); additive
    #     changes (new endpoints, new optional fields, new enum values
    #     in non-discriminator positions) DON'T bump.
    #
    # The router is mounted unconditionally — empty for now, populated
    # as Group G items 55-75 land. Mounting the empty router today
    # means new endpoint PRs are pure-additive (no FastAPI wiring
    # churn) and the OpenAPI spec already exposes the /api/v1 prefix
    # for the Angular team's client generator.
    # ------------------------------------------------------------------
    v1 = APIRouter(prefix="/api/v1")

    @v1.get(
        "/agents",
        response_model=AgentCatalogView,
        tags=["agents-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_agents(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        role: str | None = None,
        capabilities: str | None = None,
        tags: str | None = None,
    ) -> AgentCatalogView:
        """List all agents in the catalog with marketplace metadata.

        Supports optional query-param filters:

        * ``?role=support-triage`` — exact match on the agent's ``role``
          field (case-insensitive).
        * ``?capabilities=pii-detection,summarisation`` — comma-separated;
          agent must declare ALL listed capabilities (subset match).
        * ``?tags=acme,production`` — comma-separated; agent must carry
          ALL listed tags (subset match).

        Filters are ANDed. Omitting a filter returns all agents.

        Drives the Mova iO Angular Agent Catalog page — every card
        on the catalog is rendered from entries in this list.

        Errors:

        * **401** — missing / bad bearer token
        """
        _ = ctx.tenant_id  # future per-tenant isolation

        agents: list[AgentBundle] = request.app.state.agents

        # Normalise filter params.
        role_filter = role.lower().strip() if role else None
        cap_filter = (
            {c.strip().lower() for c in capabilities.split(",") if c.strip()}
            if capabilities
            else None
        )
        tag_filter = {t.strip().lower() for t in tags.split(",") if t.strip()} if tags else None

        items: list[AgentCatalogItemView] = []
        for b in agents:
            spec = b.spec
            if role_filter and spec.role.lower() != role_filter:
                continue
            if cap_filter:
                agent_caps = {c.lower() for c in spec.capabilities}
                if not cap_filter.issubset(agent_caps):
                    continue
            if tag_filter:
                agent_tags = {t.lower() for t in spec.tags}
                if not tag_filter.issubset(agent_tags):
                    continue
            items.append(
                AgentCatalogItemView(
                    name=spec.name,
                    version=spec.version,
                    description=spec.description,
                    owner=spec.owner,
                    role=spec.role,
                    persona=spec.persona,
                    capabilities=list(spec.capabilities),
                    tags=list(spec.tags),
                )
            )

        return AgentCatalogView(agents=items, count=len(items))

    @v1.post(
        "/agents",
        response_model=AgentCreatedView,
        status_code=201,
        tags=["agents-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_agent(
        request: Request,
        # Individual-files mode. Each field is optional at the FastAPI
        # level; we enforce "either bundle XOR the 4 required individual
        # files" in the handler body for a clean 422 with a hint.
        agent_yaml: UploadFile | None = File(default=None),
        prompt: UploadFile | None = File(default=None),
        input_schema: UploadFile | None = File(default=None),
        output_schema: UploadFile | None = File(default=None),
        dataset: UploadFile | None = File(default=None),
        # Context files — optional repeating field. Each upload is a
        # contexts/<name>.md that overrides the same-named entry at
        # the project level inside the deployed container.
        contexts: list[UploadFile] = File(default=[]),
        # KB corpus files — optional repeating field. Each upload is a
        # kb/<name>.json that resolve_kb_file() finds via its agent-local
        # tier when the deployed skill runs inside the container.
        kb: list[UploadFile] = File(default=[]),
        # Zipped-bundle mode. Mutually exclusive with the individual
        # fields. The zip may contain a single top-level dir
        # (e.g. ``faq-bot/agent.yaml``) — unzip_bundle strips it.
        bundle: UploadFile | None = File(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentCreatedView:
        """Create a new agent from a multipart-form bundle.

        Two input modes (mutually exclusive — pick ONE):

        1. **Individual files** — set ``agent_yaml`` + ``prompt`` +
           ``input_schema`` + ``output_schema``, optionally ``dataset``.
        2. **Zipped bundle** — set ``bundle`` to a .zip of the canonical
           layout.

        Persists to ``<agents_path>/<name>/`` using the canonical
        directory structure (item 76 / BACKLOG Group G). Validates via
        the same ``load_agent()`` path the CLI uses — bundles that
        fail Pydantic / prompt linter / schema sanity get rejected
        with a 422 before anything lands on disk.

        Returns the canonical layout in the response so the Angular UI
        can render "your agent is at agents/<name>/{...}" without a
        follow-up GET.

        Auth: requires a bearer token (any role). Tenant attribution
        lives on the auth context for future per-tenant agent
        isolation (deferred to v0.8 — today the runtime serves one
        global agents_path).

        Errors:

        * **400** — neither mode supplied OR both modes supplied
        * **409** — agent with this name already exists; use PUT to update
        * **422** — bundle failed validation (parse / linter / schema)
        * **503** — runtime was built without an ``agents_path`` (test
          configuration; production deploys always pass it)
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; POST /api/v1/agents is unavailable",
                status_code=503,
            )

        files = await _collect_bundle_files(
            agent_yaml=agent_yaml,
            prompt=prompt,
            input_schema=input_schema,
            output_schema=output_schema,
            dataset=dataset,
            bundle=bundle,
            contexts=contexts,
            kb=kb,
        )

        # Pull any nested skills/<name>/ entries out of the agent
        # bundle and persist them to the global skill registry FIRST.
        # Customer scaffolds (mdk add rag-qa → skills/web-search/)
        # ship their skill folders inside the project zip; without
        # this split they'd 422 the next time an agent declares
        # `skills: [web-search]` ("empty registry"). Skills persist
        # with PUT semantics so re-deploy is idempotent.
        agent_files, skills_per_name = split_skills_from_bundle(files)
        if skills_per_name:
            skills_path: Path | None = request.app.state.skills_path
            if skills_path is None:
                raise AgentCreationError(
                    "bundle ships skills/<name>/ entries but the runtime "
                    "was built without a skills_path; upload skills "
                    "separately via POST /api/v1/skills or restart with "
                    "--skills-path set",
                    status_code=503,
                )
            for skill_name, skill_files in skills_per_name.items():
                # Skip skills that don't ship a skill.yaml — these are
                # incomplete scaffolds (e.g. only README.md present);
                # silently ignoring keeps deploy idempotent against
                # half-built projects.
                if "skill.yaml" not in skill_files:
                    continue
                persist_skill_bundle(skill_files, skills_path=skills_path)
                _ = skill_name  # used implicitly via persist_skill_bundle

        result = persist_bundle(agent_files, agents_path=agents_path)

        # Refresh the in-memory registry so an immediate GET /agents
        # sees the new bundle. Cheap — agents_path is a flat
        # one-level scan.
        request.app.state.agents = scan_agents(agents_path)

        # Publish into the durable registry (ADR 014 D2 + ADR 021 D2): the
        # FS write above keeps local `mdk serve` working; the registry row
        # makes the agent resolvable by the async worker + other replicas
        # (closes #109) AND content-addressed so a re-create of changed
        # content updates what runs. Tenant-scoped from the auth context.
        store: StorageProvider = request.app.state.storage
        published = await _dual_write_agent_to_registry(
            store,
            result.agent_dir,
            tenant_id=ctx.tenant_id,
            version=result.bundle.spec.version,
            created_by=ctx.api_key_id,
        )

        spec = result.bundle.spec
        return AgentCreatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            agent_dir=result.agent_dir.name,
            files_persisted=result.files_persisted,
            published_version=published.version if published is not None else None,
            changed=published.published if published is not None else True,
        )

    @v1.post(
        "/agents/from-wizard",
        response_model=AgentCreatedView,
        status_code=201,
        tags=["agents-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_agent_from_wizard(
        body: WizardAgentSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentCreatedView:
        """Create a new agent from the Mova iO "Onboard Agent" wizard.

        Accepts the wizard's JSON shape (NOT multipart) and translates
        it into the canonical agent.yaml + prompt.md + default I/O
        schemas layout. Same persist path + response shape as the
        multipart ``POST /api/v1/agents`` — sibling endpoints, two
        wire shapes, one canonical contract on disk.

        Defaults applied:

        * **Schemas** — free-form ``{input: string}`` → ``{output: string}``.
          Agents needing richer I/O shapes use the multipart endpoint.
        * **Version** — ``0.1.0``. Future revisions bump via PUT
          (item 57) or via the GitHub publish flow (item 78).
        * **Marketplace metadata** — only emitted when the wizard
          populates the corresponding field. Empty fields stay unset
          in the YAML rather than serializing as empty strings.

        Field mapping documented in WizardAgentSubmission's docstring.

        Errors:

        * **400** — wizard name can't be slugified to a valid agent
          name (no alphanumeric characters)
        * **409** — agent with this name already exists
        * **422** — bundle failed validation post-translation (e.g.
          ``ai_model`` not in LiteLLM's recognized format)
        * **503** — runtime built without an ``agents_path``
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/agents/from-wizard is unavailable",
                status_code=503,
            )

        # Translate wizard JSON → canonical bundle bytes. Slugification
        # of name happens here; downstream load_agent runs the same
        # Pydantic + linter checks the multipart path uses.
        files = wizard_to_bundle_files(body)

        result = persist_bundle(files, agents_path=agents_path)

        # Refresh the in-memory registry so GET /agents + GET /agents/{name}
        # see the new bundle immediately.
        request.app.state.agents = scan_agents(agents_path)

        # Publish into the durable registry (ADR 014 D2 + ADR 021 D2) —
        # same as the multipart POST so wizard-created agents are
        # worker-resolvable and re-creates of changed content update what
        # runs.
        store: StorageProvider = request.app.state.storage
        published = await _dual_write_agent_to_registry(
            store,
            result.agent_dir,
            tenant_id=ctx.tenant_id,
            version=result.bundle.spec.version,
            created_by=ctx.api_key_id,
        )

        spec = result.bundle.spec
        return AgentCreatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            agent_dir=result.agent_dir.name,
            files_persisted=result.files_persisted,
            published_version=published.version if published is not None else None,
            changed=published.published if published is not None else True,
        )

    # ------------------------------------------------------------------
    # Unified agent-creation surface (ADR 042 — Bundle Composer +
    # additive convenience layer for Mova iO Angular).
    #
    # Single dispatcher endpoint that routes to one of five existing
    # creation paths based on a discriminated-union JSON body OR a
    # multipart upload. Every legacy endpoint
    # (POST /agents, /agents/from-wizard, etc.) continues to work
    # byte-for-byte — this endpoint COMPOSES them, never replaces.
    # ------------------------------------------------------------------
    @v1.post(
        "/projects/{project_id}/agents",
        tags=["agents-v1", "projects-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_agent_unified(
        project_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        # Multipart-form fields — populated by FastAPI ONLY when the
        # Content-Type is multipart/form-data. JSON bodies pass through
        # all-None defaults; we re-read the raw body below for those.
        agent_yaml: UploadFile | None = File(default=None),
        prompt: UploadFile | None = File(default=None),
        input_schema: UploadFile | None = File(default=None),
        output_schema: UploadFile | None = File(default=None),
        dataset: UploadFile | None = File(default=None),
        contexts: list[UploadFile] = File(default=[]),
        kb: list[UploadFile] = File(default=[]),
        bundle: UploadFile | None = File(default=None),
    ) -> Any:
        """Unified create — five sources, one route.

        Routes internally to the right pipeline based on either:

        * **multipart/form-data** → ``source: "bundle"`` path
          (same code the canonical ``POST /api/v1/agents`` runs).
        * **application/json** → parse the body's ``source``
          discriminator and dispatch:

          - ``source: "spec"`` → spec JSON → bundle bytes →
            ``persist_bundle``.
          - ``source: "wizard"`` → ``wizard_to_bundle_files`` →
            ``persist_bundle`` (identical to ``POST /agents/from-wizard``).
          - ``source: "llm"`` → 202 + ``stream_url`` SSE pipeline.
          - ``source: "catalog"`` → catalog lookup → unpack →
            apply overrides → ``persist_bundle``.

        All sync paths attach the freshly-persisted agent to
        ``project_id`` via the storage Protocol's
        ``attach_agent_to_project`` method when present. When the
        projects-storage layer isn't yet deployed, the response
        carries ``attached=false`` and the endpoint still returns
        200 — the agent IS persisted regardless.

        Errors:

        * **400** — both multipart bytes and JSON body supplied
        * **404** — project doesn't exist (when projects-storage rejects)
        * **422** — JSON body fails discriminated-union parse OR
          bundle fails validation
        * **503** — required upstream pipeline not deployed (catalog
          read API; llm-source preserves the SSE pipeline and emits
          ``stage_skipped`` instead)
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/projects/{project_id}/agents is unavailable",
                status_code=503,
            )

        content_type = (request.headers.get("content-type") or "").lower()
        is_multipart = content_type.startswith("multipart/")

        # ---- multipart path → source: "bundle" ----
        if is_multipart:
            files = await _collect_bundle_files(
                agent_yaml=agent_yaml,
                prompt=prompt,
                input_schema=input_schema,
                output_schema=output_schema,
                dataset=dataset,
                bundle=bundle,
                contexts=contexts,
                kb=kb,
            )
            # Reuse the same skills-split logic the canonical endpoint
            # uses — keeps the on-disk shape identical.
            agent_files, skills_per_name = split_skills_from_bundle(files)
            if skills_per_name:
                skills_path: Path | None = request.app.state.skills_path
                if skills_path is None:
                    raise AgentCreationError(
                        "bundle ships skills/ entries but the runtime "
                        "was built without a skills_path",
                        status_code=503,
                    )
                for skill_name, skill_files in skills_per_name.items():
                    if "skill.yaml" not in skill_files:
                        continue
                    persist_skill_bundle(skill_files, skills_path=skills_path)
                    _ = skill_name
            result = persist_bundle(agent_files, agents_path=agents_path)
            request.app.state.agents = scan_agents(agents_path)
            store: StorageProvider = request.app.state.storage
            published = await _dual_write_agent_to_registry(
                store,
                result.agent_dir,
                tenant_id=ctx.tenant_id,
                version=result.bundle.spec.version,
                created_by=ctx.api_key_id,
            )
            attachment = await attach_to_project(
                store,
                project_id=project_id,
                agent_name=result.bundle.spec.name,
                tenant_id=ctx.tenant_id,
            )
            return UnifiedAgentCreatedView(
                source="bundle",
                project_id=project_id,
                agent_name=result.bundle.spec.name,
                version=result.bundle.spec.version,
                description=result.bundle.spec.description,
                agent_dir=result.agent_dir.name,
                files_persisted=result.files_persisted,
                published_version=published.version if published is not None else None,
                changed=published.published if published is not None else True,
                attached=attachment.attached,
            )

        # ---- JSON path → parse discriminator ----
        try:
            raw_body = await request.body()
            if not raw_body:
                raise AgentCreationError(
                    "POST /api/v1/projects/{project_id}/agents requires "
                    "either multipart/form-data or a JSON body",
                    status_code=400,
                )
            body_dict = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise AgentCreationError(
                f"could not parse JSON body: {exc}",
                status_code=422,
            ) from exc

        source = body_dict.get("source")
        if source == "spec":
            return await _unified_create_spec(request, body_dict, project_id, ctx, agents_path)
        if source == "wizard":
            return await _unified_create_wizard(request, body_dict, project_id, ctx, agents_path)
        if source == "llm":
            return await _unified_create_llm(request, body_dict, project_id, ctx, agents_path)
        if source == "catalog":
            return await _unified_create_catalog(request, body_dict, project_id, ctx, agents_path)
        raise AgentCreationError(
            f"unknown ``source``: {source!r}; expected one of "
            "'bundle' (multipart), 'spec', 'wizard', 'llm', 'catalog'",
            status_code=422,
        )

    @v1.get(
        "/projects/{project_id}/agents/create-stream/{job_id}",
        tags=["agents-v1", "projects-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_agent_llm_stream(
        project_id: str,
        job_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> StreamingResponse:
        """SSE stream for the ``source: "llm"`` async authoring pipeline.

        The 202 response from the POST endpoint carries the
        ``stream_url`` pointing here; the caller subscribes
        immediately to receive ``stage_*`` events as each pipeline
        stage runs. When the request's job_id isn't in the pending
        queue, returns 404 — the caller raced past the cache or the
        runtime restarted.
        """
        pending: dict[str, dict[str, Any]] = (
            getattr(request.app.state, "_pending_llm_create", None) or {}
        )
        entry = pending.pop(job_id, None)
        if entry is None:
            raise not_found("llm-create job", job_id)

        # Enforce tenant + project scope: the cached entry must match the
        # path params and auth context.
        if entry.get("tenant_id") != ctx.tenant_id:
            raise not_found("llm-create job", job_id)
        if entry.get("project_id") != project_id:
            raise not_found("llm-create job", job_id)

        req: AgentCreateLlmRequest = entry["req"]
        agents_path_str: str = entry["agents_path"]
        store: StorageProvider = request.app.state.storage

        async def _stream() -> Any:
            async for frame in llm_authoring_stream(
                req=req,
                project_id=project_id,
                job_id=job_id,
                storage=store,
                agents_path=Path(agents_path_str) if agents_path_str else None,
                tenant_id=ctx.tenant_id,
            ):
                yield frame

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @v1.post(
        "/skills",
        response_model=SkillCreatedView,
        status_code=201,
        tags=["skills-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_skill(
        request: Request,
        skill_yaml: UploadFile = File(...),
        impl: UploadFile | None = File(default=None),
        corpus: UploadFile | None = File(default=None),
        readme: UploadFile | None = File(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> SkillCreatedView:
        """Create or replace a skill bundle under ``<skills_path>/<name>/``.

        Fixes the long-standing gap where agents declaring
        ``skills: [<name>]`` 422'd on upload with "skills resolution
        failed: ... Available: (empty registry)". The runtime now owns
        a real skill registry that customers can populate via this
        endpoint OR implicitly via the deploy command (PR 3 in the
        same stack).

        Multipart fields:

        * ``skill_yaml`` (required) — the spec. ``name`` field inside
          determines the on-disk directory.
        * ``impl`` (optional) — Python implementation file.
        * ``corpus`` (optional) — JSON corpus shipped alongside.
        * ``readme`` (optional) — human-facing notes.

        PUT semantics: re-uploading the same skill name overwrites
        atomically. Skills are referenced by name from agents, so an
        operator who tweaked their skill and re-deploys expects the
        runtime to follow — different conflict policy from agents
        (which 409 on conflict because agent identity is sticky).

        Errors:

        * **401** — missing / bad bearer token
        * **422** — bundle failed validation (parse / schema / shape)
        * **503** — runtime was built without a ``skills_path``
        """
        skills_path: Path | None = request.app.state.skills_path
        if skills_path is None:
            raise SkillCreationError(
                "runtime was built without a skills_path; POST /api/v1/skills is unavailable",
                status_code=503,
            )

        files: dict[str, bytes] = {"skill.yaml": await skill_yaml.read()}
        if impl is not None:
            files["impl.py"] = await impl.read()
        if corpus is not None:
            files["corpus.json"] = await corpus.read()
        if readme is not None:
            files["README.md"] = await readme.read()

        result = persist_skill_bundle(files, skills_path=skills_path)

        _ = ctx.tenant_id  # future per-tenant audit log entry

        spec = result.bundle.spec
        return SkillCreatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description or "",
            skill_dir=result.skill_dir.name,
            files_persisted=result.files_persisted,
        )

    @v1.get(
        "/agents/{name}",
        response_model=AgentDetailView,
        tags=["agents-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_agent(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        version: str | None = None,
    ) -> AgentDetailView:
        """Return the full agent spec + bundle metadata for a single agent.

        Drives the Mova iO Angular agent-profile view: the user clicks
        an agent in the catalog and the UI fetches this single endpoint
        to render the spec, prompt body, schemas, dataset stats, model
        config, marketplace metadata (role/persona/capabilities), and
        the list of canonical files on disk.

        **Versioning (ADR 021 D3).** ``?version=<v>`` returns that *exact*
        published registry version (404 if no such version exists for this
        agent in the caller's tenant) — it does NOT fall back to latest.
        The materialized bundle is loaded from the durable registry so the
        view reflects the published content of that version, not the API
        pod's filesystem mirror. Omitting ``?version`` returns the current
        agent from the in-memory registry (the FS mirror refreshed after
        every successful ``POST/PUT /api/v1/agents``) — byte-for-byte the
        pre-ADR-021 behavior.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent (or the requested ``?version``) not found, or a
          different tenant's agent — today's runtime is global-scoped, so
          404 just means "not found"
        """
        # Versioned lookup → resolve the exact version from the durable
        # registry (ADR 021 D3). A registry miss for the named version is
        # a 404 — we deliberately do NOT silently fall back to latest, so
        # ``?version=X`` means "X or nothing."
        if version is not None:
            store: StorageProvider = request.app.state.storage
            bundle = await resolve_agent_bundle(
                store, name, tenant_id=ctx.tenant_id, version=version
            )
            if bundle is None:
                raise not_found("agent", f"{name}@{version}")
            return _render_agent_detail(bundle)

        # Versionless lookup — today's runtime is single-tenant per
        # agents_path; future per-tenant filesystem isolation reads
        # ctx.tenant_id and walks <agents_path>/<tenant_id>/. The
        # reference here keeps the audit trail honest and prevents
        # ruff from flagging the param as unused.
        _ = ctx.tenant_id

        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == name), None)
        if bundle is None:
            raise not_found("agent", name)
        return _render_agent_detail(bundle)

    @v1.post(
        "/agents/{name}/validate",
        response_model=AgentValidationView,
        tags=["agents-v1"],
        # Read-only inspection (prompt lint + cost forecast; no mutation),
        # so it gates on ``read`` despite being a POST.
        dependencies=[_scope("read")],
    )
    async def v1_validate_agent(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentValidationView:
        """Run the prompt linter + cost forecast for an agent.

        Drives the Mova iO Angular "is this agent shippable?" gate
        BEFORE the user clicks Publish or Run Eval. Returns:

        * ``passed: bool`` — green-checkmark shortcut (zero errors)
        * ``errors[]`` — block save (red chips)
        * ``warnings[]`` — informational (yellow chips, don't block)
        * ``cost_forecast`` — pricing-table estimate for the eval
          dataset; lets the UI render "running this eval will cost
          ~$0.45" alongside the Run Eval button

        Note: the structural validation (Pydantic parse + I/O schema
        sanity) already ran at POST /agents time — agents that don't
        pass that never make it into the registry. This endpoint is
        the SOFT validation layer: prompt-template hygiene + cost.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        """
        _ = ctx.tenant_id  # future per-tenant isolation
        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == name), None)
        if bundle is None:
            raise not_found("agent", name)
        return _render_agent_validation(bundle)

    @v1.delete(
        "/agents/{name}",
        response_model=AgentDeletedView,
        tags=["agents-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_delete_agent(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentDeletedView:
        """Soft-delete an agent (item 117 / Tier I-U).

        Moves the canonical bundle to a sibling
        ``.deleted-<name>-<timestamp>/`` directory under the runtime's
        agents_path. Recoverable out-of-band by the operator until a
        future cron sweep removes it (7-day retention window planned).

        Refreshes the in-memory agents registry so the very next
        ``GET /agents`` no longer surfaces the deleted agent.

        Tenant attribution is logged via ``ctx.tenant_id`` (future
        per-tenant filesystem isolation reads this back); today's
        runtime is single-tenant per agents_path.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent dir doesn't exist at the runtime's
          agents_path
        * **500** — filesystem error (permissions, mount issues)
        * **503** — runtime built without an ``agents_path``
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "DELETE /api/v1/agents/{name} is unavailable",
                status_code=503,
            )

        _ = ctx.tenant_id  # future per-tenant audit log entry

        result = soft_delete_agent(name, agents_path=agents_path)
        # Refresh registry so GET /agents reflects reality on the
        # next request — agent disappears immediately from the catalog.
        request.app.state.agents = scan_agents(agents_path)

        return AgentDeletedView(
            name=result.name,
            deleted_dir=result.deleted_dir.name,
        )

    @v1.put(
        "/agents/{name}",
        response_model=AgentUpdatedView,
        tags=["agents-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_update_agent(
        name: str,
        request: Request,
        agent_yaml: UploadFile | None = File(default=None),
        prompt: UploadFile | None = File(default=None),
        input_schema: UploadFile | None = File(default=None),
        output_schema: UploadFile | None = File(default=None),
        dataset: UploadFile | None = File(default=None),
        contexts: list[UploadFile] = File(default=[]),
        kb: list[UploadFile] = File(default=[]),
        bundle: UploadFile | None = File(default=None),
        if_match: str | None = Header(default=None, alias="If-Match"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentUpdatedView:
        """Replace an existing agent bundle in-place (item 57 / BACKLOG G).

        Accepts the same multipart form as ``POST /api/v1/agents`` (either
        individual files or a zipped bundle). The ``{name}`` path param
        must match the ``name`` field in the uploaded ``agent.yaml``;
        mismatches are rejected with 422.

        Differences from POST:

        * **404** if the agent does not already exist (use POST to create).
        * Existing bundle is atomically replaced — never leaves partial
          state on disk.
        * ``previous_version`` in the response lets the caller detect the
          diff without a round-trip.

        **Optimistic concurrency (ADR 014 D3) — opt-in via ``If-Match``.**
        Send ``If-Match: <version>`` (or the bundle's ``content_hash``) with
        the version you believe is current; if the registry's latest version
        for this tenant no longer matches, the write is rejected with
        **409 Conflict** ("someone else updated this; re-fetch") so two
        teammates can't silently clobber each other. The header is parsed
        leniently (surrounding quotes + a leading weak-validator ``W/`` are
        stripped, RFC 7232) and matched against either the current
        ``version`` or its ``content_hash``. **Omitting ``If-Match``
        preserves today's last-write-wins behavior** — concurrency safety is
        purely opt-in, so existing clients are unaffected.

        Skills bundled inside the upload are persisted to the global
        registry with PUT semantics (idempotent re-deploy).

        Errors:

        * **400** — neither mode supplied OR both modes supplied
        * **404** — agent ``{name}`` is not registered (never created)
        * **409** — ``If-Match`` precondition is stale (concurrent publish)
        * **422** — bundle failed validation OR agent_yaml name ≠ path param
        * **503** — runtime built without an ``agents_path``
        """
        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "PUT /api/v1/agents/{name} is unavailable",
                status_code=503,
            )

        # 404 guard — the agent must already exist before we'll replace it.
        agents: list[AgentBundle] = request.app.state.agents
        existing = next((b for b in agents if b.spec.name == name), None)
        if existing is None:
            raise not_found("agent", name)
        previous_version = existing.spec.version

        # Optimistic concurrency (ADR 014 D3): only when the caller opts in
        # by sending ``If-Match``. Compare against the DURABLE registry's
        # current latest (the multi-pod source of truth), not the local FS
        # mirror — a stale precondition means another publisher won the race.
        # Absent ``If-Match`` → fall through to last-write-wins (back-compat).
        store: StorageProvider = request.app.state.storage
        if if_match is not None:
            await _check_agent_if_match(store, name, tenant_id=ctx.tenant_id, if_match=if_match)

        files = await _collect_bundle_files(
            agent_yaml=agent_yaml,
            prompt=prompt,
            input_schema=input_schema,
            output_schema=output_schema,
            dataset=dataset,
            bundle=bundle,
            contexts=contexts,
            kb=kb,
        )

        # Extract + persist bundled skills first (same as POST).
        agent_files, skills_per_name = split_skills_from_bundle(files)
        if skills_per_name:
            skills_path: Path | None = request.app.state.skills_path
            if skills_path is None:
                raise AgentCreationError(
                    "bundle ships skills/<name>/ entries but the runtime "
                    "was built without a skills_path",
                    status_code=503,
                )
            for skill_name, skill_files in skills_per_name.items():
                if "skill.yaml" not in skill_files:
                    continue
                persist_skill_bundle(skill_files, skills_path=skills_path)
                _ = skill_name

        result = persist_bundle(agent_files, agents_path=agents_path, on_conflict="replace")
        request.app.state.agents = scan_agents(agents_path)

        # Publish the updated bundle into the durable registry (ADR 014 D2
        # + ADR 021 D2). Content-addressed: a re-deploy whose bundle bytes
        # CHANGED writes a NEW immutable (name, version) row that the
        # worker + every replica resolve as the new latest — so the served
        # agent actually updates (the headline ADR 021 fix). An UNCHANGED
        # re-deploy is a no-op (no duplicate history row). When the
        # declared version collides with a different-content history entry,
        # a derived <version>+<hash8> registry version keeps the immutable
        # PK intact. Best-effort — never fails the update. ``store`` was
        # bound above for the If-Match check.
        published = await _dual_write_agent_to_registry(
            store,
            result.agent_dir,
            tenant_id=ctx.tenant_id,
            version=result.bundle.spec.version,
            created_by=ctx.api_key_id,
        )

        spec = result.bundle.spec
        return AgentUpdatedView(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            agent_dir=result.agent_dir.name,
            files_persisted=result.files_persisted,
            previous_version=previous_version,
            published_version=published.version if published is not None else None,
            changed=published.published if published is not None else True,
        )

    @v1.get(
        "/agents/{name}/versions",
        response_model=AgentVersionsView,
        tags=["agents-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_agent_versions(
        name: str,
        request: Request,
        limit: int = 50,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentVersionsView:
        """List the durable-registry version history for one agent (ADR 014 D3).

        Returns every published version of ``name`` for the caller's
        tenant, **newest-first**, with the audit fields a team needs:
        ``version``, ``created_by`` (who published it — ADR 013),
        ``created_at`` (when), and ``content_hash``. The newest row is
        flagged ``is_current`` — it's the version a versionless run/resolve
        serves and the value to send back as ``If-Match`` on a
        concurrency-safe PUT.

        Source of truth is the durable registry (``list_agent_versions``),
        not the local filesystem mirror — so the history is the same on
        every pod and survives recycles. Tenant-scoped: another tenant's
        agent (or an unknown name) returns an empty history rather than
        leaking existence (same no-leak contract as a 404).

        Drives ``mdk agent history`` + the Angular console's version panel.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — authenticated but key lacks the ``read`` scope
        """
        store: StorageProvider = request.app.state.storage
        records = await store.list_agent_versions(name, tenant_id=ctx.tenant_id, limit=limit)
        items = [
            AgentVersionView(
                version=r.version,
                created_by=r.created_by,
                created_at=r.created_at,
                content_hash=r.content_hash,
                # list_agent_versions is newest-first, so index 0 is the
                # current latest. Mark exactly that row.
                is_current=(i == 0),
            )
            for i, r in enumerate(records)
        ]
        return AgentVersionsView(name=name, versions=items, count=len(items))

    @v1.post(
        "/agents/{name}/revert",
        response_model=AgentRevertedView,
        tags=["agents-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_revert_agent(
        name: str,
        request: Request,
        body: AgentRevertSubmission | None = None,
        to_version: str | None = None,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentRevertedView:
        """Revert an agent to a prior version (ADR 014 D3 / BACKLOG #80).

        Fetches the bundle for ``to_version`` and **re-publishes it forward
        as a NEW latest version** — a fresh ``save_agent_bundle`` row with a
        new ``created_at`` / ``created_by`` and the same ``files``. This is
        **non-destructive**: no version is ever deleted or rewritten, so the
        full history (including the version you reverted away from) stays
        intact and you can revert again — even back to the version you just
        left.

        ``to_version`` may be supplied in the JSON body
        (``{"to_version": "0.2.0"}``) OR as a ``?to_version=`` query param
        for curl ergonomics; the body wins when both are present.

        Operates on the durable registry so the revert is visible to every
        pod (API + async worker) immediately. Tenant-scoped: you can only
        revert your own tenant's agents, and a cross-tenant ``to_version``
        is indistinguishable from a missing one (404).

        Drives ``mdk agent revert``.

        Errors:

        * **400** — no ``to_version`` supplied (neither body nor query)
        * **401** — missing / bad bearer token
        * **403** — authenticated but key lacks the ``admin`` scope
        * **404** — no such ``to_version`` for this agent in this tenant
        """
        target_version = body.to_version if body is not None else to_version
        if not target_version:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=400,
                message=(
                    'revert requires a target version: send {"to_version": "..."} '
                    "in the body or ?to_version=..."
                ),
            )

        store: StorageProvider = request.app.state.storage

        # The version we're rolling back to — must exist for this tenant.
        target = await store.get_agent_bundle(name, tenant_id=ctx.tenant_id, version=target_version)
        if target is None:
            raise not_found("agent version", f"{name}@{target_version}")

        # The full history (newest-first) — needed both for the response's
        # ``previous_version`` ("undo the undo") and to mint a NEW version
        # string that won't collide with the (tenant, name, version) primary
        # key. The registry resolves "latest" by created_at, so the new row's
        # exact version string only needs to be unique + human-traceable.
        history = await store.list_agent_versions(name, tenant_id=ctx.tenant_id, limit=1000)
        previous_version = history[0].version if history else target_version
        existing_versions = {r.version for r in history}
        new_version = _mint_revert_version(target_version, existing_versions)

        # Re-publish the target's bundle FORWARD as a new immutable row
        # (a fresh created_at via the model default + the reverting identity
        # as created_by). Same ``files`` + ``content_hash`` so the served
        # bundle is byte-identical to what ``to_version`` published; only the
        # registry-row ``version`` differs so it appends to history rather
        # than rewriting the immutable ``to_version`` row. History is only
        # ever appended to — the revert NEVER deletes or mutates a prior row.
        reverted = AgentBundleRecord(
            name=target.name,
            tenant_id=target.tenant_id,
            version=new_version,
            created_by=ctx.api_key_id,
            content_hash=target.content_hash,
            files=target.files,
        )
        await store.save_agent_bundle(reverted)

        # ADR 035 D1 — emit ``agent.reverted`` (the revert is a forward-
        # append publish at the registry, ADR 014 D3, so an ``agent.
        # published`` is conceptually possible too; we emit the dedicated
        # ``reverted`` kind only — front ends/webhooks subscribing to
        # publishes care about new content, not re-publishes of prior
        # versions). Fire-and-forget.
        emit_event(
            store,
            tenant_id=ctx.tenant_id,
            kind=EventKind.AGENT_REVERTED,
            subject=name,
            data={
                "version": new_version,
                "reverted_from": target_version,
                "previous_version": previous_version,
                "created_by": ctx.api_key_id,
            },
        )

        return AgentRevertedView(
            name=name,
            version=new_version,
            reverted_from=target_version,
            previous_version=previous_version,
        )

    @v1.post(
        "/agents/{name}/dataset",
        response_model=AgentDatasetUploadView,
        status_code=200,
        tags=["agents-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_upload_agent_dataset(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        file: UploadFile = File(...),
    ) -> AgentDatasetUploadView:
        """Upload or replace an agent's eval dataset (item 111 / Tier I-F).

        Accepts a ``multipart/form-data`` upload with a single field
        ``file`` containing a JSONL file — one JSON object per line.
        Writes the content to ``<agents_path>/<name>/evals/dataset.jsonl``,
        creating the ``evals/`` sub-directory if needed. Replaces any
        existing dataset atomically.

        Returns row count, a SHA-256 prefix for integrity checking, and
        a preview of the first up to three rows so the caller can confirm
        the upload was parsed correctly.

        Wizard-created agents have no dataset and can't be eval'd until
        this endpoint is called at least once.

        Errors:

        * **400** — file is not valid JSONL (non-object line detected)
        * **401** — missing / bad bearer token
        * **404** — agent not found in the runtime's agents_path
        * **503** — runtime built without an agents_path
        """
        import hashlib  # noqa: PLC0415
        import json  # noqa: PLC0415

        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/agents/{name}/dataset is unavailable",
                status_code=503,
            )

        _ = ctx.tenant_id

        agent_dir = agents_path / name
        if not agent_dir.is_dir():
            raise not_found("agent", name)

        raw = await file.read()

        # Validate: every non-empty line must be a JSON object.
        rows: list[dict[str, object]] = []
        for lineno, raw_line in enumerate(raw.decode().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentCreationError(
                    f"dataset line {lineno} is not valid JSON: {exc}",
                    status_code=400,
                ) from exc
            if not isinstance(obj, dict):
                raise AgentCreationError(
                    f"dataset line {lineno} must be a JSON object, got {type(obj).__name__}",
                    status_code=400,
                )
            rows.append(obj)

        evals_dir = agent_dir / "evals"
        evals_dir.mkdir(exist_ok=True)
        dataset_path = evals_dir / "dataset.jsonl"
        dataset_path.write_bytes(raw)

        sha256_prefix = hashlib.sha256(raw).hexdigest()[:12]
        preview = rows[:3]

        # Refresh registry so GET /agents/{name} reflects updated dataset stats.
        request.app.state.agents = scan_agents(agents_path)

        return AgentDatasetUploadView(
            agent_name=name,
            row_count=len(rows),
            sha256_prefix=sha256_prefix,
            preview=preview,
        )

    @v1.post(
        "/agents/{name}/dataset/harvest",
        response_model=HarvestView,
        status_code=200,
        tags=["agents-v1", "eval"],
        dependencies=[_scope("eval")],
    )
    async def v1_harvest_agent_dataset(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        source: str = "thumbs-down",
        limit: int = 20,
        since: str | None = None,
    ) -> HarvestView:
        """Harvest prod runs into *proposed* eval cases (ADR 016 D1).

        Selects this tenant's runs for ``name`` by feedback/sample signal and
        returns them transformed into **proposed** eval-dataset cases. This is
        a **read-only proposal**: it NEVER modifies the stored
        ``evals/dataset.jsonl``. Acceptance is a deliberate follow-up call to
        ``POST /api/v1/agents/{name}/dataset`` with the reviewed subset — the
        human-review gate is the core anti-poisoning safety property (D5).

        Scope: ``eval`` — harvesting reads run/feedback data (preview); the
        *accept* step (writing the dataset) keeps its ``admin`` scope.

        Tenant-scoped: only the authenticated tenant's runs/feedback are
        considered; another tenant's runs are never harvested.

        Query params:

        * ``source`` — ``thumbs-down`` (default) | ``thumbs-up`` |
          ``low-score`` | ``sample``.
        * ``limit`` — max proposed cases to return (default 20).
        * ``since`` — ISO-8601 timestamp; only runs/feedback at or after this
          instant are considered. Omit for no cutoff.

        Errors:

        * **400** — unknown ``source`` or unparseable ``since``.
        * **401** — missing / bad bearer token.
        * **403** — caller lacks the ``eval`` scope.
        * **404** — agent not found in the runtime's agents_path.
        * **503** — runtime built without an agents_path.
        """
        from datetime import datetime  # noqa: PLC0415

        from movate.core.harvest import harvest_runs, resolve_source  # noqa: PLC0415

        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/agents/{name}/dataset/harvest is unavailable",
                status_code=503,
            )
        if not (agents_path / name).is_dir():
            raise not_found("agent", name)

        try:
            harvest_source = resolve_source(source)
        except ValueError as exc:
            raise AgentCreationError(str(exc), status_code=400) from exc

        since_dt: datetime | None = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError as exc:
                raise AgentCreationError(
                    f"invalid 'since' timestamp {since!r}; expected ISO-8601 "
                    f"(e.g. 2026-05-01T00:00:00Z)",
                    status_code=400,
                ) from exc

        store: StorageProvider = request.app.state.storage
        result = await harvest_runs(
            store,
            agent=name,
            tenant_id=ctx.tenant_id,
            source=harvest_source,
            limit=int(limit),
            since=since_dt,
        )

        return HarvestView(
            agent_name=name,
            source=result.source.value,
            proposed_count=result.proposed_count,
            needs_review_count=result.needs_review_count,
            runs_considered=result.runs_considered,
            applied=False,
            cases=[
                HarvestedCaseView(
                    input=c.input,
                    expected=c.expected,
                    needs_review=c.needs_review,
                    provenance=c.provenance,
                )
                for c in result.cases
            ],
        )

    @v1.post(
        "/agents/{name}/kb",
        response_model=KbIngestView,
        status_code=200,
        tags=["agents-v1", "kb"],
        dependencies=[_scope("kb:write")],
    )
    async def v1_upload_agent_kb(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        files: list[UploadFile] = File(default=[]),
    ) -> KbIngestView:
        """Ingest one or more KB documents into an agent's knowledge
        base (Tier 10 RAG enhancement, PR-D).

        Accepts a ``multipart/form-data`` upload with a repeating
        ``files`` field. Each file is split into paragraph chunks,
        embedded via the configured embedding model, and persisted
        via the storage layer's :func:`save_kb_chunk` (deduped on the
        ``(agent, tenant_id, content_hash)`` constraint — re-uploading
        the same document is a no-op).

        Supported extensions: ``.md``, ``.markdown``, ``.txt``,
        ``.pdf`` (text-based; scanned-image PDFs need OCR, deferred
        to a future extras flag), ``.docx`` (Word documents; legacy
        binary .doc not supported — convert to .docx first),
        ``.html`` / ``.htm`` (extracted main-article content via
        Readability — strips nav / sidebar / ads). Files with
        unsupported extensions OR parser failures (corrupt PDF,
        non-UTF-8 text, encrypted PDF, malformed DOCX, empty HTML)
        get ``status="skipped"`` in the per-file result but the
        overall upload still returns 200 — the operator sees the
        mix instead of getting a 400 that blocks the whole batch.

        Wraps the same ingest path as ``mdk kb ingest`` (see
        :func:`movate.kb.ingest.ingest_text`); this endpoint exists so
        the Chainlit playground (and the future Angular Agent Console)
        can offer a drag-drop upload without requiring an SSH
        connection to a project directory.

        Errors:

        * **400** — empty multipart form (no ``files`` field)
        * **401** — missing / bad bearer token
        * **404** — agent not found
        * **502** — embedding API unreachable
        """
        from movate.kb.embed import embedding_model  # noqa: PLC0415
        from movate.kb.ingest import ingest_text  # noqa: PLC0415

        if not files:
            from fastapi import HTTPException  # noqa: PLC0415

            raise HTTPException(
                status_code=400,
                detail=(
                    "no files in the multipart form; supply one or more "
                    "``files`` fields (.md / .markdown / .txt)."
                ),
            )

        # 404 on unknown agent — same surface as other agent endpoints.
        agents: list[AgentBundle] = request.app.state.agents
        agent_names = {b.spec.name for b in agents}
        if name not in agent_names:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage

        # Dispatch table for per-extension parsers lives in
        # ``movate.kb.parsers`` — extends to PDF (PR-G) and future
        # DOCX / HTML without touching the endpoint code.
        from movate.kb.parsers import (  # noqa: PLC0415 — lazy: KB upload path only
            is_supported_extension,
            parse_document,
        )

        per_file: list[KbIngestFileResult] = []
        total_saved = 0
        for upload in files:
            raw_name = (upload.filename or "").lstrip("/")
            basename = Path(raw_name).name
            if not basename:
                # Unnamed multipart part — skip silently with a
                # placeholder source so the operator sees something.
                per_file.append(
                    KbIngestFileResult(
                        source="<unnamed>",
                        status="skipped",
                    )
                )
                continue
            if not is_supported_extension(basename):
                per_file.append(
                    KbIngestFileResult(
                        source=basename,
                        status="skipped",
                    )
                )
                continue
            raw = await upload.read()
            parse_result = parse_document(basename, raw)
            if parse_result is None:
                # Parser returned None — corrupt PDF, non-UTF8 .txt,
                # encrypted PDF, scanned-image PDF, etc. Skip the
                # file rather than 400'ing the whole batch.
                per_file.append(
                    KbIngestFileResult(
                        source=basename,
                        status="skipped",
                    )
                )
                continue
            summary = await ingest_text(
                storage=store,
                text=parse_result.text,
                source=basename,
                agent=name,
                tenant_id=ctx.tenant_id,
                embedding_model=embedding_model(),
                ocr=parse_result.ocr_used,
            )
            if summary is None:
                per_file.append(
                    KbIngestFileResult(
                        source=basename,
                        status="empty",
                    )
                )
                continue
            total_saved += summary.chunks_saved
            per_file.append(
                KbIngestFileResult(
                    source=basename,
                    status="ingested",
                    chunks_total=summary.chunks_total,
                    chunks_saved=summary.chunks_saved,
                    embedding_model=summary.embedding_model,
                )
            )

        return KbIngestView(
            agent_name=name,
            files=per_file,
            total_chunks_saved=total_saved,
        )

    @v1.get(
        "/agents/{name}/kb",
        response_model=KbListView,
        tags=["agents-v1", "kb"],
        dependencies=[_scope("read")],
    )
    async def v1_list_agent_kb(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        source: str | None = None,
        limit: int = 1000,
    ) -> KbListView:
        """List the chunks in an agent's knowledge base (Task 4).

        The remote twin of ``mdk kb list`` — lets an operator inspect a
        DEPLOYED agent's KB ("is my content actually in there?") without
        SSH-ing to the host or running SQL by hand. Tenant-scoped at the
        storage layer (``list_kb_chunks(..., tenant_id=...)``), so a
        caller only ever sees their own tenant's chunks.

        Query params:

        * ``?source=`` — filter to chunks from one source URI (file path
          / URL recorded at ingest time).
        * ``?limit=`` — cap the rows returned. Hard-capped at 10000 to
          keep the response bounded.

        The ``embedding`` vector is omitted from each chunk — list
        payloads are for inspection, not retrieval, and 1536 floats per
        chunk would bloat the response for no consumer benefit.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        """
        agents: list[AgentBundle] = request.app.state.agents
        if name not in {b.spec.name for b in agents}:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage
        # Hard cap mirrors the bounded-response convention on the other
        # list endpoints (jobs caps at 100; KB lists can legitimately be
        # larger, so 10k — same order as the CLI's local default ceiling).
        capped_limit = max(1, min(int(limit), 10_000))
        chunks = await store.list_kb_chunks(
            agent=name,
            tenant_id=ctx.tenant_id,
            source=source,
            limit=capped_limit,
        )
        views = [
            KbChunkView(
                chunk_id=c.chunk_id,
                source=c.source,
                text=c.text,
                embedding_model=c.embedding_model,
                content_hash=c.content_hash,
                ocr=c.ocr,
                metadata=c.metadata,
                created_at=c.created_at.isoformat(),
            )
            for c in chunks
        ]
        return KbListView(agent_name=name, chunks=views, count=len(views))

    @v1.get(
        "/agents/{name}/kb/stats",
        response_model=KbStatsView,
        tags=["agents-v1", "kb"],
        dependencies=[_scope("read")],
    )
    async def v1_agent_kb_stats(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> KbStatsView:
        """Aggregate stats for an agent's KB (Task 4).

        The remote twin of ``mdk kb stats``. Aggregation happens
        SERVER-SIDE — the runtime walks its own chunks and ships only the
        rolled-up counts, never the corpus. Returns total chunk count,
        total char count, OCR-derived chunk count, a per-source
        breakdown (chunk + char counts), and every distinct
        ``embedding_model`` present (more than one = a mixed-model KB
        that needs a re-embed before search is reliable).

        Tenant-scoped via ``list_kb_chunks(..., tenant_id=...)``.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        """
        agents: list[AgentBundle] = request.app.state.agents
        if name not in {b.spec.name for b in agents}:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage
        # Pull all chunks for accurate aggregation. The high limit matches
        # the local ``mdk kb stats`` path (which uses 100k); a KB larger
        # than that is a re-architecture problem, not a pagination one.
        chunks = await store.list_kb_chunks(
            agent=name,
            tenant_id=ctx.tenant_id,
            limit=100_000,
        )

        per_source: dict[str, list[int]] = {}
        models: set[str] = set()
        total_chars = 0
        ocr_chunks = 0
        for c in chunks:
            per_source.setdefault(c.source, []).append(len(c.text))
            models.add(c.embedding_model)
            total_chars += len(c.text)
            if c.ocr:
                ocr_chunks += 1

        # Sort per-source rows by chunk count DESC (the distribution view
        # operators care about — "which doc dominates retrieval?"), ties
        # broken alphabetically for stable output.
        sources = [
            KbStatsSourceView(source=src, chunks=len(sizes), chars=sum(sizes))
            for src, sizes in sorted(per_source.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ]
        return KbStatsView(
            agent_name=name,
            total_chunks=len(chunks),
            total_chars=total_chars,
            ocr_chunks=ocr_chunks,
            sources=sources,
            models=sorted(models),
        )

    @v1.delete(
        "/agents/{name}/kb",
        response_model=KbDeletedView,
        tags=["agents-v1", "kb"],
        dependencies=[_scope("kb:write")],
    )
    async def v1_delete_agent_kb(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        source: str | None = None,
    ) -> KbDeletedView:
        """Delete chunks from an agent's KB (Task 4).

        The remote twin of ``mdk kb clear``. With ``?source=`` set, only
        chunks from that source URI are removed (the re-ingest-with-
        --replace workflow); omit it for a full-KB wipe. Returns the
        count deleted.

        Tenant-scoped via ``delete_kb_chunks(..., tenant_id=...)`` — a
        caller can never wipe another tenant's KB by guessing the agent
        name.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        """
        agents: list[AgentBundle] = request.app.state.agents
        if name not in {b.spec.name for b in agents}:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage
        deleted = await store.delete_kb_chunks(
            agent=name,
            tenant_id=ctx.tenant_id,
            source=source,
        )
        return KbDeletedView(agent_name=name, deleted=deleted, source=source)

    @v1.post(
        "/agents/{name}/kb/search",
        response_model=KbSearchView,
        tags=["agents-v1", "kb"],
        # Read-only retrieval over the corpus (no mutation), so it gates
        # on ``read`` despite being a POST.
        dependencies=[_scope("read")],
    )
    async def v1_search_agent_kb(
        name: str,
        body: KbSearchSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> KbSearchView:
        """Semantic search over an agent's KB (Task 4).

        The remote twin of ``mdk kb search``. The runtime embeds the
        question SERVER-SIDE with the deployment's configured embedding
        model (so the query vector lands in the same space as the stored
        chunks — different models produce incomparable vectors) and runs
        the same :func:`movate.kb.search.search` pipeline the local CLI
        uses. ``hybrid=true`` adds a parallel BM25 lexical pass + RRF
        fusion. The embedding vector is omitted from each result for the
        usual payload-size reason.

        Tenant-scoped — the search runs against ``ctx.tenant_id``'s
        chunks only.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        * **502** — embedding API unreachable
        """
        from movate.kb.embed import embedding_model  # noqa: PLC0415
        from movate.kb.search import search as kb_search  # noqa: PLC0415

        agents: list[AgentBundle] = request.app.state.agents
        if name not in {b.spec.name for b in agents}:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage
        results = await kb_search(
            storage=store,
            question=body.question,
            agent=name,
            tenant_id=ctx.tenant_id,
            limit=body.k,
            embedding_model=embedding_model(),
            hybrid=body.hybrid,
        )
        views = [
            KbSearchResultView(
                chunk_id=r.chunk.chunk_id,
                source=r.chunk.source,
                text=r.chunk.text,
                embedding_model=r.chunk.embedding_model,
                score=r.score,
                ocr=r.chunk.ocr,
                metadata=r.chunk.metadata,
            )
            for r in results
        ]
        return KbSearchView(
            agent_name=name,
            question=body.question,
            results=views,
            count=len(views),
        )

    @v1.post(
        "/agents/{name}/kb/reindex",
        response_model=KbReindexView,
        tags=["agents-v1", "kb"],
        dependencies=[_scope("kb:write")],
    )
    async def v1_reindex_agent_kb(
        name: str,
        body: KbReindexSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> KbReindexView:
        """Rebuild an agent's KB vector index (Task 5).

        The remote twin of ``mdk kb reindex``. With ``reembed=false``
        (the default) the runtime rebuilds the vector index from the
        chunks already in storage — no embedding calls, for recovering a
        degraded index or applying new index parameters. With
        ``reembed=true`` it first re-runs the deployment's configured
        embedding model over every stored chunk's text (overwriting each
        vector via :func:`save_kb_chunk`'s upsert) and THEN rebuilds the
        index — the expensive path, required when the embedding
        model / dimension changes.

        Re-embedding is orchestrated HERE in the runtime layer (which may
        import the embedder), not in storage — same boundary the local
        ``mdk kb reindex`` honours. Tenant-scoped via ``ctx.tenant_id``.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        * **502** — embedding API unreachable (reembed path only)
        """
        from movate.kb.embed import embed_texts, qualified_model_name  # noqa: PLC0415
        from movate.kb.embed import embedding_model as _embedding_model  # noqa: PLC0415

        agents: list[AgentBundle] = request.app.state.agents
        if name not in {b.spec.name for b in agents}:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage

        chunks_reembedded = 0
        if body.reembed:
            # Re-embed every stored chunk's text with the deployment's
            # configured model and overwrite its vector. save_kb_chunk
            # upserts on (agent, tenant_id, content_hash), so persisting
            # the same chunk with a fresh embedding overwrites in place.
            model = _embedding_model()
            chunks = await store.list_kb_chunks(
                agent=name,
                tenant_id=ctx.tenant_id,
                limit=100_000,
            )
            if chunks:
                vectors = await embed_texts([c.text for c in chunks], model=model)
                qualified = qualified_model_name(model)
                for chunk, vector in zip(chunks, vectors, strict=True):
                    await store.save_kb_chunk(
                        chunk.model_copy(update={"embedding": vector, "embedding_model": qualified})
                    )
                chunks_reembedded = len(chunks)

        # Rebuild the index (no-op count on brute-force backends). The
        # KbReindexView reports rebuilt-or-not by backend, not the count,
        # so the return value is intentionally discarded here.
        await store.reindex_kb(agent=name, tenant_id=ctx.tenant_id)
        backend = getattr(store, "name", "unknown")
        # Only Postgres has a real vector index to rebuild; the
        # brute-force backends return the count as a no-op.
        index_rebuilt = backend == "postgres"
        return KbReindexView(
            agent=name,
            reembed=body.reembed,
            chunks_reembedded=chunks_reembedded,
            index_rebuilt=index_rebuilt,
            backend=backend,
        )

    # ------------------------------------------------------------------
    # Knowledge-graph query API (ADR 046) — read-only, graphology-native.
    #
    # A thin read layer over the ALREADY-PERSISTED GraphRAG graph (ADR 010
    # extraction → ``upsert_entity`` / ``upsert_relation``). No extraction
    # change, no new tables, no write path — every endpoint reads through
    # the existing StorageProvider surface and reshapes into graphology
    # JSON a sigma.js client imports with zero transform.
    #
    # Scoping: ``{project_id}`` is the agent (one graph per agent); every
    # query threads ``ctx.tenant_id`` so no cross-tenant node/edge can
    # appear. Every endpoint is hard-capped (default 500, max 5000
    # nodes/edges) so the browser never gets a melt-the-tab payload.
    # ------------------------------------------------------------------

    @v1.get(
        "/projects/{project_id}/graph",
        response_model=GraphologyView,
        tags=["agents-v1", "graph"],
        dependencies=[_scope("read")],
    )
    async def v1_project_graph(
        project_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        mode: str = "knowledge",
        type: str | None = None,
        root: str | None = None,
        depth: int | None = None,
        limit: int | None = None,
    ) -> GraphologyView:
        """A windowed subgraph for ``project_id`` as **graphology JSON**.

        ``project_id`` is the agent that owns the graph. The response is a
        graphology import document (``{attributes, nodes, edges}``) a
        sigma.js client feeds to ``graph.import(...)`` with zero
        transform: each node carries ``label`` / ``type`` /
        degree-derived ``size`` / ``color`` (+ ``community`` when stored);
        layout ``x`` / ``y`` are omitted (the client runs ForceAtlas2).

        Query params:

        * ``mode`` — ``knowledge`` (the GraphRAG graph) or ``topology``
          (reserved; returns empty today).
        * ``type`` — filter to one node type.
        * ``root`` — center the window on a node (bounded k-hop expansion).
          Omit for a whole-graph overview (still capped).
        * ``depth`` — hops from ``root`` (capped at 6).
        * ``limit`` — node/edge cap (default 500, max 5000).

        Tenant-scoped: a cross-tenant ``project_id`` / ``root`` yields an
        empty document (no leak). Read scope.
        """
        store: StorageProvider = request.app.state.storage
        doc = await graph_query.windowed_subgraph(
            store,
            agent=project_id,
            tenant_id=ctx.tenant_id,
            mode=_graph_mode(mode),
            type=type,
            root=root,
            depth=depth,
            limit=limit,
        )
        return GraphologyView.model_validate(doc.model_dump())

    @v1.get(
        "/graph/nodes/{node_id}",
        response_model=NodeDetailView,
        tags=["agents-v1", "graph"],
        dependencies=[_scope("read")],
    )
    async def v1_graph_node_detail(
        node_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        project: str | None = None,
    ) -> NodeDetailView:
        """Detail for one graph node: properties + provenance + neighbors.

        Returns the node's attributes, its ``provenance`` (each source
        chunk's url + snippet + extraction_confidence), the live neighbor
        count, the agents that reference it, and ``_links.expand`` (the
        neighbors endpoint). ``?project=`` scopes the lookup to one
        agent's graph; omit to search across the tenant's agents.

        Tenant-scoped at the storage layer — a node owned by another
        tenant 404s (never 403), so a caller can't probe foreign ids.

        Errors: **401** unauthed, **404** unknown / cross-tenant node.
        """
        store: StorageProvider = request.app.state.storage
        detail = await _resolve_node_detail(
            store, node_id=node_id, tenant_id=ctx.tenant_id, project=project
        )
        if detail is None:
            raise not_found("graph node", node_id)
        return NodeDetailView.model_validate(detail.model_dump(by_alias=True))

    @v1.get(
        "/graph/nodes/{node_id}/neighbors",
        response_model=GraphologyView,
        tags=["agents-v1", "graph"],
        dependencies=[_scope("read")],
    )
    async def v1_graph_node_neighbors(
        node_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        project: str | None = None,
        depth: int = 1,
        limit: int | None = None,
    ) -> GraphologyView:
        """Expand-on-demand: a node's neighborhood as **graphology JSON**.

        Drives the click-to-grow interaction — the client imports this
        document on top of the current graph. Bounded by ``depth`` (hops,
        capped at 6) and ``limit`` (node/edge cap, default 500, max 5000).
        ``?project=`` scopes to one agent's graph. Unknown / cross-tenant
        node → empty document. Read scope.
        """
        store: StorageProvider = request.app.state.storage
        agent = await _resolve_node_agent(
            store, node_id=node_id, tenant_id=ctx.tenant_id, project=project
        )
        if agent is None:
            return GraphologyView()
        doc = await graph_query.expand_node_neighbors(
            store,
            agent=agent,
            tenant_id=ctx.tenant_id,
            node_id=node_id,
            depth=depth,
            limit=limit,
        )
        return GraphologyView.model_validate(doc.model_dump())

    @v1.get(
        "/graph/search",
        response_model=GraphSearchView,
        tags=["agents-v1", "graph"],
        dependencies=[_scope("read")],
    )
    async def v1_graph_search(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        q: str = "",
        project: str | None = None,
        type: str | None = None,
        limit: int | None = None,
    ) -> GraphSearchView:
        """Substring node-label search for fly-to.

        A lexical match on node labels (case-insensitive) — the UI search
        box, not vector retrieval. ``?project=`` scopes to one agent;
        omit to search every agent in the tenant. ``?type=`` filters by
        node type. Capped at the node budget. Empty ``q`` → no results.
        Read scope, tenant-scoped.
        """
        store: StorageProvider = request.app.state.storage
        hits = await _search_graph_nodes(
            store,
            tenant_id=ctx.tenant_id,
            q=q,
            project=project,
            type=type,
            limit=limit,
        )
        results = [GraphSearchResult(key=h.key, label=h.label, type=h.type) for h in hits]
        return GraphSearchView(query=q, results=results, count=len(results))

    @v1.post(
        "/graph/query",
        response_model=GraphologyView,
        tags=["agents-v1", "graph"],
        # Read-only bounded traversal (no mutation) — gates on ``read``
        # despite being a POST (same convention as kb/search).
        dependencies=[_scope("read")],
    )
    async def v1_graph_query(
        body: GraphQueryRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> GraphologyView:
        """Bounded traverse / path / subgraph — **graphology JSON**.

        A POST so the (potentially larger) traversal spec travels in the
        body. ``project`` is the agent; ``root`` is the start node. Depth
        and breadth are bounded SERVER-SIDE regardless of the request
        (depth ≤ 6 hops, limit ≤ 5000 nodes/edges) so a hub can't blow up
        the traverse. Tenant-scoped; cross-tenant ``root`` → empty
        document. Read scope.
        """
        store: StorageProvider = request.app.state.storage
        doc = await graph_query.traverse(
            store,
            agent=body.project,
            tenant_id=ctx.tenant_id,
            root=body.root,
            depth=body.depth,
            limit=body.limit,
            type=body.type,
        )
        return GraphologyView.model_validate(doc.model_dump())

    @v1.get(
        "/projects/{project_id}/graph/stream",
        tags=["agents-v1", "graph"],
        dependencies=[_scope("read")],
        # No response_model — raw SSE byte stream (text/event-stream), not
        # a JSON body. Event payloads are documented in the docstring.
    )
    async def v1_project_graph_stream(
        project_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        mode: str = "knowledge",
        limit: int | None = None,
    ) -> StreamingResponse:
        """Growth stream for ``project_id``'s graph over **SSE**.

        Reuses the ADR 035 SSE infrastructure (:func:`_sse_frame`). Emits
        the current graph as a sequence of growth events the client
        applies incrementally:

        * ``event: node.added`` / ``data: <graphology doc with one node>``
        * ``event: edge.added`` / ``data: <graphology doc with one edge>``
        * ``event: done`` / ``data: {"nodes": N, "edges": M}``

        Each ``node.added`` / ``edge.added`` payload is itself a
        graphology-importable document (a single-element ``nodes`` or
        ``edges`` list), so the client merges every frame with the same
        zero-transform ``graph.import(...)`` it uses for the windowed
        endpoints — no special-case parsing. (``node.updated`` shares the
        ``node.added`` shape; emitted when a re-ingest changes an existing
        node.)

        Bounded by ``limit`` (node/edge cap). Tenant-scoped. Read scope.

        This is a SNAPSHOT-as-stream today (it replays the current graph
        then closes with ``done``); the live-tail seam — pushing future
        ``node.added`` events as ingest writes them — is additive and
        documented in ADR 046 D3.
        """
        store: StorageProvider = request.app.state.storage
        tenant_id = ctx.tenant_id
        cap = graph_query.clamp_cap(limit)
        graph_mode = _graph_mode(mode)

        generator = _sse_graph_growth_stream(
            store=store,
            agent=project_id,
            tenant_id=tenant_id,
            mode=graph_mode,
            cap=cap,
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @v1.post(
        "/agents/{name}/publish",
        response_model=AgentPublishedView,
        tags=["agents-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_publish_agent(
        name: str,
        body: AgentPublishSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AgentPublishedView:
        """Push the agent's canonical bundle to GitHub as one commit
        (item 78, ADR 007 decisions 1-4).

        Reads the on-disk bundle from the runtime's ``agents_path``,
        sends every file through the Git Data API in a single commit
        on the configured default branch, and returns the resulting
        commit SHA + URL.

        Behavior is gated on ``MDK_GITHUB_ENABLED=1`` + a valid
        GitHubConfig pulled from env (``MDK_GITHUB_APP_ID``,
        ``MDK_GITHUB_INSTALLATION_ID``, ``MDK_GITHUB_PRIVATE_KEY``,
        ``MDK_GITHUB_REPO``). When the flag is off the endpoint
        returns 503 — the runtime advertises the route in
        ``/openapi.json`` regardless so the Angular client can
        generate against it before the integration goes live.

        Tenant attribution: today's runtime trusts the env-supplied
        installation_id (one tenant per runtime). Multi-tenant
        installation lookup ships with item 81 (``mdk github
        bootstrap``).

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent doesn't exist at the runtime's
          agents_path
        * **422** — bundle directory empty / GitHub config malformed
        * **502** — upstream GitHub call failed (token exchange,
          tree write, ref update)
        * **503** — integration disabled or runtime built without an
          agents_path
        """
        # Lazy-import the integrations module so the dispatcher path
        # (which never publishes) doesn't trigger cryptography's
        # heavy lift at import time. Only ``GitHubError`` is needed
        # here — the client type comes from app.state regardless.
        from movate.integrations.github import GitHubError  # noqa: PLC0415

        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "POST /api/v1/agents/{name}/publish is unavailable",
                status_code=503,
            )

        client = getattr(request.app.state, "github_client", None)
        if client is None:
            raise AgentCreationError(
                "github integration is disabled; set MDK_GITHUB_ENABLED=1 "
                "and configure MDK_GITHUB_APP_ID / INSTALLATION_ID / "
                "PRIVATE_KEY / REPO to enable POST /api/v1/agents/{name}/publish",
                status_code=503,
            )
        # ``client`` is either a real GitHubClient (production) or a
        # duck-typed test double exposing ``publish_bundle`` — no
        # isinstance check needed; the call below fails loud either
        # way if the method is missing.
        _ = ctx.tenant_id  # future per-tenant audit log entry

        bundle_dir = agents_path / name
        if not bundle_dir.exists() or not bundle_dir.is_dir():
            raise not_found("agent", name)

        message = body.commit_message or f"Update {name}"
        try:
            result = await client.publish_bundle(
                bundle_dir,
                target_dir=name,
                message=message,
                author_name=body.author_name,
                author_email=body.author_email,
            )
        except GitHubError as exc:
            # Translate the integration error onto the right HTTP
            # response. The integration sets ``status_code`` per case
            # (422 config, 502 upstream, 503 disabled).
            raise AgentCreationError(
                str(exc),
                status_code=exc.status_code,
            ) from exc

        return AgentPublishedView(
            agent=name,
            commit_sha=result.commit_sha,
            commit_url=result.commit_url,
            branch=result.branch,
            files_changed=result.files_changed,
        )

    @v1.get(
        "/agents/{name}/history",
        response_model=AgentHistoryView,
        tags=["agents-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_agent_history(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        limit: int = 50,
        page: int = 1,
    ) -> AgentHistoryView:
        """Return the agent's commit history from GitHub (item 79,
        ADR 007).

        Drives the Mova iO version-history panel — one row per commit
        with sha / message / author / timestamp / html_url. Sorted
        newest-first. Empty list when the agent has no published
        commits yet (created via wizard, never published).

        Same feature-flag pattern as ``POST /publish``: returns 503
        with the ``agent_persistence_unavailable`` code when
        ``MDK_GITHUB_ENABLED`` is unset. The route advertises in
        ``/openapi.json`` regardless so client-gen tooling generates
        the typed method now.

        Tenant attribution: today's runtime trusts the env-supplied
        installation_id (one tenant per runtime). Multi-tenant
        installation lookup arrives with item 81.

        Query params:

        * ``limit`` — page size, default 50, clamped to 100 at the
          integration layer (GitHub's per_page max).
        * ``page`` — 1-indexed page number, default 1.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — agent doesn't exist on disk (we check before
          calling GitHub so a typo doesn't burn API budget)
        * **502** — upstream GitHub call failed
        * **503** — integration disabled or runtime built without an
          agents_path
        """
        # Lazy import — same convention as the publish endpoint.
        from movate.integrations.github import GitHubError  # noqa: PLC0415

        agents_path: Path | None = request.app.state.agents_path
        if agents_path is None:
            raise AgentCreationError(
                "runtime was built without an agents_path; "
                "GET /api/v1/agents/{name}/history is unavailable",
                status_code=503,
            )

        client = getattr(request.app.state, "github_client", None)
        if client is None:
            raise AgentCreationError(
                "github integration is disabled; set MDK_GITHUB_ENABLED=1 "
                "and configure MDK_GITHUB_APP_ID / INSTALLATION_ID / "
                "PRIVATE_KEY / REPO to enable GET /api/v1/agents/{name}/history",
                status_code=503,
            )

        _ = ctx.tenant_id  # future per-tenant audit log entry

        bundle_dir = agents_path / name
        if not bundle_dir.exists() or not bundle_dir.is_dir():
            raise not_found("agent", name)

        try:
            commits = await client.list_history(
                target_dir=name,
                limit=limit,
                page=page,
            )
        except GitHubError as exc:
            raise AgentCreationError(
                str(exc),
                status_code=exc.status_code,
            ) from exc

        commit_views = [
            AgentCommitView(
                sha=c.sha,
                message=c.message,
                author_name=c.author_name,
                author_email=c.author_email,
                timestamp=c.timestamp,
                html_url=c.html_url,
            )
            for c in commits
        ]
        # has_more heuristic: full page returned → there might be
        # more. Doesn't guarantee — the next fetch could come back
        # empty. The UI uses this as a "show Load More button" hint.
        return AgentHistoryView(
            agent=name,
            commits=commit_views,
            page=page,
            limit=limit,
            has_more=len(commit_views) == limit,
        )

    @v1.post(
        "/agents/{name}/runs",
        # Union response: 202 + RunAccepted in async mode (default);
        # 200 + RunView when ?wait=true. FastAPI auto-generates a
        # oneOf in OpenAPI so the Angular client can branch.
        response_model=RunAccepted | RunView,
        tags=["agents-v1"],
        dependencies=[_scope("run")],
    )
    async def v1_agent_run(
        name: str,
        body: AgentRunSubmission,
        request: Request,
        response: Response,
        ctx: AuthContext = Depends(auth_dep),
        wait: bool = False,
    ) -> RunAccepted | RunView:
        """Run an agent. Two modes:

        * **Default (?wait=false):** queue a job for the worker pool
          to claim. Returns 202 + ``{job_id, status: queued}``. Angular
          polls ``GET /jobs/{job_id}`` until terminal.

        * **Inline mode (?wait=true):** execute synchronously inside
          the API request and return the resulting ``RunView`` (200).
          Same Executor + provider stack the worker uses, but the run
          happens in-process so wizard-created agents (which don't
          ship to the worker pod yet — see BACKLOG item 109) work
          end-to-end. Trade-off: the request blocks for the full
          agent duration (typically a few seconds for one LLM call;
          can be longer with tool-use loops).

        URL-anchored variant of ``POST /run`` — the agent name comes
        from the path, ``kind=AGENT`` is implicit. REST-clean for
        Angular's resource-oriented mental model (``POST /agents/
        faq-bot/runs`` reads as "create a run under faq-bot").

        Friday-demo path uses ``wait=true`` for the wizard→run
        verb so wizard-created agents respond. Worker-queue path
        (default) is for production load where the client polls.

        Errors (both modes):

        * **401** — missing / bad bearer token
        * **404** — agent not in the registry
        * **422** — body shape failure (FastAPI handles this for us)
        * **500** — (inline mode only) execution failure surfaces
          here; the RunView's ``error`` field carries the typed info
        """
        store: StorageProvider = request.app.state.storage

        # Canary routing (ADR 016 D3) — additive + default-off. Look up the
        # per-(tenant, agent) canary ONCE; choose_version returns None when
        # there's no config / it's disabled / the kill switch (weight 0) is
        # on — so the NO-CANARY path below is byte-for-byte the pre-canary
        # call (resolve_agent_bundle(version=None) → latest; JobRecord with
        # target_version=None → worker resolves latest). The version is the
        # champion-vs-challenger slice key; we never add a field to the run.
        canary = await store.get_canary_config(name, tenant_id=ctx.tenant_id)
        chosen_version = choose_version(canary, thread_id=body.thread_id)

        # Resolve registry-first (so an agent published on another pod is
        # runnable here), filesystem-fallback (local `mdk serve --agents`
        # + the empty-registry tests). Tenant-scoped via the auth context.
        # ``version=chosen_version`` is None for the no-canary path.
        agents: list[AgentBundle] = request.app.state.agents
        bundle = await resolve_agent_bundle(
            store, name, tenant_id=ctx.tenant_id, version=chosen_version, fallback=agents
        )
        if bundle is None:
            raise not_found("agent", name)

        if wait:
            # Inline mode — same Executor stack the worker uses.
            # Lazy imports keep cold-start light for the async path.
            from movate.core.executor import Executor  # noqa: PLC0415
            from movate.core.models import RunRequest as _RunRequest  # noqa: PLC0415
            from movate.providers.base import BaseLLMProvider  # noqa: PLC0415
            from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415
            from movate.providers.mock import MockProvider  # noqa: PLC0415
            from movate.providers.pricing import load_pricing  # noqa: PLC0415
            from movate.tracing import build_tracer  # noqa: PLC0415

            # mock=true → deterministic MockProvider (sub-second, no
            # API keys). Default uses the agent's declared model via
            # LiteLLM. Same pattern the eval endpoint uses.
            provider: BaseLLMProvider = MockProvider() if body.mock else LiteLLMProvider()

            executor = Executor(
                provider=provider,
                pricing=load_pricing(),
                storage=store,
                tracer=build_tracer(),
                tenant_id=ctx.tenant_id,
                # Shared per-replica LLM response cache (NoOp/OFF unless
                # MOVATE_LLM_CACHE is set). Lives on app.state so entries
                # persist across requests; the per-request executor just
                # borrows it.
                cache=request.app.state.llm_cache,
            )
            run_request = _RunRequest(agent=name, input=body.input)
            run_response = await executor.execute(bundle, run_request)
            # Try to fetch the persisted RunRecord. On success the
            # executor always persists; on error it persists a
            # FailureRecord instead (no RunRecord). We handle both:
            # success → return the canonical RunView from storage;
            # error → synthesize a RunView shape from the RunResponse
            # + ErrorInfo so the wire contract is consistent.
            run_record = await store.get_run(run_response.run_id, tenant_id=ctx.tenant_id)
            response.status_code = 200
            if run_record is not None:
                return RunView.from_record(run_record)
            # Error path — build a minimal RunView. Status / error /
            # metrics come from the RunResponse; identifiers reflect
            # what the executor stamped during the failed attempt.
            from datetime import UTC  # noqa: PLC0415
            from datetime import datetime as _datetime  # noqa: PLC0415

            return RunView(
                run_id=run_response.run_id,
                job_id="",
                agent=bundle.spec.name,
                agent_version=bundle.spec.version,
                prompt_hash=bundle.prompt_hash,
                provider=bundle.spec.model.provider,
                provider_version="",
                pricing_version="",
                status=JobStatus.ERROR if run_response.status == "error" else JobStatus.SUCCESS,
                input=body.input,
                output=None,
                metrics=run_response.metrics,
                error=run_response.error,
                created_at=_datetime.now(UTC),
            )

        # item 37 — submission idempotency (async path only; the inline
        # ?wait=true branch above returns before here and is out of scope).
        # Pre-create check: a prior submit with this key for this tenant
        # returns the SAME job; do NOT enqueue again. No header → today's path.
        prior_job_id = await _idempotent_submit_guard(request, store, ctx)
        if prior_job_id is not None:
            response.status_code = 202
            return RunAccepted(job_id=prior_job_id, status=JobStatus.QUEUED, deduplicated=True)

        # Default async path — same as before, plus the canary-chosen
        # version (ADR 016 D3) stamped onto the job so the worker resolves
        # the SAME version this request's routing decision picked (it must
        # not re-roll a weighted/sticky draw at claim time). ``target_version``
        # is None for the no-canary path → the JobRecord is identical to a
        # pre-canary one. ``thread_id`` rides along so a threaded run still
        # joins its thread (and matches the version chosen for that thread).
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=JobKind.AGENT,
            target=name,
            status=JobStatus.QUEUED,
            input=body.input,
            api_key_id=ctx.api_key_id,
            notify_email=body.notify_email,
            thread_id=body.thread_id,
            target_version=chosen_version,
            # ADR 019: the submit→execute trace operators care about. Capture
            # the originating trace so the worker continues it.
            trace_context=inject_current_trace_context(),
        )
        await store.save_job(job)
        response.status_code = 202

        # item 37 — bind the key AFTER create so the recorded job_id is real.
        # Race-safe: if a concurrent retry won, prefer its stored job_id (one
        # canonical response; under a true simultaneous race we may have
        # enqueued one extra job).
        key = _read_idempotency_key(request)
        if key is not None:
            recorded = await store.record_run_submission(ctx.tenant_id, key, job.job_id)
            if not recorded:
                winning_job_id = await store.get_run_submission(ctx.tenant_id, key)
                if winning_job_id is not None and winning_job_id != job.job_id:
                    return RunAccepted(
                        job_id=winning_job_id, status=JobStatus.QUEUED, deduplicated=True
                    )
        return RunAccepted(job_id=job.job_id, status=job.status)

    # ------------------------------------------------------------------
    # Batch inference (item 17) — submit a whole dataset, one AGENT job
    # per row, sharing a batch_id. Reuses the existing queue: each row is
    # an ordinary JobKind.AGENT job, so it inherits retry / dead-letter /
    # canary / observability with no new execution path. Submit gates on
    # ``run`` (it executes the agent); read/status gate on ``read``.
    # ------------------------------------------------------------------

    @v1.post(
        "/agents/{name}/batch",
        response_model=BatchAcceptedView,
        status_code=202,
        tags=["agents-v1", "jobs"],
        dependencies=[_scope("run")],
    )
    async def v1_agent_batch(
        name: str,
        request: Request,
        response: Response,
        ctx: AuthContext = Depends(auth_dep),
    ) -> BatchAcceptedView:
        """Submit a dataset of inputs for ``name`` as a batch of async jobs.

        Accepts EITHER:

        * a ``multipart/form-data`` upload with a ``file`` field holding a
          **JSONL** dataset (one JSON object per line = one run's input); OR
        * an inline JSON body ``{"inputs": [ {...}, ... ], "notify_email"?: ...}``
          for programmatic callers that already have the rows in memory.

        Each row becomes ONE ordinary ``JobKind.AGENT`` job — the exact same
        shape the single-run path produces — stamped with a shared
        ``batch_id``. The worker runs them with no new dispatch branch, so
        every row is observable, retryable, dead-letter-handled, and
        canary-aware for free. A :class:`BatchRecord` (``total`` = row count)
        is persisted so ``GET /api/v1/batches/{batch_id}`` can aggregate.

        Returns ``202`` + ``{batch_id, total, status: "queued"}``. Poll the
        status endpoint for per-row progress.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — key lacks the ``run`` scope
        * **404** — agent not in the registry (same resolution as single-run)
        * **413** — dataset exceeds the per-request row cap
          (``MDK_BATCH_MAX_ROWS``, default 10000)
        * **422** — empty dataset, malformed JSONL, or a non-object row
        """
        store: StorageProvider = request.app.state.storage

        # Resolve the agent registry-first, filesystem-fallback — identical
        # to the single-run path so an unknown agent 404s the same way. We
        # do NOT apply canary version pinning here: a batch is a bulk eval /
        # backfill, so every row resolves "latest" (target_version=None),
        # byte-for-byte a pre-canary agent job. (A future PR could thread a
        # per-batch version pin; out of scope for item 17.)
        agents: list[AgentBundle] = request.app.state.agents
        bundle = await resolve_agent_bundle(
            store, name, tenant_id=ctx.tenant_id, version=None, fallback=agents
        )
        if bundle is None:
            raise not_found("agent", name)

        rows, notify_email = await _parse_batch_dataset(request)

        if not rows:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=422,
                message="batch dataset is empty — provide at least one input row",
            )
        max_rows = _batch_max_rows()
        if len(rows) > max_rows:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=413,
                message=(
                    f"batch dataset has {len(rows)} rows, exceeding the per-request "
                    f"cap of {max_rows} (set MDK_BATCH_MAX_ROWS to adjust)"
                ),
            )

        # Mint the parent + enqueue one ordinary AGENT job per row, all
        # sharing the batch_id. Persist the BatchRecord FIRST so a crash
        # mid-enqueue still leaves a discoverable parent (its children that
        # made it onto the queue still run; the status endpoint reports the
        # partial count honestly against ``total``).
        batch_id = str(uuid4())
        batch = BatchRecord(
            batch_id=batch_id,
            tenant_id=ctx.tenant_id,
            agent=name,
            total=len(rows),
            created_by=ctx.api_key_id,
        )
        await store.save_batch(batch)

        # ADR 019: capture the originating submit trace once — every child job
        # of this batch continues the same trace in its worker.
        batch_trace_context = inject_current_trace_context()
        for row in rows:
            job = JobRecord(
                job_id=str(uuid4()),
                tenant_id=ctx.tenant_id,
                kind=JobKind.AGENT,
                target=name,
                status=JobStatus.QUEUED,
                input=row,
                api_key_id=ctx.api_key_id,
                notify_email=notify_email,
                batch_id=batch_id,
                trace_context=dict(batch_trace_context),
            )
            await store.save_job(job)

        response.status_code = 202
        return BatchAcceptedView(batch_id=batch_id, total=len(rows), status="queued")

    @v1.get(
        "/batches",
        response_model=BatchListView,
        tags=["agents-v1", "jobs"],
        dependencies=[_scope("read")],
    )
    async def v1_list_batches(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        limit: int = 20,
    ) -> BatchListView:
        """List this tenant's recent batches, newest-first.

        Always tenant-scoped. Returns parent metadata only (no per-status
        aggregate — that requires fetching every child, so it lives on
        ``GET /api/v1/batches/{id}``). ``limit`` is hard-capped at 100.
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_batches(tenant_id=ctx.tenant_id, limit=capped_limit)
        items = [
            BatchListItemView(
                batch_id=b.batch_id,
                agent=b.agent,
                total=b.total,
                created_at=b.created_at,
            )
            for b in records
        ]
        return BatchListView(batches=items, count=len(items))

    @v1.get(
        "/batches/{batch_id}",
        response_model=BatchStatusView,
        tags=["agents-v1", "jobs"],
        dependencies=[_scope("read")],
    )
    async def v1_get_batch(
        batch_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> BatchStatusView:
        """Aggregate status of one batch's child jobs.

        Loads the :class:`BatchRecord` (tenant-scoped — a cross-tenant or
        missing id 404s identically, never leaking existence), fetches the
        child jobs via ``list_jobs(batch_id=...)``, and returns per-status
        counts + a derived overall ``state``: ``running`` while ANY child is
        still non-terminal (QUEUED / RUNNING), else ``complete``.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — key lacks the ``read`` scope
        * **404** — no such batch for this tenant
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_batch(batch_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("batch", batch_id)

        # A batch's child count is bounded by the submit-time row cap, so a
        # single list call with that ceiling fetches them all. Pass the
        # recorded ``total`` (min 1) as the limit so we never silently
        # truncate the aggregate.
        children = await store.list_jobs(
            tenant_id=ctx.tenant_id,
            batch_id=batch_id,
            limit=max(record.total, 1),
        )

        counts = BatchStatusCounts()
        non_terminal = {JobStatus.QUEUED, JobStatus.RUNNING}
        any_pending = False
        for child in children:
            # Field names mirror the JobStatus values 1:1.
            setattr(counts, child.status.value, getattr(counts, child.status.value) + 1)
            if child.status in non_terminal:
                any_pending = True

        # "running" while any child is still QUEUED/RUNNING; "complete" once
        # every child has reached a terminal status. An empty batch (total=0,
        # no children) reads "complete" — there's nothing left to run.
        state = "running" if any_pending else "complete"

        return BatchStatusView(
            batch_id=record.batch_id,
            agent=record.agent,
            total=record.total,
            counts=counts,
            state=state,
            created_at=record.created_at,
            job_ids=[c.job_id for c in children],
        )

    @v1.post(
        "/agents/{name}/runs/stream",
        tags=["agents-v1"],
        dependencies=[_scope("run")],
        # No response_model — this returns a raw SSE byte stream
        # (text/event-stream), not a JSON body. OpenAPI documents the
        # event shapes in the docstring instead.
    )
    async def v1_agent_run_stream(
        name: str,
        body: AgentRunSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> StreamingResponse:
        """Run an agent and stream model tokens live over **SSE**.

        Companion to the inline ``POST /agents/{name}/runs?wait=true``
        path — same Executor stack, same bundle resolution (incl.
        canary routing, ADR 016 D3), same persistence (the streamed run
        writes its ``RunRecord`` exactly as a non-streamed run, so
        ``GET /runs/{run_id}`` works after the stream closes). The ONLY
        difference is the transport: instead of blocking for the full
        run and returning one ``RunView``, we emit
        `Server-Sent Events <https://html.spec.whatwg.org/multipage/server-sent-events.html>`_
        so a client (``mdk run --target <t> --stream``) renders tokens
        as they arrive.

        Event shapes (``\\n\\n``-terminated SSE frames):

        * **per token** — ``event: token`` /
          ``data: {"text": "<delta>"}``. Zero or more; concatenating
          every ``text`` reconstructs the model's raw output.
        * **terminal success** — ``event: done`` /
          ``data: {"run_id", "status", "metrics", "output"}``.
        * **failure** — ``event: error`` /
          ``data: {"message", "code"}``. Emitted instead of ``done``
          when the executor returns an error status or raises.

        Streaming is purely *additive observation* (the Executor's
        ``on_token`` hook): cost accounting, schema validation, and
        persistence are byte-for-byte the same as a one-shot run.

        Gated on the ``run`` scope (least privilege) and tenant-scoped:
        the persisted ``RunRecord`` carries the caller's tenant, so a
        cross-tenant ``GET /runs/{id}`` 404s exactly like the sync path.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — token lacks the ``run`` scope
        * **404** — agent not in the registry
        * **422** — body shape failure
        """
        store: StorageProvider = request.app.state.storage

        # Canary routing (ADR 016 D3) — resolve EXACTLY like the sync run
        # path so a streamed run picks the same champion/challenger
        # version a non-streamed run would. choose_version returns None
        # (→ latest) when there's no config / it's disabled.
        canary = await store.get_canary_config(name, tenant_id=ctx.tenant_id)
        chosen_version = choose_version(canary, thread_id=body.thread_id)

        agents: list[AgentBundle] = request.app.state.agents
        bundle = await resolve_agent_bundle(
            store, name, tenant_id=ctx.tenant_id, version=chosen_version, fallback=agents
        )
        if bundle is None:
            raise not_found("agent", name)

        # Lazy imports keep cold-start light for the non-streaming paths.
        from movate.core.executor import Executor  # noqa: PLC0415
        from movate.core.models import RunRequest as _RunRequest  # noqa: PLC0415
        from movate.providers.base import BaseLLMProvider  # noqa: PLC0415
        from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415
        from movate.providers.mock import MockProvider  # noqa: PLC0415
        from movate.providers.pricing import load_pricing  # noqa: PLC0415
        from movate.tracing import build_tracer  # noqa: PLC0415

        provider: BaseLLMProvider = MockProvider() if body.mock else LiteLLMProvider()
        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=store,
            tracer=build_tracer(),
            tenant_id=ctx.tenant_id,
            cache=request.app.state.llm_cache,
        )
        run_request = _RunRequest(agent=name, input=body.input)
        tenant_id = ctx.tenant_id

        generator = _sse_run_stream(
            executor=executor,
            bundle=bundle,
            run_request=run_request,
            store=store,
            tenant_id=tenant_id,
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                # Defeat any intermediary buffering so tokens reach the
                # client as they're produced (matters behind nginx /
                # Azure Front Door).
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @v1.get(
        "/jobs",
        response_model=JobListView,
        tags=["jobs-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_jobs(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        status: JobStatus | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> JobListView:
        """Filterable + paginatable job history for the Angular UI's
        run-history table.

        Extends the legacy ``GET /jobs`` (which only filtered by
        ``status``) with:

        * ``agent=<name>`` — drives the agent-profile page's
          "recent runs" tab. Filters server-side via the new
          ``list_jobs(target=...)`` storage method.
        * Same tenant-scoping as the legacy endpoint — a tenant
          can never see another tenant's jobs.

        Limit is hard-capped at 100 for response size + perf.

        Errors:

        * **401** — missing / bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_jobs(
            tenant_id=ctx.tenant_id,
            status=status,
            target=agent,
            limit=capped_limit,
        )
        views = [JobView.from_record(r) for r in records]
        return JobListView(jobs=views, count=len(views))

    # ------------------------------------------------------------------
    # /api/v1 aliases for the unversioned job-poll + run-fetch routes.
    #
    # A caller that submits via ``POST /api/v1/agents/{name}/runs`` gets
    # back a ``job_id`` and naturally polls the *versioned* path
    # ``GET /api/v1/jobs/{job_id}`` (then fetches the run at
    # ``GET /api/v1/runs/{run_id}``). Those routes only existed
    # UNVERSIONED (``/jobs/{id}``, ``/runs/{id}``) — the obvious v1 path
    # 404'd. These thin aliases delegate to the SAME unversioned handler
    # closures (``get_job`` / ``get_run`` above) so there is exactly one
    # copy of the business logic, scope, and tenant-scoping. The
    # unversioned routes stay as-is for back-compat; these are additive.
    #
    # ``GET /api/v1/jobs`` (list) already exists as ``v1_list_jobs``
    # above (a superset with an ``agent=`` filter), so no list alias is
    # added here.
    # ------------------------------------------------------------------
    @v1.get(
        "/jobs/{job_id}",
        response_model=JobView,
        tags=["jobs-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_job(
        job_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> JobView:
        """Versioned alias of ``GET /jobs/{job_id}``.

        Delegates to the unversioned :func:`get_job` handler — identical
        ``read`` scope, ``JobView`` response, and tenant-scoping (404 on
        cross-tenant access, never 403)."""
        return await get_job(job_id, request, ctx)

    @v1.post(
        "/jobs/{job_id}/cancel",
        response_model=JobCancelView,
        tags=["jobs-v1"],
        dependencies=[_scope("run")],
    )
    async def v1_cancel_job(
        job_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> JobCancelView:
        """Cooperatively cancel a queued/running job (item 36, R4b).

        Body-less. Gated on the ``run`` scope (a stronger capability than
        the ``read`` scope used to poll) and tenant-scoped via ``ctx`` —
        a caller can only cancel its own tenant's jobs.

        Semantics (the returned ``status`` is the state AFTER the call):

        * ``QUEUED`` → ``cancelled`` immediately (the worker's claim only
          takes ``queued`` rows, so it's never executed).
        * ``RUNNING`` → returns ``running``: the cancel is *pending*. The
          worker finishes the in-flight work (cooperative — NO
          mid-LLM-call interruption), then DISCARDS the result and writes
          ``cancelled`` at its terminal checkpoint. Poll
          ``GET /jobs/{id}`` to observe the transition.
        * already terminal → no-op; returns the unchanged status (you
          can't cancel a finished job).

        Tenant-scoped at the storage layer (``request_job_cancel(...,
        tenant_id=...)`` filters in WHERE) so a cross-tenant id returns
        ``None`` and we 404 — never 403, which would leak the id.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — token lacks the ``run`` scope
        * **404** — no such job for this tenant
        """
        store: StorageProvider = request.app.state.storage
        status = await store.request_job_cancel(job_id, tenant_id=ctx.tenant_id)
        if status is None:
            raise not_found("job", job_id)
        return JobCancelView(job_id=job_id, status=status)

    @v1.get(
        "/runs/{run_id}",
        response_model=RunView,
        tags=["runs-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_run(
        run_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunView:
        """Versioned alias of ``GET /runs/{run_id}``.

        Delegates to the unversioned :func:`get_run` handler — identical
        ``read`` scope, ``RunView`` response (including ``output``), and
        tenant-scoping (404 on cross-tenant access, never 403)."""
        return await get_run(run_id, request, ctx)

    @v1.get(
        "/runs/{run_id}/trace",
        response_model=RunTraceView,
        tags=["runs-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_run_trace(
        run_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunTraceView:
        """Reconstructed view of a run for the Angular trace-viewer.

        Wraps the existing :func:`movate.core.replay.load_replay`
        engine (same path ``mdk trace replay`` uses) and returns the
        structured JSON the Angular trace component renders.

        Resolves ``run_id`` against BOTH the runs table and the
        workflow_runs table — the same id space is shared, so a
        single endpoint serves both single-agent and workflow trace
        replays. Discriminator is the ``kind`` field in the response.

        Tenant-scoped: a cross-tenant id returns 404 (never 403),
        which would leak the existence of the id.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — neither a run nor workflow_run matches the id
          for this tenant
        """
        # Lazy import — keeps the runtime module's import-time cost low
        # for callers (workers, tests) that never hit this endpoint.
        from movate.core.replay import (  # noqa: PLC0415
            ReplayNotFoundError,
            load_replay,
        )

        store: StorageProvider = request.app.state.storage
        try:
            replay = await load_replay(store, run_id, tenant_id=ctx.tenant_id)
        except ReplayNotFoundError as exc:
            raise not_found("run", run_id) from exc

        # Mirror the JSON shape render_replay_json produces but as a
        # typed view. ``_run_to_dict`` / ``_workflow_to_dict`` live in
        # core.replay alongside the engine — re-use them here so the
        # Angular client and the CLI's ``mdk trace replay`` see byte-
        # for-byte identical data.
        from movate.core.replay import _run_to_dict, _workflow_to_dict  # noqa: PLC0415

        if replay.kind == "agent":
            assert replay.run is not None  # narrowed by replay.kind
            return RunTraceView(
                kind="agent",
                run=_run_to_dict(replay.run),
                total_cost_usd=replay.total_cost_usd,
                total_latency_ms=replay.total_latency_ms,
            )
        # workflow path
        assert replay.workflow is not None
        return RunTraceView(
            kind="workflow",
            workflow=_workflow_to_dict(replay.workflow),
            nodes=[_run_to_dict(r) for r in (replay.children or [])],
            total_cost_usd=replay.total_cost_usd,
            total_latency_ms=replay.total_latency_ms,
        )

    # ------------------------------------------------------------------
    # Eval endpoints (BACKLOG Group H items 83-85)
    # ------------------------------------------------------------------
    @v1.post(
        "/agents/{name}/evals",
        response_model=EvalAcceptedView,
        status_code=202,
        tags=["evals-v1"],
        dependencies=[_scope("eval")],
    )
    async def v1_kick_off_eval(
        name: str,
        body: EvalSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> EvalAcceptedView:
        """Run an eval against an agent's dataset and persist the EvalRecord.

        **Default (``wait=false``):** creates a ``JobRecord(kind=EVAL)``
        and returns 202 immediately with ``{job_id, status: "queued"}``.
        The worker process claims and executes the job; poll
        ``GET /api/v1/jobs/{job_id}`` until terminal, then fetch the
        scorecard from ``GET /api/v1/evals/{result_run_id}``.

        **Synchronous (``wait=true``):** runs the eval inline and
        returns ``{eval_id, status: "success"}`` directly. Convenient
        for demos or CI scripts where a separate worker is not running.
        Avoid for large datasets (risk of HTTP gateway timeout).

        Errors:

        * **401** — bad bearer token
        * **404** — agent not in the registry
        * **422** — eval config / dataset error (``wait=true`` path only;
          async path surfaces the error via the job's error field)
        """
        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == name), None)
        if bundle is None:
            raise not_found("agent", name)

        store: StorageProvider = request.app.state.storage

        # ── Async path (default) ──────────────────────────────────────────
        if not body.wait:
            job = JobRecord(
                job_id=str(uuid4()),
                tenant_id=ctx.tenant_id,
                kind=JobKind.EVAL,
                target=name,
                input={
                    "mock": body.mock,
                    "runs": body.runs,
                    "gate_mode": body.gate_mode,
                    "gate": body.gate,
                    "objective": body.objective,
                    "baseline_id": body.baseline_id,
                    "regression_tolerance": body.regression_tolerance,
                },
                api_key_id=ctx.api_key_id,
                # ADR 019: capture the originating trace so the worker
                # continues it.
                trace_context=inject_current_trace_context(),
            )
            await store.save_job(job)
            return EvalAcceptedView(
                job_id=job.job_id,
                status="queued",
            )

        # ── Sync path (wait=true) ─────────────────────────────────────────
        from movate.core.eval import EvalConfigError, EvalEngine  # noqa: PLC0415
        from movate.core.executor import Executor  # noqa: PLC0415
        from movate.providers.base import BaseLLMProvider  # noqa: PLC0415
        from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415
        from movate.providers.mock import MockProvider  # noqa: PLC0415
        from movate.providers.pricing import load_pricing  # noqa: PLC0415
        from movate.tracing import build_tracer  # noqa: PLC0415

        provider: BaseLLMProvider = MockProvider() if body.mock else LiteLLMProvider()
        executor = Executor(
            provider=provider,
            pricing=load_pricing(),
            storage=store,
            tracer=build_tracer(),
            tenant_id=ctx.tenant_id,
        )

        try:
            engine = EvalEngine(
                executor=executor,
                provider=provider,
                runs_per_case=body.runs,
                gate_mode=body.gate_mode,
                objective_filter=body.objective,
                global_skill_responses=body.skill_responses,
            )
            summary = await engine.run(bundle)
        except EvalConfigError as exc:
            return EvalAcceptedView(
                status="failed",
                message=str(exc),
            )

        record = summary.to_record(tenant_id=ctx.tenant_id)
        await store.save_eval(record)

        return EvalAcceptedView(
            eval_id=record.eval_id,
            status="success",
        )

    @v1.get(
        "/evals/{eval_id}",
        response_model=EvalScorecardView,
        tags=["evals-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_eval(
        eval_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> EvalScorecardView:
        """Retrieve a completed eval's scorecard.

        Tenant-scoped at the storage layer (a cross-tenant id probe
        returns 404, never 403, to avoid leaking that the id exists).

        Errors:

        * **401** — bad bearer token
        * **404** — no eval record matches the id for this tenant
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_eval(eval_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("eval", eval_id)
        return _eval_record_to_view(record)

    @v1.get(
        "/evals",
        response_model=EvalListView,
        tags=["evals-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_evals(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        agent: str | None = None,
        limit: int = 20,
    ) -> EvalListView:
        """Paginated history of eval runs. Filter by ``agent=<name>``
        to drive the agent-profile "evals over time" chart.

        Same tenant scoping as every other endpoint; limit hard-
        capped at 100.

        Errors:

        * **401** — bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_evals(
            tenant_id=ctx.tenant_id,
            agent=agent,
            limit=capped_limit,
        )
        views = [_eval_record_to_view(r) for r in records]
        return EvalListView(evals=views, count=len(views))

    # ------------------------------------------------------------------
    # Aggregate monitor feed (ADR 032 D2). Two read-scoped endpoints that
    # expose the SAME rollup ``mdk report`` computes (``core.reporting``) over
    # the local store — the in-product "how are my agents doing?" feed the
    # Mova iO front end renders. Tenant-scoped at the storage read; no remote
    # calls (same path the CLI uses). The runtime never imports ``cli``; the
    # shared aggregation lives in ``core`` (``cli ⊥ runtime``).
    #
    # Bounded reads: the store fetch is capped (``_REPORT_FETCH_CAP``) so a
    # tenant with a huge history can't make the endpoint unbounded — the
    # rollup is over the most recent N runs/evals. The ``window`` param
    # narrows further (last N days; 0 = all-time).
    # ------------------------------------------------------------------
    async def _fetch_report(
        store: StorageProvider,
        *,
        tenant_id: str | None,
        agent: str | None,
        window_days: int,
        top_n: int,
    ) -> Report:
        """Fetch (tenant-scoped, bounded) + window + reduce — shared by both
        endpoints. Empty store → a zeroed :class:`Report` (never a 500)."""
        runs = await store.list_runs(
            agent=agent,
            tenant_id=tenant_id,
            limit=_REPORT_FETCH_CAP,
        )
        evals = await store.list_evals(
            agent=agent,
            tenant_id=tenant_id,
            limit=_REPORT_FETCH_CAP,
        )
        runs = _filter_runs_by_since(runs, window_days)
        evals = _filter_evals_by_since(evals, window_days)
        return build_report(
            runs,
            evals,
            window_days=window_days,
            agent_filter=agent,
            top_n=top_n,
        )

    @v1.get(
        "/report",
        response_model=ReportView,
        tags=["monitor"],
        dependencies=[_scope("read")],
    )
    async def v1_report(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        window: int = Query(
            0,
            ge=0,
            le=3650,
            description=(
                "Only count runs / evals from the last N days. 0 (default) = "
                "all-time. Mirrors ``mdk report --last N``."
            ),
        ),
        top: int = Query(
            5,
            ge=1,
            le=50,
            description="How many failing cases to surface in ``top_failing_cases``.",
        ),
    ) -> ReportView:
        """Cross-agent monitor feed (ADR 032 D2).

        The tenant-scoped rollup the Mova iO front end renders as the
        in-product monitor: pass-rate trends, cost-over-time, latency
        p50/p95/p99, top failing cases, and a per-agent/workflow rollup over
        the requested ``window``. Identical aggregation to ``mdk report``.

        Tenant-scoped: only this tenant's runs/evals contribute. An empty
        store returns a zeroed report (200), never a 500.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        """
        store: StorageProvider = request.app.state.storage
        report = await _fetch_report(
            store,
            tenant_id=ctx.tenant_id,
            agent=None,
            window_days=window,
            top_n=top,
        )
        return ReportView.from_report(report)

    @v1.get(
        "/agents/{name}/metrics",
        response_model=AgentMetricsView,
        tags=["monitor"],
        dependencies=[_scope("read")],
    )
    async def v1_agent_metrics(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        window: int = Query(
            0,
            ge=0,
            le=3650,
            description=(
                "Only count this agent's runs / evals from the last N days. 0 (default) = all-time."
            ),
        ),
        top: int = Query(
            5,
            ge=1,
            le=50,
            description="How many failing cases to surface for this agent.",
        ),
    ) -> AgentMetricsView:
        """Per-agent monitor slice (ADR 032 D2).

        The named agent's (or workflow's) rollup row plus agent-scoped totals
        and top-failing cases over ``window`` — powers the front-end
        agent-profile health panel.

        Tenant-scoped. An agent with no runs/evals in the window returns a
        **zeroed** rollup (200), not a 404 — the monitor is a metrics view, so
        an empty panel is the correct rendering, and we never leak whether an
        id exists across tenants.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        """
        store: StorageProvider = request.app.state.storage
        report = await _fetch_report(
            store,
            tenant_id=ctx.tenant_id,
            agent=name,
            window_days=window,
            top_n=top,
        )
        return AgentMetricsView.from_report(name, report)

    # ------------------------------------------------------------------
    # Continuous-eval schedules (ADR 016 D2). Additive + default-off.
    # Writes gate on the `eval` scope (same as kicking off an eval);
    # reads gate on `read`. The cadence is driven by an external cron
    # calling the scheduler tick — these endpoints only manage the rows.
    # ------------------------------------------------------------------
    def _schedule_to_view(s: EvalSchedule) -> EvalScheduleView:
        return EvalScheduleView(
            agent=s.agent,
            cadence_seconds=s.cadence_seconds,
            enabled=s.enabled,
            mock=s.mock,
            runs=s.runs,
            gate_mode=s.gate_mode,
            gate=s.gate,
            objective=s.objective,
            regression_tolerance=s.regression_tolerance,
            baseline_id=s.baseline_id,
            notify_email=s.notify_email,
            last_enqueued_at=s.last_enqueued_at.isoformat() if s.last_enqueued_at else None,
            created_at=s.created_at.isoformat(),
        )

    @v1.put(
        "/agents/{name}/eval-schedule",
        response_model=EvalScheduleView,
        tags=["evals-v1"],
        dependencies=[_scope("eval")],
    )
    async def v1_set_eval_schedule(
        name: str,
        body: EvalScheduleSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> EvalScheduleView:
        """Upsert an agent's continuous-eval cadence (ADR 016 D2).

        Idempotent: re-PUTting overwrites the agent's schedule. The schedule
        is enqueued by an external cron calling the scheduler tick — this
        endpoint only persists the cadence + drift knobs.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``eval`` scope
        * **404** — agent not registered for this tenant
        """
        store: StorageProvider = request.app.state.storage
        agents: list[AgentBundle] = request.app.state.agents
        registered = await store.get_agent_bundle(name, tenant_id=ctx.tenant_id)
        if registered is None and not any(b.spec.name == name for b in agents):
            raise not_found("agent", name)
        schedule = EvalSchedule(
            tenant_id=ctx.tenant_id,
            agent=name,
            cadence_seconds=body.cadence_seconds,
            enabled=body.enabled,
            mock=body.mock,
            runs=body.runs,
            gate_mode=body.gate_mode,
            gate=body.gate,
            objective=body.objective,
            regression_tolerance=body.regression_tolerance,
            baseline_id=body.baseline_id,
            notify_email=body.notify_email,
            created_by=ctx.api_key_id,
        )
        await store.save_eval_schedule(schedule)
        return _schedule_to_view(schedule)

    @v1.get(
        "/eval-schedules",
        response_model=EvalScheduleListView,
        tags=["evals-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_eval_schedules(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        limit: int = 100,
    ) -> EvalScheduleListView:
        """List this tenant's continuous-eval schedules.

        Errors:

        * **401** — bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        rows = await store.list_eval_schedules(tenant_id=ctx.tenant_id, limit=capped_limit)
        views = [_schedule_to_view(r) for r in rows]
        return EvalScheduleListView(schedules=views, count=len(views))

    @v1.delete(
        "/agents/{name}/eval-schedule",
        status_code=204,
        tags=["evals-v1"],
        dependencies=[_scope("eval")],
    )
    async def v1_clear_eval_schedule(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Remove an agent's continuous-eval schedule.

        Idempotent: clearing a non-existent schedule still returns 204.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``eval`` scope
        """
        store: StorageProvider = request.app.state.storage
        await store.delete_eval_schedule(name, tenant_id=ctx.tenant_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Generic agent/workflow cron schedules (ADR 017 D2). Additive +
    # default-off. These schedule *execution* (agent/workflow runs), so
    # writes gate on the `run` scope (same as POST /run); reads gate on
    # `read`. The cadence is driven by an external cron calling the
    # scheduler tick — these endpoints only manage the rows. Target
    # existence is NOT validated here (mirrors POST /run): the worker
    # surfaces an unknown agent/workflow when it claims the job.
    # ------------------------------------------------------------------
    def _job_schedule_to_view(s: JobSchedule) -> JobScheduleView:
        return JobScheduleView(
            name=s.name,
            kind=s.kind,
            target=s.target,
            cadence_seconds=s.cadence_seconds,
            enabled=s.enabled,
            input=s.input,
            notify_email=s.notify_email,
            last_enqueued_at=s.last_enqueued_at.isoformat() if s.last_enqueued_at else None,
            created_at=s.created_at.isoformat(),
        )

    @v1.put(
        "/schedules/{name}",
        response_model=JobScheduleView,
        tags=["jobs"],
        dependencies=[_scope("run")],
    )
    async def v1_set_job_schedule(
        name: str,
        body: JobScheduleSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> JobScheduleView:
        """Upsert a cron schedule that enqueues an agent/workflow job (ADR 017 D2).

        Idempotent: re-PUTting the same ``name`` overwrites the schedule. The
        schedule is enqueued by an external cron calling the scheduler tick —
        this endpoint only persists the cadence + job payload.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``run`` scope
        * **422** — ``kind`` is not ``agent``/``workflow``
        """
        store: StorageProvider = request.app.state.storage
        schedule = JobSchedule(
            tenant_id=ctx.tenant_id,
            name=name,
            kind=body.kind,
            target=body.target,
            cadence_seconds=body.cadence_seconds,
            enabled=body.enabled,
            input=body.input,
            notify_email=body.notify_email,
            created_by=ctx.api_key_id,
        )
        await store.save_job_schedule(schedule)
        return _job_schedule_to_view(schedule)

    @v1.get(
        "/schedules",
        response_model=JobScheduleListView,
        tags=["jobs"],
        dependencies=[_scope("read")],
    )
    async def v1_list_job_schedules(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        limit: int = 100,
    ) -> JobScheduleListView:
        """List this tenant's cron schedules.

        Errors:

        * **401** — bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        rows = await store.list_job_schedules(tenant_id=ctx.tenant_id, limit=capped_limit)
        views = [_job_schedule_to_view(r) for r in rows]
        return JobScheduleListView(schedules=views, count=len(views))

    @v1.get(
        "/schedules/{name}",
        response_model=JobScheduleView,
        tags=["jobs"],
        dependencies=[_scope("read")],
    )
    async def v1_get_job_schedule(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> JobScheduleView:
        """Fetch one cron schedule by its handle.

        Tenant-scoped: a schedule under another tenant 404s (no existence leak).

        Errors:

        * **401** — bad bearer token
        * **404** — no schedule with this name for this tenant
        """
        store: StorageProvider = request.app.state.storage
        row = await store.get_job_schedule(name, tenant_id=ctx.tenant_id)
        if row is None:
            raise not_found("schedule", name)
        return _job_schedule_to_view(row)

    @v1.delete(
        "/schedules/{name}",
        status_code=204,
        tags=["jobs"],
        dependencies=[_scope("run")],
    )
    async def v1_clear_job_schedule(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Remove a cron schedule by its handle.

        Idempotent: clearing a non-existent schedule still returns 204.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``run`` scope
        """
        store: StorageProvider = request.app.state.storage
        await store.delete_job_schedule(name, tenant_id=ctx.tenant_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Event/webhook triggers (ADR 017 D2). Additive + default-off.
    #
    # Two surfaces with DIFFERENT auth models:
    #
    #  * Management CRUD (POST/GET/DELETE /triggers[/{name}]) — the normal
    #    mvt_* key + AuthContext, tenant-scoped. Create/delete gate on
    #    ``admin`` (creating a trigger mints a long-lived secret credential,
    #    like minting an API key); list/get gate on ``read``.
    #  * The FIRE endpoint (POST /triggers/{trigger_id}/events) — hit by an
    #    EXTERNAL system that has NO mvt_* key. It is deliberately NOT behind
    #    the api-key auth dependency; instead it authenticates with the
    #    per-trigger secret via an HMAC-SHA256 signature over the raw body
    #    (X-Movate-Signature). On success it builds the SAME JobRecord shape
    #    POST /run + the scheduler produce (via build_triggered_job) so the
    #    run flows through the existing dispatch with no new branch.
    #
    # Replay/idempotency (item 23, ADR 017 D2 follow-up): an OPTIONAL
    # X-Movate-Delivery-Id header makes the fire path idempotent — a repeated
    # delivery (at-least-once webhook retry) returns the SAME job without
    # re-enqueuing, dedup'd on (trigger_id, delivery_id). Absent header →
    # byte-for-byte today's always-enqueue behavior. Auth still gates first:
    # an unauthenticated request never reads or writes the dedup store.
    # ------------------------------------------------------------------
    def _trigger_to_view(t: Trigger) -> TriggerView:
        return TriggerView(
            trigger_id=t.trigger_id,
            name=t.name,
            kind=t.kind,
            target=t.target,
            input_defaults=t.input_defaults,
            enabled=t.enabled,
            last_fired_at=t.last_fired_at.isoformat() if t.last_fired_at else None,
            created_at=t.created_at.isoformat(),
        )

    @v1.post(
        "/triggers",
        response_model=TriggerCreatedView,
        status_code=201,
        tags=["triggers"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_trigger(
        body: TriggerCreateRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> TriggerCreatedView:
        """Register an inbound event/webhook trigger (ADR 017 D2).

        Mints a per-trigger secret + a stable public ``trigger_id``, persists
        the trigger (secret hashed at rest), and returns the trigger metadata
        plus the plaintext ``secret`` **once** — it is irrecoverable
        afterward, exactly like a minted API key.

        The external system then POSTs events to
        ``POST /api/v1/triggers/{trigger_id}/events`` with an
        ``X-Movate-Signature: sha256=<hex>`` header = HMAC-SHA256 of the raw
        body keyed by the secret.

        Gated on ``admin`` (it mints a long-lived secret credential).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        * **422** — ``kind`` is not ``agent``/``workflow``
        """
        store: StorageProvider = request.app.state.storage
        name = body.name or body.target
        minted = mint_trigger(
            tenant_id=ctx.tenant_id,
            name=name,
            kind=body.kind,
            target=body.target,
            input_defaults=body.input_defaults,
            enabled=body.enabled,
            created_by=ctx.api_key_id,
        )
        await store.save_trigger(minted.record)
        view = _trigger_to_view(minted.record)
        return TriggerCreatedView(
            **view.model_dump(),
            secret=minted.secret,
            salt=minted.salt,
            webhook_path=f"/api/v1/triggers/{minted.record.trigger_id}/events",
        )

    @v1.get(
        "/triggers",
        response_model=TriggerListView,
        tags=["triggers"],
        dependencies=[_scope("read")],
    )
    async def v1_list_triggers(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        limit: int = 100,
    ) -> TriggerListView:
        """List this tenant's registered triggers (no secrets).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        rows = await store.list_triggers(tenant_id=ctx.tenant_id, limit=capped_limit)
        views = [_trigger_to_view(r) for r in rows]
        return TriggerListView(triggers=views, count=len(views))

    @v1.get(
        "/triggers/{name}",
        response_model=TriggerView,
        tags=["triggers"],
        dependencies=[_scope("read")],
    )
    async def v1_get_trigger(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> TriggerView:
        """Fetch one trigger by its handle (no secret).

        Tenant-scoped: a trigger under another tenant 404s (no existence leak).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        * **404** — no trigger with this name for this tenant
        """
        store: StorageProvider = request.app.state.storage
        row = await store.get_trigger(name, tenant_id=ctx.tenant_id)
        if row is None:
            raise not_found("trigger", name)
        return _trigger_to_view(row)

    @v1.delete(
        "/triggers/{name}",
        status_code=204,
        tags=["triggers"],
        dependencies=[_scope("admin")],
    )
    async def v1_delete_trigger(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Remove a trigger by its handle.

        Idempotent: deleting a non-existent trigger still returns 204. Gated
        on ``admin`` (it revokes a long-lived secret credential).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        """
        store: StorageProvider = request.app.state.storage
        await store.delete_trigger(name, tenant_id=ctx.tenant_id)
        return Response(status_code=204)

    @v1.post(
        "/triggers/{trigger_id}/events",
        response_model=RunAccepted,
        status_code=202,
        tags=["triggers"],
    )
    async def v1_fire_trigger(
        trigger_id: str,
        request: Request,
        response: Response,
    ) -> RunAccepted:
        """Fire a trigger — the endpoint the EXTERNAL system calls (ADR 017 D2).

        Authenticated by the **per-trigger secret**, NOT a normal API key:
        send ``X-Movate-Signature: sha256=<hex>`` = HMAC-SHA256 of the raw
        request body keyed by the trigger's signing key
        (``hash_secret(secret, salt)``). This is intentionally outside the
        api-key auth dependency — the external caller has no ``mvt_*`` key,
        and the secret never travels on the wire (only a body-bound HMAC).
        The raw body is the event payload (a JSON object); it is merged OVER
        the trigger's ``input_defaults`` to form the job input, and a
        ``JobKind.AGENT``/``WORKFLOW`` job is enqueued scoped to the
        **trigger's** tenant. The enqueued job is the same shape ``POST /run``
        produces, so it runs through the existing dispatch with no new branch,
        and is observable + retryable as a normal job.

        Returns **202** ``{job_id, status, deduplicated}`` (mirrors
        ``RunAccepted``).

        **Idempotency (item 23).** Send an optional
        ``X-Movate-Delivery-Id: <id>`` header (the GitHub ``X-GitHub-Delivery``
        convention) to make a delivery idempotent: a repeated delivery of the
        same id for this trigger returns the SAME ``job_id`` with
        ``deduplicated: true`` and does **not** enqueue a second job or
        re-stamp ``last_fired_at``. Auth gates first — the dedup store is only
        ever touched after the signature verifies. The id is capped at 200
        chars; an empty or over-long value is ignored (treated as absent →
        today's always-enqueue behavior). Omit the header entirely to keep the
        pre-item-23 behavior (every valid request enqueues).

        Errors:

        * **404** — unknown OR disabled trigger (we do NOT leak existence to
          an unauthenticated caller)
        * **401** — missing or invalid ``X-Movate-Signature``
        * **400** — body is present but not a JSON object
        """
        import json  # noqa: PLC0415
        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        raw_body = await request.body()

        # Resolve the trigger by its PUBLIC id (no tenant context — the caller
        # is unauthenticated). Unknown OR disabled → 404, indistinguishable,
        # so we never leak a trigger's existence to an unauthenticated caller.
        trigger = await store.get_trigger_by_id(trigger_id)
        if trigger is None or not trigger.enabled:
            raise not_found("trigger", trigger_id)

        # Per-trigger-secret auth: recompute the body-bound HMAC from the
        # stored secret_hash and constant-time compare against the presented
        # X-Movate-Signature. No normal API key is accepted here.
        presented = request.headers.get(SIGNATURE_HEADER)
        if not verify_signature(trigger, raw_body, presented):
            raise auth_required()

        # The event body becomes the job input (merged over input_defaults).
        # An empty body is allowed (→ {}); a non-object JSON body is a 400.
        if not raw_body.strip():
            event_body: dict[str, Any] = {}
        else:
            try:
                parsed = json.loads(raw_body)
            except json.JSONDecodeError:
                raise http_error(
                    ErrorCode.BAD_REQUEST,
                    status_code=400,
                    message="event body must be a JSON object",
                ) from None
            if not isinstance(parsed, dict):
                raise http_error(
                    ErrorCode.BAD_REQUEST,
                    status_code=400,
                    message="event body must be a JSON object",
                )
            event_body = parsed

        # item 23 — replay / idempotency. Read the OPTIONAL delivery id only
        # AFTER auth (above): an unauthenticated request never touches the
        # dedup store. Cap the length + reject empty so an arbitrary header
        # can't bloat storage; an unusable value is treated as absent →
        # today's always-enqueue behavior.
        raw_delivery_id = request.headers.get(DELIVERY_ID_HEADER)
        delivery_id = raw_delivery_id.strip() if raw_delivery_id else None
        if not delivery_id or len(delivery_id) > DELIVERY_ID_MAX_LEN:
            delivery_id = None

        if delivery_id is not None:
            # A prior delivery of this id for this trigger → return the SAME
            # job; do NOT enqueue again and do NOT re-stamp last_fired_at.
            prior_job_id = await store.get_trigger_delivery(trigger.trigger_id, delivery_id)
            if prior_job_id is not None:
                response.status_code = 202
                return RunAccepted(job_id=prior_job_id, status=JobStatus.QUEUED, deduplicated=True)

        job = build_triggered_job(trigger, event_body)
        await store.save_job(job)

        if delivery_id is not None:
            # Atomic INSERT-OR-IGNORE: if a concurrent duplicate delivery won
            # the race, record_trigger_delivery returns False and we prefer
            # its stored job_id (the common retry path stays exact; under a
            # true simultaneous race we may have enqueued one extra job, but
            # the response is consistent — one canonical job_id).
            recorded = await store.record_trigger_delivery(
                trigger.trigger_id, delivery_id, job.job_id
            )
            if not recorded:
                winning_job_id = await store.get_trigger_delivery(trigger.trigger_id, delivery_id)
                if winning_job_id is not None and winning_job_id != job.job_id:
                    response.status_code = 202
                    return RunAccepted(
                        job_id=winning_job_id, status=JobStatus.QUEUED, deduplicated=True
                    )

        await store.touch_trigger(trigger.trigger_id, last_fired_at=datetime.now(UTC))
        response.status_code = 202
        return RunAccepted(job_id=job.job_id, status=job.status)

    # ------------------------------------------------------------------
    # Per-tenant provider keys (BYOK, ADR 018). Each tenant manages its own
    # OpenAI/Anthropic/etc. provider key, encrypted at rest; the runtime
    # resolves it tenant-key-first at run time (shared fleet key as a
    # back-compat fallback). All endpoints are tenant-scoped off the
    # AuthContext, and the plaintext key is NEVER returned (only a masked
    # fingerprint). PUT/DELETE gate on `admin` (writing/revoking a long-lived
    # credential); GET on `read`. Additive + default-off: a tenant with no
    # key transparently uses the env-default fleet key — today's behavior.
    # ------------------------------------------------------------------
    def _provider_key_to_view(k: TenantProviderKey) -> ProviderKeyView:
        # Metadata + masked fingerprint ONLY — never the ciphertext/plaintext.
        return ProviderKeyView(
            provider=k.provider,
            fingerprint=k.fingerprint,
            created_at=k.created_at.isoformat(),
            updated_at=k.updated_at.isoformat(),
        )

    @v1.put(
        "/provider-keys/{provider}",
        response_model=ProviderKeyView,
        tags=["provider-keys"],
        dependencies=[_scope("admin")],
    )
    async def v1_set_provider_key(
        provider: str,
        body: ProviderKeySetRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProviderKeyView:
        """Set (or rotate) this tenant's own key for ``provider`` (ADR 018 BYOK).

        Encrypts the plaintext ``api_key`` at rest (Fernet, keyed by
        ``MOVATE_PROVIDER_KEY_SECRET``) and persists it scoped to the calling
        tenant. The response carries only metadata + a masked fingerprint —
        the value is **never** returned. A re-PUT rotates the key in place.

        Gated on ``admin`` (it stores a long-lived provider credential).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        * **500** — ``MOVATE_PROVIDER_KEY_SECRET`` is unset/misconfigured
          (the operator must set the encryption key before BYOK can be used)
        """
        store: StorageProvider = request.app.state.storage
        try:
            record = mint_tenant_provider_key(
                tenant_id=ctx.tenant_id,
                provider=provider,
                plaintext=body.api_key,
                created_by=ctx.api_key_id,
            )
        except ProviderKeyError as exc:
            raise http_error(
                ErrorCode.INTERNAL,
                status_code=500,
                message=str(exc),
            ) from exc
        await store.save_tenant_provider_key(record)
        return _provider_key_to_view(record)

    @v1.get(
        "/provider-keys",
        response_model=ProviderKeyListView,
        tags=["provider-keys"],
        dependencies=[_scope("read")],
    )
    async def v1_list_provider_keys(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProviderKeyListView:
        """List this tenant's configured provider keys (providers + fingerprints).

        Never returns a secret — only which providers have a key set and a
        masked fingerprint for each.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        """
        store: StorageProvider = request.app.state.storage
        rows = await store.list_tenant_provider_keys(tenant_id=ctx.tenant_id)
        views = [_provider_key_to_view(r) for r in rows]
        return ProviderKeyListView(provider_keys=views, count=len(views))

    @v1.delete(
        "/provider-keys/{provider}",
        status_code=204,
        tags=["provider-keys"],
        dependencies=[_scope("admin")],
    )
    async def v1_delete_provider_key(
        provider: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Remove this tenant's key for ``provider`` (ADR 018 BYOK).

        Idempotent: deleting a non-existent key still returns 204. After
        deletion the tenant falls back to the shared fleet key (if the
        fallback is on) on its next run. Gated on ``admin``.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        """
        store: StorageProvider = request.app.state.storage
        norm = normalize_provider(provider)
        await store.delete_tenant_provider_key(norm, tenant_id=ctx.tenant_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Canary / champion-challenger rollout (ADR 016 D3). Additive +
    # default-off. Two surfaces:
    #   * set / status / compare — manage + observe the canary. set gates on
    #     `admin` (it changes which version prod traffic hits); status +
    #     compare gate on `read`.
    #   * promote / rollback — move the champion pointer. Both gate on
    #     `admin` (ADR 013). Assisted by default; auto-promote is opt-in +
    #     eval-gated.
    # All tenant-scoped via the AuthContext. The version is the slice key —
    # NO RunRecord field is added; champion vs challenger is sliced by
    # `agent_version`.
    # ------------------------------------------------------------------
    def _canary_to_view(c: CanaryConfig) -> CanaryView:
        return CanaryView(
            agent=c.agent,
            challenger_version=c.challenger_version,
            champion_version=c.champion_version,
            weight=c.weight,
            sticky=c.sticky,
            enabled=c.enabled,
            auto_promote=c.auto_promote,
            eval_gate=c.eval_gate,
            auto_rollback=c.auto_rollback,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat(),
        )

    async def _aggregate_side(
        store: StorageProvider,
        *,
        agent: str,
        tenant_id: str,
        version: str | None,
    ) -> CanarySideView:
        """Aggregate live quality for one agent_version slice → wire view.

        Delegates the actual run/feedback aggregation to the pure
        :func:`movate.core.canary.aggregate_side` (reused by the CLI) and
        maps the resulting :class:`SideStats` to the wire shape.
        """
        stats = await aggregate_side(store, agent=agent, tenant_id=tenant_id, version=version)
        return CanarySideView(
            version=stats.version,
            run_count=stats.run_count,
            success_count=stats.success_count,
            error_count=stats.error_count,
            thumbs_up=stats.thumbs_up,
            thumbs_down=stats.thumbs_down,
            feedback_count=stats.feedback_count,
            success_rate=stats.success_rate,
            thumbs_up_rate=stats.thumbs_up_rate,
        )

    async def _confirm_version_exists(
        store: StorageProvider, agent: str, *, tenant_id: str, version: str
    ) -> bool:
        """Whether ``version`` is a published version of ``agent`` (ADR 014).

        Used before honoring a challenger / promoting a target so we never
        point traffic at a version the registry doesn't have. Falls back to
        the filesystem-scanned bundles (local serve / tests carry no registry
        row) so the same check works in every deployment.
        """
        record = await store.get_agent_bundle(agent, tenant_id=tenant_id, version=version)
        if record is not None:
            return True
        # Late read of app.state.agents (annotated for the type checker) so we
        # see the current filesystem-scanned bundles, not a stale snapshot.
        fs_agents: list[AgentBundle] = app.state.agents
        return any(b.spec.name == agent and b.spec.version == version for b in fs_agents)

    @v1.post(
        "/agents/{name}/canary",
        response_model=CanaryView,
        tags=["canary-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_set_canary(
        name: str,
        body: CanarySetRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CanaryView:
        """Set (or update) an agent's canary rollout (ADR 016 D3).

        Routes ``weight``% of prod traffic to ``challenger_version`` (0 =
        kill switch). Gated on ``admin`` (it changes which version prod
        traffic hits). Additive + default-off — until this is called, the
        agent has no canary and routes 100% to its champion.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        * **404** — ``challenger_version`` is not a published version
        * **422** — ``auto_promote`` requested without an ``eval_gate``
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        if body.auto_promote and body.eval_gate is None:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=422,
                message="auto_promote requires an eval_gate (the bar a challenger must clear)",
            )
        if not await _confirm_version_exists(
            store, name, tenant_id=ctx.tenant_id, version=body.challenger_version
        ):
            raise not_found("agent version", f"{name}@{body.challenger_version}")
        now = datetime.now(UTC)
        existing = await store.get_canary_config(name, tenant_id=ctx.tenant_id)
        config = CanaryConfig(
            tenant_id=ctx.tenant_id,
            agent=name,
            challenger_version=body.challenger_version,
            champion_version=body.champion_version,
            weight=body.weight,
            sticky=body.sticky,
            enabled=body.enabled,
            auto_promote=body.auto_promote,
            eval_gate=body.eval_gate,
            auto_rollback=body.auto_rollback,
            created_by=ctx.api_key_id,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        await store.save_canary_config(config)
        return _canary_to_view(config)

    @v1.get(
        "/agents/{name}/canary",
        response_model=CanaryView,
        tags=["canary-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_canary(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CanaryView:
        """Fetch an agent's canary config (status).

        Tenant-scoped: a canary under another tenant 404s. 404 also when the
        agent simply has no canary (the default-off state).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        * **404** — no canary for this agent/tenant
        """
        store: StorageProvider = request.app.state.storage
        config = await store.get_canary_config(name, tenant_id=ctx.tenant_id)
        if config is None:
            raise not_found("canary", name)
        return _canary_to_view(config)

    @v1.delete(
        "/agents/{name}/canary",
        status_code=204,
        tags=["canary-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_delete_canary(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Remove an agent's canary (the kill switch's hard variant).

        Idempotent: deleting a non-existent canary still returns 204. After
        this the agent routes 100% to its champion. Gated on ``admin``.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        """
        store: StorageProvider = request.app.state.storage
        await store.delete_canary_config(name, tenant_id=ctx.tenant_id)
        return Response(status_code=204)

    @v1.get(
        "/agents/{name}/canary/compare",
        response_model=CanaryCompareView,
        tags=["canary-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_compare_canary(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        challenger: str | None = None,
        champion: str | None = None,
    ) -> CanaryCompareView:
        """Compare live quality champion-vs-challenger (ADR 016 D3).

        Aggregates the agent's runs + feedback, sliced by ``agent_version``
        (the canary slice key): run/success/error counts and 👍/👎 counts +
        rate for each side, plus the delta (challenger - champion). The
        versions come from the agent's canary config; ``?challenger=`` /
        ``?champion=`` override them (e.g. to compare two arbitrary versions
        without a config).

        The champion side, when not pinned, slices by "the latest published
        version" so a registry-latest champion is still measured.

        Live feedback + error slicing is the must-have here; an eval-based
        slice (running the eval suite per version) is a documented follow-up.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        * **422** — no challenger version (neither config nor ``?challenger=``)
        """
        store: StorageProvider = request.app.state.storage
        config = await store.get_canary_config(name, tenant_id=ctx.tenant_id)
        challenger_version = challenger or (config.challenger_version if config else None)
        if challenger_version is None:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=422,
                message=(
                    "no challenger version to compare — set a canary or pass ?challenger=<version>"
                ),
            )
        # Champion side: explicit override → config pin → registry latest.
        champion_version = champion or (config.champion_version if config else None)
        if champion_version is None:
            latest = await store.get_agent_bundle(name, tenant_id=ctx.tenant_id)
            champion_version = latest.version if latest is not None else None
        champion_side = await _aggregate_side(
            store, agent=name, tenant_id=ctx.tenant_id, version=champion_version
        )
        challenger_side = await _aggregate_side(
            store, agent=name, tenant_id=ctx.tenant_id, version=challenger_version
        )
        return CanaryCompareView(
            agent=name,
            champion=champion_side,
            challenger=challenger_side,
            success_rate_delta=challenger_side.success_rate - champion_side.success_rate,
            thumbs_up_rate_delta=challenger_side.thumbs_up_rate - champion_side.thumbs_up_rate,
            canary=_canary_to_view(config) if config else None,
        )

    @v1.post(
        "/agents/{name}/canary/promote",
        response_model=CanaryPromotedView,
        tags=["canary-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_promote_canary(
        name: str,
        body: CanaryPromoteRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CanaryPromotedView:
        """Promote a version to champion (ADR 016 D3).

        **Assisted by default** — a human calls this and the human is the
        gate. When ``auto_promote`` is requested (or enabled on the config),
        the target's measured ``thumbs_up_rate`` must clear the config's
        ``eval_gate`` or this 409s with a clear reason (fail-safe: never
        auto-ship a regression).

        Promotion updates the canary config: the promoted version becomes
        ``champion_version`` (the new served version pointer), ``weight`` →
        0 (the canary has concluded), and the prior champion is returned for
        rollback/audit. Agent versions stay immutable (ADR 014) — this moves
        a pointer in the storage-backed canary, not the registry's history.
        Gated on ``admin`` (ADR 013).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        * **404** — no canary for this agent, or the target version is not
          published
        * **409** — auto-promote requested but the eval-gate is unmet
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        config = await store.get_canary_config(name, tenant_id=ctx.tenant_id)
        if config is None:
            raise not_found("canary", name)
        target = body.to_version or config.challenger_version
        if not await _confirm_version_exists(store, name, tenant_id=ctx.tenant_id, version=target):
            raise not_found("agent version", f"{name}@{target}")

        auto = body.auto_promote or config.auto_promote
        mode = "auto" if auto else "assisted"
        if auto:
            # Eval-gate guard: the challenger's measured live quality must
            # clear the bar. A None gate is unsatisfiable (fail-safe).
            if config.eval_gate is None:
                raise conflict(
                    "auto-promote refused: no eval_gate is configured (nothing to clear)"
                )
            side = await _aggregate_side(store, agent=name, tenant_id=ctx.tenant_id, version=target)
            if side.thumbs_up_rate < config.eval_gate:
                raise conflict(
                    f"auto-promote refused: challenger thumbs-up rate "
                    f"{side.thumbs_up_rate:.3f} < eval_gate {config.eval_gate:.3f}"
                )

        previous_champion = config.champion_version
        updated = config.model_copy(
            update={
                "champion_version": target,
                "weight": 0,
                "updated_at": datetime.now(UTC),
            }
        )
        await store.save_canary_config(updated)
        # Audit the successful promotion (item 35): target is the agent +
        # promoted version; mode records assisted vs auto.
        record_audit_event(
            "canary.promote",
            actor=ctx.api_key_id,
            tenant_id=ctx.tenant_id,
            target=f"{name}@{target}",
            mode=mode,
            previous_champion=previous_champion,
        )
        # ADR 035 D1 — emit ``canary.promoted`` for the human-initiated
        # promotion. The auto-rollback path emits its own ``canary.
        # demoted`` from dispatch._maybe_auto_rollback. Fire-and-forget.
        emit_event(
            store,
            tenant_id=ctx.tenant_id,
            kind=EventKind.CANARY_PROMOTED,
            subject=name,
            data={
                "promoted_version": target,
                "previous_champion": previous_champion,
                "mode": mode,
                "actor": ctx.api_key_id,
            },
        )
        return CanaryPromotedView(
            agent=name,
            promoted_version=target,
            previous_champion=previous_champion,
            mode=mode,
            canary=_canary_to_view(updated),
        )

    @v1.post(
        "/agents/{name}/canary/rollback",
        response_model=CanaryPromotedView,
        tags=["canary-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_rollback_canary(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CanaryPromotedView:
        """Roll back the champion to the prior recorded champion (instant).

        The inverse of promote: it sets ``champion_version`` back to the
        canary's currently-recorded champion pin and zeroes the weight, so
        traffic returns to the prior version immediately. Use it when a
        just-promoted challenger turns out bad. Gated on ``admin``.

        If the canary has no recorded champion pin (champion was
        registry-latest), rollback clears the pin (→ latest) and zeroes the
        weight — still an instant return to champion-by-default.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        * **404** — no canary for this agent/tenant
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        config = await store.get_canary_config(name, tenant_id=ctx.tenant_id)
        if config is None:
            raise not_found("canary", name)
        # Revert: champion pin stays the recorded champion; weight → 0 routes
        # 100% to it instantly. (Promote set champion_version = the promoted
        # version, so this re-asserts that as the served champion at 0%.)
        target = config.champion_version
        updated = config.model_copy(update={"weight": 0, "updated_at": datetime.now(UTC)})
        await store.save_canary_config(updated)
        # Audit the successful rollback (item 35).
        record_audit_event(
            "canary.rollback",
            actor=ctx.api_key_id,
            tenant_id=ctx.tenant_id,
            target=f"{name}@{target if target is not None else '<latest>'}",
        )
        # ADR 035 D1 — emit ``canary.demoted`` for the human-initiated
        # rollback (same kind as the drift-auto-rollback path; ``actor``
        # discriminates). Fire-and-forget.
        emit_event(
            store,
            tenant_id=ctx.tenant_id,
            kind=EventKind.CANARY_DEMOTED,
            subject=name,
            data={
                "challenger_version": config.challenger_version,
                "champion_version": target,
                "actor": ctx.api_key_id,
                "reason": "manual_rollback",
            },
        )
        return CanaryPromotedView(
            agent=name,
            promoted_version=target if target is not None else "<latest>",
            previous_champion=config.champion_version,
            mode="rollback",
            canary=_canary_to_view(updated),
        )

    # ------------------------------------------------------------------
    # Bench endpoints (BACKLOG #64) — multi-model comparison persistence.
    # Mirror the eval endpoints beat-for-beat: kickoff enqueues a
    # JobKind.BENCH job; the worker runs BenchEngine + persists a
    # BenchRecord; the result + list endpoints render it.
    # ------------------------------------------------------------------
    @v1.post(
        "/bench/{agent}",
        response_model=BenchAcceptedView,
        status_code=202,
        tags=["bench-v1"],
        dependencies=[_scope("eval")],
    )
    async def v1_kick_off_bench(
        agent: str,
        body: BenchSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> BenchAcceptedView:
        """Kick off a multi-model bench against an agent and persist the
        BenchRecord.

        Creates a ``JobRecord(kind=BENCH)`` and returns 202 immediately
        with ``{job_id, bench_id, status: "queued"}``. The worker process
        claims and executes the job; poll ``GET /api/v1/jobs/{job_id}``
        until terminal, then fetch the comparison from
        ``GET /api/v1/bench/{bench_id}``.

        Errors:

        * **401** — bad bearer token
        * **404** — agent not in the registry
        """
        agents: list[AgentBundle] = request.app.state.agents
        bundle = next((b for b in agents if b.spec.name == agent), None)
        if bundle is None:
            raise not_found("agent", agent)

        store: StorageProvider = request.app.state.storage

        # Pre-generate the bench_id so the caller can fetch the result
        # the moment the job completes. The worker derives the same id by
        # passing it through the job input; if absent (older worker), the
        # worker generates its own and the caller reads it off the job's
        # result_run_id instead.
        bench_id = str(uuid4())
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=JobKind.BENCH,
            target=agent,
            input={
                "bench_id": bench_id,
                "models": body.models,
                "input": body.input,
                "judge": body.judge,
                "rubric": body.rubric,
                "runs": body.runs,
                "gate_mode": body.gate_mode,
                "mock": body.mock,
            },
            api_key_id=ctx.api_key_id,
            # ADR 019: capture the originating trace so the worker continues it.
            trace_context=inject_current_trace_context(),
        )
        await store.save_job(job)
        return BenchAcceptedView(
            bench_id=bench_id,
            job_id=job.job_id,
            status="queued",
        )

    @v1.get(
        "/bench/{bench_id}",
        response_model=BenchResultView,
        tags=["bench-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_bench(
        bench_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> BenchResultView:
        """Retrieve a completed bench's comparison.

        Tenant-scoped at the storage layer (a cross-tenant id probe
        returns 404, never 403, to avoid leaking that the id exists).

        Errors:

        * **401** — bad bearer token
        * **404** — no bench record matches the id for this tenant
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_bench(bench_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("bench", bench_id)
        return _bench_record_to_view(record)

    @v1.get(
        "/bench",
        response_model=BenchListView,
        tags=["bench-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_bench(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        agent: str | None = None,
        limit: int = 20,
    ) -> BenchListView:
        """Paginated history of bench runs. Filter by ``agent=<name>``.

        Same tenant scoping as every other endpoint; limit hard-capped
        at 100.

        Errors:

        * **401** — bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_bench(
            tenant_id=ctx.tenant_id,
            agent=agent,
            limit=capped_limit,
        )
        views = [_bench_record_to_view(r) for r in records]
        return BenchListView(bench=views, count=len(views))

    # ------------------------------------------------------------------
    # Model catalog + pricing (BACKLOG #67 / #68) — read-only mirrors of
    # the ``mdk models`` / ``mdk pricing`` CLI surfaces. Static data
    # (no storage / tenant scoping) but auth-gated for consistency. The
    # catalogue is the shared movate.providers.model_catalog module — the
    # same source of truth the CLI uses (runtime never imports cli).
    # ------------------------------------------------------------------

    @v1.get(
        "/pricing",
        response_model=PricingView,
        tags=["catalog-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_pricing(
        ctx: AuthContext = Depends(auth_dep),
    ) -> PricingView:
        """Return the packaged model pricing table.

        Serialises :func:`movate.providers.pricing.load_pricing` — the
        versioned ``pricing.yaml`` MDK uses to cost every run. Per-1K-token
        units, one entry per model (sorted by model id). For per-1M-token
        prices + capability metadata use ``GET /api/v1/models`` instead.

        Errors:

        * **401** — bad / missing bearer token
        """
        from movate.providers.pricing import load_pricing  # noqa: PLC0415

        table = load_pricing()
        entries = [
            PricingEntryView(
                model_id=model_id,
                input_per_1k=price.input_per_1k,
                output_per_1k=price.output_per_1k,
                cached_input_per_1k=price.cached_input_per_1k,
            )
            for model_id, price in sorted(table.models.items())
        ]
        return PricingView(
            version=table.version,
            last_verified=table.last_verified,
            entries=entries,
            count=len(entries),
        )

    @v1.get(
        "/models",
        response_model=ModelCatalogView,
        tags=["catalog-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_models(
        ctx: AuthContext = Depends(auth_dep),
    ) -> ModelCatalogView:
        """List every model in the catalog: pricing + capabilities.

        Combines the pricing table with capability metadata (context
        window, tool-use, vision) — the same view ``mdk models list``
        renders. Sorted by ``(provider, model_id)``.

        Errors:

        * **401** — bad / missing bearer token
        """
        from movate.providers.model_catalog import model_catalog  # noqa: PLC0415

        views = [_model_info_to_view(info) for info in model_catalog()]
        return ModelCatalogView(models=views, count=len(views))

    @v1.get(
        "/models/{model_id:path}",
        response_model=ModelInfoView,
        tags=["catalog-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_model(
        model_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ModelInfoView:
        """Pricing + capabilities for one model.

        Mirrors ``mdk models show <model_id>``. ``model_id`` is the full
        LiteLLM provider string (e.g. ``anthropic/claude-sonnet-4-6``);
        the ``:path`` converter lets the embedded slash through.

        Errors:

        * **401** — bad / missing bearer token
        * **404** — model not in the catalog
        """
        from movate.providers.model_catalog import model_info  # noqa: PLC0415

        info = model_info(model_id)
        if info is None:
            raise not_found("model", model_id)
        return _model_info_to_view(info)

    # ------------------------------------------------------------------
    # Run explain (BACKLOG #66) — read-only mirror of ``mdk explain``.
    # The decision chain for a stored run, tenant-scoped at the storage
    # layer (a cross-tenant id returns 404, never 403). The record→dict
    # logic is the shared movate.core.explain.explain_run seam.
    # ------------------------------------------------------------------

    @v1.get(
        "/runs/{run_id}/explain",
        response_model=RunExplainView,
        tags=["runs-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_explain_run(
        run_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        steps: bool = False,
    ) -> RunExplainView:
        """Return the decision chain for a stored run.

        Mirrors ``mdk explain <run_id> --json``: identity + status, input,
        the LLM-call summary, output (or error), and the per-step
        ``skill_calls``. Pass ``?steps=true`` to embed the full skill-call
        breakdown; otherwise a one-line ``skill_calls_hint`` summarises the
        count.

        Tenant-scoped at the storage layer — a cross-tenant id returns 404
        (never 403), so the existence of another tenant's run never leaks.

        Errors:

        * **401** — bad / missing bearer token
        * **404** — no run matches the id for this tenant
        """
        from movate.core.explain import explain_run  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        record = await store.get_run(run_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("run", run_id)
        chain = explain_run(record, steps=steps)
        return RunExplainView(
            run_id=chain["run_id"],
            agent=chain["agent"],
            agent_version=chain["agent_version"],
            status=chain["status"],
            input=chain["input"],
            llm_call=RunExplainLlmCallView(**chain["llm_call"]),
            output=chain["output"],
            error=chain["error"],
            skill_calls=chain.get("skill_calls"),
            skill_calls_hint=chain.get("skill_calls_hint"),
        )

    # ------------------------------------------------------------------
    # Workflow definitions (ADR 037 D1) — workflow API parity.
    #
    # Workflow analogue of the agent CRUD/version/publish/revert surface
    # above. The wire shape mirrors agents row-for-row: same scopes (read /
    # admin), same If-Match optimistic-concurrency on PUT, same versions
    # endpoint pattern, same error envelope. The bundle layout is narrower
    # (just workflow.yaml + state schema + optional evals/dataset), so the
    # multipart form has fewer fields, and validation runs the Pydantic +
    # compiler path rather than the prompt linter. This is the runtime
    # surface backing the front end's workflow profile / catalog views.
    # ------------------------------------------------------------------

    @v1.post(
        "/workflows",
        response_model=WorkflowCreatedView,
        status_code=201,
        tags=["workflows-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_workflow(
        request: Request,
        # Multipart fields. The route accepts EITHER individual files OR a
        # single zipped bundle; the choice is detected in the handler. A
        # pure-JSON sibling endpoint at ``/workflows/from-spec`` covers
        # callers that don't want multipart encoding (mirrors agents'
        # ``POST /api/v1/agents/from-wizard``).
        workflow_yaml: UploadFile | None = File(default=None),
        state_schema: UploadFile | None = File(default=None),
        dataset: UploadFile | None = File(default=None),
        bundle: UploadFile | None = File(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowCreatedView:
        """Create a new workflow from a multipart-form bundle.

        Mirrors ``POST /api/v1/agents``: two multipart input modes (pick ONE):

        1. **Individual multipart files** — ``workflow_yaml`` + optional
           ``state_schema`` + optional ``dataset``.
        2. **Zipped bundle** — single ``bundle`` field carrying the canonical
           workflow layout (workflow.yaml + schema/ + evals/).

        For pure-JSON callers, see ``POST /api/v1/workflows/from-spec``.

        Persists to ``<workflows_path>/<name>/`` and writes an immutable row
        to the durable registry (ADR 037 D1).

        Errors:

        * **400** — neither mode supplied OR multiple modes supplied
        * **409** — workflow with this name already exists; use PUT to update
        * **422** — bundle failed validation (parse / Pydantic / compiler)
        * **503** — runtime was built without a ``workflows_path``
        """
        workflows_path: Path | None = request.app.state.workflows_path
        if workflows_path is None:
            raise WorkflowPersistenceError(
                "runtime was built without a workflows_path; POST /api/v1/workflows is unavailable",
                status_code=503,
            )

        files = await _collect_workflow_bundle_files(
            body=None,
            workflow_yaml=workflow_yaml,
            state_schema=state_schema,
            dataset=dataset,
            bundle=bundle,
        )

        result = persist_workflow_bundle(files, workflows_path=workflows_path)

        store: StorageProvider = request.app.state.storage
        published = await _dual_write_workflow_to_registry(
            store,
            result.workflow_dir,
            tenant_id=ctx.tenant_id,
            version=result.spec.version,
            created_by=ctx.api_key_id,
        )

        return WorkflowCreatedView(
            name=result.spec.name,
            version=result.spec.version,
            description=result.spec.description,
            workflow_dir=result.workflow_dir.name,
            files_persisted=result.files_persisted,
            published_version=published.version if published is not None else None,
            changed=published.published if published is not None else True,
        )

    @v1.post(
        "/workflows/from-spec",
        response_model=WorkflowCreatedView,
        status_code=201,
        tags=["workflows-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_workflow_from_spec(
        body: WorkflowCreateRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowCreatedView:
        """Create a new workflow from a JSON body (no multipart).

        Sibling of the multipart ``POST /api/v1/workflows`` — same persist
        path + response shape, JSON wire format. Mirrors the role
        ``POST /api/v1/agents/from-wizard`` plays for agents: a clean
        no-multipart shape for Angular and other JSON-only clients.
        """
        workflows_path: Path | None = request.app.state.workflows_path
        if workflows_path is None:
            raise WorkflowPersistenceError(
                "runtime was built without a workflows_path; "
                "POST /api/v1/workflows/from-spec is unavailable",
                status_code=503,
            )

        files = await _collect_workflow_bundle_files(
            body=body,
            workflow_yaml=None,
            state_schema=None,
            dataset=None,
            bundle=None,
        )

        result = persist_workflow_bundle(files, workflows_path=workflows_path)

        store: StorageProvider = request.app.state.storage
        published = await _dual_write_workflow_to_registry(
            store,
            result.workflow_dir,
            tenant_id=ctx.tenant_id,
            version=result.spec.version,
            created_by=ctx.api_key_id,
        )

        return WorkflowCreatedView(
            name=result.spec.name,
            version=result.spec.version,
            description=result.spec.description,
            workflow_dir=result.workflow_dir.name,
            files_persisted=result.files_persisted,
            published_version=published.version if published is not None else None,
            changed=published.published if published is not None else True,
        )

    @v1.get(
        "/workflows",
        response_model=WorkflowListResponse,
        tags=["workflows-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_workflows(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        published_only: bool = False,
        limit: int = 100,
    ) -> WorkflowListResponse:
        """List workflows for the caller's tenant, newest-first.

        ``?published_only=true`` narrows to workflows that have at least one
        published version (ADR 037 D1). The returned row is still the
        *latest* version of each name — the ``published_version`` field
        on each item names the version flagged as published, so a UI can
        render "blessed v0.2.0 (latest v0.3.0)" drift without a second
        round-trip.

        Tenant-scoped — other tenants' workflows are invisible.
        """
        store: StorageProvider = request.app.state.storage
        rows = await store.list_workflows(
            tenant_id=ctx.tenant_id,
            published_only=published_only,
            limit=limit,
        )
        items: list[WorkflowView] = []
        for row in rows:
            tags, description = _peek_workflow_yaml_tags(row.files)
            # Look up the published version (if any) for this name. One extra
            # cheap query per row — keeps list_workflows' API minimal.
            published_version = await _resolve_published_version(
                store, row.name, tenant_id=ctx.tenant_id
            )
            items.append(
                WorkflowView(
                    name=row.name,
                    version=row.version,
                    description=description,
                    published_version=published_version,
                    tags=tags,
                    created_at=row.created_at,
                )
            )
        return WorkflowListResponse(workflows=items, count=len(items))

    @v1.get(
        "/workflows/{name}",
        response_model=WorkflowDetailView,
        tags=["workflows-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_workflow(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        version: str | None = None,
    ) -> WorkflowDetailView:
        """Return the parsed spec + bundle metadata for a single workflow.

        ``?version=<v>`` returns that exact registry version (404 if not
        found for this tenant); omitting ``?version`` returns the current
        latest. Tenant-scoped — a cross-tenant lookup is 404, not 403.

        Sets a strong ``ETag`` carrying the current version so an Angular
        client can pass it as ``If-Match`` on a subsequent PUT for
        optimistic concurrency (mirrors the agent endpoint).
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_workflow_bundle(name, tenant_id=ctx.tenant_id, version=version)
        if record is None:
            raise not_found(
                "workflow",
                f"{name}@{version}" if version else name,
            )
        return _render_workflow_detail(record)

    @v1.get(
        "/workflows/{name}/versions",
        response_model=WorkflowVersionsView,
        tags=["workflows-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_workflow_versions(
        name: str,
        request: Request,
        limit: int = 50,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowVersionsView:
        """List the durable-registry version history for one workflow.

        Newest-first. The newest row is flagged ``is_current``; the version
        with ``published=True`` (if any) is flagged ``is_published``.
        Mirrors :func:`v1_list_agent_versions`.
        """
        store: StorageProvider = request.app.state.storage
        records = await store.list_workflow_versions(name, tenant_id=ctx.tenant_id, limit=limit)
        items = [
            WorkflowVersionView(
                version=r.version,
                created_by=r.created_by,
                created_at=r.created_at,
                content_hash=r.content_hash,
                is_current=(i == 0),
                is_published=r.published,
            )
            for i, r in enumerate(records)
        ]
        return WorkflowVersionsView(name=name, versions=items, count=len(items))

    @v1.put(
        "/workflows/{name}",
        response_model=WorkflowUpdatedView,
        tags=["workflows-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_update_workflow(
        name: str,
        request: Request,
        workflow_yaml: UploadFile | None = File(default=None),
        state_schema: UploadFile | None = File(default=None),
        dataset: UploadFile | None = File(default=None),
        bundle: UploadFile | None = File(default=None),
        if_match: str | None = Header(default=None, alias="If-Match"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowUpdatedView:
        """Replace an existing workflow bundle in-place (multipart).

        Accepts the same multipart modes as POST. The path ``{name}`` must
        match the uploaded ``workflow.yaml``'s ``name`` field; mismatches
        are rejected with 422. For JSON-body callers see
        ``PUT /api/v1/workflows/{name}/from-spec``.

        **Optimistic concurrency** — opt-in via ``If-Match``. Send the
        version or ``content_hash`` the caller believes is current; if it
        no longer matches the registry's latest for this tenant, 409
        ("someone else updated this; re-fetch"). Absent ``If-Match`` →
        last-write-wins (back-compat).

        Errors:

        * **400** — neither mode supplied OR multiple modes supplied
        * **404** — workflow ``{name}`` is not registered (never created)
        * **409** — ``If-Match`` precondition is stale
        * **422** — bundle failed validation OR workflow_yaml.name ≠ path
        * **503** — runtime built without a ``workflows_path``
        """
        return await _do_update_workflow(
            request=request,
            name=name,
            body=None,
            workflow_yaml=workflow_yaml,
            state_schema=state_schema,
            dataset=dataset,
            bundle=bundle,
            if_match=if_match,
            ctx=ctx,
        )

    @v1.put(
        "/workflows/{name}/from-spec",
        response_model=WorkflowUpdatedView,
        tags=["workflows-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_update_workflow_from_spec(
        name: str,
        body: WorkflowCreateRequest,
        request: Request,
        if_match: str | None = Header(default=None, alias="If-Match"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowUpdatedView:
        """Replace an existing workflow from a JSON body (no multipart).

        Sibling of the multipart ``PUT /api/v1/workflows/{name}``. Same
        If-Match semantics, same 404/409/422 behavior.
        """
        return await _do_update_workflow(
            request=request,
            name=name,
            body=body,
            workflow_yaml=None,
            state_schema=None,
            dataset=None,
            bundle=None,
            if_match=if_match,
            ctx=ctx,
        )

    @v1.delete(
        "/workflows/{name}",
        response_model=WorkflowDeletedView,
        tags=["workflows-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_delete_workflow(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowDeletedView:
        """Soft-delete a workflow.

        Moves the on-disk bundle to ``.deleted-<name>-<timestamp>/`` (no
        rmtree — recoverable out-of-band) AND clears the registry's
        ``published`` flag from every version of this name. Versions
        themselves are PRESERVED — the history stays intact so an operator
        can revert later if the delete was a mistake.

        Errors:

        * **404** — workflow dir doesn't exist at the runtime's
          workflows_path AND no registry rows match
        * **503** — runtime built without a ``workflows_path``
        """
        workflows_path: Path | None = request.app.state.workflows_path
        if workflows_path is None:
            raise WorkflowPersistenceError(
                "runtime was built without a workflows_path; "
                "DELETE /api/v1/workflows/{name} is unavailable",
                status_code=503,
            )
        store: StorageProvider = request.app.state.storage
        # Soft-delete on disk if the dir exists. If only the registry has it,
        # we still clear the published flag below so the catalog reflects
        # the delete.
        deleted_dir_name = ""
        on_disk = (workflows_path / name).is_dir()
        if on_disk:
            result = soft_delete_workflow(name, workflows_path=workflows_path)
            deleted_dir_name = result.deleted_dir.name
        else:
            # Registry-only delete: 404 if nothing exists for this tenant.
            registry_versions = await store.list_workflow_versions(
                name, tenant_id=ctx.tenant_id, limit=1
            )
            if not registry_versions:
                raise not_found("workflow", name)
            deleted_dir_name = f"<registry-only:{name}>"

        # Demote any published version so the front-end catalog
        # (?published_only=true) stops surfacing this name immediately.
        # Versions are preserved — operators can revert.
        await _demote_workflow_published(store, name, tenant_id=ctx.tenant_id)

        return WorkflowDeletedView(
            name=name,
            deleted_dir=deleted_dir_name,
        )

    @v1.post(
        "/workflows/{name}/validate",
        response_model=WorkflowValidationView,
        tags=["workflows-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_validate_workflow(
        name: str,
        request: Request,
        workflow_yaml: UploadFile | None = File(default=None),
        state_schema: UploadFile | None = File(default=None),
        bundle: UploadFile | None = File(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowValidationView:
        """Validate a workflow bundle WITHOUT persisting (multipart).

        Runs the same Pydantic + compiler path persist would run — duplicate
        node ids, unknown entrypoint, dangling edges, missing/malformed
        state schema. Returns ``passed=True`` when the spec is structurally
        valid. Cost forecast is intentionally omitted today (workflows have
        no single-token estimate; ADR 029 D4 will add this).

        If no multipart file is supplied, validates the currently-persisted
        bundle on disk (so the UI can pre-validate the latest registered
        workflow before opening the editor). For JSON-body validation see
        ``POST /api/v1/workflows/{name}/validate/from-spec``.

        ``read`` scope — pure inspection, no mutation.
        """
        return await _do_validate_workflow(
            request=request,
            name=name,
            body=None,
            workflow_yaml=workflow_yaml,
            state_schema=state_schema,
            bundle=bundle,
            ctx=ctx,
        )

    @v1.post(
        "/workflows/{name}/validate/from-spec",
        response_model=WorkflowValidationView,
        tags=["workflows-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_validate_workflow_from_spec(
        name: str,
        body: WorkflowCreateRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowValidationView:
        """Validate a workflow bundle from a JSON body (no multipart)."""
        return await _do_validate_workflow(
            request=request,
            name=name,
            body=body,
            workflow_yaml=None,
            state_schema=None,
            bundle=None,
            ctx=ctx,
        )

    @v1.post(
        "/workflows/{name}/publish",
        response_model=WorkflowPublishedView,
        tags=["workflows-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_publish_workflow(
        name: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        version: str | None = None,
    ) -> WorkflowPublishedView:
        """Promote a version to "published" (ADR 037 D1).

        Soft promote on the durable registry: sets ``published=True`` on
        the target version and clears every other version of the same name
        in this tenant. ``?version=<v>`` selects the version; omit it to
        publish the current LATEST (newest ``created_at``).

        Idempotent — re-promoting the same version is a no-op. The
        response carries the prior published version (if any) so the UI
        can label the transition.

        Errors:

        * **404** — no such version for this workflow in this tenant
        """
        store: StorageProvider = request.app.state.storage
        target_version = version
        # Versionless → promote the current latest.
        if target_version is None:
            latest = await store.get_workflow_bundle(name, tenant_id=ctx.tenant_id)
            if latest is None:
                raise not_found("workflow", name)
            target_version = latest.version

        # Look up the previously-published version BEFORE we flip it.
        previous_published = await _resolve_published_version(store, name, tenant_id=ctx.tenant_id)
        ok = await store.publish_workflow_version(
            name, tenant_id=ctx.tenant_id, version=target_version
        )
        if not ok:
            raise not_found("workflow version", f"{name}@{target_version}")
        return WorkflowPublishedView(
            name=name,
            published_version=target_version,
            previous_published_version=(
                previous_published if previous_published != target_version else None
            ),
        )

    @v1.post(
        "/workflows/{name}/revert",
        response_model=WorkflowRevertedView,
        tags=["workflows-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_revert_workflow(
        name: str,
        request: Request,
        body: WorkflowRevertSubmission | None = None,
        to_version: str | None = None,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WorkflowRevertedView:
        """Revert a workflow to a prior version (non-destructive).

        Fetches the bundle for ``to_version`` and re-publishes it FORWARD
        as a new latest version — a fresh ``save_workflow_bundle`` row
        with a new ``created_at`` / ``created_by`` and the same ``files``.
        No version is ever deleted or rewritten.

        ``to_version`` may be supplied in the JSON body OR as a query
        param; the body wins when both are present. Mirrors the agent
        revert endpoint.

        Errors:

        * **400** — no ``to_version`` supplied
        * **404** — no such ``to_version`` for this workflow in this tenant
        """
        target_version = body.to_version if body is not None else to_version
        if not target_version:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=400,
                message=(
                    'revert requires a target version: send {"to_version": "..."} '
                    "in the body or ?to_version=..."
                ),
            )
        store: StorageProvider = request.app.state.storage
        target = await store.get_workflow_bundle(
            name, tenant_id=ctx.tenant_id, version=target_version
        )
        if target is None:
            raise not_found("workflow version", f"{name}@{target_version}")

        history = await store.list_workflow_versions(name, tenant_id=ctx.tenant_id, limit=1000)
        previous_version = history[0].version if history else target_version
        existing_versions = {r.version for r in history}
        new_version = mint_workflow_revert_version(target_version, existing_versions)

        reverted = WorkflowBundleRecord(
            name=target.name,
            tenant_id=target.tenant_id,
            version=new_version,
            created_by=ctx.api_key_id,
            content_hash=target.content_hash,
            files=target.files,
            published=False,
        )
        await store.save_workflow_bundle(reverted)
        return WorkflowRevertedView(
            name=name,
            version=new_version,
            reverted_from=target_version,
            previous_version=previous_version,
        )

    # ------------------------------------------------------------------
    # Workflow HITL — resume-on-signal (ADR 017 D5, PR 2).
    #
    # A paused workflow run (the runner stopped at a HUMAN gate and
    # persisted a durable PAUSED checkpoint in PR 1) is resumed when an
    # authenticated operator signals their decision. Control vs execution
    # plane: the signal endpoint validates + records + ENQUEUES a
    # continuation JobKind.WORKFLOW job (carrying resume_workflow_run_id);
    # the WORKER resumes the runner from the gate's successor. The endpoint
    # never runs the workflow inline. Idempotent: flipping the record out of
    # PAUSED means a second signal 409s (no double-resume).
    # ------------------------------------------------------------------

    @v1.get(
        "/workflow-runs",
        response_model=WorkflowRunListView,
        tags=["workflow-runs-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_workflow_runs(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        status: WorkflowStatus | None = None,
        limit: int = 20,
    ) -> WorkflowRunListView:
        """List this tenant's workflow runs, newest first.

        ``?status=paused`` finds runs awaiting a human signal (the HITL
        queue): each PAUSED row surfaces its ``human_task`` (prompt +
        output_contract) so an operator knows what decision to supply to
        ``POST /workflow-runs/{id}/signal``. Omit ``status`` for all states.

        Always tenant-scoped (``read`` scope); ``limit`` is hard-capped at
        100 to keep the response bounded.

        Errors:

        * **401** — missing / bad bearer token
        """
        capped_limit = max(1, min(limit, 100))
        store: StorageProvider = request.app.state.storage
        records = await store.list_workflow_runs(
            tenant_id=ctx.tenant_id,
            status=status,
            limit=capped_limit,
        )
        views = [WorkflowRunView.from_record(r) for r in records]
        return WorkflowRunListView(workflow_runs=views, count=len(views))

    @v1.post(
        "/workflow-runs/{workflow_run_id}/signal",
        response_model=RunAccepted,
        status_code=202,
        tags=["workflow-runs-v1"],
        dependencies=[_scope("run")],
    )
    async def v1_signal_workflow_run(
        workflow_run_id: str,
        body: WorkflowSignalRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunAccepted:
        """Signal a human decision to resume a paused workflow run.

        The human approver (an authenticated operator, gated on the ``run``
        scope) supplies their decision — a dict of the state keys the gate's
        ``output_contract`` requires. The endpoint:

        1. Loads the run (tenant-scoped; **404** if missing / other tenant).
        2. **409** if the run is not ``PAUSED`` (already resumed / terminal —
           idempotency: a second signal must not double-resume).
        3. **422** if the decision is missing a required ``output_contract``
           key.
        4. Merges the decision into the checkpoint's ``paused_state``
           (decision wins) and persists the run flipped OUT of ``PAUSED``
           (``paused_node_id`` carried forward as the resume target, but
           ``status`` set to ``RUNNING`` so a re-signal hits the 409 in step
           2) — the worker reads this single source of truth.
        5. Enqueues a continuation ``JobKind.WORKFLOW`` job carrying
           ``resume_workflow_run_id``; the worker resumes the runner from the
           gate's successor. Returns **202** ``{job_id, status}``.

        Control vs execution plane: the workflow is NOT run inline here — it
        is enqueued, and the worker executes it. This is the contract a Teams
        Adaptive Card button (ADR 003) would POST to in a later PR.

        Errors:

        * **401** — bad / missing bearer token
        * **404** — no paused run matches the id for this tenant
        * **409** — the run is not PAUSED (already resumed / terminal)
        * **422** — the decision omits a required output_contract key
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_workflow_run(workflow_run_id, tenant_id=ctx.tenant_id)
        if record is None:
            raise not_found("workflow_run", workflow_run_id)
        human_task = record.human_task or {}
        # Idempotency: a run is signalable only while it is PAUSED *and* not
        # already consumed. We mark a consumed checkpoint with
        # ``human_task["signaled"] = True`` rather than mutating ``status``
        # (there is no WorkflowStatus.RUNNING, and SUCCESS/ERROR are terminal
        # + wrong here). This keeps ``status == PAUSED`` + ``paused_node_id``
        # intact so the worker's ``runner.resume(graph, record)`` consumes the
        # checkpoint directly, while a SECOND signal hits this 409 (no
        # double-resume). When the worker resumes to completion / a new gate
        # it upserts a fresh record, clearing the marker for the next gate.
        if record.status is not WorkflowStatus.PAUSED or human_task.get("signaled"):
            raise conflict(
                f"workflow_run {workflow_run_id!r} is not awaiting a signal "
                f"(status={record.status.value!r}, "
                f"already_signaled={bool(human_task.get('signaled'))}) — "
                f"cannot signal (already resumed or terminal)"
            )

        # Validate the decision against the gate's output_contract: every
        # required key must be present. The contract lives on the checkpoint's
        # human_task spec captured at pause time.
        required = list(human_task.get("output_contract", []))
        missing = [k for k in required if k not in body.decision]
        if missing:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=422,
                message=(
                    f"decision is missing required output_contract key(s): "
                    f"{', '.join(sorted(missing))}"
                ),
            )

        # Merge the decision into the paused state (decision wins) and persist
        # the updated checkpoint as the single source of truth the worker
        # resumes from. ``status`` STAYS ``PAUSED`` and ``paused_node_id``
        # stays set (the worker's runner.resume needs both); the
        # ``human_task["signaled"]`` marker is what flips the run out of
        # "awaiting a signal" so a re-signal 409s.
        merged_state = {**(record.paused_state or {}), **body.decision}
        consumed_human_task = {**human_task, "signaled": True}
        resumed_record = record.model_copy(
            update={
                "paused_state": merged_state,
                "final_state": merged_state,
                "human_task": consumed_human_task,
            }
        )
        await store.save_workflow_run(resumed_record)

        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=JobKind.WORKFLOW,
            target=record.workflow,
            status=JobStatus.QUEUED,
            input={},
            api_key_id=ctx.api_key_id,
            resume_workflow_run_id=workflow_run_id,
            # ADR 019: the submit→execute workflow trace operators care about.
            # Capture the originating trace so the worker continues it.
            trace_context=inject_current_trace_context(),
        )
        await store.save_job(job)
        return RunAccepted(job_id=job.job_id, status=job.status)

    # ------------------------------------------------------------------
    # Auth key management — requires the ``admin`` scope (ADR 013 L2).
    #
    # Pre-ADR-013 these gated on the single ``scope == "fleet-admin"``
    # value. They now gate on the ``admin`` scope via ``require_scope``.
    # Back-compat: a legacy key carrying only ``fleet-admin`` resolves
    # (via ``effective_scopes``) to the full scope set — which INCLUDES
    # ``admin`` — so existing fleet keys keep managing keys unchanged.
    # Tenant isolation is still enforced: admin keys only see/manage
    # keys for their own tenant.
    # ------------------------------------------------------------------

    @v1.post(
        "/auth/keys",
        response_model=ApiKeyMintedView,
        status_code=201,
        summary="Mint a new API key for the calling tenant (admin only).",
        dependencies=[_scope("admin")],
    )
    async def v1_mint_key(
        request: Request,
        body: ApiKeyMintRequest,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ApiKeyMintedView:
        """Mint a new bearer key for the calling tenant.

        The ``full_key`` in the response is shown **once** — it cannot
        be recovered. Store it immediately in your secrets vault.

        The calling key must carry the ``admin`` scope. ``body.scopes``
        sets the new key's least-privilege grant; omit it to mint a key
        with the legacy default ``{read, run, eval}``.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks the ``admin`` scope
        * **422** — ``body.scopes`` contains an unknown scope string
        """
        # Validate requested scopes against the known set (fail-closed on
        # typos). Empty/omitted → legacy default at mint time.
        requested = body.scopes if body.scopes is not None else list(LEGACY_DEFAULT_SCOPES)
        unknown = [s for s in requested if s not in ALL_SCOPES]
        if unknown:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=400,
                message=(
                    f"unknown scope(s): {', '.join(sorted(unknown))}. "
                    f"Valid scopes: {', '.join(sorted(ALL_SCOPES))}."
                ),
            )
        store: StorageProvider = request.app.state.storage
        try:
            env = ApiKeyEnv(ctx.env)
        except ValueError:
            env = ApiKeyEnv.LIVE
        minted = mint_api_key(
            tenant_id=ctx.tenant_id,
            env=env,
            label=body.label,
            ttl_days=body.ttl_days,
            scopes=requested,
        )
        await store.save_api_key(minted.record)
        # Audit the successful mint (item 35). Never logs the key value —
        # only the new key's id as the target.
        record_audit_event(
            "api_key.mint",
            actor=ctx.api_key_id,
            tenant_id=ctx.tenant_id,
            target=minted.record.key_id,
        )
        return ApiKeyMintedView(
            key_id=minted.record.key_id,
            full_key=minted.full_key,
            tenant_id=minted.record.tenant_id,
            env=minted.record.env.value,
            label=minted.record.label,
            expires_at=minted.record.expires_at,
        )

    @v1.get(
        "/auth/keys",
        response_model=ApiKeyListView,
        summary="List active API keys for the calling tenant (admin only).",
        dependencies=[_scope("admin")],
    )
    async def v1_list_keys(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        include_revoked: bool = False,
    ) -> ApiKeyListView:
        """List API keys belonging to the calling tenant, newest first.

        Pass ``include_revoked=true`` to show revoked keys too.

        The calling key must carry the ``admin`` scope.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks the ``admin`` scope
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        records = await store.list_api_keys(
            tenant_id=ctx.tenant_id,
            include_revoked=include_revoked,
        )
        now = datetime.now(UTC)
        views = [
            ApiKeyView(
                key_id=r.key_id,
                tenant_id=r.tenant_id,
                env=r.env.value,
                label=r.label,
                created_at=r.created_at,
                last_used_at=r.last_used_at,
                expires_at=r.expires_at,
                status=(
                    "revoked"
                    if r.revoked_at is not None
                    else (
                        "expired" if r.expires_at is not None and r.expires_at < now else "active"
                    )
                ),
            )
            for r in records
        ]
        return ApiKeyListView(keys=views, count=len(views))

    @v1.delete(
        "/auth/keys/{key_id}",
        response_model=ApiKeyRevokedView,
        summary="Revoke an API key (admin only).",
        dependencies=[_scope("admin")],
    )
    async def v1_revoke_key(
        request: Request,
        key_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ApiKeyRevokedView:
        """Revoke the API key with the given ``key_id``.

        Idempotent — revoking an already-revoked key returns 200.
        Tenant-scoped: you can only revoke keys belonging to your tenant.

        The calling key must carry the ``admin`` scope.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks the ``admin`` scope
        * **404** — key not found or belongs to a different tenant
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_api_key(key_id)
        if record is None or record.tenant_id != ctx.tenant_id:
            raise not_found("api_key", key_id)
        await store.revoke_api_key(key_id, tenant_id=ctx.tenant_id)
        # Audit the successful revoke (item 35).
        record_audit_event(
            "api_key.revoke",
            actor=ctx.api_key_id,
            tenant_id=ctx.tenant_id,
            target=key_id,
        )
        return ApiKeyRevokedView(key_id=key_id)

    @v1.post(
        "/auth/keys/{key_id}/rotate",
        response_model=ApiKeyRotatedView,
        status_code=201,
        summary="Rotate an API key with a zero-downtime grace window (admin only).",
        dependencies=[_scope("admin")],
    )
    async def v1_rotate_key(
        request: Request,
        key_id: str,
        body: ApiKeyRotateRequest,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ApiKeyRotatedView:
        """Rotate the key ``key_id``: mint a successor, then start a grace
        window on the old key (ADR 013 D5).

        The successor inherits the old key's ``env``, ``scopes`` and
        ``label`` (label suffixed ``(rotated)``) — rotation never widens or
        narrows access. The old key's ``expires_at`` is set to
        ``now + grace_seconds`` (default 24h, capped at 30d), so **both**
        keys authenticate until the window lapses — zero downtime. After
        the window, only the successor works.

        The ``full_key`` in the response is shown **once** — store it now.

        The calling key must carry the ``admin`` scope.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks the ``admin`` scope
        * **404** — key not found, another tenant's, or already revoked
        """
        store: StorageProvider = request.app.state.storage
        old = await store.get_api_key(key_id)
        # 404 (not 403/409) on missing / cross-tenant / revoked — never
        # leak whether another tenant's key id exists, and a revoked key
        # is not a rotation candidate.
        if old is None or old.tenant_id != ctx.tenant_id or old.revoked_at is not None:
            raise not_found("api_key", key_id)

        grace = (
            body.grace_seconds
            if body.grace_seconds is not None
            else KEY_DEFAULT_ROTATION_GRACE_SECONDS
        )
        ttl = body.ttl_days if body.ttl_days is not None else KEY_DEFAULT_TTL_DAYS
        rotated = rotate_key_record(old, grace_seconds=grace, ttl_days=ttl)

        # Persist the successor first, THEN arm the old key's grace expiry.
        # Ordering matters: if the second write fails the worst case is a
        # spare valid successor (safe) rather than a prematurely-dead old
        # key (an outage).
        await store.save_api_key(rotated.minted.record)
        await store.set_api_key_expiry(
            old.key_id, tenant_id=ctx.tenant_id, expires_at=rotated.old_expires_at
        )
        # Audit the successful rotation (item 35): old key id → its successor.
        record_audit_event(
            "api_key.rotate",
            actor=ctx.api_key_id,
            tenant_id=ctx.tenant_id,
            target=old.key_id,
            successor_key_id=rotated.minted.record.key_id,
        )
        return ApiKeyRotatedView(
            key_id=rotated.minted.record.key_id,
            full_key=rotated.minted.full_key,
            tenant_id=rotated.minted.record.tenant_id,
            env=rotated.minted.record.env.value,
            label=rotated.minted.record.label,
            expires_at=rotated.minted.record.expires_at,
            old_key_id=old.key_id,
            old_expires_at=rotated.old_expires_at,
        )

    @v1.post(
        "/auth/keys/revoke-all",
        response_model=ApiKeyBulkRevokedView,
        summary="Revoke ALL active keys for the calling tenant (admin only).",
        dependencies=[_scope("admin")],
    )
    async def v1_revoke_all_keys(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        except_key_id: str | None = None,
    ) -> ApiKeyBulkRevokedView:
        """Revoke **every** active key for the calling tenant — a
        compromise-response break-glass (ADR 013 D5).

        **Safety:** the calling key is spared by default so the operator
        isn't instantly locked out (they keep a working key to mint
        replacements). Pass ``except_key_id`` to spare a *different* key
        instead (e.g. a CI key you trust); it overrides the auto-spare of
        the caller's own key.

        Tenant-scoped: only the caller's tenant's keys are touched. Returns
        the count revoked and which key was spared.

        The calling key must carry the ``admin`` scope.

        Errors:

        * **401** — bad or missing bearer token
        * **403** — authenticated but key lacks the ``admin`` scope
        """
        store: StorageProvider = request.app.state.storage
        # Default safety: spare the caller's own key. An explicit
        # ``except_key_id`` overrides (operator chooses which to keep).
        spared = except_key_id if except_key_id is not None else ctx.api_key_id
        count = await store.revoke_all_api_keys(tenant_id=ctx.tenant_id, except_key_id=spared)
        return ApiKeyBulkRevokedView(revoked_count=count, spared_key_id=spared)

    @v1.get(
        "/auth/me",
        response_model=AuthWhoamiView,
        summary="Return the identity of the calling API key.",
    )
    async def v1_auth_whoami(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AuthWhoamiView:
        """Return identity of the calling bearer key: key_id, tenant, env, scope, expiry.

        Useful for CLI ``mdk auth whoami`` and for operators to verify
        which key they are authenticating with before minting new ones.

        Errors:

        * **401** — bad or missing bearer token
        """
        store: StorageProvider = request.app.state.storage
        record = await store.get_api_key(ctx.api_key_id)
        return AuthWhoamiView(
            key_id=ctx.api_key_id,
            tenant_id=ctx.tenant_id,
            env=ctx.env,
            scope=ctx.scope,
            scopes=sorted(ctx.scopes),
            label=record.label if record is not None else None,
            expires_at=record.expires_at if record is not None else None,
        )

    # ------------------------------------------------------------------
    # Conversation thread management (Tier 10.5, PR-O). The MESSAGES
    # endpoint that creates a threaded run lives in PR-Q (needs worker
    # thread_id propagation); these endpoints handle the create/get/list
    # management half. Used by the Chainlit playground thread-aware
    # mode (PR-P) + the Mova iO Angular console's thread browser.
    # ------------------------------------------------------------------

    @v1.post(
        "/threads",
        response_model=ThreadView,
        status_code=201,
        tags=["threads-v1"],
        dependencies=[_scope("run")],
    )
    async def v1_create_thread(
        body: ThreadCreateSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ThreadView:
        """Open a new multi-turn conversation with one agent.

        Returns the freshly-minted thread with a new ``thread_id``
        (URL-safe hex uuid). Clients store this id + send subsequent
        messages via ``POST /api/v1/threads/{id}/messages``
        (endpoint lands in PR-Q).

        Threads are bound to ONE agent — the operator picks at
        creation time and can't swap mid-thread. To target a different
        agent, open a new thread.

        Errors:

        * **401** — missing / bad bearer token
        * **422** — invalid body (missing ``agent``, oversize ``title``)
        """
        from movate.core.models import ConversationThread  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        thread = ConversationThread(
            thread_id=uuid4().hex,
            tenant_id=ctx.tenant_id,
            agent=body.agent,
            title=body.title,
        )
        await store.save_conversation_thread(thread)
        return ThreadView.from_record(thread)

    @v1.get(
        "/threads",
        response_model=ThreadListView,
        tags=["threads-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_threads(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        agent: str | None = None,
        limit: int = 100,
    ) -> ThreadListView:
        """List threads for the authenticated tenant, ordered
        ``updated_at DESC`` (most recently active first).

        Query params:

        * ``?agent=<name>`` — scope to one agent's threads (typical
          Chainlit case: the picker is per-agent).
        * ``?limit=N`` — cap on returned rows (default 100, no hard
          maximum at this tier — the storage layer's internal cap
          protects against runaway).
        """
        store: StorageProvider = request.app.state.storage
        rows = await store.list_conversation_threads(
            tenant_id=ctx.tenant_id,
            agent=agent,
            limit=int(limit),
        )
        views = [ThreadView.from_record(r) for r in rows]
        return ThreadListView(threads=views, count=len(views))

    @v1.get(
        "/threads/{thread_id}",
        response_model=ThreadView,
        tags=["threads-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_thread(
        thread_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        include_runs: bool = True,
        runs_limit: int = 100,
    ) -> ThreadView:
        """Get a thread by id with optional chronological run history.

        When ``include_runs=true`` (the default), the response includes
        a ``runs`` array sorted ASC by ``created_at`` — earliest turn
        first so clients can render the conversation top-to-bottom.
        Set ``include_runs=false`` for clients that just want the
        thread metadata (saves the history scan).

        Errors:

        * **401** — missing / bad bearer token
        * **404** — thread doesn't exist OR belongs to a different
          tenant (the 404 NEVER leaks cross-tenant existence — same
          contract as ``GET /runs/{id}`` and ``GET /jobs/{id}``)
        """
        store: StorageProvider = request.app.state.storage
        thread = await store.get_conversation_thread(thread_id, tenant_id=ctx.tenant_id)
        if thread is None:
            raise not_found("thread", thread_id)

        runs_view: list[RunView] | None = None
        if include_runs:
            run_records = await store.list_runs_for_thread(
                thread_id, tenant_id=ctx.tenant_id, limit=int(runs_limit)
            )
            runs_view = [RunView.from_record(r) for r in run_records]
        return ThreadView.from_record(thread, runs=runs_view)

    @v1.delete(
        "/threads/{thread_id}",
        status_code=204,
        tags=["threads-v1"],
        dependencies=[_scope("run")],
    )
    async def v1_delete_thread(
        thread_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Hard-delete a thread by id.

        Returns 204 No Content on success. Tenant-scoped: a thread
        belonging to a different tenant returns 404 (NEVER 403 —
        matches the contract on every other thread endpoint, never
        confirms cross-tenant existence).

        Runs that previously referenced the thread stay in storage
        (the operator deleting a thread expresses "I don't want to
        see this conversation anymore", not "nuke the run records").
        Their ``thread_id`` column becomes a dangling reference —
        harmless because ``GET /api/v1/threads/{id}`` returns 404
        for the deleted thread and ``list_runs_for_thread`` only
        runs when the operator explicitly queries by an id.

        Errors:

        * **401** — missing / bad bearer token
        * **404** — thread doesn't exist OR belongs to a different tenant
        """
        store: StorageProvider = request.app.state.storage
        deleted = await store.delete_conversation_thread(thread_id, tenant_id=ctx.tenant_id)
        if not deleted:
            raise not_found("thread", thread_id)
        # FastAPI emits an empty body when status_code=204 + the
        # handler returns a Response; explicit return keeps the
        # type contract clean.
        return Response(status_code=204)

    @v1.post(
        "/threads/{thread_id}/messages",
        response_model=RunAccepted,
        status_code=202,
        tags=["threads-v1"],
        dependencies=[_scope("run")],
    )
    async def v1_thread_submit_message(
        thread_id: str,
        body: ThreadMessageSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunAccepted:
        """Submit a new message in the context of an existing thread.

        Equivalent to ``POST /run`` but with the resulting JobRecord
        carrying the thread linkage. The worker propagates
        ``job.thread_id`` onto the spawned RunRecord
        (``dispatch.py``) so the run shows up in
        ``GET /api/v1/threads/{id}``'s history.

        Also refreshes the thread's ``updated_at`` so it floats to the
        top of the operator's "recent conversations" list.

        Returns ``202 Accepted`` with ``job_id`` — same polling
        protocol as ``POST /run``. Clients poll ``/jobs/{id}`` until
        terminal, then fetch the run via ``GET /runs/{id}`` OR
        ``GET /api/v1/threads/{id}`` (the run now appears in the
        thread's history once it lands).

        Errors:

        * **401** — missing / bad bearer token
        * **404** — thread doesn't exist OR belongs to a different
          tenant (the 404 NEVER leaks cross-tenant existence;
          same contract as ``GET /api/v1/threads/{id}``)
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        # Tenant-scoped lookup — cross-tenant returns None → 404.
        thread = await store.get_conversation_thread(thread_id, tenant_id=ctx.tenant_id)
        if thread is None:
            raise not_found("thread", thread_id)

        # Inject prior conversation turns into the input dict so the
        # agent's prompt template can render them via
        # ``{{ input.conversation_history }}``. Agents that don't
        # reference the field ignore it (Jinja's StrictUndefined fires
        # only when an *unused* template variable is missing AND
        # referenced — here we ADD a variable the schema doesn't
        # know about, which the templating layer tolerates).
        #
        # The pre-existing key wins on collision: if the caller
        # supplies their own ``conversation_history``, we don't
        # overwrite it. Lets advanced operators pre-format the
        # history (e.g. summarize older turns) before submission.
        #
        # PR-W: per-agent overrides on the thread-history caps. The
        # agent's ``retrieval.history_turns`` + ``history_char_budget``
        # let operators dial budgets per agent (verbose-turn threads
        # get more; FAQ agents save tokens). Falls back to the
        # process-wide defaults when the agent doesn't set them OR
        # when the runtime can't find the bundle (e.g. an agent
        # that landed on storage but not the registry yet).
        history_turns = _THREAD_HISTORY_TURNS
        history_char_budget = _THREAD_HISTORY_CHAR_BUDGET
        history_summarize = False
        agents: list[AgentBundle] = request.app.state.agents
        for bundle in agents:
            if bundle.spec.name == thread.agent:
                cfg = bundle.spec.retrieval
                if cfg.history_turns is not None:
                    history_turns = cfg.history_turns
                if cfg.history_char_budget is not None:
                    history_char_budget = cfg.history_char_budget
                history_summarize = cfg.history_summarize
                break

        # Bug fix (CI-caught from PR-W): list_runs_for_thread returns
        # ASC by created_at, so a small LIMIT here would return the
        # OLDEST N turns. We want the MOST RECENT N. Fetch a wide
        # window + slice [-history_turns:] — matches operator expectation
        # of "show me the last 20 turns of context", not "show me the
        # first 20 turns the thread ever had".
        prior_runs_all = await store.list_runs_for_thread(
            thread_id, tenant_id=ctx.tenant_id, limit=1000
        )
        prior_runs = prior_runs_all[-history_turns:]
        augmented_input = dict(body.input)
        if "conversation_history" not in augmented_input:
            raw_turns = [
                {
                    "input": r.input,
                    "output": r.output,
                }
                for r in prior_runs
            ]
            # PR-Z: when the agent opted into history_summarize AND
            # the raw history exceeds the char budget, replace the
            # OLDEST turns with a synthetic summary entry so the
            # agent sees the GIST of earlier context instead of
            # losing it. Falls back to raw truncation on any LLM
            # failure (the summarizer's own degraded path).
            #
            # Default path (history_summarize=False) → PR-U's raw
            # budget-aware truncation. Byte-for-byte unchanged from
            # before PR-Z for back-compat.
            applied_turns = raw_turns
            if history_summarize and raw_turns:
                import json  # noqa: PLC0415 — lazy: only paid for opt-in agents

                from movate.kb.history_summary import (  # noqa: PLC0415
                    summarize_older_turns,
                )

                total_chars = sum(len(json.dumps(t, default=str)) for t in raw_turns)
                if total_chars > history_char_budget:
                    # Keep the most recent turns whose total fits the
                    # budget; everything older gets summarized.
                    kept_chars = 0
                    keep_recent = 0
                    for t in reversed(raw_turns):
                        size = len(json.dumps(t, default=str))
                        if kept_chars + size > history_char_budget:
                            break
                        kept_chars += size
                        keep_recent += 1
                    keep_recent = max(keep_recent, 1)
                    applied_turns = await summarize_older_turns(raw_turns, keep_recent=keep_recent)
            # PR-U: budget-aware truncation — drops OLDEST turns
            # first when the raw history exceeds the char budget.
            # Most recent context survives; pathological 50KB-turn
            # threads no longer break everyone else.
            augmented_input["conversation_history"] = _apply_history_char_budget(
                applied_turns, budget=history_char_budget
            )

        # Queue the job with the thread linkage. Worker dispatch
        # (``runtime/dispatch.py``) reads ``job.thread_id`` and passes
        # it as ``thread_id`` to ``Executor.execute``, which stamps it
        # onto the spawned RunRecord.
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=JobKind.AGENT,
            target=thread.agent,
            status=JobStatus.QUEUED,
            input=augmented_input,
            api_key_id=ctx.api_key_id,
            notify_email=body.notify_email,
            thread_id=thread_id,
            # ADR 019: capture the originating trace so the worker continues it.
            trace_context=inject_current_trace_context(),
        )
        await store.save_job(job)

        # Refresh the thread's updated_at so it floats to the top of
        # the list view (sorted updated_at DESC). Preserves
        # created_at + title; just stamps the activity timestamp.
        refreshed = thread.model_copy(update={"updated_at": datetime.now(UTC)})
        await store.save_conversation_thread(refreshed)

        return RunAccepted(job_id=job.job_id, status=job.status)

    # ------------------------------------------------------------------
    # Agent catalog (ADR 041). Three namespaces (movate / private /
    # community) under one read API; tenant-private writes land in the
    # caller's tenant; the sync handler is a STUB in v1 (preserves the
    # contract for future activation against catalog.movate.io).
    # ------------------------------------------------------------------

    import logging  # noqa: PLC0415

    catalog_logger = logging.getLogger("movate.runtime.catalog")

    catalog_sync_stub_detail = (
        "Production sync from catalog.movate.io will be wired by the "
        "Movate-side service. The stub logged the intent, advanced the "
        "watermark, and returned 202 — no upstream fetch occurred."
    )

    def _parse_catalog_source(value: str) -> CatalogSource:
        try:
            return CatalogSource(value)
        except ValueError:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=400,
                message=(
                    f"unknown catalog source {value!r}; expected one of movate/private/community"
                ),
            ) from None

    def _decode_bundle_tar(b64: str) -> bytes:
        try:
            return base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=400,
                message=f"bundle_tar_b64 is not valid base64: {exc}",
            ) from None

    def _catalog_entry_to_view(entry: CatalogEntry) -> CatalogEntryView:
        return CatalogEntryView(
            slug=entry.slug,
            source=entry.source.value,
            tenant_id=entry.tenant_id,
            latest_version=entry.latest_version,
            name=entry.name,
            title=entry.title,
            description=entry.description,
            tags=list(entry.tags),
            shape=entry.shape,
            recommended_for=entry.recommended_for,
            ratings_summary=CatalogRatingsSummaryView(
                count=entry.ratings_summary.count,
                avg=entry.ratings_summary.avg,
            ),
            popularity=entry.popularity,
            synced_at=entry.synced_at.isoformat(),
        )

    @v1.get(
        "/catalog/agents",
        response_model=CatalogEntryListResponse,
        tags=["catalog"],
        dependencies=[_scope("read")],
    )
    async def v1_list_catalog_entries(
        request: Request,
        source: str | None = Query(default=None),
        tag: str | None = Query(default=None),
        shape: str | None = Query(default=None),
        q: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        after_slug: str | None = Query(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> CatalogEntryListResponse:
        """List catalog entries visible to the caller (ADR 041 D2 / D5).

        Visibility join: all ``movate`` entries + the caller's
        ``private`` entries + (future) ``community`` (always empty in v1
        because writes to that namespace are blocked).

        Filters (all ANDed):

        * ``source`` — narrow to one namespace.
        * ``tag``    — single-tag membership match.
        * ``shape``  — exact (ADR 028 shape taxonomy).
        * ``q``      — case-insensitive substring over name / title / description.

        Pagination: pass the slug of the last entry you saw as
        ``after_slug``; ``next_after_slug`` in the response is the cursor
        for the next page (``None`` once the end is reached).

        Errors:

        * **400** — bad ``source`` value
        * **401** — bad bearer token
        """
        store: StorageProvider = request.app.state.storage
        source_filter = _parse_catalog_source(source) if source else None
        entries = await store.list_catalog_entries(
            ctx.tenant_id,
            source_filter=source_filter,
            tag_filter=tag,
            shape_filter=shape,
            q=q,
            limit=limit,
            after_slug=after_slug,
        )
        views = [_catalog_entry_to_view(e) for e in entries]
        next_cursor = views[-1].slug if len(views) >= limit else None
        return CatalogEntryListResponse(
            entries=views,
            count=len(views),
            next_after_slug=next_cursor,
        )

    @v1.get(
        "/catalog/agents/{slug}",
        response_model=CatalogEntryDetailView,
        tags=["catalog"],
        dependencies=[_scope("read")],
    )
    async def v1_get_catalog_entry(
        slug: str,
        request: Request,
        source: str = Query(default="movate"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> CatalogEntryDetailView:
        """Detail view for one catalog entry (latest version + summary).

        Pass ``?source=private`` to fetch a tenant-private entry (scoped
        to the caller). Public entries default to ``source=movate``.

        Errors:

        * **404** — no matching entry (or cross-tenant for ``private``)
        """
        store: StorageProvider = request.app.state.storage
        src = _parse_catalog_source(source)
        tenant_filter = ctx.tenant_id if src is CatalogSource.PRIVATE else None
        entry = await store.get_catalog_entry(slug, source=src, tenant_id=tenant_filter)
        if entry is None:
            raise not_found("catalog entry", f"{slug} (source={src.value})")

        latest_digest: str | None = None
        version_row = await store.get_catalog_entry_version(
            slug,
            source=src,
            version=entry.latest_version,
            tenant_id=tenant_filter,
        )
        if version_row is not None:
            latest_digest = version_row.digest

        return CatalogEntryDetailView(
            slug=entry.slug,
            source=entry.source.value,
            tenant_id=entry.tenant_id,
            latest_version=entry.latest_version,
            name=entry.name,
            title=entry.title,
            description=entry.description,
            tags=list(entry.tags),
            shape=entry.shape,
            recommended_for=entry.recommended_for,
            ratings_summary=CatalogRatingsSummaryView(
                count=entry.ratings_summary.count,
                avg=entry.ratings_summary.avg,
            ),
            popularity=entry.popularity,
            synced_at=entry.synced_at.isoformat(),
            latest_version_digest=latest_digest,
        )

    @v1.get(
        "/catalog/agents/{slug}/versions",
        response_model=list[CatalogEntryVersionView],
        tags=["catalog"],
        dependencies=[_scope("read")],
    )
    async def v1_list_catalog_versions(
        slug: str,
        request: Request,
        source: str = Query(default="movate"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> list[CatalogEntryVersionView]:
        """List versions of one catalog entry, newest-first.

        Omits ``bundle_tar_b64`` — the bytes ship only from the
        per-version endpoint to keep this list cheap to render.
        """
        store: StorageProvider = request.app.state.storage
        src = _parse_catalog_source(source)
        tenant_filter = ctx.tenant_id if src is CatalogSource.PRIVATE else None
        versions = await store.get_catalog_entry_versions(slug, source=src, tenant_id=tenant_filter)
        return [
            CatalogEntryVersionView(
                slug=v.slug,
                source=v.source.value,
                tenant_id=v.tenant_id,
                version=v.version,
                digest=v.digest,
                published_at=v.published_at.isoformat(),
                deprecated_at=(v.deprecated_at.isoformat() if v.deprecated_at else None),
                bundle_tar_b64=None,
            )
            for v in versions
        ]

    @v1.get(
        "/catalog/agents/{slug}/versions/{version}",
        response_model=CatalogEntryVersionView,
        tags=["catalog"],
        dependencies=[_scope("read")],
    )
    async def v1_get_catalog_version(
        slug: str,
        version: str,
        request: Request,
        source: str = Query(default="movate"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> CatalogEntryVersionView:
        """Fetch one specific version, including ``bundle_tar`` (base64)."""
        store: StorageProvider = request.app.state.storage
        src = _parse_catalog_source(source)
        tenant_filter = ctx.tenant_id if src is CatalogSource.PRIVATE else None
        v = await store.get_catalog_entry_version(
            slug, source=src, version=version, tenant_id=tenant_filter
        )
        if v is None:
            raise not_found("catalog entry version", f"{slug}@{version} (source={src.value})")
        return CatalogEntryVersionView(
            slug=v.slug,
            source=v.source.value,
            tenant_id=v.tenant_id,
            version=v.version,
            digest=v.digest,
            published_at=v.published_at.isoformat(),
            deprecated_at=(v.deprecated_at.isoformat() if v.deprecated_at else None),
            bundle_tar_b64=base64.b64encode(v.bundle_tar).decode("ascii"),
        )

    @v1.post(
        "/catalog/agents",
        response_model=CatalogEntryView,
        status_code=201,
        tags=["catalog"],
        dependencies=[_scope("admin")],
    )
    async def v1_submit_catalog_entry(
        body: CatalogSubmitRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CatalogEntryView:
        """Submit a tenant-private catalog entry (ADR 041 D5).

        Server forces ``source='private'`` and
        ``tenant_id=caller_tenant`` regardless of any client value — the
        public namespaces are read-only over the customer-facing API.

        Errors:

        * **400** — bad bundle base64 / missing required fields
        * **409** — slug already exists for this tenant
        """
        store: StorageProvider = request.app.state.storage
        bundle_bytes = _decode_bundle_tar(body.bundle_tar_b64)

        from datetime import UTC, datetime  # noqa: PLC0415

        existing = await store.get_catalog_entry(
            body.slug, source=CatalogSource.PRIVATE, tenant_id=ctx.tenant_id
        )
        if existing is not None:
            raise conflict(f"catalog entry {body.slug!r} already exists for this tenant")

        digest = hashlib.sha256(bundle_bytes).hexdigest()
        await store.upsert_catalog_entry_version(
            body.slug,
            source=CatalogSource.PRIVATE,
            version=body.version,
            bundle_tar=bundle_bytes,
            digest=digest,
            tenant_id=ctx.tenant_id,
        )
        entry = CatalogEntry(
            slug=body.slug,
            source=CatalogSource.PRIVATE,
            tenant_id=ctx.tenant_id,
            latest_version=body.version,
            name=body.name,
            title=body.title,
            description=body.description,
            tags=list(body.tags),
            shape=body.shape,
            recommended_for=body.recommended_for,
            ratings_summary=CatalogRatingsSummary(),
            popularity=0,
            synced_at=datetime.now(UTC),
        )
        await store.upsert_catalog_entry(entry)
        return _catalog_entry_to_view(entry)

    @v1.post(
        "/catalog/agents/{slug}/versions",
        response_model=CatalogEntryVersionView,
        status_code=201,
        tags=["catalog"],
        dependencies=[_scope("admin")],
    )
    async def v1_publish_catalog_version(
        slug: str,
        body: CatalogPublishVersionRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CatalogEntryVersionView:
        """Publish a new version of a tenant-private entry (ADR 041 D5).

        Allowed only for the caller's own tenant's private entries — the
        catalog.movate.io namespace is gated through the GitHub +
        catalog-CI path (ADR 041 D3), not this endpoint.

        Errors:

        * **400** — bad bundle base64
        * **404** — no such tenant-private entry
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        entry = await store.get_catalog_entry(
            slug, source=CatalogSource.PRIVATE, tenant_id=ctx.tenant_id
        )
        if entry is None:
            raise not_found("tenant-private catalog entry", slug)
        bundle_bytes = _decode_bundle_tar(body.bundle_tar_b64)
        digest = hashlib.sha256(bundle_bytes).hexdigest()
        record = await store.upsert_catalog_entry_version(
            slug,
            source=CatalogSource.PRIVATE,
            version=body.version,
            bundle_tar=bundle_bytes,
            digest=digest,
            tenant_id=ctx.tenant_id,
        )
        # Bump latest_version on the entry so detail reflects the new tip.
        bumped = entry.model_copy(
            update={
                "latest_version": body.version,
                "synced_at": datetime.now(UTC),
            }
        )
        await store.upsert_catalog_entry(bumped)
        return CatalogEntryVersionView(
            slug=record.slug,
            source=record.source.value,
            tenant_id=record.tenant_id,
            version=record.version,
            digest=record.digest,
            published_at=record.published_at.isoformat(),
            deprecated_at=(record.deprecated_at.isoformat() if record.deprecated_at else None),
            bundle_tar_b64=None,
        )

    @v1.post(
        "/catalog/agents/{slug}/ratings",
        response_model=CatalogRatingsSummaryView,
        tags=["catalog"],
        dependencies=[_scope("run")],
    )
    async def v1_rate_catalog_entry(
        slug: str,
        body: CatalogRatingRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CatalogRatingsSummaryView:
        """Record this tenant's rating for one catalog entry.

        Re-rating overwrites the prior row (one rating per tenant per
        entry). Returns the rolled-up summary AFTER the write — clients
        can refresh the entry card without a second read.
        """
        store: StorageProvider = request.app.state.storage
        src = _parse_catalog_source(body.source)
        if src is CatalogSource.COMMUNITY:
            # Schema-ready but the namespace has no rows in v1 — rating
            # a non-existent entry surfaces as a 501 so a future
            # community ADR can flip this on without changing the URL.
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=501,
                message=(
                    "rating the community namespace is reserved for a future "
                    "community-moderation ADR (ADR 041 D7)"
                ),
            )
        summary = await store.record_catalog_rating(
            slug,
            tenant_id=ctx.tenant_id,
            source=src,
            rating=body.rating,
            comment=body.comment,
        )
        return CatalogRatingsSummaryView(count=summary.count, avg=summary.avg)

    @v1.post(
        "/catalog/sync",
        response_model=CatalogSyncResponse,
        status_code=202,
        tags=["catalog"],
        dependencies=[_scope("admin")],
    )
    async def v1_catalog_sync(
        body: CatalogSyncRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> CatalogSyncResponse:
        """Trigger a sync from the requested catalog source.

        **v1 stub (ADR 041 D4).** The handler:

        1. Logs an "intent to sync" event so an operator can confirm the
           call landed.
        2. Advances the per-source watermark to ``now()``.
        3. Returns 202 with ``status="stub"`` + a note that the production
           wiring against ``catalog.movate.io`` is a separate Movate-side
           build.

        This preserves the API contract so the production handler can
        flip on without changing the customer-facing surface or any
        client code.

        Errors:

        * **400** — ``source='private'`` (private entries don't sync — D5)
        * **501** — ``source='community'`` (deferred — D7)
        """
        store: StorageProvider = request.app.state.storage
        src = _parse_catalog_source(body.source)
        if src is CatalogSource.PRIVATE:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=400,
                message=(
                    "tenant-private entries are not synced from any upstream "
                    "(ADR 041 D5 — data sovereignty)"
                ),
            )
        if src is CatalogSource.COMMUNITY:
            raise http_error(
                ErrorCode.BAD_REQUEST,
                status_code=501,
                message=(
                    "community-namespace sync is deferred to a future moderation ADR (ADR 041 D7)"
                ),
            )

        from datetime import UTC, datetime  # noqa: PLC0415

        prior = await store.get_catalog_sync_watermark(src)
        now = datetime.now(UTC)
        catalog_logger.info(
            "catalog.sync.intent source=%s tenant=%s prior_watermark=%s",
            src.value,
            ctx.tenant_id,
            prior.isoformat() if prior else None,
        )
        await store.set_catalog_sync_watermark(src, now)
        return CatalogSyncResponse(
            source=src.value,
            status="stub",
            watermark=now.isoformat(),
            detail=catalog_sync_stub_detail,
        )

    # ------------------------------------------------------------------
    # Observability Intelligence layer (ADR 047). Five routes:
    #   GET  /observability/insights   (read)  — the append-only insight feed
    #   GET  /observability/health     (read)  — latest health score + digest
    #   POST /observability/ask        (read)  — NL question, grounded answer
    #   POST /observability/troubleshoot (read)— symptom → root-cause narrative
    #   POST /observability/analyze    (admin) — on-demand analyst trigger
    #
    # ask/troubleshoot/insights/health are PURE READS (only save_insight +
    # the analyst write). The NL detail path runs a FIXED set of read-only,
    # parameterized query templates — the LLM never authors raw SQL (the
    # SQL-safety contract lives in movate.core.observability.query). Every
    # ask/troubleshoot answer carries evidence[] (citations mandatory).
    # ------------------------------------------------------------------

    def _build_observability_llm(*, mock: bool) -> Any:
        """Build the LLM the budget-capped NL queries use (LiteLLM default).

        ``mock=True`` uses the deterministic MockProvider (the hermetic-test
        path, mirroring the eval/bench endpoints). Returns ``None`` if no
        provider can be built — the query layer then returns a deterministic,
        still-grounded fallback answer rather than failing the request.
        """
        try:
            if mock:
                from movate.providers.mock import MockProvider  # noqa: PLC0415

                return MockProvider()
            from movate.providers.litellm import LiteLLMProvider  # noqa: PLC0415

            return LiteLLMProvider()
        except Exception:  # pragma: no cover - provider construction is cheap
            logging.getLogger(__name__).warning(
                "observability_llm_build_failed — answering without LLM", exc_info=True
            )
            return None

    @v1.get(
        "/observability/insights",
        response_model=ObservabilityInsightListView,
        tags=["observability-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_observability_insights(
        request: Request,
        project_id: str | None = Query(default=None),
        since: str | None = Query(default=None, description="ISO YYYY-MM-DD, inclusive."),
        until: str | None = Query(default=None, description="ISO YYYY-MM-DD, inclusive."),
        limit: int = Query(default=90, ge=1, le=365),
        ctx: AuthContext = Depends(auth_dep),
    ) -> ObservabilityInsightListView:
        """List this tenant's daily observability insights, newest-day-first.

        Tenant-scoped at the SQL layer (``list_insights`` requires
        ``tenant_id``). Append-only re-runs collapse to the latest row per day.
        Pure read.
        """
        from datetime import date as _date  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage

        def _parse(d: str | None) -> Any:
            if not d:
                return None
            try:
                return _date.fromisoformat(d)
            except ValueError as exc:
                raise http_error(
                    ErrorCode.BAD_REQUEST,
                    status_code=400,
                    message=f"bad date {d!r} — expected ISO YYYY-MM-DD",
                ) from exc

        rows = await store.list_insights(
            ctx.tenant_id,
            project_id=project_id,
            since=_parse(since),
            until=_parse(until),
            limit=limit,
        )
        views = [ObservabilityInsightView.from_record(r) for r in rows]
        return ObservabilityInsightListView(insights=views, count=len(views))

    @v1.get(
        "/observability/health",
        response_model=ObservabilityHealthView,
        tags=["observability-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_observability_health(
        request: Request,
        project_id: str = Query(default="default"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> ObservabilityHealthView:
        """Return the latest health score + digest for a project. Pure read.

        ``has_insight=False`` (cold start) when the analyst hasn't run yet —
        a clean 200 with nulls, not a 404 (the project may simply be new).
        """
        store: StorageProvider = request.app.state.storage
        rows = await store.list_insights(ctx.tenant_id, project_id=project_id, limit=1)
        if not rows:
            return ObservabilityHealthView(project_id=project_id, has_insight=False)
        latest = rows[0]
        return ObservabilityHealthView(
            project_id=latest.project_id,
            date=latest.date.isoformat(),
            health_score=latest.health_score,
            narrative_digest=latest.narrative_digest,
            anomaly_count=len(latest.anomalies),
            has_insight=True,
        )

    @v1.post(
        "/observability/ask",
        response_model=GroundedAnswerView,
        tags=["observability-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_observability_ask(
        body: AskRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> GroundedAnswerView:
        """Answer a natural-language observability question with citations.

        Reads the insights store (fast path) and may run BOUNDED, read-only
        parameterized query templates (the LLM picks a template + typed params
        from a CLOSED set — never raw SQL). Budget-capped. Pure read.
        """
        from movate.core.observability.query import ask as _ask  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        answer = await _ask(
            body.question,
            tenant_id=ctx.tenant_id,
            project_id=body.project_id,
            storage=store,
            llm=_build_observability_llm(mock=body.mock),
            budget_usd=body.budget_usd,
        )
        return GroundedAnswerView.from_record(answer)

    @v1.post(
        "/observability/troubleshoot",
        response_model=GroundedAnswerView,
        tags=["observability-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_observability_troubleshoot(
        body: TroubleshootRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> GroundedAnswerView:
        """Correlate deploys + drift + error clusters + recent failures into a
        root-cause narrative with evidence. Budget-capped. Pure read.
        """
        from movate.core.observability.query import troubleshoot as _troubleshoot  # noqa: PLC0415

        store: StorageProvider = request.app.state.storage
        answer = await _troubleshoot(
            body.symptom,
            body.time_window_days,
            tenant_id=ctx.tenant_id,
            project_id=body.project_id,
            storage=store,
            llm=_build_observability_llm(mock=body.mock),
            budget_usd=body.budget_usd,
        )
        return GroundedAnswerView.from_record(answer)

    @v1.post(
        "/observability/analyze",
        response_model=AnalyzeAcceptedView,
        status_code=202,
        tags=["observability-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_observability_analyze(
        body: AnalyzeRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> AnalyzeAcceptedView:
        """Enqueue an on-demand overnight-analyst run (admin scope).

        Enqueues a ``JobKind.OBSERVABILITY_ANALYZE`` job the worker picks up
        and runs (``dispatch._execute_observability_analyze``). Returns 202
        with the ``job_id`` — same async protocol as run/eval/bench submits.
        The nightly cron uses the same JobKind via a JobSchedule (a documented
        follow-up); this endpoint is the manual trigger.
        """
        store: StorageProvider = request.app.state.storage
        job_input: dict[str, Any] = {
            "project_id": body.project_id,
            "budget_usd": body.budget_usd,
        }
        if body.date:
            job_input["date"] = body.date
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=JobKind.OBSERVABILITY_ANALYZE,
            target=f"observability:{body.project_id}",
            status=JobStatus.QUEUED,
            input=job_input,
            api_key_id=ctx.api_key_id,
            trace_context=inject_current_trace_context(),
        )
        await store.save_job(job)
        return AnalyzeAcceptedView(
            job_id=job.job_id, kind=job.kind.value, project_id=body.project_id
        )

    # ------------------------------------------------------------------
    # Events outbox (ADR 035 D1) — read-only feed of lifecycle events
    # (``run.completed``, ``agent.published``, ``eval.failed``,
    # ``drift.detected``, ``canary.promoted/demoted``, ...).
    #
    # D1 surface is read-only: ``record_event`` is called by the runtime
    # edges (executor / dispatch / deploy), never by clients. D2 will
    # add webhook subscriptions that DELIVER these events; D3 will add
    # an SSE stream that PUSHES them. Both consume the same outbox.
    # ------------------------------------------------------------------

    @v1.get(
        "/events",
        response_model=EventListView,
        tags=["events-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_events(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        since: datetime | None = Query(
            None,
            description=("ISO-8601 lower bound on ``created_at`` (inclusive). Default: now - 24h."),
        ),
        until: datetime | None = Query(
            None,
            description="ISO-8601 upper bound on ``created_at`` (exclusive).",
        ),
        kind: str | None = Query(
            None,
            description=(
                "Filter to one event kind (e.g. ``run.completed``, "
                "``agent.published``, ``canary.demoted``). Exact match."
            ),
        ),
        subject: str | None = Query(
            None,
            description=(
                "Filter to one subject — the thing the event is about "
                "(agent name / run id / etc.). Exact match."
            ),
        ),
        limit: int = Query(
            200,
            ge=1,
            le=1000,
            description="Page size; capped at 1000.",
        ),
        after_id: str | None = Query(
            None,
            description=(
                "Cursor: pass the previous response's ``next_after_id`` "
                "to continue paginating in oldest-first order."
            ),
        ),
        tenant: str | None = Query(
            None,
            description=(
                "Operator override: list events for a specific tenant. "
                "Requires a ``fleet-admin`` key; ignored otherwise."
            ),
        ),
    ) -> EventListView:
        """List lifecycle events for the calling tenant (ADR 035 D1).

        Tenant-scoped by default (the caller's ``ctx.tenant_id``).
        A key with the ``fleet-admin`` scope may pass ``?tenant=<id>``
        to scope the read to a different tenant; non-fleet-admin
        callers ignore the parameter (their own tenant only — no leak).

        The default time window is the last 24 hours so a polling
        client doesn't accidentally request the full history of an
        active tenant. ``until`` defaults to "now" (unbounded). Pass
        ``since=1970-01-01T00:00:00Z`` to read the full outbox.

        Cursor pagination is **oldest-first** — consumers reading
        forward in time pass the response's ``next_after_id`` back as
        ``?after_id=`` to continue. ``next_after_id`` is populated only
        when results were truncated at ``limit``; ``None`` means the
        page is the tail.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — authenticated but key lacks the ``read`` scope
        * **422** — ``limit`` outside ``[1, 1000]`` (FastAPI handles)
        """
        store: StorageProvider = request.app.state.storage
        # Resolve tenant scope: fleet-admin may override; everyone else
        # is locked to their own tenant (silently — same 404-not-403
        # no-leak posture as cross-tenant resource lookups).
        target_tenant = ctx.tenant_id
        if tenant is not None and "fleet-admin" in ctx.scopes:
            target_tenant = tenant
        # Default window: last 24h (unless caller pinned ``since``).
        effective_since = since if since is not None else datetime.now(UTC) - timedelta(hours=24)
        records = await store.list_events(
            target_tenant,
            since=effective_since,
            until=until,
            kind=kind,
            subject=subject,
            limit=limit,
            after_id=after_id,
        )
        views = [EventView.from_record(r) for r in records]
        # Cursor: populate ``next_after_id`` only when the result hit
        # the page cap (more rows may exist). The client passes this
        # back unchanged on the next request.
        next_after_id: str | None = records[-1].id if len(records) == limit else None
        return EventListView(events=views, count=len(views), next_after_id=next_after_id)

    # ==================================================================
    # /api/v1/projects (ADR 040) — Project CRUD + membership.
    # Tenant-scoped via the existing auth middleware (``ctx.tenant_id``
    # filters every storage call); the "admin scope OR project owner
    # role" gate composes so a non-tenant-admin owner can CRUD their own
    # project (D4), while a fleet/tenant admin scope still works
    # fleet-wide.
    # ==================================================================

    project_default_name = "default"
    """Reserved per-tenant project name (ADR 040 D5). Auto-created lazily
    by storage; rejected on create + archive at the API layer (storage
    rejects archive too, defense in depth)."""

    def _principal_from_auth(ctx: AuthContext) -> str:
        """Resolve the caller's stable principal id from the auth context.

        Today's middleware surfaces a per-API-key identity (no separate
        tenant-user registry exists yet), so opaque-key calls map to
        ``api_key:<key_id>``. Project membership keys off this string —
        any future "real user" surface (OIDC sub, etc.) is additive (the
        storage layer just stores the bytes).
        """
        return f"api_key:{ctx.api_key_id}"

    async def _resolve_project_role(
        request: Request,
        ctx: AuthContext,
        project_id: str,
    ) -> ProjectMemberRole | None:
        """Return the caller's effective role on ``project_id``, or ``None``.

        Two gates compose for project mutations (ADR 040 D4):

        1. **Admin scope** (``admin`` / ``fleet-admin``) — checked at the
           endpoint via :func:`require_scope`; lets a tenant/fleet admin
           CRUD any project in their tenant.
        2. **Project owner role** — checked here per request; lets a
           non-admin who owns the project still mutate it.

        Both are accepted; either grants write access. ``None`` means
        "no membership row" — the admin-scope path can still grant
        access even when this returns ``None``.
        """
        store: StorageProvider = request.app.state.storage
        principal = _principal_from_auth(ctx)
        member = await store.get_project_member(project_id, principal)
        return member.role if member is not None else None

    def _caller_has_admin_scope(ctx: AuthContext) -> bool:
        """``True`` when the caller's resolved scopes include ``admin`` or
        ``fleet-admin`` (the latter expands to all scopes, but check both
        names so a future direct ``fleet-admin``-only key path still
        composes correctly)."""
        return "admin" in ctx.scopes or "fleet-admin" in ctx.scopes

    async def _require_project_write(
        request: Request,
        ctx: AuthContext,
        project_id: str,
    ) -> None:
        """Enforce the composed gate for project mutations.

        Admin scope OR ``owner`` project role grants access; anything
        else 403s with a clear, non-sensitive message. Called per
        endpoint after the project's existence has been confirmed (so a
        wrong-tenant project is 404, not 403 — preserves the no-leak
        contract every other tenant-scoped getter follows).
        """
        if _caller_has_admin_scope(ctx):
            return
        role = await _resolve_project_role(request, ctx, project_id)
        if role == ProjectMemberRole.OWNER:
            return
        raise http_error(
            ErrorCode.FORBIDDEN,
            status_code=403,
            message=("project mutation requires the 'admin' scope or the project 'owner' role"),
        )

    def _unprocessable(message: str) -> Any:
        """422 envelope — used for reserved-name + default-project +
        last-owner guards. The runtime's :class:`ErrorCode` has no
        dedicated "unprocessable" code, so we re-use ``BAD_REQUEST``
        with the 422 status (the standard mapping for a syntactically
        valid but semantically rejected body)."""
        return http_error(
            ErrorCode.BAD_REQUEST,
            status_code=422,
            message=message,
        )

    def _precondition_failed(message: str) -> Any:
        """412 envelope — ``If-Match`` precondition stale. Re-uses the
        ``CONFLICT`` code (the closest existing semantic) so a wire
        consumer can branch without a new code; the status code (412 vs
        409) is the distinguishing signal."""
        return http_error(
            ErrorCode.CONFLICT,
            status_code=412,
            message=message,
        )

    @v1.post(
        "/projects",
        response_model=ProjectView,
        status_code=201,
        tags=["projects-v1"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_project(
        request: Request,
        body: ProjectCreateRequest,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectView:
        """Create a project in the caller's tenant.

        ``name`` is unique within the tenant. The reserved literal
        ``"default"`` is rejected (422) — the per-tenant default project
        is auto-created by storage at first read. ``owner_principal_id``
        defaults to the caller's principal (``api_key:<key_id>`` today)
        when omitted; the API layer also writes an initial ``owner``
        member row so the requesting principal has project-level access
        from creation.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks the ``admin`` scope
        * **409** — a project with this name already exists in the tenant
        * **422** — ``name == "default"`` (reserved)
        """
        if body.name.strip().lower() == project_default_name:
            raise _unprocessable(
                "project name 'default' is reserved — the per-tenant default "
                "project is created automatically on first use"
            )
        store: StorageProvider = request.app.state.storage
        owner_principal = body.owner_principal_id or _principal_from_auth(ctx)
        project = Project(
            tenant_id=ctx.tenant_id,
            name=body.name,
            description=body.description,
            owner_principal_id=owner_principal,
        )
        try:
            created = await store.create_project(project)
        except ValueError as exc:
            raise conflict(str(exc)) from None
        # Initial owner membership: the creator (or the explicit
        # ``owner_principal_id``) gets an ``owner`` row so the
        # "non-admin owner can mutate their own project" gate has
        # something to bind to (D4). Suppress the storage layer's
        # duplicate-row ValueError — possible if a caller-supplied
        # ``owner_principal_id`` collides with the same auth principal
        # (the create is otherwise idempotent for that case).
        with contextlib.suppress(ValueError):
            await store.add_project_member(
                created.project_id,
                owner_principal,
                ProjectMemberRole.OWNER,
                added_by=_principal_from_auth(ctx),
            )
        return ProjectView.from_record(created)

    @v1.get(
        "/projects",
        response_model=ProjectListResponse,
        tags=["projects-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_projects(
        request: Request,
        include_archived: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
        after_id: str | None = Query(default=None),
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectListResponse:
        """List the caller's tenant's projects, newest-first.

        ``include_archived=true`` surfaces soft-deleted projects; the
        default hides them. ``limit`` + ``after_id`` provide stable
        keyset pagination (pass the last ``project_id`` from the prior
        page).

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks the ``read`` scope
        """
        store: StorageProvider = request.app.state.storage
        rows = await store.list_projects(
            ctx.tenant_id,
            include_archived=include_archived,
            limit=limit,
            after_id=after_id,
        )
        views = [ProjectView.from_record(p) for p in rows]
        return ProjectListResponse(projects=views, count=len(views))

    @v1.get(
        "/projects/{project_id}",
        response_model=ProjectView,
        tags=["projects-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_project(
        request: Request,
        project_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectView:
        """Project detail. Archived projects ARE returned (operators may
        want to inspect them) — filter on the listing endpoint, not here.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks the ``read`` scope
        * **404** — no such project in this tenant (same shape for
          cross-tenant misses — no existence leak)
        """
        store: StorageProvider = request.app.state.storage
        project = await store.get_project(ctx.tenant_id, project_id)
        if project is None:
            raise not_found("project", project_id)
        return ProjectView.from_record(project)

    @v1.put(
        "/projects/{project_id}",
        response_model=ProjectView,
        tags=["projects-v1"],
    )
    async def v1_update_project(
        request: Request,
        project_id: str,
        body: ProjectUpdateRequest,
        if_match: str | None = Header(default=None, alias="If-Match"),
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectView:
        """Rename / re-describe a project.

        Writes are admitted to callers with the ``admin`` scope OR the
        ``owner`` role on this specific project (ADR 040 D4 — composes,
        doesn't replace). Optional ``If-Match: "<etag>"`` opts into
        optimistic concurrency: 412 if the stored ``updated_at`` no
        longer matches; absent header → last-write-wins (back-compat).

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks both ``admin`` scope and ``owner`` role
        * **404** — no such project in this tenant
        * **409** — rename collision (the new name is taken)
        * **412** — ``If-Match`` precondition stale
        * **422** — attempting to rename to the reserved ``"default"``
        """
        store: StorageProvider = request.app.state.storage
        current = await store.get_project(ctx.tenant_id, project_id)
        if current is None:
            raise not_found("project", project_id)

        await _require_project_write(request, ctx, project_id)

        if if_match is not None:
            expected = _normalize_if_match(if_match)
            if expected != current.updated_at.isoformat():
                raise _precondition_failed(
                    f"project {project_id!r} was updated concurrently: "
                    f"If-Match {expected!r} no longer matches the current "
                    "version — re-fetch and retry",
                )

        if body.name is not None and body.name.strip().lower() == project_default_name:
            raise _unprocessable("project name 'default' is reserved and cannot be assigned")

        try:
            updated = await store.update_project(
                ctx.tenant_id,
                project_id,
                name=body.name,
                description=body.description,
            )
        except ValueError as exc:
            raise conflict(str(exc)) from None
        if updated is None:
            # Lost a race with archive / cross-tenant — re-raise as 404.
            raise not_found("project", project_id)
        return ProjectView.from_record(updated)

    @v1.delete(
        "/projects/{project_id}",
        response_model=ProjectView,
        tags=["projects-v1"],
    )
    async def v1_archive_project(
        request: Request,
        project_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectView:
        """Soft-delete (archive) a project.

        The per-tenant default project (``name == "default"``) cannot be
        archived (422) — it absorbs unattached resources for D5 back-compat.
        Idempotent: re-archiving an already-archived project is a no-op
        that still returns the (already-archived) detail view.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks both ``admin`` scope and ``owner`` role
        * **404** — no such project in this tenant
        * **422** — attempting to archive the default project
        """
        store: StorageProvider = request.app.state.storage
        current = await store.get_project(ctx.tenant_id, project_id)
        if current is None:
            raise not_found("project", project_id)

        await _require_project_write(request, ctx, project_id)

        if current.name == project_default_name:
            raise _unprocessable(
                "the per-tenant 'default' project cannot be archived — it "
                "absorbs unattached agents/workflows"
            )

        try:
            await store.archive_project(ctx.tenant_id, project_id)
        except ValueError as exc:
            # Defense in depth — storage independently rejects the
            # default project; surface it as the same 422.
            raise _unprocessable(str(exc)) from None
        # Re-read to return the post-archive detail (the archived_at
        # field is the meaningful return shape).
        archived = await store.get_project(ctx.tenant_id, project_id)
        # Re-read can only return None if a concurrent deletion fully
        # purged the row, which the soft-delete contract doesn't do —
        # but defend the type contract anyway.
        if archived is None:  # pragma: no cover - defensive
            raise not_found("project", project_id)
        return ProjectView.from_record(archived)

    # -- Members -------------------------------------------------------

    @v1.get(
        "/projects/{project_id}/members",
        response_model=ProjectMemberListView,
        tags=["projects-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_list_project_members(
        request: Request,
        project_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectMemberListView:
        """List the project's members (creation order).

        Tenant-scoped — the project lookup runs the same no-leak 404
        contract as the project detail endpoint.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks the ``read`` scope
        * **404** — no such project in this tenant
        """
        store: StorageProvider = request.app.state.storage
        project = await store.get_project(ctx.tenant_id, project_id)
        if project is None:
            raise not_found("project", project_id)
        members = await store.list_project_members(project_id)
        return ProjectMemberListView(
            members=[ProjectMemberView.from_record(m) for m in members],
            count=len(members),
        )

    @v1.post(
        "/projects/{project_id}/members",
        response_model=ProjectMemberView,
        status_code=201,
        tags=["projects-v1"],
    )
    async def v1_add_project_member(
        request: Request,
        project_id: str,
        body: ProjectMemberAddRequest,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectMemberView:
        """Invite a principal to the project with a role.

        Membership mutations are admin-scope-OR-owner-role gated (same
        composed gate as project PUT/DELETE). ``added_by`` is the
        caller's principal (audit attribution distinct from the project
        owner field).

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks both ``admin`` scope and ``owner`` role
        * **404** — no such project in this tenant
        * **409** — principal is already a member of this project
        """
        store: StorageProvider = request.app.state.storage
        project = await store.get_project(ctx.tenant_id, project_id)
        if project is None:
            raise not_found("project", project_id)
        await _require_project_write(request, ctx, project_id)
        try:
            await store.add_project_member(
                project_id,
                body.principal_id,
                body.role,
                added_by=_principal_from_auth(ctx),
            )
        except ValueError as exc:
            raise conflict(str(exc)) from None
        member = await store.get_project_member(project_id, body.principal_id)
        if member is None:  # pragma: no cover - defensive
            raise not_found("project member", body.principal_id)
        return ProjectMemberView.from_record(member)

    @v1.get(
        "/projects/{project_id}/members/{principal_id}",
        response_model=ProjectMemberView,
        tags=["projects-v1"],
        dependencies=[_scope("read")],
    )
    async def v1_get_project_member(
        request: Request,
        project_id: str,
        principal_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectMemberView:
        """Get one project member by principal id.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks the ``read`` scope
        * **404** — no such project in this tenant OR no such member
        """
        store: StorageProvider = request.app.state.storage
        project = await store.get_project(ctx.tenant_id, project_id)
        if project is None:
            raise not_found("project", project_id)
        member = await store.get_project_member(project_id, principal_id)
        if member is None:
            raise not_found("project member", principal_id)
        return ProjectMemberView.from_record(member)

    @v1.patch(
        "/projects/{project_id}/members/{principal_id}",
        response_model=ProjectMemberView,
        tags=["projects-v1"],
    )
    async def v1_update_project_member(
        request: Request,
        project_id: str,
        principal_id: str,
        body: ProjectMemberPatchRequest,
        ctx: AuthContext = Depends(auth_dep),
    ) -> ProjectMemberView:
        """Change a member's role (e.g. viewer → editor → owner).

        Rejects demotions that would leave the project with zero
        ``owner`` members (422); the storage layer is permissive by
        design (last-write-wins; the API enforces the social contract).

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks both ``admin`` scope and ``owner`` role
        * **404** — no such project in this tenant OR no such member
        * **422** — would remove the project's last ``owner``
        """
        store: StorageProvider = request.app.state.storage
        project = await store.get_project(ctx.tenant_id, project_id)
        if project is None:
            raise not_found("project", project_id)
        await _require_project_write(request, ctx, project_id)
        existing = await store.get_project_member(project_id, principal_id)
        if existing is None:
            raise not_found("project member", principal_id)
        if existing.role == ProjectMemberRole.OWNER and body.role != ProjectMemberRole.OWNER:
            members = await store.list_project_members(project_id)
            owner_count = sum(1 for m in members if m.role == ProjectMemberRole.OWNER)
            if owner_count <= 1:
                raise _unprocessable(
                    "cannot demote the last 'owner' on a project — promote another member first",
                )
        updated = await store.update_project_member(
            project_id,
            principal_id,
            role=body.role,
        )
        if updated is None:  # pragma: no cover - defensive race
            raise not_found("project member", principal_id)
        return ProjectMemberView.from_record(updated)

    @v1.delete(
        "/projects/{project_id}/members/{principal_id}",
        status_code=204,
        tags=["projects-v1"],
    )
    async def v1_remove_project_member(
        request: Request,
        project_id: str,
        principal_id: str,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Remove a member from the project.

        Refuses to remove the last ``owner`` (422). Idempotent — a
        repeat remove of an already-gone member returns 204 (the
        post-state is the same: not a member).

        Errors:

        * **401** — missing / bad bearer token
        * **403** — caller lacks both ``admin`` scope and ``owner`` role
        * **404** — no such project in this tenant
        * **422** — would remove the project's last ``owner``
        """
        store: StorageProvider = request.app.state.storage
        project = await store.get_project(ctx.tenant_id, project_id)
        if project is None:
            raise not_found("project", project_id)
        await _require_project_write(request, ctx, project_id)
        existing = await store.get_project_member(project_id, principal_id)
        if existing is not None and existing.role == ProjectMemberRole.OWNER:
            members = await store.list_project_members(project_id)
            owner_count = sum(1 for m in members if m.role == ProjectMemberRole.OWNER)
            if owner_count <= 1:
                raise _unprocessable(
                    "cannot remove the last 'owner' from a project — promote "
                    "another member to owner first",
                )
        await store.remove_project_member(project_id, principal_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Webhook subscriptions (ADR 035 D2) — CRUD + attempts feed. The
    # delivery worker (movate.runtime.webhook_worker) lives alongside
    # the job worker and consumes these subscriptions out-of-band; the
    # endpoints below are the management surface only.
    #
    # Secret discipline:
    #
    # * POST returns the plaintext secret EXACTLY ONCE in
    #   ``WebhookCreatedView.secret``. Subsequent reads (GET / list /
    #   PATCH) return ``WebhookView`` which carries only a last-4
    #   ``secret_hint`` — the secret never re-traverses the wire.
    # * The HMAC scheme is Stripe-style (t=<ts>,v1=<hmac_hex> over
    #   ``"<ts>.<raw_body>"`` UTF-8 bytes); subscribers verify against
    #   the secret they captured on create.
    #
    # Scopes: admin for mutation (create/delete/patch — minting or
    # revoking a long-lived secret is a privileged action, mirroring
    # api-key + trigger creation); read for the get/list/attempts
    # views.
    # ------------------------------------------------------------------

    @v1.post(
        "/webhooks",
        response_model=WebhookCreatedView,
        status_code=201,
        tags=["webhooks"],
        dependencies=[_scope("admin")],
    )
    async def v1_create_webhook(
        body: WebhookCreateRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WebhookCreatedView:
        """Subscribe a webhook to lifecycle events (ADR 035 D2).

        Mints a per-subscription HMAC secret and returns it ONCE — copy
        it now, it is never retransmitted. Subsequent reads surface a
        ``secret_hint`` (last 4 chars) only.

        The delivery worker (mdk worker process) drains the events
        outbox and POSTs each matching event to ``url`` with a
        Stripe-style ``X-MDK-Signature: t=<ts>,v1=<hex>`` header. The
        canonical signed string is ``"<ts>.<raw_body>"``; subscribers
        verify by recomputing the HMAC under the stored secret.

        Gated on ``admin`` — creating a subscription mints a long-
        lived secret credential (same posture as api-key + trigger
        create).

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        * **422** — non-HTTPS URL / malformed ``kind_filter``
        """
        store: StorageProvider = request.app.state.storage
        sub = WebhookSubscription(
            tenant_id=ctx.tenant_id,
            url=body.url,
            kind_filter=body.kind_filter,
            enabled=body.enabled,
        )
        await store.create_webhook(sub)
        return WebhookCreatedView.from_record(sub)

    @v1.get(
        "/webhooks",
        response_model=WebhookListView,
        tags=["webhooks"],
        dependencies=[_scope("read")],
    )
    async def v1_list_webhooks(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        include_disabled: bool = Query(
            True,
            description=(
                "Include disabled subscriptions. Default true (the management view "
                "wants to see what's there); the delivery worker uses the in-process "
                "Protocol with ``enabled_only=True``."
            ),
        ),
    ) -> WebhookListView:
        """List this tenant's webhook subscriptions (no full secrets).

        Each row carries a last-4 ``secret_hint`` only — the full
        secret is irrecoverable from this surface.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        """
        store: StorageProvider = request.app.state.storage
        rows = await store.list_webhooks(ctx.tenant_id, enabled_only=not include_disabled)
        views = [WebhookView.from_record(r) for r in rows]
        return WebhookListView(webhooks=views, count=len(views))

    @v1.get(
        "/webhooks/{webhook_id}",
        response_model=WebhookView,
        tags=["webhooks"],
        dependencies=[_scope("read")],
    )
    async def v1_get_webhook(
        webhook_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WebhookView:
        """Fetch one webhook by id (no full secret).

        Tenant-scoped: a webhook under another tenant 404s rather than
        403s — same no-leak contract as every other single-record
        getter.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        * **404** — no webhook with this id for this tenant
        """
        store: StorageProvider = request.app.state.storage
        row = await store.get_webhook(ctx.tenant_id, webhook_id)
        if row is None:
            raise not_found("webhook", webhook_id)
        return WebhookView.from_record(row)

    @v1.delete(
        "/webhooks/{webhook_id}",
        status_code=204,
        tags=["webhooks"],
        dependencies=[_scope("admin")],
    )
    async def v1_delete_webhook(
        webhook_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> Response:
        """Remove a webhook subscription.

        Idempotent: deleting a non-existent subscription still returns
        204. Tenant-scoped (a cross-tenant id is a 204 no-op, never a
        cross-tenant delete). Gated on ``admin`` — it revokes the
        long-lived signing credential.

        The historical ``webhook_attempts`` log rows are intentionally
        kept (operators may want post-mortem on a removed webhook);
        a later GC sweep can prune them.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        """
        store: StorageProvider = request.app.state.storage
        await store.delete_webhook(ctx.tenant_id, webhook_id)
        return Response(status_code=204)

    @v1.patch(
        "/webhooks/{webhook_id}",
        response_model=WebhookView,
        tags=["webhooks"],
        dependencies=[_scope("admin")],
    )
    async def v1_update_webhook(
        webhook_id: str,
        body: WebhookUpdateRequest,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> WebhookView:
        """Toggle a webhook's ``enabled`` flag.

        Only ``enabled`` is mutable on this endpoint. URL / kind_filter
        changes belong in delete+recreate so the audit log + secret
        story stay explicit about a subscriber rewire.

        ``failure_count`` is bumped by the delivery worker; operators
        clear it (when they will at all) via delete+recreate.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``admin`` scope
        * **404** — no webhook with this id for this tenant
        """
        store: StorageProvider = request.app.state.storage
        updated = await store.update_webhook(ctx.tenant_id, webhook_id, enabled=body.enabled)
        if updated is None:
            raise not_found("webhook", webhook_id)
        return WebhookView.from_record(updated)

    @v1.get(
        "/webhooks/{webhook_id}/attempts",
        response_model=WebhookAttemptListView,
        tags=["webhooks"],
        dependencies=[_scope("read")],
    )
    async def v1_list_webhook_attempts(
        webhook_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        since: datetime | None = Query(
            None,
            description="ISO-8601 lower bound on ``attempted_at`` (inclusive).",
        ),
        limit: int = Query(
            100,
            ge=1,
            le=500,
            description="Page size; capped at 500.",
        ),
    ) -> WebhookAttemptListView:
        """List recent delivery attempts for ``webhook_id``.

        Newest-first. Each row carries ``status_code`` / ``error_kind``
        / ``response_excerpt`` (truncated to ~512 chars) so an ops
        dashboard can render flake patterns at a glance. Tenant-scoped
        — a webhook under another tenant 404s.

        Errors:

        * **401** — bad bearer token
        * **403** — missing the ``read`` scope
        * **404** — no webhook with this id for this tenant
        * **422** — ``limit`` outside ``[1, 500]``
        """
        store: StorageProvider = request.app.state.storage
        # Verify the webhook exists for this tenant before exposing
        # any attempts — protects against a leak via a guessed id.
        webhook = await store.get_webhook(ctx.tenant_id, webhook_id)
        if webhook is None:
            raise not_found("webhook", webhook_id)
        rows = await store.list_webhook_attempts(
            ctx.tenant_id,
            webhook_id=webhook_id,
            since=since,
            limit=limit,
        )
        views = [WebhookAttemptView.from_record(r) for r in rows]
        return WebhookAttemptListView(attempts=views, count=len(views))

    # ------------------------------------------------------------------
    # GET /api/v1/events/stream — SSE event-stream (ADR 035 D3)
    #
    # Companion to ``GET /api/v1/events`` (D1's pull surface). Subscribers
    # hold a long-lived HTTP connection; new events are pushed as they're
    # recorded. Both endpoints read the SAME outbox — D2's webhook worker
    # consumes it too — so emitters stay unchanged (the recorder is
    # fire-and-forget and never knows about subscribers).
    #
    # D3 is the LOW-latency path; D2 webhooks are the durable+retried
    # path. They coexist deliberately: a browser front end uses SSE for
    # in-product realtime, a customer system uses webhooks for at-least-
    # once delivery to its own systems.
    # ------------------------------------------------------------------

    @v1.get(
        "/events/stream",
        tags=["events-v1"],
        dependencies=[_scope("read")],
        # No response_model — raw SSE byte stream (text/event-stream),
        # not a JSON body. OpenAPI documents the event shape in the
        # docstring; the response content-type is declared via
        # ``responses=`` below so the generated spec advertises it for
        # client codegen.
        responses={
            200: {
                "description": (
                    "Live stream of lifecycle events as Server-Sent Events. Each frame is "
                    "an ``EventView`` JSON object on a ``data:`` line; SSE comments "
                    "(``:keepalive``) keep the connection open."
                ),
                "content": {"text/event-stream": {}},
            },
            503: {
                "description": (
                    "Per-tenant SSE connection cap reached (``MDK_EVENTS_SSE_MAX_PER_TENANT``)."
                )
            },
        },
    )
    async def v1_events_stream(
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
        since: datetime | None = Query(
            None,
            description=(
                "ISO-8601 lower bound on ``created_at`` (inclusive). When set, "
                "the stream FIRST replays events recorded at-or-after this "
                "timestamp, then goes live. Default: no replay — only new "
                "events emitted while the connection is open are pushed."
            ),
        ),
        kind: str | None = Query(
            None,
            description=(
                "Filter to one event kind (e.g. ``run.completed``). Applies "
                "to both the replay window and the live push loop."
            ),
        ),
        subject: str | None = Query(
            None,
            description=(
                "Filter to one subject (agent name / run id / etc.). "
                "Exact match; applies to replay + live."
            ),
        ),
        tenant: str | None = Query(
            None,
            description=(
                "Operator override: stream events for a specific tenant. "
                "Requires a ``fleet-admin`` key; ignored otherwise."
            ),
        ),
        last_event_id: str | None = Header(
            None,
            alias="Last-Event-ID",
            description=(
                "SSE-standard resumption header. If set, the stream first "
                "replays any events recorded AFTER the id (using it as the "
                "outbox cursor), then goes live — so a reconnecting client "
                "misses no events between the drop and the redial."
            ),
        ),
    ) -> StreamingResponse:
        """Stream lifecycle events as **Server-Sent Events** (ADR 035 D3).

        Tenant-scoped by the caller's ``ctx.tenant_id``; a ``fleet-admin``
        key may pass ``?tenant=<id>`` to subscribe to a different
        tenant. Same auth / scoping shape as ``GET /api/v1/events``.

        **Frame shape.** Each event is emitted as a single SSE frame:

        ``id: <event_id>\\n``
        ``data: <EventView JSON>\\n``
        ``\\n``

        The ``id:`` field is the event's outbox id; a reconnecting
        client SHOULD send it back as the ``Last-Event-ID`` header to
        resume without gaps. Heartbeats are SSE comments (``:keepalive``)
        emitted every ~15s so intermediaries (Azure Front Door /
        Application Gateway / nginx) don't close an idle connection.

        **Replay semantics.** ``since`` (timestamp) or ``Last-Event-ID``
        (cursor) trigger a one-shot replay of matching events before the
        live loop starts. Without either, only events recorded AFTER the
        connection opens are pushed (no backfill — matches the
        front-end's "from now on" expectation).

        **Polling cadence.** The handler polls the outbox every ~500ms
        and advances its cursor by id. This is intentional — the
        recorder is fire-and-forget and doesn't know about subscribers,
        so D3 stays decoupled from emit. Postgres ``LISTEN/NOTIFY`` is
        the documented upgrade path when scale demands it.

        **Per-tenant cap.** Subscribers per tenant are capped (default
        50, override ``MDK_EVENTS_SSE_MAX_PER_TENANT``) to prevent a
        runaway client from owning the pool. A connection over the cap
        is rejected with ``503``.

        **Disconnect.** The async generator polls
        ``request.is_disconnected()`` between iterations; a client TCP
        drop unwinds the loop cleanly and the connection counter
        decrements in the ``finally`` block — no leaked subscribers.

        Errors:

        * **401** — missing / bad bearer token
        * **403** — token lacks the ``read`` scope
        * **503** — per-tenant connection cap reached
        """
        store: StorageProvider = request.app.state.storage

        # Tenant scope — fleet-admin may override; non-admin is silently
        # locked to their own tenant (matches GET /api/v1/events).
        target_tenant = ctx.tenant_id
        if tenant is not None and "fleet-admin" in ctx.scopes:
            target_tenant = tenant

        # Resolve poll / heartbeat / cap from app.state (so tests can
        # override) with module-default fall-throughs. Reading via
        # getattr keeps the contract for build_app callers that haven't
        # set these explicitly byte-identical.
        poll_interval_s: float = getattr(
            request.app.state, "events_sse_poll_interval_s", _EVENTS_SSE_POLL_INTERVAL_S
        )
        heartbeat_interval_s: float = getattr(
            request.app.state,
            "events_sse_heartbeat_interval_s",
            _EVENTS_SSE_HEARTBEAT_INTERVAL_S,
        )
        max_per_tenant: int = _events_sse_max_per_tenant()

        # Per-tenant connection cap (advisory). Take the lock for the
        # check + increment so a burst of concurrent opens can't race
        # past the ceiling. The matching decrement runs in the
        # generator's ``finally`` block below — guaranteed even on
        # client disconnect.
        connections: dict[str, int] = request.app.state.events_sse_connections
        lock: asyncio.Lock = request.app.state.events_sse_lock
        async with lock:
            current = connections.get(target_tenant, 0)
            if current >= max_per_tenant:
                import logging  # noqa: PLC0415

                logging.getLogger(__name__).warning(
                    "events_sse_cap_exceeded tenant_id=%s active=%d cap=%d",
                    target_tenant,
                    current,
                    max_per_tenant,
                )
                raise http_error(
                    ErrorCode.INTERNAL,
                    status_code=503,
                    message=(
                        f"events SSE connection cap reached for tenant "
                        f"(active={current}, cap={max_per_tenant})"
                    ),
                )
            connections[target_tenant] = current + 1

        # OTel UpDownCounter — incremented on accept, decremented in the
        # wrapper's finally so even a disconnect mid-loop drops it.
        inc_sse_connections(tenant_id=target_tenant)

        async def _streaming_wrapper() -> AsyncIterator[str]:
            """Wrap the testable generator with the cap/metric finally.

            The generator itself is :func:`_events_sse_generator` — a
            top-level async generator unit-tested without the HTTP
            transport. This wrapper layers in the per-tenant counter
            decrement + OTel metric decrement on exit (which depend on
            ``request.app.state`` and must run for ANY exit reason
            including client disconnect)."""
            try:
                async for frame in _events_sse_generator(
                    store=store,
                    target_tenant=target_tenant,
                    kind=kind,
                    subject=subject,
                    since=since,
                    last_event_id=last_event_id,
                    poll_interval_s=poll_interval_s,
                    heartbeat_interval_s=heartbeat_interval_s,
                    is_disconnected=request.is_disconnected,
                ):
                    yield frame
            finally:
                # ALWAYS run — happy exit, disconnect, or exception.
                # A leak here turns the advisory cap into a fake
                # ceiling within minutes.
                async with lock:
                    connections[target_tenant] = max(0, connections.get(target_tenant, 0) - 1)
                dec_sse_connections(tenant_id=target_tenant)

        return StreamingResponse(
            _streaming_wrapper(),
            media_type="text/event-stream",
            headers={
                # Defeat any intermediary buffering so events reach the
                # client as they're emitted (matters behind nginx /
                # Azure Front Door — mirrors the run-stream endpoint).
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(v1)

    # ------------------------------------------------------------------
    # Typed exception → HTTP code translator. AgentCreationError carries
    # the intended status_code; FastAPI's default handling would 500
    # everything otherwise.
    # ------------------------------------------------------------------
    @app.exception_handler(AgentCreationError)
    async def _agent_creation_error_handler(
        _request: Request, exc: AgentCreationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {
                    "error": {
                        "code": _agent_creation_error_code(exc.status_code),
                        "message": str(exc),
                    }
                }
            },
        )

    # SkillCreationError uses the same status_code → wire-code mapping
    # as AgentCreationError (409/422/500/503 all carry the same
    # operator-facing semantics regardless of resource); shared handler
    # would couple the two unnecessarily, so keep them parallel.
    @app.exception_handler(SkillCreationError)
    async def _skill_creation_error_handler(
        _request: Request, exc: SkillCreationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {
                    "error": {
                        "code": _agent_creation_error_code(exc.status_code),
                        "message": str(exc),
                    }
                }
            },
        )

    # ADR 037 D1: WorkflowPersistenceError uses the same status_code → wire-
    # code mapping as the agent/skill counterparts so the Angular front end
    # branches uniformly on ``error.code``.
    @app.exception_handler(WorkflowPersistenceError)
    async def _workflow_persistence_error_handler(
        _request: Request, exc: WorkflowPersistenceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {
                    "error": {
                        "code": _agent_creation_error_code(exc.status_code),
                        "message": str(exc),
                    }
                }
            },
        )

    return app


# Re-export for convenience — callers don't have to import the module
# just to suppress an "unused" lint on the auth helper above.
__all__ = ["auth_required", "build_app"]
