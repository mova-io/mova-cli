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
from dataclasses import dataclass
from typing import Any

import httpx


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


class PlaygroundClient:
    """Thin async client over httpx for the playground's needs."""

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

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return the runtime's agent catalog as a list of
        ``{name, version, description, input_schema, output_schema}``
        dicts. Reads ``GET /api/v1/agents``."""
        resp = await self._client.get("/api/v1/agents")
        resp.raise_for_status()
        data = resp.json()
        # The /api/v1/agents endpoint returns
        # {"agents": [{...}, ...], "count": N} — extract the list.
        return list(data.get("agents") or [])

    async def get_agent_detail(self, name: str) -> dict[str, Any]:
        """Return full agent detail (including resolved input + output
        schemas, prompt path, contexts, skills). Reads
        ``GET /api/v1/agents/{name}``."""
        resp = await self._client.get(f"/api/v1/agents/{name}")
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def submit_run(self, *, agent: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Queue a run job via ``POST /run``. Returns the
        ``{job_id, status}`` envelope. Use ``wait_for_run`` to poll
        until the job completes + the resulting run is available."""
        payload = {"kind": "agent", "target": agent, "input": input_data}
        resp = await self._client.post("/run", json=payload)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """Fetch the current state of a job (queued / running /
        success / failed / etc.). Reads ``GET /jobs/{job_id}``."""
        resp = await self._client.get(f"/jobs/{job_id}")
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """Fetch a completed run's full result (output + metrics).
        Reads ``GET /runs/{run_id}``."""
        resp = await self._client.get(f"/runs/{run_id}")
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
        resp = await self._client.post(f"/runs/{run_id}/feedback", json=payload)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result
