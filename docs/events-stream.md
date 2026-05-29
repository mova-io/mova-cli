# Events SSE stream (ADR 035 D3)

`GET /api/v1/events/stream` is the **low-latency** half of the outbound
events system. It complements:

* **D1 — `GET /api/v1/events`** — durable pull (cursor-paginated).
* **D2 — `POST /api/v1/webhooks`** — durable push (HMAC-signed,
  at-least-once, retried).

D3 streams the same outbox over a long-lived HTTP connection as
Server-Sent Events. Pick it when the consumer is a browser or another
in-process subscriber that wants push semantics without the
acknowledgement / retry overhead of webhooks.

## Frame shape

Each event is one SSE frame:

```
id: <event_id>
data: <EventView JSON>

```

`id:` is the outbox row id — use it as the `Last-Event-ID` header on a
reconnect to resume without gaps. Heartbeats are SSE comments
(`:keepalive\n\n`) emitted every ~15s so proxies (Azure Front Door /
App Gateway / nginx) don't close the connection.

## Query parameters

* `since=<ISO-8601>` — replay events at-or-after this timestamp BEFORE
  going live. Omit for live-only ("from now on") mode.
* `kind=<k>` — exact-match filter on event kind (e.g. `run.completed`).
* `subject=<s>` — exact-match filter on subject (agent name / run id).
* `tenant=<id>` — operator override; requires `fleet-admin` (silently
  ignored otherwise — non-admin callers stream their own tenant only).
* `Last-Event-ID: <id>` — SSE-standard resumption header. Replays
  events recorded after the id, then goes live.

## Auth

`read` scope. Tenant-scoped from the bearer.

## Consuming the stream

**curl** (with `--no-buffer` so frames surface as they arrive):

```bash
curl --no-buffer \
  -H "Authorization: Bearer $MDK_API_KEY" \
  -H "Accept: text/event-stream" \
  https://api.example.com/api/v1/events/stream
```

**Browser** via the standard `EventSource` API:

```js
const url = new URL("/api/v1/events/stream", "https://api.example.com");
const es = new EventSource(url, { withCredentials: false });
es.onmessage = (ev) => {
  const event = JSON.parse(ev.data);
  console.log(event.kind, event.subject, event.data);
};
es.onerror = () => { /* EventSource auto-reconnects */ };
```

`EventSource` doesn't let you set custom request headers — front ends
that need the bearer typically authenticate the page first and rely on
the session cookie, or proxy the SSE request through a same-origin
endpoint that injects the header server-side. For programmatic clients
(non-browser), prefer `curl` / `fetch` with the SSE parser of your
choice so you can pass `Authorization` directly.

## Operational notes

* The handler polls the outbox at ~500ms intervals — push lag is
  bounded by half that. Override is not required for v1; Postgres
  `LISTEN/NOTIFY` is the documented scale path.
* The per-tenant connection cap defaults to 50, overridable via
  `MDK_EVENTS_SSE_MAX_PER_TENANT`. Over-cap connections return 503.
* `mdk.sse.connections_active` (OTel UpDownCounter) tracks live
  subscribers; pair with the rest of the metric set from
  `movate.tracing.metrics` for dashboards.
