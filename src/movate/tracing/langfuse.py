"""Langfuse tracer — opt-in, env-gated, fail-soft.

Langfuse is an optional dependency. Install with::

    uv sync --extra langfuse

This adapter targets the **Langfuse v3 SDK** (the OpenTelemetry-based SDK
shipped with self-hosted Langfuse v3). It replaced the v2 SDK, whose
``Langfuse().trace(...)`` / ``trace.span(...)`` / ``span.generation(...)``
object model was a significant API change. The mapping is documented inline
at each method (see ADR 015, implementation-plan step 2).

Activation precedence (see :func:`movate.tracing.build_tracer`):

1. ``MOVATE_TRACER=langfuse`` — explicit opt-in.
2. ``LANGFUSE_SECRET_KEY`` set in the environment — implicit opt-in.

Either path will gracefully fall back to the stdout tracer (with a stderr
warning) if the langfuse package isn't importable or if the SDK rejects
our keys at construction time. We never let tracing break a run.

Span model (v3):

* The first ``start_span`` on a fresh tracer instance creates a Langfuse
  *root span* via ``client.start_span(...)``. In v3 the root span IS the
  trace anchor (there is no separate ``trace`` object) — trace-level fields
  (``session_id`` / ``user_id`` / ``tags``) are set on it via
  ``span.update_trace(...)``. Nested spans (those passed a ``parent``)
  become child spans via ``parent.start_span(...)``.
* ``log_event`` maps to ``span.create_event(name="event", metadata=event)``.
* ``set_attribute`` mirrors the value into the span's metadata via
  ``span.update(metadata={key: value})``.
* ``end_span`` closes the underlying Langfuse object with ``span.end()``.
  In v3 *every* observation (root included) exposes ``.end()`` — the v2
  "trace root has no ``.end()``" quirk is gone — so we record terminal
  status via ``update(status_message=...)`` then ``end()``.

The local ``SpanCtx`` stays a pure dataclass — Langfuse handles are kept
in a private dict keyed by ``span_id`` so callers never see SDK objects.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any
from uuid import uuid4

from movate.tracing.base import SpanCtx, Tracer


class LangfuseUnavailableError(Exception):
    """Raised when the langfuse package isn't installed or the client can't init."""


class LangfuseTracer(Tracer):
    """Forwards :class:`Tracer` Protocol calls to Langfuse v3 SDK objects."""

    name = "langfuse"

    def __init__(self, *, client: Any | None = None) -> None:
        """Construct from an existing client, or build one from env vars.

        ``client=`` is the test seam: pass a stub that mirrors the v3 SDK
        surface we use — a top-level ``start_span(name=, metadata=)``
        returning an observation object with ``start_span(...)``,
        ``start_observation(as_type="generation", ...)``, ``create_event(...)``,
        ``update(...)``, ``update_trace(...)``, ``end()``, a ``trace_id``
        attribute, plus a top-level ``create_score(...)`` /
        ``flush()`` / ``shutdown()``.
        """
        if client is None:
            client = _build_client_from_env()
        self._client = client
        # span_id → langfuse observation handle (root or child span). Lookups
        # are O(1); unbounded growth is bounded by the lifetime of the run
        # since ``end_span`` pops.
        self._handles: dict[str, Any] = {}

    # ----- start ------------------------------------------------------------

    def start_span(
        self,
        name: str,
        attrs: dict[str, Any] | None = None,
        parent: SpanCtx | None = None,
    ) -> SpanCtx:
        attributes = dict(attrs or {})
        # Pop Langfuse-native first-class fields before forwarding attrs as
        # metadata. Keys prefixed with ``_`` are executor-private signals
        # that should map to Langfuse's dedicated trace parameters rather
        # than landing in the generic metadata blob.
        session_id: str | None = attributes.pop("_session_id", None) or None
        user_id: str | None = attributes.pop("_user_id", None) or None
        tags: list[str] = attributes.pop("_tags", None) or []
        if parent is None:
            # v3: the root span anchors the trace. Create it, then push
            # trace-level fields onto it via update_trace (v2 set these as
            # client.trace(session_id=..., user_id=..., tags=...) kwargs —
            # in v3 there's no separate trace object).
            handle = self._client.start_span(name=name, metadata=attributes)
            self._update_trace_fields(handle, session_id=session_id, user_id=user_id, tags=tags)
            trace_id = getattr(handle, "trace_id", None) or str(uuid4())
            ctx = SpanCtx(
                trace_id=trace_id,
                parent_id=None,
                name=name,
                attributes=attributes,
            )
        else:
            parent_handle = self._handles.get(parent.span_id)
            if parent_handle is None:
                # Parent's already ended — fall back to a top-level root span
                # rather than dropping the span on the floor.
                handle = self._client.start_span(name=name, metadata=attributes)
                trace_id = getattr(handle, "trace_id", None) or parent.trace_id
            else:
                handle = parent_handle.start_span(name=name, metadata=attributes)
                trace_id = parent.trace_id
            ctx = SpanCtx(
                trace_id=trace_id,
                parent_id=parent.span_id,
                name=name,
                attributes=attributes,
            )
        self._handles[ctx.span_id] = handle
        return ctx

    @staticmethod
    def _update_trace_fields(
        handle: Any,
        *,
        session_id: str | None,
        user_id: str | None,
        tags: list[str],
    ) -> None:
        """Set v3 trace-level fields on the root span via ``update_trace``.

        Build kwargs selectively so we don't pass ``None`` for fields the
        SDK might reject, and skip the call entirely when nothing is set.
        Fail-soft: tracing must never break a run.
        """
        trace_kwargs: dict[str, Any] = {}
        if session_id:
            trace_kwargs["session_id"] = session_id
        if user_id:
            trace_kwargs["user_id"] = user_id
        if tags:
            trace_kwargs["tags"] = tags
        if not trace_kwargs:
            return
        update_trace = getattr(handle, "update_trace", None)
        if callable(update_trace):
            with contextlib.suppress(Exception):
                update_trace(**trace_kwargs)

    # ----- end --------------------------------------------------------------

    def end_span(self, span: SpanCtx, status: str = "ok") -> None:
        handle = self._handles.pop(span.span_id, None)
        if handle is None:
            return
        # v3 unifies the observation API: EVERY observation (root span and
        # child spans alike) exposes ``.end()`` — the v2 split where the
        # trace ROOT lacked ``.end()`` and had to be finalized via
        # ``.update()`` is gone. Record terminal status on the span's
        # metadata (v3 has no first-class status enum on spans we map to),
        # then close it. Wrapped fail-soft: tracing must never break a run.
        with contextlib.suppress(Exception):
            update = getattr(handle, "update", None)
            if callable(update):
                update(metadata={"status": status})
            handle.end()

    # ----- events / attributes ---------------------------------------------

    def log_event(self, span: SpanCtx, event: dict[str, Any]) -> None:
        handle = self._handles.get(span.span_id)
        if handle is None:
            return
        with contextlib.suppress(Exception):
            handle.create_event(name="event", metadata=event)

    def set_attribute(self, span: SpanCtx, key: str, value: Any) -> None:
        # Mutate the local ctx so callers reading ``span.attributes`` see it.
        span.attributes[key] = value
        handle = self._handles.get(span.span_id)
        if handle is None:
            return
        with contextlib.suppress(Exception):
            handle.update(metadata={key: value})

    # ----- generation -------------------------------------------------------

    def log_generation(
        self,
        span: SpanCtx,
        *,
        model: str,
        input_messages: list[dict[str, Any]],
        output_text: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        """Emit a Langfuse Generation observation for the LLM completion.

        This populates the Generations tab in Langfuse UI and feeds the
        model-level token-usage + cost dashboards. Called once per
        ``executor.execute()`` call after the final response is received.

        v3 mapping: the v2 ``span.generation(name=, model=, input=, output=,
        usage={"input": .., "output": .., "total": .., "unit": "TOKENS"})``
        one-shot call becomes ``span.start_observation(as_type="generation",
        ...)`` returning a child generation that we immediately ``.end()``.
        Token usage moves from the v2 ``usage=`` blob to v3 ``usage_details=``
        (a plain ``{input, output, total}`` int dict — v3 dropped the ``unit``
        field), and cost moves from a metadata bag to the first-class v3
        ``cost_details={"total": ...}`` so it lands on the cost dashboards.
        Fail-soft: any SDK exception is swallowed so tracing never breaks a run.
        """
        handle = self._handles.get(span.span_id)
        if handle is None:
            return
        with contextlib.suppress(Exception):  # never let tracing break a run
            generation = handle.start_observation(
                as_type="generation",
                name="llm-completion",
                model=model,
                input=input_messages,
                output=output_text,
                usage_details={
                    "input": input_tokens,
                    "output": output_tokens,
                    "total": input_tokens + output_tokens,
                },
                cost_details={"total": cost_usd} if cost_usd else None,
            )
            # The generation is a point-in-time record of an already-complete
            # call, so close it immediately (v3 requires an explicit .end()).
            generation.end()

    # ----- lifecycle --------------------------------------------------------

    def flush(self) -> None:
        """Flush queued events. Called by ``shutdown_runtime`` at CLI exit.

        v3 keeps both ``flush()`` (force-send buffered spans/scores) and
        ``shutdown()`` (flush + tear down the exporter threads). We call
        ``flush()`` here to match the v2 semantics ``shutdown_runtime``
        relied on; ``shutdown()`` is exposed separately for a full teardown.
        """
        flush = getattr(self._client, "flush", None)
        if callable(flush):
            flush()

    def shutdown(self) -> None:
        """Flush and tear down the v3 SDK's background exporter.

        v3 is OpenTelemetry-based and runs a background span processor;
        ``shutdown()`` flushes pending data and joins those threads. Safe to
        call on a client that only has ``flush()`` (e.g. older stubs) — we
        fall back to ``flush()`` in that case. Fail-soft.
        """
        with contextlib.suppress(Exception):
            shutdown = getattr(self._client, "shutdown", None)
            if callable(shutdown):
                shutdown()
                return
            self.flush()

    async def push_run_feedback_score(self, run_record: Any, feedback: Any) -> str | None:
        """Mirror :class:`FeedbackRecord` to Langfuse as a trace-level score.

        Called best-effort from the feedback endpoint after the row is
        about to be saved. The runtime's feedback endpoint catches any
        exception we raise; we still try/except internally so a bad
        Langfuse client doesn't surface as an opaque error in the
        endpoint logs.

        Langfuse v3 SDK shape::

            client.create_score(
                trace_id=<run's trace id>,
                name="user_feedback",
                value=<numeric>,
                data_type="NUMERIC",
                comment=<optional str>,
            ) -> None   # v3 create_score returns None (no Score object)

        Semantic difference vs. v2: v2's ``client.score(...)`` returned a
        ``Score`` object with an ``.id`` we surfaced as
        ``feedback.langfuse_score_id``. v3's ``create_score`` returns
        ``None`` (the score id is generated client-side and not handed
        back), so this method returns ``None`` on success — the
        cross-link id is no longer available from the SDK. The score is
        still pushed; only the returned id is gone. Callers (``app.py``)
        only set the cross-link when a truthy id comes back, so a ``None``
        return is handled correctly (the Postgres row stays the source of
        truth).

        Mapping:

        * trace id: ``run_record.metrics.langfuse_trace_id`` if the
          executor wrote it there, else ``trace_id`` → we skip the push
          (no trace = nothing to attach to).
        * score value: ``feedback.score`` (raw -1/+1 or 1-5).
        * name: ``user_feedback`` (operators can filter on this in
          Langfuse UI; ``mdk_*`` namespace is reserved for system scores).
        * comment: ``feedback.comment`` (the SDK accepts None).
        """
        # Pull trace id from the run's metrics. Different code paths
        # write it under slightly different keys (legacy vs current);
        # try both.
        metrics = getattr(run_record, "metrics", None)
        if metrics is None:
            return None
        trace_id = getattr(metrics, "langfuse_trace_id", None) or getattr(metrics, "trace_id", None)
        if not trace_id:
            return None

        # The Langfuse client's create_score() is synchronous. We don't
        # want to block the feedback endpoint's event loop on a network
        # call to Langfuse, so dispatch via ``asyncio.to_thread``.
        import asyncio  # noqa: PLC0415

        try:
            return await asyncio.to_thread(
                self._create_score,
                trace_id=trace_id,
                name="user_feedback",
                value=float(feedback.score),
                comment=feedback.comment,
            )
        except Exception:
            # Last-resort guard: even ``to_thread`` shouldn't fail in
            # normal operation, but if it does, swallow and proceed.
            # The feedback row in Postgres is the source of truth.
            return None

    async def score_trace(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> str | None:
        """Push a named numeric score to an existing Langfuse trace.

        Used by the eval engine to record the accuracy dimension score so
        it appears on the Langfuse Generations / Traces view alongside the
        per-run token usage. Dispatched via ``asyncio.to_thread`` so the
        synchronous Langfuse v3 ``create_score()`` call doesn't block the
        eval event loop. Fail-soft: returns ``None`` on any error.

        Semantic difference vs. v2: as with :meth:`push_run_feedback_score`,
        v3's ``create_score`` returns ``None`` rather than a ``Score`` with
        an ``.id``, so this returns ``None`` on success (the score is still
        recorded; the SDK simply doesn't hand back an id). Callers use it
        best-effort and don't depend on the return value.

        This method is NOT part of the :class:`Tracer` Protocol — it's a
        Langfuse-specific extension. Callers access it via ``getattr``
        (or ``isinstance(tracer, LangfuseTracer)``) so other tracers don't
        need to implement it.
        """
        if not trace_id:
            return None

        import asyncio  # noqa: PLC0415

        try:
            return await asyncio.to_thread(
                self._create_score,
                trace_id=trace_id,
                name=name,
                value=value,
                comment=comment,
            )
        except Exception:
            return None

    def _create_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None,
    ) -> str | None:
        """Synchronous v3 ``create_score`` push; fail-soft.

        v3's ``create_score`` returns ``None`` (no Score object), so we
        return ``None`` on success. We still return the id when a stub /
        future SDK happens to hand one back, so callers that look for a
        cross-link id keep working if the SDK ever restores it.
        """
        try:
            result = self._client.create_score(
                trace_id=trace_id,
                name=name,
                value=value,
                data_type="NUMERIC",
                comment=comment,
            )
        except Exception:
            return None
        # v3 returns None; tolerate a stub/future SDK that returns an object.
        return getattr(result, "id", None)

    # ----- eval-summary scores (ADR 031 D1) ---------------------------------

    async def score_eval_summary(
        self,
        *,
        trace_id: str,
        pass_rate: float,
        mean_score: float,
        dimension_means: dict[str, float] | None = None,
        drift_deltas: dict[str, float] | None = None,
    ) -> None:
        """Push an eval run's aggregate quality as Langfuse scores (ADR 031 D1).

        Records, on the run's trace:

        * ``eval_pass_rate`` — fraction of cases that passed the gate.
        * ``eval_mean_score`` — mean aggregated score across cases.
        * ``eval_dim_<dimension>`` — one score per scored dimension
          (faithfulness, coverage, …) from the run's dimensional means.
        * ``eval_drift_<metric>`` — one score per drift delta
          (``mean_score`` / ``pass_rate`` / a dimension), each
          ``current - baseline`` so a negative value reads as a regression.

        These render in Langfuse natively so pass-rate, per-dimension quality,
        and drift trends are visible alongside the run's traces. Best-effort:
        a missing ``trace_id`` is a no-op, and any SDK error is swallowed so
        an eval never fails because Langfuse is down (mirrors
        :meth:`score_trace`). Not part of the :class:`Tracer` Protocol — a
        Langfuse-specific extension callers reach via ``getattr``.
        """
        if not trace_id:
            return
        scores: list[tuple[str, float, str | None]] = [
            ("eval_pass_rate", pass_rate, None),
            ("eval_mean_score", mean_score, None),
        ]
        for dim, value in (dimension_means or {}).items():
            scores.append((f"eval_dim_{dim}", value, None))
        for metric, delta in (drift_deltas or {}).items():
            # Comment makes the sign legible in the Langfuse UI.
            drift_comment = f"drift delta {delta:+.4f} (current minus baseline)"
            scores.append((f"eval_drift_{metric}", delta, drift_comment))

        import asyncio  # noqa: PLC0415

        for name, value, comment in scores:
            try:
                await asyncio.to_thread(
                    self._create_score,
                    trace_id=trace_id,
                    name=name,
                    value=float(value),
                    comment=comment,
                )
            except Exception:
                # Last-resort guard; the eval result stays the source of truth.
                continue

    # ----- dataset sync (ADR 031 D1) ----------------------------------------

    async def sync_dataset(
        self,
        *,
        name: str,
        items: list[dict[str, Any]],
        description: str | None = None,
    ) -> int:
        """Idempotent upsert of eval cases into a Langfuse dataset (ADR 031 D1).

        Creates the dataset (if absent) then upserts each item so the agent's
        ``evals/dataset.jsonl`` (+ harvested cases, ADR 016 D1) lives alongside
        its traces in Langfuse. Idempotent: re-running re-upserts the same
        items (Langfuse keys dataset items by ``id`` — we pass a stable id per
        case — so a second sync updates rather than duplicates).

        Each ``items`` entry is ``{"id": <stable str>, "input": <obj>,
        "expected_output": <obj | None>, "metadata": <obj | None>}``.

        Returns the number of items successfully upserted (``0`` when Langfuse
        rejects the dataset or every item, or when there's nothing to sync).
        Best-effort: any SDK error is swallowed — syncing must never break an
        eval. Langfuse v3 SDK surface used: ``client.create_dataset(name=,
        description=)`` and ``client.create_dataset_item(dataset_name=, id=,
        input=, expected_output=, metadata=)`` (create_dataset_item is an
        upsert keyed on ``id``). Not part of the :class:`Tracer` Protocol.
        """
        if not name or not items:
            return 0

        import asyncio  # noqa: PLC0415

        return await asyncio.to_thread(
            self._sync_dataset_sync,
            name=name,
            items=items,
            description=description,
        )

    def _sync_dataset_sync(
        self,
        *,
        name: str,
        items: list[dict[str, Any]],
        description: str | None,
    ) -> int:
        """Synchronous dataset upsert; fail-soft. See :meth:`sync_dataset`."""
        create_dataset = getattr(self._client, "create_dataset", None)
        if callable(create_dataset):
            with contextlib.suppress(Exception):
                # create_dataset is itself an upsert (no-op if the dataset
                # already exists), so a second sync doesn't error.
                create_dataset(name=name, description=description)
        create_item = getattr(self._client, "create_dataset_item", None)
        if not callable(create_item):
            return 0
        synced = 0
        for item in items:
            try:
                create_item(
                    dataset_name=name,
                    id=item.get("id"),
                    input=item.get("input"),
                    expected_output=item.get("expected_output"),
                    metadata=item.get("metadata"),
                )
                synced += 1
            except Exception:
                continue
        return synced


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client_from_env() -> Any:
    """Construct a real Langfuse client from environment variables.

    Raises :class:`LangfuseUnavailableError` if the package is missing or the
    keys aren't usable. Caller (``build_tracer``) catches and falls back
    to stdout.
    """
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    if not secret or not public:
        raise LangfuseUnavailableError(
            "LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY must both be set"
        )
    try:
        # Lazy import: langfuse is an optional dep, only pulled here.
        from langfuse import Langfuse  # noqa: PLC0415 - lazy by design
    except ImportError as exc:
        raise LangfuseUnavailableError(
            "langfuse package not installed; "
            "install with: uv tool install --reinstall movate-cli --extra langfuse"
        ) from exc
    # Accept LANGFUSE_HOST (canonical) or LANGFUSE_BASE_URL (Langfuse SDK
    # alias) so both spellings work when stored in ~/.movate/credentials.
    # v3 renamed the constructor kwarg ``host`` → ``base_url`` but still
    # accepts ``host=`` as a deprecated alias, so the existing call shape
    # keeps working against a self-hosted v3 instance.
    host = (
        os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    )
    try:
        return Langfuse(secret_key=secret, public_key=public, host=host)
    except Exception as exc:
        raise LangfuseUnavailableError(f"langfuse client init failed: {exc}") from exc
