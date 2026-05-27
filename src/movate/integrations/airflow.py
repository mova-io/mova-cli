"""Airflow adapter — ``MovateAgentOperator`` (ADR 017 D3).

OPT-IN. Install the extra to use it::

    uv pip install 'movate-cli[airflow]'

A team that already runs Airflow drives a movate agent/workflow as one
task in a DAG. :class:`MovateAgentOperator` is a *thin* ``BaseOperator``
subclass whose ``execute()`` runs the same ``submit → poll → fetch``
sequence as every other D3 adapter, via
:func:`movate.integrations.orchestration.run_target_async`. It adds **no**
runtime logic — Airflow gets the agent's typed ``output`` dict pushed to
XCom, and movate gets retries/scheduling/observability from Airflow
without taking Airflow as a dependency.

ADR 017's posture is binding: **movate stays the *callable*, never the
*dependent*.** ``airflow`` is imported *lazily*: the operator class is
built by :func:`make_movate_agent_operator` (and exposed as the module
attribute ``MovateAgentOperator`` via :pep:`562` ``__getattr__``) only
when first accessed — so ``import movate.integrations.airflow`` (and
therefore ``import movate``) never requires Airflow. Constructing /
accessing the operator without the extra raises a clear "install the
extra" error instead of an opaque ``ImportError``.

Usage (inside an Airflow DAG file)::

    from airflow import DAG
    from movate.integrations.airflow import MovateAgentOperator

    with DAG("triage", ...) as dag:
        triage = MovateAgentOperator(
            task_id="triage",
            agent="triage-bot",
            payload={"ticket_id": "{{ dag_run.conf['ticket_id'] }}"},
            # base_url/api_key omitted → read from MOVATE_RUNTIME_URL +
            # MOVATE_API_KEY env on the worker. Or wire from a Connection.
        )

``execute()`` returns the agent's ``output`` dict, which Airflow pushes to
XCom (default) so a downstream task consumes it. For a movate *workflow*
(DAG) target, pass ``kind="workflow"``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from movate.core.models import JobKind
from movate.integrations.orchestration import (
    MovateConnection,
    OrchestrationError,
    run_target_async,
)

# Cache so repeated access builds the class (and imports Airflow) exactly
# once per process — the bound base + execute() are stable thereafter.
_operator_cls: type | None = None


def _require_base_operator() -> type:
    """Import Airflow's ``BaseOperator`` lazily, with a clear install hint.

    Importing here (not at module top) is the lazy-import contract:
    ``import movate.integrations.airflow`` must NOT require Airflow — only
    building / using the operator does.
    """
    try:
        from airflow.models import BaseOperator  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via the no-dep test
        raise OrchestrationError(
            "the Airflow adapter requires the `airflow` extra "
            "(install with `uv pip install 'movate-cli[airflow]'`)"
        ) from exc
    base: type = BaseOperator
    return base


def make_movate_agent_operator() -> type:
    """Build (and cache) the ``MovateAgentOperator`` class.

    The class subclasses Airflow's real ``BaseOperator``, which is imported
    here — so the heavy Airflow import is deferred until an operator is
    actually wanted. DAG authors normally reach this via the module
    attribute ``MovateAgentOperator`` (see the module ``__getattr__``);
    this factory is the explicit, import-friendly entry point.

    Raises:
        OrchestrationError: if the ``airflow`` extra isn't installed.
    """
    global _operator_cls  # noqa: PLW0603 — single-process one-shot class cache
    if _operator_cls is not None:
        return _operator_cls

    base_operator = _require_base_operator()

    class MovateAgentOperator(base_operator):  # type: ignore[misc,valid-type]
        """Run a movate agent (or workflow) as an Airflow task.

        ``execute()`` submits the run to the movate runtime, polls until
        terminal, fetches the output, and returns it (Airflow XCom-pushes
        the return value). A failed/blocked run or a transport error raises
        :class:`OrchestrationError`, which Airflow treats as a failed task
        (so its retries/alerts apply).

        Args:
            agent: agent (or workflow) name registered on the runtime.
            payload: the run input (the JSON object the target expects).
                Airflow-templated (``template_fields``) so Jinja in the
                values (e.g. ``{{ dag_run.conf[...] }}``) is rendered before
                execute().
            kind: ``"agent"`` (default) or ``"workflow"``.
            base_url / api_key: runtime URL + bearer token. Omit BOTH to
                read ``MOVATE_RUNTIME_URL`` + ``MOVATE_API_KEY`` from the
                worker's env. Pass BOTH to override (e.g. wired from an
                Airflow Connection in the DAG).
            poll_interval: seconds between job-status polls.
            poll_timeout: max seconds to wait; ``None`` waits indefinitely.
            notify_email: optional address the worker emails on terminal
                status.
        """

        # Airflow renders these from the task's Jinja context before
        # execute(). Listing `payload` lets DAG authors template the input.
        template_fields = ("agent", "payload", "base_url")

        def __init__(
            self,
            *,
            agent: str,
            payload: dict[str, Any],
            kind: str = "agent",
            base_url: str | None = None,
            api_key: str | None = None,
            poll_interval: float = 1.0,
            poll_timeout: float | None = 300.0,
            notify_email: str | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self.agent = agent
            self.payload = payload
            # Validate the kind eagerly so a typo'd DAG fails at construct
            # time, not deep inside execute() on the worker.
            self.kind = JobKind(kind)
            self.base_url = base_url
            self.api_key = api_key
            self.poll_interval = poll_interval
            self.poll_timeout = poll_timeout
            self.notify_email = notify_email

        def execute(self, context: Any) -> dict[str, Any]:
            """Run the target, block until terminal, return ``output`` dict.

            ``context`` is Airflow's task-instance context (unused — the run
            input comes from the templated ``payload``). The return value is
            XCom-pushed by Airflow for downstream tasks.
            """
            connection = (
                MovateConnection(base_url=self.base_url, api_key=self.api_key)
                if self.base_url is not None and self.api_key is not None
                else MovateConnection.from_env()
            )
            # Airflow worker tasks run in their own process with no live
            # event loop, so a fresh loop per execute() is correct + isolated.
            return asyncio.run(
                run_target_async(
                    connection=connection,
                    target=self.agent,
                    payload=self.payload,
                    kind=self.kind,
                    poll_interval=self.poll_interval,
                    poll_timeout=self.poll_timeout,
                    notify_email=self.notify_email,
                )
            )

    _operator_cls = MovateAgentOperator
    return _operator_cls


def __getattr__(name: str) -> Any:
    """:pep:`562` module-level lazy attribute.

    Lets ``from movate.integrations.airflow import MovateAgentOperator``
    work without importing Airflow at module-import time: the class is
    built (and Airflow imported) only when the name is actually accessed.
    """
    if name == "MovateAgentOperator":
        return make_movate_agent_operator()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ``MovateAgentOperator`` is resolved lazily via the module ``__getattr__``
# above (PEP 562), so it isn't a statically-bound module symbol — the F822
# suppression tells ruff that's intentional, not a typo.
__all__ = ["MovateAgentOperator", "make_movate_agent_operator"]  # noqa: F822
