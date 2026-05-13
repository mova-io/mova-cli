"""Parse a Bot Framework Activity into a slash-command tuple.

Supported commands (slice 3.1.a):

* ``help`` — print available commands. Always works.
* ``ping`` — liveness check ("pong"). Used by smoke tests + the
  Teams developer-portal "Test in chat" feature.
* ``run <agent-name> <json-input>`` — placeholder for the agent
  execution path. Slice 3.1.a parses the args + echoes them back;
  slice 3.1.b wires :class:`MovateClient` and renders an Adaptive
  Card with the result.

Future commands (out of scope here):

* ``eval <agent> <dataset>`` — slice 3.2
* ``connect <api-key>`` — slice 3.1.c (identity binding)
* ``rotate-key`` — slice 3.1.c

Design notes
------------

* **Mentions are stripped before parsing.** Teams sends
  ``"<at>movate</at> run faq-agent {...}"`` as ``Activity.text``.
  The actual ``@movate`` markup lives in ``Activity.entities`` as
  a :class:`Mention`. We strip every mention's ``text`` substring
  from ``Activity.text``, then tokenize the rest.
* **JSON args are everything after the agent name.** ``run`` takes
  an agent name + a JSON blob. We don't require the JSON to be
  quoted — splitting on whitespace would break ``{"question":
  "what is movate?"}``. So we take the first whitespace-delimited
  token as the agent name and the rest of the string as JSON.
* **Unknown commands → ``ParsedCommand(action="unknown", ...)``.**
  The handler surfaces this as a friendly error card rather than
  a 4xx response, so the user sees something useful in Teams.
* **Empty text → ``ParsedCommand(action="empty")``.** Happens on
  ``conversationUpdate`` activities (bot added to channel) — the
  handler treats it as a no-op.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from movate.teams_bot.activity import Activity


@dataclass
class ParsedCommand:
    """Result of parsing an :class:`Activity` into a command.

    ``action`` is the slash command name. The other fields are
    action-specific:

    * ``help`` / ``ping`` / ``empty`` / ``unknown`` — no extra fields.
    * ``run`` — ``agent`` (str) + ``input`` (parsed JSON dict).
    * ``connect`` — ``api_key`` (str). Validation lives in the handler.
    * ``whoami`` — no extra fields. Handler reads the user's binding.
    * ``disconnect`` — no extra fields. Handler removes the binding.
    * Future: ``eval`` — ``agent`` + dataset path; ``rotate-key`` —
      shares ``api_key`` with ``connect``.

    A ``parse_error`` field carries a human-readable explanation when
    the command was recognized but its arguments were malformed (e.g.
    ``run faq-agent not-json``). The handler echoes this to the user
    so they can correct their input without leaving Teams.
    """

    action: str
    """One of: ``help``, ``ping``, ``run``, ``connect``, ``whoami``,
    ``disconnect``, ``empty``, ``unknown``."""

    agent: str = ""
    """For ``run``: which agent to invoke."""

    input: dict[str, Any] = field(default_factory=dict)
    """For ``run``: the parsed JSON input payload."""

    api_key: str = ""
    """For ``connect``: the API key the user pasted. Never logged.
    Validation (regex + maybe a healthz check) happens in the handler
    because the parser doesn't know which validation is appropriate
    — same string would be valid under multiple key schemas."""

    raw_args: str = ""
    """Everything after the command word, before any parsing. Surfaced
    in the error path so we can echo back what the user typed."""

    parse_error: str = ""
    """Empty when parsing succeeded. Non-empty when the command was
    recognized but its arguments were malformed — the handler
    composes a friendly error from this."""


# Regex matching the standard Teams mention markup. Teams sends
# ``<at>BotName</at>`` in Activity.text; the entities array carries
# the same substring under ``mention.text``. We strip every entity's
# text by literal-substring match (safer than re-deriving the markup),
# and fall back to this regex for older clients that ship malformed
# entity arrays. Lazy quantifier so a single message with multiple
# mentions only matches one at a time per iteration.
_MENTION_MARKUP_RE = re.compile(r"<at>.*?</at>", re.IGNORECASE | re.DOTALL)


def _strip_mentions(activity: Activity) -> str:
    """Remove every ``@mention`` markup from the activity's text.

    Two passes: (1) strip every entity's literal text substring;
    (2) fall back to the markup regex for any leftover ``<at>…</at>``
    blocks the entity list missed (rare but happens on some channels
    when a mention is added mid-edit).
    """
    text = activity.text or ""
    for entity in activity.entities:
        if entity.text:
            text = text.replace(entity.text, "")
    text = _MENTION_MARKUP_RE.sub("", text)
    return text.strip()


def parse_command(activity: Activity) -> ParsedCommand:
    """Convert an inbound Activity into a structured command.

    Always returns a :class:`ParsedCommand` — never raises. Malformed
    inputs land in ``action="unknown"`` (for unrecognized commands)
    or ``action="<known>"`` with a non-empty ``parse_error`` (for
    recognized commands with bad arguments). The handler turns either
    into a friendly Teams reply.
    """
    # Non-message activities (conversationUpdate when bot is added,
    # typing indicators, etc.) yield ``empty``. Handler treats this
    # as a no-op.
    if activity.type != "message":
        return ParsedCommand(action="empty")

    text = _strip_mentions(activity)
    if not text:
        return ParsedCommand(action="empty")

    # Tokenize: first word is the command, rest is action-specific.
    # We use ``split(maxsplit=1)`` so the remainder keeps its
    # internal whitespace — important for ``run`` whose JSON arg
    # contains spaces.
    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    raw_args = parts[1] if len(parts) > 1 else ""

    if command == "help":
        return ParsedCommand(action="help")
    if command == "ping":
        return ParsedCommand(action="ping")
    if command == "run":
        return _parse_run(raw_args)
    if command == "connect":
        return _parse_connect(raw_args)
    if command == "whoami":
        return ParsedCommand(action="whoami")
    if command == "disconnect":
        return ParsedCommand(action="disconnect")
    return ParsedCommand(action="unknown", raw_args=text)


def _parse_connect(raw_args: str) -> ParsedCommand:
    """Parse ``connect <api-key>``.

    Just the shape — the handler does the actual validation (regex
    match against :func:`parse_api_key`) so it can produce the most
    informative error card. We strip whitespace because users often
    paste keys with leading spaces.
    """
    api_key = raw_args.strip()
    if not api_key:
        return ParsedCommand(
            action="connect",
            parse_error=(
                "missing API key. usage: `/movate connect <api-key>` "
                "(DM only — don't paste keys in channels). Generate one "
                "with `mdk auth create-key --tenant <yours>`."
            ),
        )
    return ParsedCommand(action="connect", api_key=api_key, raw_args=raw_args)


def _parse_run(raw_args: str) -> ParsedCommand:
    """Parse the ``run <agent> <json-input>`` arguments.

    Splits off the first whitespace-delimited token as the agent name
    and treats the rest of the string as a JSON object. A missing
    agent or unparseable JSON lands in ``parse_error`` so the handler
    can render a helpful card.
    """
    if not raw_args:
        return ParsedCommand(
            action="run",
            parse_error=("missing agent name. usage: `@movate run <agent> <json-input>`"),
        )
    sub = raw_args.split(maxsplit=1)
    agent = sub[0]
    body = sub[1] if len(sub) > 1 else ""
    if not body:
        return ParsedCommand(
            action="run",
            agent=agent,
            raw_args=raw_args,
            parse_error=(f"missing input JSON. usage: `@movate run {agent} {{...}}`"),
        )
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return ParsedCommand(
            action="run",
            agent=agent,
            raw_args=raw_args,
            parse_error=f"invalid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})",
        )
    if not isinstance(parsed, dict):
        return ParsedCommand(
            action="run",
            agent=agent,
            raw_args=raw_args,
            parse_error=(
                f"input must be a JSON object, got {type(parsed).__name__}. "
                f'example: `{{"question": "..."}}`'
            ),
        )
    return ParsedCommand(action="run", agent=agent, input=parsed, raw_args=raw_args)
