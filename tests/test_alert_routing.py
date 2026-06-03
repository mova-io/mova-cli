"""Alert routing seam tests (ADR 057 D1-D5).

Covers, tests-first per CLAUDE.md rule 9:

* routing: match precedence (first-match vs all-match), min_severity gating,
  tenant + subject_glob (D2);
* throttle/dedup: a storm is suppressed to one delivery per window, with the
  suppressed count surfaced on the next delivery (D4);
* best-effort: a throwing / failing sink never propagates back to the caller
  (D5);
* opt-in: no routes ⇒ no delivery (and an unconfigured env ⇒ no sinks);
* each sink's payload shape, via a faked HTTP transport (D3) — including the
  HMAC signature on the generic webhook.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from movate.core.alert_sinks import (
    GenericWebhookSink,
    SlackSink,
    TeamsSink,
    build_sinks_from_env,
)
from movate.core.alerts import (
    AlertEvent,
    AlertKind,
    AlertRouter,
    DeliveryLog,
    DeliveryStatus,
    RouteTable,
    Severity,
    SinkRegistry,
    Throttle,
    load_alert_routes,
    load_route_table,
)
from movate.core.webhooks import verify_signature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(**overrides) -> AlertEvent:
    base = dict(
        kind=AlertKind.DRIFT_REGRESSION,
        severity=Severity.CRITICAL,
        tenant_id="acme",
        subject="billing-agent",
        summary="drift -0.21 vs baseline",
        data={"score": 0.61, "baseline": 0.82},
        dedup_key="drift:billing-agent",
    )
    base.update(overrides)
    return AlertEvent(**base)


class RecordingSink:
    """An in-memory sink that records every event it's asked to deliver."""

    def __init__(self, name: str, *, ok: bool = True) -> None:
        self.name = name
        self._ok = ok
        self.delivered: list[tuple[AlertEvent, int]] = []

    async def deliver(self, event: AlertEvent, *, suppressed_count: int = 0) -> bool:
        self.delivered.append((event, suppressed_count))
        return self._ok


class ThrowingSink:
    name = "boom"

    async def deliver(self, event: AlertEvent, *, suppressed_count: int = 0) -> bool:
        raise RuntimeError("sink exploded")


# ---------------------------------------------------------------------------
# D1 — event model + enums
# ---------------------------------------------------------------------------


def test_severity_ordering_and_parsing() -> None:
    assert Severity.INFO < Severity.WARNING < Severity.CRITICAL
    assert Severity.from_str("warning") is Severity.WARNING
    assert Severity.from_str("CRITICAL") is Severity.CRITICAL
    assert Severity.WARNING.label == "warning"


def test_severity_parse_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown severity"):
        Severity.from_str("apocalyptic")


def test_alert_event_coerces_string_severity() -> None:
    ev = _event(severity="warning")
    assert ev.severity is Severity.WARNING


# ---------------------------------------------------------------------------
# D2 — routing: match precedence, min_severity, tenant, glob
# ---------------------------------------------------------------------------


async def test_no_routes_is_noop_opt_in() -> None:
    sink = RecordingSink("ops")
    router = AlertRouter(registry=SinkRegistry([sink]))  # empty table
    assert router.is_active is False
    await router.route(_event())
    assert sink.delivered == []


async def test_catch_all_route_delivers() -> None:
    sink = RecordingSink("ops-email")
    table = load_route_table({"routes": [{"match": {}, "sink": "ops-email"}]})
    router = AlertRouter(table=table, registry=SinkRegistry([sink]))
    assert router.is_active is True
    await router.route(_event())
    assert len(sink.delivered) == 1


async def test_first_match_precedence() -> None:
    pager = RecordingSink("pager")
    email = RecordingSink("email")
    table = load_route_table(
        {
            "routes": [
                {"match": {"kind": "drift_regression"}, "sink": "pager"},
                {"match": {}, "sink": "email"},
            ]
        }
    )
    router = AlertRouter(table=table, registry=SinkRegistry([pager, email]))
    await router.route(_event(kind=AlertKind.DRIFT_REGRESSION))
    assert len(pager.delivered) == 1
    assert email.delivered == []  # first match wins, catch-all not reached


async def test_all_match_fans_out() -> None:
    pager = RecordingSink("pager")
    email = RecordingSink("email")
    table = load_route_table(
        {
            "first_match": False,
            "routes": [
                {"match": {"kind": "drift_regression"}, "sink": "pager"},
                {"match": {}, "sink": "email"},
            ],
        }
    )
    router = AlertRouter(table=table, registry=SinkRegistry([pager, email]))
    await router.route(_event(kind=AlertKind.DRIFT_REGRESSION))
    assert len(pager.delivered) == 1
    assert len(email.delivered) == 1


async def test_min_severity_gates() -> None:
    pager = RecordingSink("pager")
    table = load_route_table(
        {
            "routes": [
                {
                    "match": {"kind": "drift_regression", "min_severity": "critical"},
                    "sink": "pager",
                }
            ]
        }
    )
    router = AlertRouter(table=table, registry=SinkRegistry([pager]))
    # warning < critical → not routed
    await router.route(_event(severity=Severity.WARNING, dedup_key="w"))
    assert pager.delivered == []
    # critical >= critical → routed
    await router.route(_event(severity=Severity.CRITICAL, dedup_key="c"))
    assert len(pager.delivered) == 1


async def test_tenant_match() -> None:
    acme = RecordingSink("acme")
    table = load_route_table({"routes": [{"match": {"tenant": "acme"}, "sink": "acme"}]})
    router = AlertRouter(table=table, registry=SinkRegistry([acme]))
    await router.route(_event(tenant_id="globex", dedup_key="g"))
    assert acme.delivered == []
    await router.route(_event(tenant_id="acme", dedup_key="a"))
    assert len(acme.delivered) == 1


async def test_subject_glob_match() -> None:
    sink = RecordingSink("s")
    table = load_route_table({"routes": [{"match": {"subject_glob": "billing-*"}, "sink": "s"}]})
    router = AlertRouter(table=table, registry=SinkRegistry([sink]))
    await router.route(_event(subject="support-agent", dedup_key="x"))
    assert sink.delivered == []
    await router.route(_event(subject="billing-agent", dedup_key="y"))
    assert len(sink.delivered) == 1


async def test_unknown_sink_name_dropped_not_raised() -> None:
    log = DeliveryLog()
    table = load_route_table({"routes": [{"match": {}, "sink": "missing"}]})
    router = AlertRouter(table=table, registry=SinkRegistry(), delivery_log=log)
    await router.route(_event())  # must not raise
    statuses = [r.status for r in log.records()]
    assert DeliveryStatus.NO_SINK in statuses


def test_malformed_route_config_raises_loudly() -> None:
    # A route with an empty sink name is an operator config error → surfaced.
    with pytest.raises(Exception):
        load_route_table({"routes": [{"match": {}, "sink": ""}]})


def test_unknown_match_key_rejected() -> None:
    with pytest.raises(Exception):
        load_route_table({"routes": [{"match": {"bogus": 1}, "sink": "s"}]})


# ---------------------------------------------------------------------------
# D4 — throttle + dedup
# ---------------------------------------------------------------------------


async def test_storm_suppressed_to_one_per_window() -> None:
    sink = RecordingSink("ops")
    table = load_route_table(
        {
            "throttle_window_seconds": 900,
            "routes": [{"match": {}, "sink": "ops"}],
        }
    )
    router = AlertRouter(table=table, registry=SinkRegistry([sink]))
    t0 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    # 50 identical alerts inside the window → exactly one delivery.
    for i in range(50):
        await router.route(_event(dedup_key="same"), now=t0 + timedelta(seconds=i))
    assert len(sink.delivered) == 1
    assert sink.delivered[0][1] == 0  # first delivery: nothing suppressed yet


async def test_suppressed_count_surfaced_on_next_window() -> None:
    sink = RecordingSink("ops")
    table = load_route_table(
        {
            "throttle_window_seconds": 900,
            "routes": [{"match": {}, "sink": "ops"}],
        }
    )
    router = AlertRouter(table=table, registry=SinkRegistry([sink]))
    t0 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    # First delivery, then 9 suppressed inside the window.
    for i in range(10):
        await router.route(_event(dedup_key="same"), now=t0 + timedelta(seconds=i))
    assert len(sink.delivered) == 1
    # After the window elapses, the next alert delivers and carries the count.
    await router.route(_event(dedup_key="same"), now=t0 + timedelta(seconds=901))
    assert len(sink.delivered) == 2
    assert sink.delivered[1][1] == 9  # +9 suppressed since last alert


async def test_distinct_dedup_keys_not_throttled_together() -> None:
    sink = RecordingSink("ops")
    table = load_route_table({"routes": [{"match": {}, "sink": "ops"}]})
    router = AlertRouter(table=table, registry=SinkRegistry([sink]))
    t0 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    await router.route(_event(dedup_key="a"), now=t0)
    await router.route(_event(dedup_key="b"), now=t0)
    assert len(sink.delivered) == 2


def test_per_route_window_override() -> None:
    throttle = Throttle(default_window=timedelta(seconds=900))
    table = RouteTable.model_validate(
        {"routes": [{"match": {}, "sink": "ops", "throttle_window_seconds": 10}]}
    )
    route = table.routes[0]
    t0 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    ev = _event(dedup_key="k")
    assert throttle.admit(route, ev, now=t0)[0] is True
    # 5s later: still inside the 10s per-route window → suppressed.
    assert throttle.admit(route, ev, now=t0 + timedelta(seconds=5))[0] is False
    # 11s later: window elapsed → admitted again.
    assert throttle.admit(route, ev, now=t0 + timedelta(seconds=11))[0] is True


# ---------------------------------------------------------------------------
# D5 — best-effort delivery
# ---------------------------------------------------------------------------


async def test_throwing_sink_does_not_propagate() -> None:
    log = DeliveryLog()
    table = load_route_table({"routes": [{"match": {}, "sink": "boom"}]})
    router = AlertRouter(table=table, registry=SinkRegistry([ThrowingSink()]), delivery_log=log)
    # Must NOT raise — alerting can never break the emitting source.
    await router.route(_event())
    assert log.records()[-1].status is DeliveryStatus.FAILED


async def test_failing_sink_recorded_failed() -> None:
    log = DeliveryLog()
    table = load_route_table({"routes": [{"match": {}, "sink": "ops"}]})
    router = AlertRouter(
        table=table,
        registry=SinkRegistry([RecordingSink("ops", ok=False)]),
        delivery_log=log,
    )
    await router.route(_event())
    assert log.records()[-1].status is DeliveryStatus.FAILED


async def test_successful_delivery_logged_sent() -> None:
    log = DeliveryLog()
    table = load_route_table({"routes": [{"match": {}, "sink": "ops"}]})
    router = AlertRouter(
        table=table, registry=SinkRegistry([RecordingSink("ops")]), delivery_log=log
    )
    await router.route(_event())
    assert log.records()[-1].status is DeliveryStatus.SENT


# ---------------------------------------------------------------------------
# D3 — sink payload shapes (faked HTTP transport)
# ---------------------------------------------------------------------------


class _Capture:
    """Captures the single request a sink makes via a faked transport."""

    def __init__(self, status: int = 200) -> None:
        self.request: httpx.Request | None = None
        self._status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(self._status)


def _patched_client(monkeypatch, transport: httpx.MockTransport) -> None:
    orig = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return orig(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_slack_sink_payload(monkeypatch) -> None:
    cap = _Capture()
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    sink = SlackSink(webhook_url="https://hooks.slack.test/x")
    ok = await sink.deliver(_event(), suppressed_count=3)
    assert ok is True
    assert cap.request is not None
    body = json.loads(cap.request.content)
    assert "text" in body
    assert "drift_regression" in body["text"]
    assert "CRITICAL" in body["text"]
    assert "+3 suppressed" in body["text"]
    assert "acme" in body["text"]


async def test_teams_sink_payload(monkeypatch) -> None:
    cap = _Capture()
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    sink = TeamsSink(webhook_url="https://teams.test/x")
    ok = await sink.deliver(_event())
    assert ok is True
    body = json.loads(cap.request.content)
    assert body["@type"] == "MessageCard"
    assert "drift_regression" in body["title"]
    facts = body["sections"][0]["facts"]
    assert {"name": "Tenant", "value": "acme"} in facts


async def test_generic_webhook_signed_payload(monkeypatch) -> None:
    cap = _Capture()
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    secret = "topsecret"
    sink = GenericWebhookSink(url="https://hook.test/x", secret=secret)
    ev = _event()
    ok = await sink.deliver(ev, suppressed_count=2)
    assert ok is True
    body = cap.request.content
    parsed = json.loads(body)
    assert parsed["type"] == "alert"
    assert parsed["suppressed_count"] == 2
    assert parsed["alert"]["kind"] == "drift_regression"
    assert parsed["alert"]["dedup_key"] == ev.dedup_key
    # HMAC signature is present and verifies against the exact wire bytes.
    sig = cap.request.headers["X-MDK-Signature"]
    assert verify_signature(secret=secret, body=body, header_value=sig) is True


async def test_generic_webhook_unsigned_when_no_secret(monkeypatch) -> None:
    cap = _Capture()
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    sink = GenericWebhookSink(url="https://hook.test/x")
    await sink.deliver(_event())
    assert "X-MDK-Signature" not in cap.request.headers


async def test_sink_non_2xx_returns_false(monkeypatch) -> None:
    cap = _Capture(status=500)
    _patched_client(monkeypatch, httpx.MockTransport(cap.handler))
    sink = SlackSink(webhook_url="https://hooks.slack.test/x")
    assert await sink.deliver(_event()) is False


async def test_sink_transport_error_returns_false(monkeypatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    _patched_client(monkeypatch, httpx.MockTransport(boom))
    sink = SlackSink(webhook_url="https://hooks.slack.test/x")
    assert await sink.deliver(_event()) is False


# ---------------------------------------------------------------------------
# BYOK autoload (ADR 018) — opt-in
# ---------------------------------------------------------------------------


def test_build_sinks_empty_env_is_opt_in() -> None:
    registry = build_sinks_from_env(env={})
    assert registry.names() == []


def test_build_sinks_from_env_registers_configured() -> None:
    registry = build_sinks_from_env(
        env={
            "SLACK_WEBHOOK_URL": "https://hooks.slack.test/x",
            "TEAMS_WEBHOOK_URL": "https://teams.test/x",
            "MDK_ALERT_WEBHOOK_URL": "https://hook.test/x",
            "MDK_ALERT_WEBHOOK_SECRET": "s3cr3t",
        }
    )
    assert set(registry.names()) == {"slack", "teams", "webhook"}


def test_build_sinks_partial_env() -> None:
    registry = build_sinks_from_env(env={"SLACK_WEBHOOK_URL": "https://x/y"})
    assert registry.names() == ["slack"]


# ---------------------------------------------------------------------------
# Config discovery (D2) — alerts.yaml / project.yaml `alerts:` block
# ---------------------------------------------------------------------------


def test_load_alert_routes_absent_is_empty(tmp_path) -> None:
    table = load_alert_routes(tmp_path)
    assert table.routes == []


def test_load_alert_routes_dedicated_file(tmp_path) -> None:
    (tmp_path / "alerts.yaml").write_text(
        "routes:\n  - match: {kind: drift_regression}\n    sink: pager\n"
    )
    table = load_alert_routes(tmp_path)
    assert len(table.routes) == 1
    assert table.routes[0].sink == "pager"
    assert table.routes[0].match.kind is AlertKind.DRIFT_REGRESSION


def test_load_alert_routes_inline_block(tmp_path) -> None:
    (tmp_path / "project.yaml").write_text(
        "alerts:\n  routes:\n    - match: {}\n      sink: ops-email\n"
    )
    table = load_alert_routes(tmp_path)
    assert table.routes[0].sink == "ops-email"


def test_load_alert_routes_dedicated_wins_over_inline(tmp_path) -> None:
    (tmp_path / "project.yaml").write_text(
        "alerts:\n  routes:\n    - match: {}\n      sink: inline\n"
    )
    (tmp_path / "alerts.yaml").write_text("routes:\n  - match: {}\n    sink: dedicated\n")
    table = load_alert_routes(tmp_path)
    assert table.routes[0].sink == "dedicated"


def test_load_alert_routes_project_without_alerts_block(tmp_path) -> None:
    (tmp_path / "project.yaml").write_text("agents_dir: ./agents\n")
    table = load_alert_routes(tmp_path)
    assert table.routes == []
