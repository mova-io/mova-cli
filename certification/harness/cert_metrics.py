"""Emit certification outcomes as the ``mdk.certification.scenario`` metric.

The certification scenarios assert *platform capabilities* (see
:mod:`certification.harness.asserts`). This thin wrapper records each
capability's pass/fail as the OTel counter ``mdk.certification.scenario``
(``{scenario, capability, status=pass|fail}``) so the live results show up on the
**mdk - certification** Grafana dashboard — a pass/fail matrix of mdk's
production-ready guarantees, alongside the golden-signal metrics those
assertions actually read.

Use :func:`certify` as a context manager around one capability assertion: it
records ``pass`` if the block completes and ``fail`` (re-raising) if an
``AssertionError`` escapes. The metric only emits when a meter is configured
(``init_metrics`` ran with an OTLP sink) — otherwise it's a silent no-op, so the
harness still runs assertions normally without observability wired.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from movate.tracing.metrics import record_certification_result


def record(scenario: str, capability: str, *, passed: bool) -> None:
    """Record one capability outcome for a scenario (pass/fail)."""
    record_certification_result(
        scenario=scenario,
        capability=capability,
        status="pass" if passed else "fail",
    )


@contextmanager
def certify(scenario: str, capability: str) -> Iterator[None]:
    """Run a capability assertion; emit pass/fail to the certification metric.

    Records ``status=fail`` and re-raises if the wrapped assertion raises
    (``AssertionError`` or otherwise), else records ``status=pass``::

        with certify("expense-approval", "durable-execution"):
            asserts.assert_completed(result)
    """
    try:
        yield
    except BaseException:
        record(scenario, capability, passed=False)
        raise
    record(scenario, capability, passed=True)
