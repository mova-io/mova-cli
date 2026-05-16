"""Remote agent executor — swap-in for :class:`Executor` that runs the agent
against a deployed movate runtime over HTTP instead of in-process.

Used by ``mdk eval <https://…>`` to score a deployed agent without running
it locally. The eval engine's seam is :meth:`Executor.execute(bundle,
request) -> RunResponse`; :class:`RemoteExecutor` implements the same
signature by submitting each case as a job, polling until terminal, and
fetching the resulting ``RunRecord`` via ``GET /runs/{id}``.

Limited scope on purpose. v1 supports single-agent eval against the
movate runtime's existing submit/poll/get-run flow. It does NOT support:

* arbitrary non-movate HTTP endpoints (those have no shared response
  contract for us to assert against);
* workflow execution (the eval engine only scores per-agent today);
* streaming or token-callbacks (irrelevant for eval scoring).

Failures map straight onto :class:`RunResponse` so the eval engine
treats remote agent errors identically to local executor errors —
score 0.0, rationale = error message, case fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from movate.core.client import MovateClient, MovateClientError
from movate.core.models import (
    ErrorInfo,
    JobKind,
    JobStatus,
    Metrics,
    RunRequest,
    RunResponse,
)

if TYPE_CHECKING:
    from movate.core.loader import AgentBundle


# How long to wait for a single remote run to reach terminal status. Six
# minutes is generous for typical agent latencies (<30s) plus warm-up
# headroom on cold ACA replicas; longer than this and we surface a clean
# timeout instead of hanging the eval suite. Configurable via the
# constructor; default is what an operator would set in CI.
DEFAULT_MAX_WAIT_SECONDS = 360.0

# Poll cadence while a job is QUEUED or RUNNING. 1s is the same default
# `mdk jobs wait` uses interactively — fast enough that the eval
# progress bar updates feel live, slow enough that we don't hammer the
# runtime when the worker is busy.
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class RemoteExecutor:
    """:class:`Executor`-shaped facade over :class:`MovateClient`.

    Constructed once per eval run; reused across every case in the
    dataset. The underlying ``MovateClient`` keeps its httpx
    connection pool alive between calls (one TCP + TLS handshake
    amortised across N cases instead of N).

    ``api_key`` is required — every authenticated endpoint on the
    runtime expects ``Authorization: Bearer <key>``. Caller resolves
    it from ``--api-key`` or ``MOVATE_API_KEY`` before passing it in.
    """

    def __init__(
        self,
        client: MovateClient,
        *,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    ) -> None:
        self._client = client
        self._poll_interval_seconds = poll_interval_seconds
        self._max_wait_seconds = max_wait_seconds

    async def execute(
        self,
        bundle: AgentBundle,
        request: RunRequest,
        *,
        skill_fixture: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> RunResponse:
        """Run one case against the deployed agent and shape the result
        into a :class:`RunResponse` the eval engine can score.

        Signature is intentionally narrower than :meth:`Executor.execute`
        — the kw-only fields that local execution uses (``model_override``,
        ``history``, ``on_token``, ``workflow_run_id``, ``node_id``,
        ``tenant_id_override``) have no meaning for a remote runtime
        we don't control. ``skill_fixture`` is accepted for API compat
        but silently ignored — the remote runtime calls real skills.
        """
        _ = skill_fixture  # accepted for Executor API compat; unused for remote evals
        try:
            accepted = await self._client.submit_job(
                kind=JobKind.AGENT,
                target=bundle.spec.name,
                input=request.input,
            )
        except MovateClientError as exc:
            return _error_response(
                type_="submit_failed",
                message=f"POST /run failed: {exc}",
            )

        try:
            terminal_job = await self._client.wait_for_terminal(
                accepted.job_id,
                poll_interval_seconds=self._poll_interval_seconds,
                max_wait_seconds=self._max_wait_seconds,
            )
        except TimeoutError as exc:
            return _error_response(type_="timeout", message=str(exc), retryable=True)
        except MovateClientError as exc:
            return _error_response(
                type_="poll_failed",
                message=f"GET /jobs/{accepted.job_id} failed: {exc}",
            )

        # Map terminal job state → RunResponse the eval engine expects.
        # ``RunView`` is the source of truth for the actual agent output
        # (``JobView`` deliberately omits it — see runtime/schemas.py).
        if terminal_job.status == JobStatus.SUCCESS:
            if terminal_job.result_run_id is None:
                # SUCCESS without a result_run_id is a runtime bug. We
                # treat it as an eval-side error rather than crashing
                # the whole suite — score 0.0 for this case and move on.
                return _error_response(
                    type_="missing_run_id",
                    message=(
                        f"job {accepted.job_id} reached SUCCESS but the runtime "
                        "didn't set result_run_id; can't fetch the output"
                    ),
                )
            try:
                run_view = await self._client.get_run(terminal_job.result_run_id)
            except MovateClientError as exc:
                return _error_response(
                    type_="get_run_failed",
                    message=f"GET /runs/{terminal_job.result_run_id} failed: {exc}",
                )
            return RunResponse(
                status="success",
                run_id=run_view.run_id,
                data=run_view.output or {},
                metrics=run_view.metrics,
            )

        # Non-success terminal — surface the runtime's error envelope
        # verbatim. ``safety_blocked`` propagates as its own status so
        # callers (and the eval engine's per-case rationale) can
        # distinguish "model refused" from "everything else broken".
        status_literal: Literal["error", "safety_blocked"] = (
            "safety_blocked" if terminal_job.status == JobStatus.SAFETY_BLOCKED else "error"
        )
        return RunResponse(
            status=status_literal,
            error=terminal_job.error
            or ErrorInfo(
                type=terminal_job.status.value,
                message=f"remote job terminated with status={terminal_job.status.value}",
            ),
        )


def _error_response(*, type_: str, message: str, retryable: bool = False) -> RunResponse:
    """Compose a uniform ``RunResponse`` for transport-layer failures.

    Eval-side errors (network blip, runtime 5xx, missing run_id) all
    flow through this so the eval summary attributes a 0.0 score with
    a readable rationale, instead of crashing the whole run on a
    raised exception.
    """
    return RunResponse(
        status="error",
        error=ErrorInfo(type=type_, message=message, retryable=retryable),
        metrics=Metrics(),
    )
