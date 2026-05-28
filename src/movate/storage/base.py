"""StorageProvider Protocol — every implementation passes the same conformance suite.

v0.1 surface is intentionally narrow: runs + failures, plus list_runs for
``movate logs``. Jobs / API keys / evals join in v0.2 and v0.5 as their
phases ship.

**Tenant isolation (v1.0 stage 4).** Every read of a single record by id
takes a mandatory ``tenant_id`` kwarg and filters by it server-side, so a
caller authenticated as tenant A can never read tenant B's data even by
guessing ids. List methods that omit ``tenant_id`` reserve cross-tenant
reads for operator tooling (``movate worker --tenant-id=None`` drain
mode) — never exposed on the HTTP API. Mutating methods on per-tenant
rows (``update_job``, ``revoke_api_key``, ``touch_api_key``) likewise
require ``tenant_id`` so the WHERE clause stops cross-tenant writes at
the SQL layer, not just at the HTTP layer.

The one exception is ``get_api_key(key_id)`` — the auth middleware
parses the full ``mvt_<env>_<tenant>_<keyid>_<secret>`` key before
lookup and cross-checks the record's ``tenant_id`` against the
presented tenant prefix in ``check_record``. Tenant isolation on this
path is enforced by ``check_record``, not the storage method.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from movate.core.dr_backup import ImportResult
from movate.core.job_retry import ReclaimResult
from movate.core.models import (
    AgentBundleRecord,
    ApiKeyRecord,
    BatchRecord,
    BenchRecord,
    CanaryConfig,
    ConversationThread,
    DiagnosisRecord,
    Entity,
    EntityWithScore,
    EvalRecord,
    EvalSchedule,
    FailureRecord,
    FeedbackRecord,
    JobRecord,
    JobSchedule,
    JobStatus,
    KbChunk,
    KbChunkWithScore,
    Relation,
    RunRecord,
    Subgraph,
    TenantBudget,
    TenantProviderKey,
    Trigger,
    WorkflowRunRecord,
    WorkflowStatus,
)


class StorageProvider(Protocol):
    async def init(self) -> None:
        """Idempotent setup (schema migration, etc.)."""

    async def ping(self) -> None:
        """Cheap liveness check: validate the backend connection is alive.

        Used by ``GET /ready`` to gate ACA traffic — if this raises,
        the pod is reporting "not ready" and ACA stops routing to it
        without restarting it (the liveness probe on ``/healthz``
        stays green so the pod isn't killed for a transient blip).

        Implementations should make this as cheap as possible:
        sqlite does a ``SELECT 1``; postgres does a pool-acquire +
        ``SELECT 1``. Raises any backend error on failure (the
        caller catches and converts to a 503).
        """

    async def save_run(self, run: RunRecord) -> None: ...

    async def save_failure(self, f: FailureRecord) -> None: ...

    async def save_eval(self, e: EvalRecord) -> None: ...

    async def save_bench(self, b: BenchRecord) -> None: ...

    async def save_workflow_run(self, w: WorkflowRunRecord) -> None: ...

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        """Exact lookup by run_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the run exists but belongs to
        a different tenant — same return shape either way so a caller
        can't probe for the existence of other tenants' runs.
        """

    async def get_workflow_run(
        self, workflow_run_id: str, *, tenant_id: str
    ) -> WorkflowRunRecord | None:
        """Exact lookup by workflow_run_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the workflow run belongs to
        a different tenant.
        """

    async def get_eval(self, eval_id: str, *, tenant_id: str) -> EvalRecord | None:
        """Exact lookup by eval_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the eval belongs to a
        different tenant.
        """

    async def get_bench(self, bench_id: str, *, tenant_id: str) -> BenchRecord | None:
        """Exact lookup by bench_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the bench belongs to a
        different tenant — same no-leak contract as ``get_eval``.
        """

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]: ...

    async def list_evals(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[EvalRecord]:
        """List evals newest-first, optionally filtered.

        ``tenant_id=None`` returns evals across all tenants — operator
        tooling only; never exposed on the HTTP API.
        """

    async def list_bench(
        self,
        *,
        tenant_id: str | None = None,
        agent: str | None = None,
        limit: int = 20,
    ) -> list[BenchRecord]:
        """List bench runs newest-first, optionally filtered.

        Same tenant-scoping + signature as :meth:`list_evals`:
        ``tenant_id=None`` returns benches across all tenants — operator
        tooling only; never exposed on the HTTP API.
        """

    async def list_workflow_runs(
        self,
        *,
        tenant_id: str | None = None,
        workflow: str | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 20,
    ) -> list[WorkflowRunRecord]:
        """List workflow runs newest-first, optionally filtered.

        ``status`` narrows to one :class:`WorkflowStatus` (e.g. ``PAUSED``
        to find runs awaiting a human signal — ADR 017 D5). ``tenant_id=None``
        returns runs across all tenants — operator tooling only; never exposed
        on the HTTP API.
        """

    # ------------------------------------------------------------------
    # Eval schedules (ADR 016 D2 — continuous eval cadence)
    #
    # Additive, default-off: a row exists only when an operator sets a
    # cadence for an agent. ``(tenant_id, agent)`` is the unique key;
    # ``save_eval_schedule`` upserts. The portable scheduler tick
    # (movate.core.scheduler) reads ``list_eval_schedules`` and enqueues
    # EVAL jobs for due rows.
    # ------------------------------------------------------------------

    async def save_eval_schedule(self, schedule: EvalSchedule) -> None:
        """Upsert one schedule keyed by ``(tenant_id, agent)``.

        Re-setting an agent's cadence overwrites the prior row (last write
        wins) rather than creating a duplicate — one active schedule per
        agent per tenant.
        """

    async def get_eval_schedule(self, agent: str, *, tenant_id: str) -> EvalSchedule | None:
        """Exact lookup by ``(agent, tenant_id)``.

        Returns ``None`` if no schedule OR if it belongs to a different
        tenant — same no-leak contract as the other ``get_*`` methods.
        """

    async def list_eval_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[EvalSchedule]:
        """List schedules, optionally tenant-scoped.

        ``tenant_id=None`` returns schedules across all tenants — used by
        the scheduler tick's cron drain mode (and operator tooling); never
        exposed on the HTTP API.
        """

    async def delete_eval_schedule(self, agent: str, *, tenant_id: str) -> bool:
        """Delete the schedule for ``(agent, tenant_id)``.

        Returns ``True`` if a row was deleted, ``False`` if none existed.
        Tenant-scoped: a wrong-tenant delete is a no-op (returns ``False``).
        """

    async def touch_eval_schedule(
        self,
        agent: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        """Stamp ``last_enqueued_at`` after the tick enqueues a job.

        Drives the cadence due-check + idempotency. No-op if the schedule
        doesn't exist (it may have been cleared mid-tick).
        """

    # ------------------------------------------------------------------
    # Job schedules (ADR 017 D2 — generic agent/workflow cron cadence)
    #
    # Additive, default-off: a row exists only when an operator sets a
    # cadence for an agent/workflow. ``(tenant_id, name)`` is the unique
    # key; ``save_job_schedule`` upserts. The portable scheduler tick
    # (movate.core.scheduler) reads ``list_job_schedules`` and enqueues a
    # JobKind.AGENT/WORKFLOW job for due rows — the same enqueue-on-cron
    # primitive the eval scheduler uses, with a different job builder.
    # ------------------------------------------------------------------

    async def save_job_schedule(self, schedule: JobSchedule) -> None:
        """Upsert one schedule keyed by ``(tenant_id, name)``.

        Re-setting a schedule's cadence/input overwrites the prior row
        (last write wins) rather than creating a duplicate — one active
        schedule per name per tenant.
        """

    async def get_job_schedule(self, name: str, *, tenant_id: str) -> JobSchedule | None:
        """Exact lookup by ``(name, tenant_id)``.

        Returns ``None`` if no schedule OR if it belongs to a different
        tenant — same no-leak contract as the other ``get_*`` methods.
        """

    async def list_job_schedules(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[JobSchedule]:
        """List schedules, optionally tenant-scoped.

        ``tenant_id=None`` returns schedules across all tenants — used by
        the scheduler tick's cron drain mode (and operator tooling); never
        exposed on the HTTP API.
        """

    async def delete_job_schedule(self, name: str, *, tenant_id: str) -> bool:
        """Delete the schedule for ``(name, tenant_id)``.

        Returns ``True`` if a row was deleted, ``False`` if none existed.
        Tenant-scoped: a wrong-tenant delete is a no-op (returns ``False``).
        """

    async def touch_job_schedule(
        self,
        name: str,
        *,
        tenant_id: str,
        last_enqueued_at: datetime,
    ) -> None:
        """Stamp ``last_enqueued_at`` after the tick enqueues a job.

        Drives the cadence due-check + idempotency. No-op if the schedule
        doesn't exist (it may have been cleared mid-tick).
        """

    # ------------------------------------------------------------------
    # Triggers (ADR 017 D2 — inbound event/webhook → enqueue a job)
    #
    # Additive, default-off: a row exists only when an operator registers a
    # trigger. ``(tenant_id, name)`` is the unique management key;
    # ``save_trigger`` upserts. ``trigger_id`` is the separate public id in
    # the webhook URL — the fire endpoint resolves a trigger by it
    # (``get_trigger_by_id``) WITHOUT a tenant context (the external caller
    # is unauthenticated), and the trigger carries its own tenant. The
    # secret is hashed at rest (never the plaintext), like an API key.
    # ------------------------------------------------------------------

    async def save_trigger(self, trigger: Trigger) -> None:
        """Upsert one trigger keyed by ``(tenant_id, name)``.

        Re-registering a trigger of the same name overwrites the prior row
        (last write wins) — one active trigger per name per tenant.
        """

    async def get_trigger(self, name: str, *, tenant_id: str) -> Trigger | None:
        """Exact lookup by ``(name, tenant_id)`` — the management path.

        Returns ``None`` if no trigger OR if it belongs to a different
        tenant — same no-leak contract as the other ``get_*`` methods.
        """

    async def get_trigger_by_id(self, trigger_id: str) -> Trigger | None:
        """Lookup by the public ``trigger_id`` — the fire path.

        Deliberately **not** tenant-scoped: the fire endpoint is hit by an
        unauthenticated external caller who only knows the URL's
        ``trigger_id``. The returned :class:`Trigger` carries its own
        ``tenant_id``, which scopes the enqueued job. Returns ``None`` when
        no trigger matches.
        """

    async def list_triggers(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[Trigger]:
        """List triggers, optionally tenant-scoped.

        ``tenant_id=None`` returns triggers across all tenants — reserved for
        operator tooling; never exposed on the HTTP API.
        """

    async def delete_trigger(self, name: str, *, tenant_id: str) -> bool:
        """Delete the trigger for ``(name, tenant_id)``.

        Returns ``True`` if a row was deleted, ``False`` if none existed.
        Tenant-scoped: a wrong-tenant delete is a no-op (returns ``False``).
        """

    async def touch_trigger(self, trigger_id: str, *, last_fired_at: datetime) -> None:
        """Stamp ``last_fired_at`` after the fire endpoint enqueues a job.

        Keyed by the public ``trigger_id`` (the fire path has no tenant
        context). Observational only — does not gate firing. No-op if the
        trigger doesn't exist (it may have been deleted mid-request).
        """

    # ------------------------------------------------------------------
    # Trigger delivery dedup (item 23 — trigger replay / idempotency,
    # ADR 017 D2 follow-up)
    #
    # Additive, opt-in by the caller: a row exists only when a fire request
    # carries an ``X-Movate-Delivery-Id`` header (the GitHub
    # ``X-GitHub-Delivery`` convention). The dedup key is
    # ``(trigger_id, delivery_id)`` → the ``job_id`` the first delivery
    # enqueued. A repeated delivery of the same id returns the SAME job and
    # does NOT re-enqueue. No header → no row → byte-for-byte the pre-item-23
    # fire behavior. ``trigger_id`` is tenant-bound, so the key is implicitly
    # tenant-scoped and never leaks across triggers.
    # ------------------------------------------------------------------

    async def get_trigger_delivery(self, trigger_id: str, delivery_id: str) -> str | None:
        """Return the ``job_id`` a prior delivery of this id enqueued, or ``None``.

        Keyed by the public ``trigger_id`` (the fire path has no tenant
        context) + the caller-supplied ``delivery_id``. ``None`` means this
        is the first time we've seen this delivery for this trigger.
        """

    async def record_trigger_delivery(self, trigger_id: str, delivery_id: str, job_id: str) -> bool:
        """Record that ``delivery_id`` for ``trigger_id`` enqueued ``job_id``.

        Atomic insert (``INSERT ... ON CONFLICT DO NOTHING`` /
        ``INSERT OR IGNORE``) so a concurrent double-delivery races safely to
        a single winner rather than recording two jobs. Returns ``True`` if
        this call inserted the row, ``False`` if a row already existed (the
        existing ``job_id`` is preserved — never overwritten).
        """

    # ------------------------------------------------------------------
    # Run-submission dedup (item 37 — submission idempotency)
    #
    # Additive, opt-in by the caller: a row exists only when an async submit
    # request carries an ``Idempotency-Key`` header. The dedup key is
    # ``(tenant_id, idempotency_key)`` → the ``job_id`` the first submission
    # enqueued. A retry with the same key returns the SAME job and does NOT
    # re-enqueue, bounding the at-least-once submit story (complements the
    # trigger-delivery dedup above). No header → no row → byte-for-byte the
    # pre-item-37 submit behavior. Tenant-scoped: two tenants reusing the same
    # key string never collide.
    # ------------------------------------------------------------------

    async def get_run_submission(self, tenant_id: str, idempotency_key: str) -> str | None:
        """Return the ``job_id`` a prior submission with this key enqueued, or ``None``.

        Keyed by ``(tenant_id, idempotency_key)``. ``None`` means this is the
        first time we've seen this key for this tenant.
        """

    async def record_run_submission(
        self, tenant_id: str, idempotency_key: str, job_id: str
    ) -> bool:
        """Record that ``idempotency_key`` for ``tenant_id`` enqueued ``job_id``.

        Atomic insert (``INSERT ... ON CONFLICT DO NOTHING`` /
        ``INSERT OR IGNORE``) so a concurrent retry races safely to a single
        winner rather than recording two jobs. Returns ``True`` if this call
        inserted the row, ``False`` if a row already existed (the existing
        ``job_id`` is preserved — never overwritten).
        """

    # ------------------------------------------------------------------
    # Tenant provider keys (ADR 018 — per-tenant BYOK provider credentials)
    #
    # Additive, default-off: a row exists only when a tenant brings its own
    # provider key. ``(tenant_id, provider)`` is the unique key;
    # ``save_tenant_provider_key`` upserts (a re-set rotates in place). The
    # secret is stored as a Fernet ``ciphertext`` (encrypted at the edge in
    # movate.core.provider_keys before save, decrypted ONLY inside the
    # ProviderKeyResolver) — the plaintext is never persisted, returned, or
    # logged. With no row (and the shared-key fallback on, the default) the
    # run path is byte-for-byte today's. All methods are tenant-scoped: a
    # wrong-tenant get/delete is a no-op / None (no existence leak).
    # ------------------------------------------------------------------

    async def save_tenant_provider_key(self, key: TenantProviderKey) -> None:
        """Upsert one provider key keyed by ``(tenant_id, provider)``.

        Re-setting a provider's key overwrites the prior row (last write wins,
        re-fingerprinted) rather than creating a duplicate — one stored key per
        provider per tenant. The caller encrypts the plaintext before this
        call; only the ``ciphertext`` + masked ``fingerprint`` persist.
        """

    async def get_tenant_provider_key(
        self, provider: str, *, tenant_id: str
    ) -> TenantProviderKey | None:
        """Exact lookup by ``(provider, tenant_id)`` — the resolver path.

        Returns ``None`` if no key OR it belongs to a different tenant — same
        no-leak contract as the other ``get_*`` methods. The returned record
        carries the ``ciphertext``; the resolver decrypts it.
        """

    async def list_tenant_provider_keys(self, *, tenant_id: str) -> list[TenantProviderKey]:
        """List a tenant's configured provider keys (metadata only).

        Tenant-scoped (no cross-tenant mode — provider keys are never listed
        fleet-wide). Callers render ``provider`` + masked ``fingerprint``;
        the ``ciphertext`` is present on the record but must never be returned
        on the wire.
        """

    async def list_all_tenant_provider_keys(
        self, *, limit: int = 100_000
    ) -> list[TenantProviderKey]:
        """List every tenant's provider keys, fleet-wide (item 26 — DR export).

        The cross-tenant companion to :meth:`list_tenant_provider_keys`,
        added for the logical DR export (``mdk export``). Deliberately
        **operator-only** — like the other ``tenant_id=None`` list modes it is
        never exposed on the HTTP API; the only caller is
        :func:`movate.core.dr_backup.export_state`, run as an operator against
        the DB directly. Carries the ``ciphertext`` (the whole point of the
        backup is to preserve the encrypted-at-rest secret); the plaintext is
        still never recoverable without ``MOVATE_PROVIDER_KEY_SECRET``, which
        is NOT in the export. Ordered ``(tenant_id, provider)`` for a stable,
        diff-friendly snapshot.
        """

    async def delete_tenant_provider_key(self, provider: str, *, tenant_id: str) -> bool:
        """Delete the key for ``(provider, tenant_id)``.

        Returns ``True`` if a row was deleted, ``False`` if none existed.
        Tenant-scoped: a wrong-tenant delete is a no-op (returns ``False``).
        """

    # ------------------------------------------------------------------
    # Canary configs (ADR 016 D3 — champion/challenger rollout)
    #
    # Additive, default-off: a row exists only when an operator opts an
    # agent into a canary (``mdk canary set`` / ``POST .../canary``).
    # ``(tenant_id, agent)`` is the unique key; ``save_canary_config``
    # upserts. The run/enqueue path reads ``get_canary_config`` once per run
    # to decide champion vs challenger (movate.core.canary.choose_version);
    # NO row → no read effect → byte-for-byte the pre-canary behavior.
    # ------------------------------------------------------------------

    async def save_canary_config(self, config: CanaryConfig) -> None:
        """Upsert one canary keyed by ``(tenant_id, agent)``.

        Re-setting an agent's canary overwrites the prior row (last write
        wins) rather than creating a duplicate — one active canary per agent
        per tenant. Used for set, promote (challenger→champion, weight→0),
        and rollback.
        """

    async def get_canary_config(self, agent: str, *, tenant_id: str) -> CanaryConfig | None:
        """Exact lookup by ``(agent, tenant_id)`` — the routing path.

        Returns ``None`` if no canary OR if it belongs to a different tenant
        — same no-leak contract as the other ``get_*`` methods. ``None`` is
        the common case: the routing helper treats it as "champion, latest".
        """

    async def list_canary_configs(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[CanaryConfig]:
        """List canaries, optionally tenant-scoped.

        ``tenant_id=None`` returns canaries across all tenants — reserved for
        operator tooling; never exposed on the HTTP API.
        """

    async def delete_canary_config(self, agent: str, *, tenant_id: str) -> bool:
        """Delete the canary for ``(agent, tenant_id)`` — the kill switch's
        hard variant (``mdk canary off --delete``).

        Returns ``True`` if a row was deleted, ``False`` if none existed.
        Tenant-scoped: a wrong-tenant delete is a no-op (returns ``False``).
        """

    # ------------------------------------------------------------------
    # Batches (item 17 — batch inference)
    #
    # A batch is parent metadata over N child jobs. The submit endpoint
    # mints a batch_id, persists one BatchRecord(total=N), and enqueues N
    # ordinary JobKind.AGENT jobs each carrying jobs.batch_id = batch_id.
    # The status endpoint loads the BatchRecord then aggregates over the
    # children via list_jobs(batch_id=...). Additive new table — no row
    # exists unless a batch was submitted, so non-batch behavior is
    # byte-for-byte unchanged.
    # ------------------------------------------------------------------

    async def save_batch(self, batch: BatchRecord) -> None:
        """Insert a brand-new batch parent row. Errors on duplicate ``batch_id``."""

    async def get_batch(self, batch_id: str, *, tenant_id: str) -> BatchRecord | None:
        """Exact lookup by ``batch_id``, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the batch belongs to a different
        tenant — same no-leak contract as ``get_job`` (a cross-tenant lookup
        is indistinguishable from a missing one, so callers 404 either way).
        """

    async def list_batches(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 20,
    ) -> list[BatchRecord]:
        """List batches newest-first, optionally tenant-scoped.

        ``tenant_id=None`` returns batches across all tenants — reserved for
        operator tooling; never exposed on the HTTP API.
        """

    # ------------------------------------------------------------------
    # Job queue (v0.5)
    # ------------------------------------------------------------------

    async def save_job(self, job: JobRecord) -> None:
        """Insert a brand-new ``QUEUED`` job. Errors on duplicate ``job_id``."""

    async def get_job(self, job_id: str, *, tenant_id: str) -> JobRecord | None:
        """Exact lookup by job_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the job belongs to a
        different tenant — same return shape either way so a caller
        can't probe for the existence of other tenants' jobs.
        """

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        target: str | None = None,
        batch_id: str | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        """List jobs newest-first, optionally filtered.

        Tenants must filter by ``tenant_id`` for the multi-tenant audit
        path. Listing across tenants (``tenant_id=None``) is reserved for
        operator tooling (``movate worker --all-tenants``) — never exposed
        on the HTTP API.

        ``target`` filters to one agent (or workflow) name — drives the
        Angular agent-profile page's "recent runs" tab via
        ``GET /api/v1/jobs?agent=<name>`` (item 74).

        ``batch_id`` filters to the child jobs of one batch (item 17). The
        batch-status endpoint pairs this with a high ``limit`` so it can
        aggregate per-status counts over every row of a batch. ``None`` (the
        common case) doesn't constrain on batch membership, so the pre-batch
        listing behavior is unchanged.
        """

    async def claim_next_job(self, *, tenant_id: str | None = None) -> JobRecord | None:
        """Atomically claim the oldest ``QUEUED`` job and flip it to ``RUNNING``.

        Returns the now-claimed :class:`JobRecord` (with ``claimed_at`` set)
        or ``None`` if the queue is empty for this tenant.

        Implementations must guarantee no two callers ever return the same
        job — Postgres uses ``SELECT ... FOR UPDATE SKIP LOCKED``;
        sqlite uses ``BEGIN IMMEDIATE`` + atomic UPDATE.
        """

    async def update_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        status: JobStatus,
        result_run_id: str | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        """Transition a claimed job to a terminal status, scoped to ``tenant_id``.

        ``status`` must be one of ``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED``
        / ``DEAD_LETTER``; ``QUEUED`` and ``RUNNING`` are reserved for the
        lifecycle helpers (``save_job``, ``claim_next_job``, ``requeue_job``).
        Sets ``completed_at = now()`` as a side effect.

        The ``tenant_id`` filter on WHERE is the SQL-layer enforcement
        that prevents a misconfigured worker (or a direct storage call
        from a buggy path) from mutating another tenant's job. Silently
        no-op if no row matches both id + tenant.
        """

    async def requeue_job(
        self,
        job_id: str,
        *,
        tenant_id: str,
        next_retry_at: datetime,
        attempt_count: int,
    ) -> None:
        """Re-queue a ``RUNNING`` job after a transient failure.

        Sets status back to ``QUEUED``, clears ``claimed_at``, bumps
        ``attempt_count``, and stamps ``next_retry_at`` so the claim
        path skips this row until backoff elapses.

        The worker calls this instead of ``update_job`` when the
        dispatch outcome reports a retryable error AND the retry
        budget isn't exhausted (see :mod:`movate.core.job_retry`).
        Tenant-scoped in WHERE; silently no-ops on mismatch.
        """

    async def reclaim_stale_jobs(
        self,
        *,
        older_than: datetime,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> ReclaimResult:
        """Crash-recovery sweep: reclaim jobs orphaned in ``RUNNING``.

        ``claim_next_job`` flips a job to ``RUNNING`` and stamps
        ``claimed_at``, but if the worker is hard-killed mid-job
        (OOM / SIGKILL / node loss) nothing ever transitions the row —
        the claim path only scans ``status='queued'``, so the orphan is
        stuck forever. This reaper finds those rows and recovers them.

        **Operator-level / cross-tenant** — this is a system action, NOT
        tenant-scoped. It targets every tenant's orphaned jobs in one
        atomic sweep.

        Target rows: ``status = 'running' AND claimed_at IS NOT NULL AND
        claimed_at < older_than``. Callers pass
        ``older_than = now - visibility_timeout`` (the timeout MUST be
        generously larger than the longest expected job — a too-small
        value risks reclaiming a still-running job, causing at-least-once
        double-execution).

        Of those rows:

        * ``attempt_count + 1 >= max_attempts`` → ``DEAD_LETTER``
          (``completed_at = now``, ``error`` set to a
          ``reaper_dead_letter`` JSON blob). A reclaim counts as an
          attempt, so a worker that keeps crashing on a poison job
          eventually dead-letters instead of cycling forever.
        * the rest → ``QUEUED`` (``claimed_at = NULL``,
          ``attempt_count += 1``, ``next_retry_at = now`` for immediate
          re-claim eligibility — the crash wasn't a dispatch error, so no
          backoff is warranted).

        Both transitions happen in a SINGLE transaction (atomic). Two
        workers racing the reaper is safe: the atomic
        ``UPDATE ... WHERE status='running'`` means the second worker's
        predicate won't match rows the first already flipped.

        Returns a :class:`ReclaimResult` with the
        ``(requeued, dead_lettered)`` counts.
        """

    async def request_job_cancel(self, job_id: str, *, tenant_id: str) -> JobStatus | None:
        """Cooperatively cancel a job, scoped to ``tenant_id`` (item 36, R4b).

        Atomic, state-dependent transition:

        * ``QUEUED`` → flip straight to ``CANCELLED`` (stamp
          ``completed_at = now``). The claim path only ever takes
          ``status='queued'`` rows, so a cancelled-while-queued job is
          NEVER picked up — the cancellation is effective immediately.
          Returns ``CANCELLED``.
        * ``RUNNING`` → set ``cancel_requested = TRUE`` (status stays
          ``RUNNING``). The cancellation is *pending*: a worker is
          actively running this job and finalizes it at its terminal
          checkpoint (writing ``CANCELLED`` instead of the dispatch
          outcome). Returns ``RUNNING`` so the caller knows the cancel
          was accepted but isn't terminal yet.
        * already terminal (``SUCCESS`` / ``ERROR`` / ``SAFETY_BLOCKED``
          / ``DEAD_LETTER`` / ``CANCELLED``) → no-op; returns the
          unchanged current status (you can't cancel a finished job).

        Returns ``None`` if no row matches ``job_id`` AND ``tenant_id``
        — same shape as :meth:`get_job` so a caller can't probe for the
        existence of another tenant's job (cross-tenant → 404, never 403).

        There is **no** mid-execution interruption: cancellation is
        cooperative (the worker honors the flag at a checkpoint), so a
        ``RUNNING`` job's in-flight LLM call is allowed to complete; its
        result is then discarded in favor of ``CANCELLED``.
        """

    # ------------------------------------------------------------------
    # API keys (v0.5 stage 2)
    # ------------------------------------------------------------------

    async def save_api_key(self, key: ApiKeyRecord) -> None:
        """Persist a freshly-minted ApiKeyRecord (no plaintext secret)."""

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        """Exact lookup by key_id. Returns ``None`` if no match.

        The HTTP middleware uses this to resolve the presented key into
        a record for verification.
        """

    async def list_api_keys(
        self,
        *,
        tenant_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[ApiKeyRecord]:
        """List keys for the management UI. Defaults to active keys only.

        ``tenant_id=None`` returns keys across all tenants — operator-only,
        never exposed on the HTTP API.
        """

    async def revoke_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Set ``revoked_at`` to now, scoped to ``tenant_id``.

        Idempotent — re-revoking is a no-op. The ``tenant_id`` filter
        on WHERE prevents a tenant from revoking another tenant's keys
        by guessing key ids.
        """

    async def touch_api_key(self, key_id: str, *, tenant_id: str) -> None:
        """Bump ``last_used_at``, scoped to ``tenant_id``.

        Called fire-and-forget after a successful verify; failure to
        touch must not fail the request. The tenant filter is defense
        in depth — the auth middleware has already cross-checked the
        record's tenant matches the presented key, but the storage
        layer enforces it independently.
        """

    async def set_api_key_expiry(
        self, key_id: str, *, tenant_id: str, expires_at: datetime
    ) -> None:
        """Set ``expires_at`` on an existing, non-revoked key (ADR 013 D5).

        Used to start the **grace window** on the OLD key during a
        zero-downtime rotation: the old key keeps authenticating until
        ``expires_at`` passes, then :func:`movate.core.auth.check_record`
        rejects it. ``tenant_id`` in WHERE keeps it tenant-scoped — a
        tenant can't touch another tenant's keys. No-op on a missing,
        cross-tenant, or already-revoked key (an already-revoked key is
        dead regardless of expiry).
        """

    async def update_api_key_scopes(self, key_id: str, *, scopes: list[str]) -> None:
        """Overwrite an existing key's ``scopes`` in place by ``key_id``.

        Touches ONLY the ``scopes`` column — ``secret_hash`` / ``salt`` /
        ``tenant_id`` / ``env`` / ``created_at`` and every other field stay
        exactly as stored (the key value itself is unchanged, so re-hashing
        would be wrong). No-op on a missing key.

        Deliberately NOT tenant-scoped: the only caller is the runtime's
        startup bootstrap-key self-heal (``_seed_bootstrap_key``), which
        resolves the row by the parsed ``key_id`` from ``MOVATE_SEED_API_KEY``
        before any request/tenant context exists. ``save_api_key`` is
        insert-only on every backend (errors on a duplicate ``key_id``), so
        an in-place scope correction needs its own narrow update path rather
        than a re-save.
        """

    async def revoke_all_api_keys(self, *, tenant_id: str, except_key_id: str | None = None) -> int:
        """Revoke every active key for ``tenant_id``; return the count revoked.

        Compromise-response primitive (ADR 013 D5 — ``mdk auth revoke
        --all-for <tenant>``). Sets ``revoked_at`` on every key whose
        ``revoked_at IS NULL``. ``except_key_id`` spares one key (the
        operator's own, so a bulk revoke doesn't instantly lock them out).
        Tenant-scoped: only the caller's tenant's keys are affected.
        Idempotent — re-running revokes only what's still active (0 the
        second time).
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Tenant budgets (post-v1.0)
    # ------------------------------------------------------------------

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        """Return the budget row for ``tenant_id``, or ``None`` if no
        budget is set (= unlimited).

        Read on every ``Executor.execute`` entry, so implementations
        should be cheap (PK lookup; sub-millisecond).
        """

    async def upsert_tenant_budget(self, budget: TenantBudget) -> None:
        """Insert-or-update the row for ``budget.tenant_id``.

        Sets ``updated_at = now()`` server-side so the operator can see
        when a limit was last touched. ``created_at`` is preserved on
        update — only changes on the first insert for a tenant.
        """

    async def list_tenant_budgets(self) -> list[TenantBudget]:
        """List all configured tenant budgets, oldest-first. Operator
        tooling only — never exposed on the HTTP API."""

    async def sum_tenant_cost_current_month(self, tenant_id: str) -> float:
        """Sum ``runs.metrics.cost_usd`` for ``tenant_id`` for the
        current calendar month (UTC). 0.0 if no runs.

        ``Executor`` calls this at the top of every run to check
        against the budget; the cost-drift + per-run budget checks
        later in execute() are independent of this. Index on
        ``(tenant_id, created_at)`` is the perf path.
        """

    # ------------------------------------------------------------------
    # Run feedback (added 2026-05-19) — Chainlit playground writes here.
    # ------------------------------------------------------------------

    async def save_feedback(self, feedback: FeedbackRecord) -> None:
        """Persist a :class:`FeedbackRecord`. Idempotent on
        ``feedback_id``: re-saving the same id updates score / comment
        / dimensions in place (operators can edit their feedback).
        """

    async def list_feedback(
        self,
        *,
        run_id: str | None = None,
        agent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        """List feedback rows ordered created_at DESC. Filters AND
        together. Used by the analytics dashboard + by the playground
        when the operator re-opens a run they previously rated.
        """

    # ------------------------------------------------------------------
    # KB chunks (added 0.8.2.13) — vector retrieval MVP. The retrieval
    # primitive is cosine similarity computed in Python over JSONB-
    # stored float arrays; pgvector will swap in later behind the same
    # protocol surface.
    # ------------------------------------------------------------------

    async def save_kb_chunk(self, chunk: KbChunk) -> None:
        """Persist a :class:`KbChunk`. Upsert on ``(agent, tenant_id,
        content_hash)``: re-ingesting an unchanged document is idempotent
        (existing chunks updated in place, not duplicated). Chunks whose
        ``content_hash`` already exists for the agent get their
        ``embedding`` + ``embedding_model`` + ``metadata`` refreshed.
        """

    async def search_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """Top-K most-similar chunks for the agent's KB.

        Implementation: load all chunks matching ``(agent, tenant_id)``
        from storage, compute cosine similarity against
        ``query_embedding`` in Python, sort descending, return the top
        ``limit``. Acceptable for KBs up to ~10k chunks; beyond that
        the linear scan becomes a bottleneck and you'd want a real
        vector index (pgvector / sqlite-vss).

        Empty KB returns ``[]`` cleanly — no special-case needed.
        """

    async def search_kb_chunks_lexical(
        self,
        *,
        agent: str,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[KbChunkWithScore]:
        """Full-text BM25 lexical search over ``text`` column.

        SQLite uses FTS5 + native ``bm25()`` ranking.
        Postgres uses ``to_tsvector`` + GIN index + ``ts_rank``.
        InMemory falls back to the Python BM25 scorer in
        :func:`movate.kb.lexical.bm25_search`.

        Returns up to ``limit`` chunks ranked by relevance.
        Empty query or no matching terms → empty list.
        NEVER raises — same graceful contract as the other
        retrieval helpers.
        """

    async def list_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
        limit: int = 1000,
    ) -> list[KbChunk]:
        """List chunks for inspection / debugging. Filters AND
        together. Returns embeddings + text + metadata; callers that
        only need text should slice their fields after this returns
        rather than this method maintaining a thin variant."""

    async def delete_kb_chunks(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        """Delete chunks scoped to an agent. When ``source`` is set,
        only chunks from that source URI are removed (re-ingest with
        --replace workflow). Returns the count deleted."""

    async def reindex_kb(self, *, agent: str, tenant_id: str) -> int:
        """Rebuild the backend's vector index from the stored chunk
        vectors and return the number of chunks indexed for ``(agent,
        tenant_id)``.

        Used by ``mdk kb reindex`` (and the runtime's ``POST
        .../kb/reindex``) to recover from a degraded index or to pick up
        new index parameters WITHOUT re-embedding — the stored vectors
        are reused as-is. Re-embedding (when the model/dim changes) is
        orchestrated one layer up in ``kb``/cli, which re-embeds each
        chunk's text and persists via :meth:`save_kb_chunk` before
        calling this; the storage layer never imports the embedder.

        Backends with a real vector index (Postgres / pgvector) drop and
        re-create it here. Backends that brute-force search (sqlite,
        in-memory) have no index to rebuild and return the chunk count as
        a graceful no-op — NEVER raise. The HNSW index on Postgres is
        global to the ``kb_chunks`` table, not per-agent, so rebuilding
        it serves every agent; the returned count is still scoped to
        ``(agent, tenant_id)`` so callers can report what they touched."""

    # ------------------------------------------------------------------
    # Conversation threads (Tier 10.5, added 0.8.2.27 / PR-N) — group
    # runs together so multi-turn agents can fetch prior context when
    # rendering the next message's prompt. Runtime endpoint + Chainlit
    # thread-aware mode land in follow-up PRs.
    # ------------------------------------------------------------------

    async def save_conversation_thread(self, thread: ConversationThread) -> None:
        """Persist a :class:`ConversationThread`. Idempotent on
        ``thread_id``: re-saving the same id refreshes ``title`` /
        ``updated_at`` (clients call this each time they append a
        message so the thread sorts most-recently-active first)."""

    async def get_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> ConversationThread | None:
        """Fetch a thread by id, scoped to ``tenant_id``. Returns
        ``None`` if the thread doesn't exist OR belongs to a different
        tenant — never leaks existence across tenants (mirrors the
        single-record-by-id contract on every storage method)."""

    async def list_conversation_threads(
        self,
        *,
        tenant_id: str,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[ConversationThread]:
        """List threads for a tenant, ordered ``updated_at DESC`` so
        the active conversations float to the top. Optional ``agent``
        filter when the client wants threads for one specific agent
        (Chainlit's typical case — one tab per agent picker)."""

    async def list_runs_for_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[RunRecord]:
        """Fetch runs that belong to ``thread_id``, ordered
        ``created_at ASC`` (chronological — earliest turn first) so the
        runtime can render the conversation history straight from the
        list without an extra reverse. Tenant-scoped: a cross-tenant
        thread id returns ``[]`` rather than raising or leaking."""

    async def delete_conversation_thread(
        self,
        thread_id: str,
        *,
        tenant_id: str,
    ) -> bool:
        """Hard-delete a thread row scoped to ``tenant_id``.

        Returns True when a row was deleted, False when no matching
        thread existed (or it belonged to a different tenant — same
        404-not-403 semantics as ``get_conversation_thread``).

        Runs that referenced the thread_id stay in storage; their
        ``thread_id`` column becomes a dangling reference (the runs
        still exist but ``list_runs_for_thread`` returns them only
        when the operator queries by the now-deleted thread id, which
        is fine — operators delete a thread when they don't want to
        see it anymore, not when they want to nuke the historical
        runs themselves)."""

    # ------------------------------------------------------------------
    # Agent registry (ADR 014 D1) — durable, versioned agent bundles.
    # A bundle is the small text files of a published agent stored as one
    # immutable (name, version) row, tenant-scoped, on every backend. KB
    # stays out of the bundle (it lives in pgvector, ADR 009). This is the
    # storage layer only — runtime resolve-from-registry is a later step.
    # ------------------------------------------------------------------

    async def save_agent_bundle(self, bundle: AgentBundleRecord) -> None:
        """Persist one published agent bundle as an immutable
        ``(name, tenant_id, version)`` row.

        Each publish writes a new row, so the table is also the version
        history. Errors on a duplicate ``(name, tenant_id, version)`` —
        a given version is written exactly once and never mutated.
        """

    async def get_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> AgentBundleRecord | None:
        """Fetch one agent bundle by ``name``, scoped to ``tenant_id``.

        ``version=None`` returns the **latest** version (newest
        ``created_at``); an explicit ``version`` returns that exact
        version. Returns ``None`` if no match OR if the agent belongs to a
        different tenant — same 404-not-403 contract as every other
        single-record getter, so a caller can't probe for another
        tenant's agents.
        """

    async def list_agents(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> list[AgentBundleRecord]:
        """List the **latest version per agent name**, newest-first,
        scoped to ``tenant_id``.

        One row per distinct ``name`` (the most recently published
        version of each), ordered by that version's ``created_at`` DESC.
        Drives a registry listing — "what agents are published in this
        tenant." ``limit`` caps the number of distinct agents returned.
        """

    async def list_agent_versions(
        self,
        name: str,
        *,
        tenant_id: str,
        limit: int = 50,
    ) -> list[AgentBundleRecord]:
        """List the version history for one agent ``name``, newest-first,
        scoped to ``tenant_id``.

        Returns every published version of ``name`` ordered by
        ``created_at`` DESC (drives ``mdk agent history`` / rollback in a
        later step). A cross-tenant or unknown ``name`` returns ``[]``
        rather than raising — same no-leak contract as the getters.
        """

    async def list_all_agent_bundles(self, *, limit: int = 100_000) -> list[AgentBundleRecord]:
        """List **every** published agent-bundle version, all tenants
        (item 26 — DR export).

        Unlike :meth:`list_agents` (latest version per name, one tenant) and
        :meth:`list_agent_versions` (all versions of one name, one tenant),
        this returns the *whole* ``agent_bundles`` table — every
        ``(name, tenant_id, version)`` row — because the registry table
        doubles as the version history, and a faithful backup must preserve
        all versions so a restore can roll back to any of them.

        Deliberately **operator-only** — never exposed on the HTTP API; the
        only caller is :func:`movate.core.dr_backup.export_state`. Ordered
        ``(tenant_id, name, created_at)`` for a stable, diff-friendly snapshot.
        """

    async def delete_agent_bundle(
        self,
        name: str,
        *,
        tenant_id: str,
        version: str | None = None,
    ) -> int:
        """Delete agent bundle rows scoped to ``tenant_id``; return the
        count deleted.

        ``version=None`` removes **all** versions of ``name`` (deregister
        the agent); an explicit ``version`` removes just that one version.
        Tenant-scoped in WHERE so a caller can't delete another tenant's
        agents by guessing names — a cross-tenant or unknown name deletes
        nothing and returns ``0``.
        """

    # ------------------------------------------------------------------
    # Knowledge graph (GraphRAG) — entities + relations layered over the
    # KB chunks. Storage mirrors kb_chunks: embeddings as JSONB/TEXT float
    # arrays, cosine in Python (pgvector swap stays behind this surface).
    # The ONLY traversal primitive exposed is ``expand_neighbors`` (bounded
    # k-hop) — no raw query language crosses the Protocol boundary, so a
    # future Neo4jProvider implements the same contract without leaking
    # Cypher to callers.
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> None:
        """Persist an :class:`Entity`. Upsert on ``(agent, tenant_id,
        content_hash)``: re-ingesting the same corpus refreshes
        ``description`` / ``embedding`` / ``embedding_model`` / ``metadata``
        and UNIONs ``source_chunk_ids`` in place rather than duplicating
        the node. The dedup key is ``content_hash`` (SHA-256 of normalized
        name+type), so two extractions of the same real-world entity
        collapse to one row."""

    async def upsert_relation(self, relation: Relation) -> None:
        """Persist a :class:`Relation`. Upsert on ``(agent, tenant_id,
        content_hash)``; UNIONs ``source_chunk_ids`` on conflict.

        The caller MUST upsert both endpoint entities before the relation —
        the storage layer does not auto-create dangling endpoints. It does
        not enforce referential integrity either (no FK): an edge whose
        endpoint was deleted simply never appears in an expansion because
        the join drops it. Keeps the write path cheap and backend-portable."""

    async def search_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[EntityWithScore]:
        """Top-K most-similar entities for the agent's graph — the vector
        SEED step of GraphRAG retrieval.

        Same primitive as :meth:`search_kb_chunks`: load entities matching
        ``(agent, tenant_id)``, compute cosine against ``query_embedding``
        in Python, return the top ``limit``. Empty graph returns ``[]``.
        Callers feed the resulting ``entity_id``s into
        :meth:`expand_neighbors`."""

    async def expand_neighbors(
        self,
        *,
        agent: str,
        tenant_id: str,
        entity_ids: list[str],
        hops: int = 1,
        limit: int = 50,
    ) -> Subgraph:
        """Bounded k-hop expansion from ``entity_ids`` — the ONLY traversal
        primitive. Returns the reached entities (including the seeds) plus
        every relation traversed, as a flat :class:`Subgraph`.

        ``hops`` caps traversal depth; ``limit`` caps the total number of
        relations followed (the budget guard against a hub node exploding
        the result). Edges are followed in descending ``weight`` order so a
        truncated expansion keeps the strongest relationships. Traversal is
        undirected for reachability (an edge connects its endpoints both
        ways) — direction is preserved in the returned ``Relation`` rows for
        the caller to interpret.

        Implementations: recursive CTE over ``kb_relations`` on sqlite /
        postgres; breadth-first walk in :class:`InMemoryStorage`. Unknown or
        cross-tenant ``entity_ids`` contribute nothing rather than raising —
        same no-leak contract as the single-record getters. Empty
        ``entity_ids`` → empty :class:`Subgraph`."""

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        """Exact lookup by entity_id, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the entity belongs to a different
        tenant — same 404-not-403 shape as every other single-record
        getter, so a caller can't probe for other tenants' nodes."""

    async def list_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        source_chunk_id: str | None = None,
        limit: int = 1000,
    ) -> list[Entity]:
        """List entities for inspection / debugging. When ``source_chunk_id``
        is set, returns only entities extracted from that chunk (drives
        provenance views — "what did this passage contribute to the
        graph?"). Filters AND together. Empty graph → ``[]``."""

    async def list_relations(
        self,
        *,
        agent: str,
        tenant_id: str,
        limit: int = 1000,
    ) -> list[Relation]:
        """List relations for inspection / debugging, scoped to
        ``(agent, tenant_id)``. Empty graph → ``[]``."""

    async def delete_graph(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        """Delete an agent's graph, scoped to ``tenant_id``. When ``source``
        is set, removes only entities/relations whose ``source_chunk_ids``
        trace to chunks from that source URI (the per-source re-ingest
        workflow, mirroring :meth:`delete_kb_chunks`). Returns the total
        rows deleted (entities + relations)."""

    # ------------------------------------------------------------------
    # DR backup/restore (item 26) — a portable logical snapshot of the
    # operator-critical, non-reconstructible control-plane state (agent
    # registry, api keys, canary configs, eval/job schedules, per-tenant
    # provider keys). This is the *escape hatch*; the primary DR for a
    # deployed runtime is Azure Postgres PITR (docs/runbooks/dr-backup.md).
    # High-volume/reconstructible history (runs, jobs, evals, KB, threads,
    # memory) is EXCLUDED by design — PITR owns it.
    #
    # The orchestration is backend-agnostic and lives once in
    # movate.core.dr_backup; every backend's implementation delegates there,
    # reading/writing only through the Protocol's existing list/get/save
    # methods (+ the two cross-tenant list accessors above), so the snapshot
    # round-trips identically across sqlite / postgres / in-memory.
    # ------------------------------------------------------------------

    async def export_state(self) -> dict[str, object]:
        """Return a JSON-serializable snapshot of the in-scope control-plane
        state, versioned with ``schema_version`` + ``exported_at``.

        See :func:`movate.core.dr_backup.export_state` for the entity scope,
        the secrets/Fernet posture, and the snapshot shape.
        """

    async def import_state(
        self, snapshot: dict[str, object], *, mode: str = "skip-existing"
    ) -> ImportResult:
        """Load a snapshot from :meth:`export_state` back into this store.

        ``mode="skip-existing"`` (the safe default) never clobbers a row that
        already exists; ``mode="overwrite"`` re-saves every row. Idempotent +
        safe to re-run. Returns per-entity imported/skipped counts. See
        :func:`movate.core.dr_backup.import_state`.
        """

    # ------------------------------------------------------------------
    # Diagnoses (ADR 043 D1 — failure-pattern diagnoser)
    #
    # Persisted output of the diagnose endpoint. A row is created with
    # ``status=running`` when the POST lands and updated (in place,
    # upsert) when the background task finishes (``completed`` or
    # ``error``). The structured result is stored as an opaque JSON blob
    # — the typed-fix taxonomy is validated at the wire edge so future
    # ADR 043 extensions don't require a storage migration.
    #
    # Read-only with respect to agent state: persisting these rows
    # never touches the agent's prompt / KB / context / model. ADR 043's
    # apply step (later PR) is the only thing that does.
    # ------------------------------------------------------------------

    async def save_diagnosis(self, record: DiagnosisRecord) -> None:
        """Upsert a :class:`DiagnosisRecord` keyed by ``diagnosis_id``.

        Idempotent on ``diagnosis_id``: re-saving the same id updates
        ``status`` / ``result`` / ``error`` / ``tokens_used`` /
        ``cost_usd`` / ``completed_at`` in place — the background task
        uses this to transition a row from ``running`` to ``completed``
        without a separate update method. ``tenant_id`` / ``agent`` /
        ``created_at`` are preserved across the upsert (insert-time
        fields are never re-written)."""

    async def get_diagnosis(self, diagnosis_id: str, *, tenant_id: str) -> DiagnosisRecord | None:
        """Exact lookup by ``diagnosis_id``, scoped to ``tenant_id``.

        Returns ``None`` if no match OR if the diagnosis belongs to a
        different tenant — same 404-not-403 contract as the other
        single-record getters, so a caller can't probe for the existence
        of another tenant's diagnoses."""

    async def close(self) -> None: ...
