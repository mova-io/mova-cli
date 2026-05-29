"""Server-Sent Events parsing for the streaming run path (pure logic).

The runtime's ``POST /api/v1/agents/{name}/runs/stream`` emits SSE
frames (``event:`` + ``data:`` lines, blank-line-terminated). This
module turns a stream of *lines* into a stream of :class:`StreamEvent`s
— the byte-mirror of the runtime's ``_sse_frame`` writer.

Kept separate from :mod:`movate.playground.client` (and free of httpx /
Chainlit) so the frame-assembly logic is unit-testable against a list of
lines without a live connection. The client feeds it ``resp.aiter_lines``;
tests feed it a hand-built async iterator.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """One parsed SSE frame.

    ``event`` is the ``event:`` field (defaults to ``"message"`` per the
    SSE spec when omitted). ``data`` is the parsed JSON ``data:`` payload
    when it parses as a JSON object, else ``{}`` with the raw text kept
    on :attr:`raw_data` so a non-JSON frame is never silently dropped.
    """

    event: str
    data: dict[str, Any] = field(default_factory=dict)
    raw_data: str = ""

    @property
    def is_token(self) -> bool:
        return self.event == "token"

    @property
    def is_done(self) -> bool:
        return self.event == "done"

    @property
    def is_error(self) -> bool:
        return self.event == "error"

    @property
    def text(self) -> str:
        """The token delta for a ``token`` frame (``data.text``)."""
        value = self.data.get("text")
        return value if isinstance(value, str) else ""


def _build_event(event_lines: list[str], data_lines: list[str]) -> StreamEvent:
    """Assemble a :class:`StreamEvent` from accumulated field lines.

    Per the SSE spec, multiple ``data:`` lines join with ``\\n``. We try
    to parse the joined payload as a JSON object; non-object / non-JSON
    payloads keep their raw text on :attr:`StreamEvent.raw_data`.
    """
    event = event_lines[-1] if event_lines else "message"
    raw_data = "\n".join(data_lines)
    data: dict[str, Any] = {}
    if raw_data:
        try:
            parsed = json.loads(raw_data)
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return StreamEvent(event=event, data=data, raw_data=raw_data)


def _strip_field(line: str, prefix: str) -> str:
    """Return the value of an SSE field line, honoring the optional space.

    SSE allows ``field:value`` and ``field: value`` (one leading space is
    stripped). Matches how the runtime writes ``event: x`` / ``data: {…}``.
    """
    value = line[len(prefix) :]
    if value.startswith(" "):
        value = value[1:]
    return value


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[StreamEvent]:
    """Parse an async iterator of SSE *lines* into :class:`StreamEvent`s.

    A blank line terminates the current frame and yields it. ``event:``
    and ``data:`` fields accumulate until then; ``:``-prefixed comment
    lines and unknown fields are ignored (per spec). A trailing frame
    with no terminating blank line is still yielded at end-of-stream.
    """
    event_lines: list[str] = []
    data_lines: list[str] = []
    have_fields = False

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line == "":
            if have_fields:
                yield _build_event(event_lines, data_lines)
                event_lines = []
                data_lines = []
                have_fields = False
            continue
        if line.startswith(":"):
            continue  # SSE comment / keep-alive
        if line.startswith("event:"):
            event_lines.append(_strip_field(line, "event:"))
            have_fields = True
        elif line.startswith("data:"):
            data_lines.append(_strip_field(line, "data:"))
            have_fields = True
        # Unknown fields (id:, retry:, …) are ignored — not needed here.

    if have_fields:
        yield _build_event(event_lines, data_lines)


def parse_sse_lines(lines: Iterable[str]) -> list[StreamEvent]:
    """Synchronous convenience wrapper over :func:`iter_sse_events`.

    Parses a finite iterable of lines into a list of events — handy for
    unit tests that have the full frame text in hand. Shares the exact
    framing logic so the sync + async paths can't drift.
    """
    events: list[StreamEvent] = []
    event_lines: list[str] = []
    data_lines: list[str] = []
    have_fields = False
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line == "":
            if have_fields:
                events.append(_build_event(event_lines, data_lines))
                event_lines, data_lines, have_fields = [], [], False
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_lines.append(_strip_field(line, "event:"))
            have_fields = True
        elif line.startswith("data:"):
            data_lines.append(_strip_field(line, "data:"))
            have_fields = True
    if have_fields:
        events.append(_build_event(event_lines, data_lines))
    return events
