"""``JobSchedule`` model — kind validation, cadence bound, extra=forbid (ADR 017 D2).

Mirrors the EvalSchedule constraints. Asserts the generic schedule:

* accepts ``kind=agent`` / ``kind=workflow`` and rejects ``eval`` / ``bench``
  with a clear validator error (eval has its own scheduler; bench isn't a
  scheduling target).
* enforces ``cadence_seconds >= 1``.
* rejects unknown fields (``extra="forbid"``).
* defaults: enabled True, input {}, last_enqueued_at None.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movate.core.models import JobKind, JobSchedule


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "name": "nightly",
        "kind": JobKind.AGENT,
        "target": "faq-agent",
        "cadence_seconds": 3600,
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_accepts_agent_and_workflow() -> None:
    a = JobSchedule(**_kwargs(kind=JobKind.AGENT))  # type: ignore[arg-type]
    w = JobSchedule(**_kwargs(kind=JobKind.WORKFLOW))  # type: ignore[arg-type]
    assert a.kind == JobKind.AGENT
    assert w.kind == JobKind.WORKFLOW


@pytest.mark.unit
@pytest.mark.parametrize("bad_kind", [JobKind.EVAL, JobKind.BENCH])
def test_rejects_eval_and_bench_kind(bad_kind: JobKind) -> None:
    with pytest.raises(ValidationError) as exc:
        JobSchedule(**_kwargs(kind=bad_kind))  # type: ignore[arg-type]
    # The validator message names the allowed kinds.
    assert "agent" in str(exc.value) and "workflow" in str(exc.value)


@pytest.mark.unit
def test_cadence_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        JobSchedule(**_kwargs(cadence_seconds=0))
    # 1 is the floor and is accepted.
    assert JobSchedule(**_kwargs(cadence_seconds=1)).cadence_seconds == 1


@pytest.mark.unit
def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        JobSchedule(**_kwargs(surprise="nope"))


@pytest.mark.unit
def test_defaults() -> None:
    s = JobSchedule(**_kwargs())
    assert s.enabled is True
    assert s.input == {}
    assert s.notify_email is None
    assert s.created_by is None
    assert s.last_enqueued_at is None
