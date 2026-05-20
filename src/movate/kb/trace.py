"""Search trace — per-stage telemetry for the retrieval pipeline.

The full retrieval stack now has up to four LLM-stage layers
(rewriter / rerank / multi-hop planner) plus per-variant vector +
BM25 lookups. When an operator says "this chunk didn't surface,
why?" or "search is slow, where's the time going?", they need
visibility into what happened at each stage — not just the final
chunk list.

This module is the observability tap. ``SearchTrace`` accumulates
per-stage records; ``movate.kb.search.search`` writes into it
when the caller passes one in (and skips the bookkeeping entirely
when ``trace=None``, the default — zero overhead on the hot path).

CLI: ``mdk kb search --trace`` renders the trace as a table after
the results. Programmatic callers (skill, Chainlit, future
observability sinks) construct a trace, pass it to ``search``, and
inspect ``trace.stages`` after the call.

Design choices (v0.9 MVP):

* **Mutable trace object passed in**, not a return-value tuple.
  Keeps ``search()``'s return type stable (``list[KbChunkWithScore]``)
  and matches Python's logging-by-injection idiom. The alternative
  (``search()`` returns ``(chunks, trace)``) would require every
  call site to unpack.
* **Stage-level granularity**, not per-chunk. Per-chunk tracking
  (which stages each chunk passed through, rank at each stage)
  is more useful but 5x the bookkeeping; deferred until operators
  actually request it.
* **Latency via** ``time.perf_counter()`` — monotonic + microsecond
  resolution. ``time.time()`` would surface wall-clock drift in
  long-running multi-hop traces.
* **Free-form ``details`` dict** so each stage can stash its own
  shape (variant strings for rewrite, sub-query string for
  multi-hop, dropped-candidate count for rerank). Not part of the
  wire contract; rendering is best-effort.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageRecord:
    """One stage's outcome — name + timing + counts + details.

    ``name`` is the conventional stage identifier (``rewrite``,
    ``retrieve[0]``, ``rrf_fuse``, ``rerank``, ``multi_hop:hop_1``,
    etc.). Indexed variants get bracket suffixes so the operator can
    see the fan-out.

    ``input_count`` and ``output_count`` carry the candidate counts
    going in and coming out. For stages with no meaningful input
    (the first retrieve), input is 0. The total across stages tells
    the operator how the candidate pool widened / narrowed.
    """

    name: str
    duration_ms: float
    input_count: int = 0
    output_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchTrace:
    """Per-call trace for ``movate.kb.search.search``.

    Callers construct one of these and pass it as the ``trace``
    kwarg. Each retrieval stage appends a :class:`StageRecord`.
    Stages execute in declaration order (the list is the timeline).

    Convenience methods:

    * :meth:`record` — synchronous record-after-the-fact helper,
      used when the caller times the operation externally.
    * :meth:`time` — context manager that times the body and
      records on exit. Use this when the stage's wall-time IS
      what you're measuring.
    * :meth:`total_ms` — sum across all stages. Approximates the
      full pipeline latency (excludes inter-stage Python overhead,
      but that's typically <1ms).
    """

    stages: list[StageRecord] = field(default_factory=list)

    def record(
        self,
        name: str,
        duration_ms: float,
        *,
        input_count: int = 0,
        output_count: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a stage record. Caller owns the timing measurement.

        Used when the caller has already timed the operation
        externally (e.g. the rewriter returns + we measured the LLM
        call separately).
        """
        self.stages.append(
            StageRecord(
                name=name,
                duration_ms=duration_ms,
                input_count=input_count,
                output_count=output_count,
                details=dict(details or {}),
            )
        )

    def time(self, name: str, **details: Any) -> _StageTimer:
        """Context manager that times the body + records on exit.

        Example::

            with trace.time("rewrite") as rec:
                variants = await rewrite_query(...)
                rec.output_count = len(variants)
                rec.details["variants"] = variants

        The timer records the elapsed wall-time even if the body
        raises — incomplete stages are still useful for debugging
        timeouts and partial failures.
        """
        return _StageTimer(self, name, details)

    def total_ms(self) -> float:
        """Sum of per-stage durations. Approximates pipeline latency."""
        return sum(s.duration_ms for s in self.stages)


class _StageTimer:
    """Internal context manager returned by :meth:`SearchTrace.time`.

    Mutate ``self.output_count`` / ``self.input_count`` / ``self.details``
    inside the body to attach those to the recorded stage.
    """

    __slots__ = ("_name", "_start", "_trace", "details", "input_count", "output_count")

    def __init__(self, trace: SearchTrace, name: str, initial_details: dict[str, Any]) -> None:
        self._trace = trace
        self._name = name
        self._start = 0.0
        self.input_count = 0
        self.output_count = 0
        self.details: dict[str, Any] = dict(initial_details)

    def __enter__(self) -> _StageTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        # perf_counter() returns seconds — convert to ms with 2-decimal
        # precision (microsecond noise floor isn't useful for stages
        # that run in 50-500ms).
        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self._trace.record(
            self._name,
            round(elapsed_ms, 2),
            input_count=self.input_count,
            output_count=self.output_count,
            details=self.details,
        )
