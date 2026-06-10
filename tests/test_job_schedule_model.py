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
    assert s.cron is None
    assert s.timezone is None


# ---------------------------------------------------------------------------
# Cron form (ADR 100 D1) — exactly one of cron | cadence_seconds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cron_schedule_accepted_with_zero_cadence_sentinel() -> None:
    s = JobSchedule(**_kwargs(cadence_seconds=0, cron="0 7 * * 1-5", timezone="America/New_York"))
    assert s.cron == "0 7 * * 1-5"
    assert s.timezone == "America/New_York"
    assert s.cadence_seconds == 0


@pytest.mark.unit
def test_cron_schedule_accepted_with_cadence_omitted() -> None:
    """Omitting cadence_seconds entirely defaults to the 0 sentinel."""
    kwargs = _kwargs(cron="0 7 * * *")
    kwargs.pop("cadence_seconds")
    s = JobSchedule(**kwargs)
    assert s.cadence_seconds == 0
    assert s.timezone is None  # UTC default


@pytest.mark.unit
def test_cron_and_cadence_together_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        JobSchedule(**_kwargs(cadence_seconds=3600, cron="0 7 * * *"))
    assert "exactly one" in str(exc.value)


@pytest.mark.unit
def test_neither_cron_nor_cadence_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        JobSchedule(**_kwargs(cadence_seconds=0))
    assert "exactly one" in str(exc.value)


@pytest.mark.unit
def test_timezone_without_cron_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        JobSchedule(**_kwargs(timezone="America/New_York"))
    assert "only valid with a cron expression" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("bad_cron", ["99 99 * * *", "not-a-cron", "* * * *"])
def test_invalid_cron_expression_rejected(bad_cron: str) -> None:
    with pytest.raises(ValidationError) as exc:
        JobSchedule(**_kwargs(cadence_seconds=0, cron=bad_cron))
    assert "invalid cron expression" in str(exc.value)


@pytest.mark.unit
def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        JobSchedule(**_kwargs(cadence_seconds=0, cron="0 7 * * *", timezone="Mars/Olympus"))
    assert "invalid timezone" in str(exc.value)


@pytest.mark.unit
def test_interval_form_unchanged() -> None:
    """Back-compat: the pre-ADR-100 interval form is byte-for-byte valid."""
    s = JobSchedule(**_kwargs(cadence_seconds=3600))
    assert s.cadence_seconds == 3600
    assert s.cron is None
