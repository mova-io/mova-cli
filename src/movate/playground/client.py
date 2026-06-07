"""Async HTTP client for the Movate runtime.

The Chainlit app needs to talk to a deployed runtime (or a local
``mdk serve``) to: list agents, run an agent, fetch a run's full
result, and post feedback. This module is the thin wrapper.

Keep this dependency-light — only ``httpx`` (already in the
``[playground]`` extra) plus stdlib. No imports from
``movate.core.client`` because that class has additional concerns
(retry policy, target resolution) that the playground doesn't need.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from movate.playground.sse import StreamEvent, iter_sse_events

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry helpers (#216 — runtime-down resilience)
# ---------------------------------------------------------------------------

#: Default retry schedule (seconds between attempts). Three retries with
#: exponential backoff: 1s → 2s → 4s. Generous enough for a cold-start;
#: tight enough to keep the UI responsive.
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

#: HTTP status codes that are retryable (transient server errors).
_RETRYABLE_STATUS: frozenset[int] = frozenset({500, 502, 503, 504})

#: Rate-limit status code.
_RATE_LIMITED_STATUS: int = 429


def _is_retryable(exc: Exception) -> bool:
    """True when ``exc`` is a transient failure worth retrying.

    Covers ``httpx.HTTPStatusError`` with a 5xx status and any
    ``httpx.ConnectError`` / ``httpx.ConnectTimeout`` (runtime not
    reachable).  Rate-limit (429) is handled separately by the caller.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    # ConnectError, ConnectTimeout, RemoteProtocolError — all transient.
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError))


def _is_rate_limited(exc: Exception) -> bool:
    """True when ``exc`` is an HTTP 429 Too Many Requests."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == _RATE_LIMITED_STATUS
    return False


def _is_quota_exceeded(exc: Exception) -> bool:
    """True when the 429 response body signals a hard quota limit (not a
    transient rate-limit window).

    The convention is ``{"error": {"code": "quota_exceeded", ...}}`` in
    the response body, though we also accept a top-level ``code`` field.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code != _RATE_LIMITED_STATUS:
        return False
    try:
        body = exc.response.json()
    except Exception:
        return False
    code = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code", ""))
        if not code:
            code = str(body.get("code", ""))
    return code.lower() in {"quota_exceeded", "quota-exceeded"}


def _parse_retry_after(exc: Exception) -> float | None:
    """Extract the ``Retry-After`` header value in seconds, or ``None``.

    Handles both the ``delta-seconds`` (integer) and HTTP-date forms
    defined by RFC 9110 §10.2.3. Returns ``None`` when the header is
    absent or unparseable — the caller falls back to its own backoff.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    raw = exc.response.headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    # HTTP-date parsing is complex; for the playground use-case a numeric
    # Retry-After is the expected case. Fall back to None for dates.
    return None


def _request_id() -> str:
    """Generate a unique ``X-Request-Id`` for playground → runtime tracing (#220)."""
    return f"pg-{uuid.uuid4().hex[:12]}"


@dataclass
class PlaygroundClientConfig:
    """Configuration the Chainlit app reads at startup."""

    runtime_url: str
    """Base URL of the runtime (e.g. ``https://movate-prod-api.eastus2.azurecontainerapps.io``).
    No trailing slash."""

    api_key: str | None = None
    """Bearer token for runtime auth. ``None`` = anonymous mode
    (only works for runtimes started without auth — local dev)."""

    timeout_s: float = 60.0
    """Per-request timeout. Generous because some agent runs are
    multi-second; the polling loop has its own ceiling on top."""

    poll_interval_s: float = 0.75
    """Sleep between job-status polls. 0.75s is the sweet spot —
    feels responsive in the UI without hammering the API."""

    poll_max_wait_s: float = 120.0
    """Hard ceiling on how long the playground waits for a run to
    finish. Beyond this, the UI shows a timeout card with a link to
    the job's admin page."""

    retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS
    """Exponential-backoff schedule for retryable errors (#216).
    Length = max attempts; each entry = seconds to wait before the
    next attempt.  ``(1.0, 2.0, 4.0)`` → up to 3 retries."""


@dataclass(frozen=True)
class RetryOutcome:
    """Result of a retry-aware request (#216).

    ``ok`` is True when the request ultimately succeeded; ``attempts``
    counts how many times the request was tried (1 = first attempt
    succeeded, no retry). On failure, ``error`` holds the final
    exception for the caller to surface in the UI.

    ``rate_limited`` and ``quota_exceeded`` are set on 429 responses so
    the UI can show differentiated messages.
    """

    ok: bool
    attempts: int
    error: Exception | None = None
    rate_limited: bool = False
    quota_exceeded: bool = False
    retry_after_s: float | None = None


class PlaygroundClient:
    """Thin async client over httpx for the playground's needs.

    #220: every request carries an ``X-Request-Id`` header so operators can
    correlate playground requests to runtime traces. The request_id is also
    stored on the instance as :attr:`last_request_id` so error messages in
    the UI can include it.
    """

    def __init__(self, config: PlaygroundClientConfig) -> None:
        self._config = config
        headers: dict[str, str] = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.AsyncClient(
            base_url=config.runtime_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout_s),
            headers=headers,
        )
        self.last_request_id: str = ""

    def _rid_headers(self) -> dict[str, str]:
        """Build an ``X-Request-Id`` header dict and stash the id (#220)."""
        rid = _request_id()
        self.last_request_id = rid
        return {"X-Request-Id": rid}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Retry-aware request wrapper (#216)
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        on_retry: Any | None = None,
        **kwargs: Any,
    ) -> tuple[httpx.Response, RetryOutcome]:
        """Issue an HTTP request with exponential-backoff retry on transient errors.

        ``on_retry`` is an optional async callback ``(attempt: int, delay: float) -> None``
        that the caller can use to update the UI (e.g. "retrying...").

        Returns ``(response, outcome)`` on success.  On exhausted retries the
        outcome has ``ok=False`` and the response is ``None`` (access via
        ``outcome.error`` instead).

        Rate-limit (429) handling:
        * Quota-exceeded → immediate failure, no retry (hard limit).
        * Transient rate-limit → honor ``Retry-After``, then retry.
        """
        delays = self._config.retry_delays
        last_exc: Exception | None = None
        for attempt in range(1, len(delays) + 2):  # +2: first try + len(delays) retries
            try:
                resp = await self._client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp, RetryOutcome(ok=True, attempts=attempt)
            except Exception as exc:
                last_exc = exc
                # 429 — differentiate quota-exceeded from transient rate-limit.
                if _is_rate_limited(exc):
                    if _is_quota_exceeded(exc):
                        return None, RetryOutcome(  # type: ignore[return-value]
                            ok=False,
                            attempts=attempt,
                            error=exc,
                            rate_limited=True,
                            quota_exceeded=True,
                        )
                    # Transient rate-limit: honor Retry-After header.
                    retry_after = _parse_retry_after(exc)
                    fallback = delays[attempt - 1] if attempt <= len(delays) else delays[-1]
                    wait = retry_after if retry_after is not None else fallback
                    if on_retry is not None:
                        await on_retry(attempt, wait)
                    await asyncio.sleep(wait)
                    continue
                # Retryable 5xx / connection error — backoff and retry.
                if _is_retryable(exc) and attempt <= len(delays):
                    delay = delays[attempt - 1]
                    if on_retry is not None:
                        await on_retry(attempt, delay)
                    await asyncio.sleep(delay)
                    continue
                # Non-retryable error (4xx, etc.) — fail immediately.
                return None, RetryOutcome(  # type: ignore[return-value]
                    ok=False,
                    attempts=attempt,
                    error=exc,
                    rate_limited=_is_rate_limited(exc),
                    retry_after_s=_parse_retry_after(exc) if _is_rate_limited(exc) else None,
                )
        # Exhausted retries.
        return None, RetryOutcome(  # type: ignore[return-value]
            ok=False,
            attempts=len(delays) + 1,
            error=last_exc,
        )

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return the runtime's agent catalog as a list of
        ``{name, version, description, input_schema, output_schema}``
        dicts. Reads ``GET /api/v1/agents``."""
        resp = await self._client.get("/api/v1/agents", headers=self._rid_headers())
        resp.raise_for_status()
        data = resp.json()
        # The /api/v1/agents endpoint returns
        # {"agents": [{...}, ...], "count": N} — extract the list.
        return list(data.get("agents") or [])

    async def get_agent_detail(self, name: str) -> dict[str, Any]:
        """Return full agent detail (including resolved input + output
        schemas, prompt path, contexts, skills). Reads
        ``GET /api/v1/agents/{name}``."""
        resp = await self._client.get(f"/api/v1/agents/{name}", headers=self._rid_headers())
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def submit_run(self, *, agent: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Queue an agent run via ``POST /api/v1/agents/{name}/runs``.

        Returns the ``{job_id, status}`` (``RunAccepted``) envelope from
        the default async path. Use :meth:`wait_for_run` to poll until
        the job completes + the resulting run is available.

        REST-clean, agent-scoped endpoint (the run is created *under* the
        agent). Body carries just the input — ``kind=AGENT`` is implicit
        from the URL. Same envelope the legacy ``POST /run`` returned, so
        the polling loop is unchanged.
        """
        resp = await self._client.post(
            f"/api/v1/agents/{agent}/runs",
            json={"input": input_data},
            headers=self._rid_headers(),
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def get_capabilities(self) -> dict[str, Any] | None:
        """Fetch the runtime's capability descriptor.

        Reads ``GET /api/v1/capabilities`` — the playground's
        feature-detection hook. Returns the raw JSON dict on success, or
        ``None`` when the endpoint is absent (404) / the runtime predates
        capability discovery. The caller (:func:`parse_capabilities`)
        degrades a ``None`` to the all-off default, so an old runtime
        still works in today's single-shot, client-managed, buffered
        mode.

        Only a 404 / connection failure maps to ``None`` — any other
        HTTP error is surfaced so a genuinely broken runtime is loud.
        """
        try:
            resp = await self._client.get("/api/v1/capabilities", headers=self._rid_headers())
        except httpx.HTTPError:
            return None
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def create_session(self, *, agent: str) -> dict[str, Any]:
        """Open a server-managed conversation session (ADR 045 D10).

        POSTs to ``POST /api/v1/sessions``. Used only when the runtime
        advertises the ``sessions`` capability (selected by
        :func:`~movate.playground.conversation.select_backend`). Returns
        the ``{session_id, ...}`` envelope; subsequent turns go via
        :meth:`submit_session_message`.
        """
        resp = await self._client.post(
            "/api/v1/sessions", json={"agent": agent}, headers=self._rid_headers()
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def submit_session_message(
        self,
        *,
        session_id: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a turn within a server-managed session (ADR 045 D10).

        POSTs to ``POST /api/v1/sessions/{session_id}/messages``. The
        runtime threads prior turns into the model context, so the body
        carries only the new message. Returns either a ``{job_id}``
        envelope (poll via :meth:`wait_for_run`) or an inline run record.
        """
        resp = await self._client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"input": input_data},
            headers=self._rid_headers(),
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def submit_run_with_retry(
        self,
        *,
        agent: str,
        input_data: dict[str, Any],
        on_retry: Any | None = None,
    ) -> tuple[dict[str, Any] | None, RetryOutcome]:
        """Submit an agent run with exponential-backoff retry (#216).

        Wraps ``POST /api/v1/agents/{name}/runs`` through
        :meth:`_request_with_retry`. Returns ``(result_dict, outcome)``
        — the caller checks ``outcome.ok`` before reading the dict.
        """
        resp, outcome = await self._request_with_retry(
            "POST",
            f"/api/v1/agents/{agent}/runs",
            json={"input": input_data},
            on_retry=on_retry,
        )
        if outcome.ok and resp is not None:
            return resp.json(), outcome
        return None, outcome

    async def stream_run(
        self,
        *,
        agent: str,
        input_data: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        """Stream an agent run's tokens over SSE (ADR 045 D11).

        POSTs to ``POST /api/v1/agents/{name}/runs/stream`` with
        ``Accept: text/event-stream`` and yields :class:`StreamEvent`s as
        frames arrive:

        * ``event="token"`` → ``data={"text": "<delta>"}`` (0+; concat
          reconstructs the output).
        * ``event="done"`` → ``data={"run_id", "status", "metrics",
          "output"}`` (terminal success).
        * ``event="error"`` → ``data={"message", "code"}`` (terminal
          failure).

        Used only when the runtime advertises ``run_streaming``. The
        bearer token rides on the shared client's default headers — never
        in the URL or query string. The connection is held open for the
        run's duration; the caller renders tokens into a ``cl.Message``
        as they land.
        """
        headers = {"Accept": "text/event-stream", **self._rid_headers()}
        async with self._client.stream(
            "POST",
            f"/api/v1/agents/{agent}/runs/stream",
            json={"input": input_data},
            headers=headers,
            timeout=httpx.Timeout(self._config.poll_max_wait_s),
        ) as resp:
            resp.raise_for_status()
            async for event in iter_sse_events(resp.aiter_lines()):
                yield event

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """Fetch the current state of a job (queued / running /
        success / failed / etc.). Reads ``GET /jobs/{job_id}``."""
        resp = await self._client.get(f"/jobs/{job_id}", headers=self._rid_headers())
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """Fetch a completed run's full result (output + metrics).
        Reads ``GET /runs/{run_id}``."""
        resp = await self._client.get(f"/runs/{run_id}", headers=self._rid_headers())
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def wait_for_run(self, job_id: str) -> dict[str, Any]:
        """Poll ``/jobs/{id}`` until terminal, then return the run
        record via ``/runs/{result_run_id}``. Raises ``TimeoutError``
        if the configured ``poll_max_wait_s`` elapses.

        Terminal statuses per the API contract: ``success``, ``failed``,
        ``dead_letter`` (post-v1 retry queue). Any other value (queued,
        claimed, running) keeps polling.
        """
        elapsed = 0.0
        terminal = {"success", "failed", "dead_letter"}
        while elapsed < self._config.poll_max_wait_s:
            job = await self.get_job(job_id)
            if job.get("status") in terminal:
                run_id = job.get("result_run_id")
                if run_id:
                    return await self.get_run(run_id)
                # Failed-before-running case: no run_id, return the
                # job record itself as a stand-in (caller renders the
                # error from the ``error`` field).
                return job
            await asyncio.sleep(self._config.poll_interval_s)
            elapsed += self._config.poll_interval_s
        raise TimeoutError(
            f"job {job_id!r} did not finish within "
            f"{self._config.poll_max_wait_s}s. The job may still "
            f"complete — check ``mdk jobs show {job_id}``."
        )

    async def upload_kb_files(
        self,
        *,
        agent: str,
        files: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        """Upload one or more KB documents to ``agent``'s knowledge base.

        ``files`` is a list of ``(filename, bytes)`` tuples. Each is
        sent as one part of a ``multipart/form-data`` POST to
        ``/api/v1/agents/{agent}/kb`` with the field name ``files``
        (repeated). The runtime chunks + embeds + persists each file
        via the storage layer.

        Returns the ``KbIngestView`` payload — ``{agent_name,
        total_chunks_saved, files: [...]}``. The caller can render
        per-file status to confirm what landed.

        Raises ``httpx.HTTPStatusError`` for 4xx/5xx — typical causes
        are 404 (agent not found in the runtime catalog) and 502
        (embedding API unreachable).
        """
        # Repeating multipart field "files" — httpx accepts a list of
        # tuples for this; each tuple is (field_name, (filename, content)).
        multipart_files = [("files", (name, content)) for name, content in files]
        resp = await self._client.post(
            f"/api/v1/agents/{agent}/kb",
            files=multipart_files,
            headers=self._rid_headers(),
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    # ------------------------------------------------------------------
    # Conversation threads (Tier 10.5 / PR-P)
    # ------------------------------------------------------------------

    async def create_thread(
        self,
        *,
        agent: str,
        title: str = "",
    ) -> dict[str, Any]:
        """Open a new multi-turn conversation thread with ``agent``.

        POSTs to ``/api/v1/threads``. Returns the
        ``{thread_id, agent, title, created_at, ...}`` envelope —
        clients store ``thread_id`` and send subsequent messages via
        :meth:`submit_thread_message`.
        """
        payload: dict[str, Any] = {"agent": agent}
        if title:
            payload["title"] = title
        resp = await self._client.post("/api/v1/threads", json=payload, headers=self._rid_headers())
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def list_threads(
        self,
        *,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List threads for the authenticated tenant, ordered
        ``updated_at DESC``. Optional ``agent`` filter narrows the
        result to one agent's threads (typical Chainlit case)."""
        params: dict[str, Any] = {"limit": limit}
        if agent is not None:
            params["agent"] = agent
        resp = await self._client.get("/api/v1/threads", params=params, headers=self._rid_headers())
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("threads") or [])

    async def get_thread(
        self,
        thread_id: str,
        *,
        include_runs: bool = True,
    ) -> dict[str, Any]:
        """Fetch a thread by id with optional chronological run history.

        Returns ``{thread_id, agent, title, runs?, ...}``. Set
        ``include_runs=False`` to skip the history scan when the
        client only needs metadata."""
        params = {"include_runs": "true" if include_runs else "false"}
        resp = await self._client.get(
            f"/api/v1/threads/{thread_id}", params=params, headers=self._rid_headers()
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def submit_thread_message(
        self,
        *,
        thread_id: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit a message in the context of an existing thread.

        POSTs to ``/api/v1/threads/{thread_id}/messages``. Returns
        the same ``{job_id, status}`` envelope as
        :meth:`submit_run` — clients poll ``/jobs/{id}`` until
        terminal, then fetch the run via :meth:`get_run`.
        """
        resp = await self._client.post(
            f"/api/v1/threads/{thread_id}/messages",
            json={"input": input_data},
            headers=self._rid_headers(),
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def post_feedback(
        self,
        *,
        run_id: str,
        score: int,
        comment: str | None = None,
        dimensions: dict[str, float] | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist operator feedback to the runtime. POSTs to
        ``/runs/{run_id}/feedback``; the runtime writes the row to
        Postgres and (best-effort) mirrors the score to Langfuse.

        ``score`` convention: ``-1`` / ``+1`` thumbs OR ``1-5`` stars.
        The endpoint rejects other values at the schema layer.
        """
        payload: dict[str, Any] = {"score": score}
        if comment is not None:
            payload["comment"] = comment
        if dimensions is not None:
            payload["dimensions"] = dimensions
        if user_id is not None:
            payload["user_id"] = user_id
        resp = await self._client.post(
            f"/runs/{run_id}/feedback", json=payload, headers=self._rid_headers()
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def post_feedback_with_retry(
        self,
        *,
        run_id: str,
        score: int,
        comment: str | None = None,
        dimensions: dict[str, float] | None = None,
        user_id: str | None = None,
        max_retries: int = 1,
        retry_delay_s: float = 2.0,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Persist feedback with a single retry on failure (#219).

        Returns ``(result_dict, success)``.  On final failure,
        ``result_dict`` is ``None`` and ``success`` is ``False`` — the
        caller shows a "try again" message and leaves buttons active.
        """
        payload: dict[str, Any] = {"score": score}
        if comment is not None:
            payload["comment"] = comment
        if dimensions is not None:
            payload["dimensions"] = dimensions
        if user_id is not None:
            payload["user_id"] = user_id

        for attempt in range(1, max_retries + 2):
            try:
                resp = await self._client.post(f"/runs/{run_id}/feedback", json=payload)
                resp.raise_for_status()
                return resp.json(), True
            except Exception:
                if attempt <= max_retries:
                    logger.warning(
                        "Feedback POST failed (attempt %d/%d), retrying in %.1fs...",
                        attempt,
                        max_retries + 1,
                        retry_delay_s,
                    )
                    await asyncio.sleep(retry_delay_s)
                    continue
                logger.warning(
                    "Feedback POST failed after %d attempt(s).",
                    attempt,
                )
                return None, False
        return None, False  # unreachable but satisfies type checker
