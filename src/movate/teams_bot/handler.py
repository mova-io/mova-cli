"""Bot Framework Activity → reply Activity dispatcher.

The handler is an async function: it takes an inbound :class:`Activity`
plus a :class:`HandlerContext` (carries the runtime client, langfuse
host, etc.) and returns a :class:`ReplyActivity` (or ``None`` for
activities we deliberately ignore). It does NOT do HTTP — the FastAPI
app calls it and serialises the result.

What's new in slice 3.1.c
-------------------------

* **Per-user identity binding**. Three new DM-only commands:
  ``connect <api-key>``, ``whoami``, ``disconnect``. The bot stores
  the user's Movate API key (encrypted at rest via :mod:`crypto`)
  and uses THEIR key for every subsequent ``run`` so the
  ``RunRecord.created_by`` audit trail attributes back correctly.
* :class:`HandlerContext` gains ``identity_resolver`` — when set,
  the ``run`` path looks up the user's bound client first; falls
  back to ``runtime_client`` (the fleet key) when no binding exists
  AND ``require_binding`` is False (the alpha default).

Earlier slices
--------------

* 3.1.b — Adaptive Cards + MovateClient integration. ``run`` calls
  the runtime and renders an Adaptive Card with cost/latency/trace.
* 3.1.a — Skeleton. Activity parsing, plain-text replies.

What's still deferred (3.1.d / .e / 3.2)
----------------------------------------

* File-attachment ingestion (drag agent.yaml + dataset.jsonl) — 3.1.d.
* Teams manifest + Bot Service registration — 3.1.e.
* Eval-with-upload + 4-dim scorecard — 3.2.
* ``rotate-key`` command (3.1.c follow-up; same flow as connect, just
  different command word — easy add).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from movate.core.auth import ApiKeyParseError, parse_api_key
from movate.core.client import MovateClient
from movate.teams_bot.activity import Activity, Attachment, ReplyActivity
from movate.teams_bot.cards import build_error_card, build_run_result_card
from movate.teams_bot.cards._common import ADAPTIVE_CARD_CONTENT_TYPE
from movate.teams_bot.client import RunOutcome, execute_run
from movate.teams_bot.crypto import TeamsCryptoError
from movate.teams_bot.identity import IdentityResolver
from movate.teams_bot.parser import ParsedCommand, parse_command
from movate.teams_bot.storage import TeamsUsersStore


@dataclass
class HandlerContext:
    """Per-app collaborators handed to the handler on every request.

    Held on ``FastAPI.app.state.handler_ctx`` and passed through the
    endpoint. The underlying long-lived collaborators (MovateClient,
    TeamsUsersStore, IdentityResolver) are built once per bot process
    — see :func:`build_app`.

    Adding a field is one line + a default — future slices add an
    attachment_handler (3.1.d), a notification_resolver, etc.
    """

    runtime_client: MovateClient | None = None
    """**Fleet** HTTP client bound to the deployed Movate runtime.
    Used as a fallback when ``identity_resolver`` is set but the user
    isn't bound (and ``require_binding`` is False), or when the bot
    runs in single-tenant mode (no per-user keys). ``None`` when the
    operator started the bot without a runtime URL — `run` then
    returns a config-error card."""

    langfuse_public_host: str | None = None
    """When set (e.g. ``https://langfuse.movate.com``), successful run
    cards get a "View trace" button deep-linking to the run's trace.
    Off by default; the link only surfaces when we know the host is
    routable for the audience (don't show prospects an internal URL)."""

    identity_resolver: IdentityResolver | None = None
    """Per-user MovateClient resolver. ``None`` disables the
    identity-binding feature entirely — `run` always uses the fleet
    client (3.1.b behavior). When set, `run` looks up the user's
    bound client; falls back to fleet unless ``require_binding`` is
    True. ``connect``/``whoami``/``disconnect`` REQUIRE this; they
    return a config-error card when ``None``."""

    users_store: TeamsUsersStore | None = None
    """Direct access to the binding store, used by ``connect`` /
    ``whoami`` / ``disconnect`` for the mutating operations. The
    resolver above is for ``run``'s read-path. Both share the same
    underlying sqlite db; the resolver caches the derived MovateClient
    while the store owns the persistent record."""

    require_binding: bool = False
    """Strict mode. When True, ``run`` rejects with a "please connect"
    card for unbound users instead of falling back to the fleet
    client. Default False for alpha — let internal users smoke-test
    without per-user binding. Set True for multi-tenant deployments
    where attribution matters."""


# Help text shown for `@movate help`. Now mentions the identity-
# binding commands that shipped in 3.1.c.
_HELP_TEXT = (
    "👋 movate bot — commands:\n"
    "\n"
    "• `@movate ping` — liveness check\n"
    "• `@movate run <agent> <json-input>` — run an agent + card reply\n"
    "• `@movate help` — this message\n"
    "\n"
    "**Identity (DM the bot)**\n"
    "• `/movate connect <api-key>` — bind your Movate API key\n"
    "• `/movate whoami` — show your current binding\n"
    "• `/movate disconnect` — remove your binding\n"
    "\n"
    "More coming: `eval` (3.2), `rotate-key` (follow-up). Track in ADR 003."
)


# Identity commands MUST be in a 1:1 DM (conversation_type=="personal").
# Channel posts get rejected so API keys don't leak — the user pastes
# the key in chat, which is visible to everyone in a channel but only
# to the user + bot in a personal scope.
def _is_dm(activity: Activity) -> bool:
    return activity.conversation.conversation_type == "personal"


def _dm_only_reject(activity: Activity, command: str) -> ReplyActivity:
    """Friendly rejection card for identity commands posted in a
    channel. Tells the user to DM the bot instead of pasting their
    key in a channel."""
    return _card_reply(
        activity,
        card=build_error_card(
            title=f"`{command}` is DM-only",
            message=(
                f"`/movate {command}` reveals or uses your Movate API key "
                "— that's not safe in a channel. DM me directly and I'll "
                "process it there."
            ),
            hint=(
                "In Teams: click my avatar → 'Send a chat message' → "
                "paste the command. The Bot Framework Emulator's "
                "'Conversation' menu has a similar 'Start over' that "
                "creates a personal-scope conversation."
            ),
            category="channel_rejected",
        ),
        fallback_text=f"/{command} is DM-only — DM the bot.",
    )


def _text_reply(activity: Activity, text: str) -> ReplyActivity:
    """Build a text-only reply (no card).

    Used for trivial commands like ``ping`` / ``help`` and for cases
    where the parse failed so completely that there's nothing to
    render in card form.
    """
    return ReplyActivity(
        type="message",
        text=text,
        replyToId=activity.id,
        conversation=activity.conversation,
    )


def _card_reply(
    activity: Activity,
    *,
    card: dict[str, Any],
    fallback_text: str = "",
) -> ReplyActivity:
    """Build a reply carrying an Adaptive Card attachment.

    ``fallback_text`` shows on channels that don't render cards
    (none today for Teams, but Bot Framework lets us deploy to other
    channels later). It's also what screen readers fall back to.
    """
    return ReplyActivity(
        type="message",
        text=fallback_text,
        replyToId=activity.id,
        conversation=activity.conversation,
        attachments=[
            Attachment(contentType=ADAPTIVE_CARD_CONTENT_TYPE, content=card),
        ],
    )


async def handle_activity(
    activity: Activity,
    ctx: HandlerContext | None = None,
) -> ReplyActivity | None:
    """Dispatch an inbound Activity to the matching command handler.

    ``ctx`` is optional for back-compat with the 3.1.a test suite that
    didn't pass one — when ``None``, we use an empty default context
    (no runtime client, no langfuse host). The ``run`` path checks for
    a configured client and returns an error card when missing.

    Returns ``None`` for activities we deliberately don't respond to
    (conversationUpdate, empty messages, etc.) — the FastAPI app
    surfaces this as ``HTTP 200`` with an empty body, which Teams
    treats as "no reply, OK".
    """
    if ctx is None:
        ctx = HandlerContext()

    cmd = parse_command(activity)

    if cmd.action == "empty":
        # Bot was added to a channel, or user sent a message that's
        # just an @mention with no command. Either way: don't spam.
        return None

    if cmd.action == "ping":
        return _text_reply(activity, "pong")

    if cmd.action == "help":
        return _text_reply(activity, _HELP_TEXT)

    if cmd.action == "run":
        return await _handle_run(activity, cmd, ctx)

    if cmd.action == "connect":
        return await _handle_connect(activity, cmd, ctx)

    if cmd.action == "whoami":
        return await _handle_whoami(activity, ctx)

    if cmd.action == "disconnect":
        return await _handle_disconnect(activity, ctx)

    # Unknown command — render the static help as a friendly fallback.
    first_word = cmd.raw_args.split(maxsplit=1)[0] if cmd.raw_args else ""
    return _text_reply(
        activity,
        f"❓ I don't recognize `{first_word}` as a command. Try `@movate help`.",
    )


async def _handle_run(
    activity: Activity,
    cmd: ParsedCommand,
    ctx: HandlerContext,
) -> ReplyActivity:
    """Execute a ``run`` command and render the result as a card.

    Six paths:

    1. **Parse error** — bad JSON or missing arg.
    2. **No runtime configured** — bot started without a URL.
    3. **Resolve which client to use**:
       a. Identity resolver wired + user is bound → use their client.
       b. Identity resolver wired + user is unbound + ``require_binding``
          → reject with a "please connect" card.
       c. Otherwise → fall back to the fleet client (3.1.b behavior).
    4. **Successful execution** — render run-result card.
    5. **Terminal failure** — render error card.
    6. **Timeout** — render error card with job id.
    """
    # Path 1: parse error.
    if cmd.parse_error:
        return _card_reply(
            activity,
            card=build_error_card(
                title="Couldn't parse `run`",
                message=cmd.parse_error,
                hint=(
                    'Usage: `@movate run <agent-name> {"...": "..."}`. '
                    "JSON must be a single object."
                ),
                category="parse_error",
            ),
            fallback_text=f"Couldn't parse run: {cmd.parse_error}",
        )

    # Path 3: pick the client.
    client_for_run, client_choice_reply = await _resolve_run_client(activity, ctx)
    if client_choice_reply is not None:
        return client_choice_reply
    assert client_for_run is not None  # the helper guarantees this branch invariant

    # Paths 4-6: actually execute.
    outcome = await execute_run(
        client=client_for_run,
        agent=cmd.agent,
        input_payload=cmd.input,
    )
    return _render_outcome(activity, outcome, ctx)


async def _resolve_run_client(
    activity: Activity,
    ctx: HandlerContext,
) -> tuple[MovateClient | None, ReplyActivity | None]:
    """Pick the MovateClient to use for a ``run``.

    Returns ``(client, None)`` on success, ``(None, error_reply)`` when
    no client is available (config error / unbound + strict mode).

    Resolution order:

    1. Identity resolver wired + user has a binding → that client.
    2. Identity resolver wired + unbound user + ``require_binding=True``
       → 'please connect' error card.
    3. Fleet runtime client → use it.
    4. Otherwise → 'no runtime configured' error card.
    """
    aad_id = activity.from_.aad_object_id

    # Prefer the user's bound client when the resolver is wired.
    if ctx.identity_resolver is not None and aad_id:
        try:
            bound = await ctx.identity_resolver.client_for(aad_id)
        except TeamsCryptoError as exc:
            # Encryption key probably rotated since the binding was
            # written. Surface a clear "please rebind" message.
            return None, _card_reply(
                activity,
                card=build_error_card(
                    title="Couldn't decrypt your stored key",
                    message=str(exc),
                    hint=(
                        "Mint a fresh key with `mdk auth create-key` "
                        "and DM me `/movate connect <new-key>`."
                    ),
                    category="encryption_drift",
                ),
                fallback_text="Couldn't decrypt your stored key.",
            )
        if bound is not None:
            return bound, None
        # Unbound user. Reject in strict mode; otherwise fall through
        # to the fleet client (3.1.b behavior).
        if ctx.require_binding:
            return None, _card_reply(
                activity,
                card=build_error_card(
                    title="Not connected yet",
                    message=(
                        "This bot requires every user to bind their own "
                        "Movate API key. DM me `/movate connect <api-key>` "
                        "to set yours."
                    ),
                    hint=("Generate a key with `mdk auth create-key --tenant <yours>`."),
                    category="no_binding",
                ),
                fallback_text="Not connected — DM /movate connect <api-key>.",
            )

    # Fallback to fleet client.
    if ctx.runtime_client is None:
        return None, _card_reply(
            activity,
            card=build_error_card(
                title="No runtime configured",
                message=(
                    "This bot wasn't started with a runtime URL. "
                    "The `run` command needs a deployed Movate runtime to call."
                ),
                hint=(
                    "Restart with `mdk teams-bot serve --runtime-url "
                    "http://...` or set MOVATE_RUNTIME_URL in the env."
                ),
                category="config_error",
            ),
            fallback_text="No runtime configured for this bot.",
        )
    return ctx.runtime_client, None


# ---------------------------------------------------------------------------
# Identity commands — connect / whoami / disconnect (DM only)
# ---------------------------------------------------------------------------


def _require_identity_ctx(
    activity: Activity, ctx: HandlerContext, *, command: str
) -> ReplyActivity | None:
    """Common precondition check for identity commands. Returns an
    error reply when the bot isn't configured for identity binding,
    or ``None`` when it's safe to proceed."""
    if ctx.users_store is None or ctx.identity_resolver is None:
        return _card_reply(
            activity,
            card=build_error_card(
                title=f"`{command}` is not available on this bot",
                message=(
                    "Identity binding requires a configured "
                    "MOVATE_TEAMS_ENCRYPTION_KEY + a writable bot DB. "
                    "This bot was started without one."
                ),
                hint=(
                    "Operator: set MOVATE_TEAMS_ENCRYPTION_KEY and restart `mdk teams-bot serve`."
                ),
                category="config_error",
            ),
            fallback_text=f"`/{command}` not available.",
        )
    return None


async def _handle_connect(
    activity: Activity,
    cmd: ParsedCommand,
    ctx: HandlerContext,
) -> ReplyActivity:
    """Bind a Teams user to a Movate API key. DM-only."""
    if not _is_dm(activity):
        return _dm_only_reject(activity, "connect")

    pre = _require_identity_ctx(activity, ctx, command="connect")
    if pre is not None:
        return pre
    assert ctx.users_store is not None and ctx.identity_resolver is not None

    if cmd.parse_error:
        return _card_reply(
            activity,
            card=build_error_card(
                title="Couldn't parse `connect`",
                message=cmd.parse_error,
                category="parse_error",
            ),
            fallback_text=cmd.parse_error,
        )

    aad_id = activity.from_.aad_object_id
    if not aad_id:
        return _card_reply(
            activity,
            card=build_error_card(
                title="Missing AAD object id",
                message=(
                    "Teams didn't tell us your AAD object id, so we can't "
                    "bind this DM to a Movate API key. This usually means "
                    "the bot isn't installed under your tenant — please "
                    "ping the bot's operator."
                ),
                category="no_aad_id",
            ),
            fallback_text="Missing AAD object id.",
        )

    # Validate key format. Surfaces the structured ApiKeyParseError so
    # the user sees "wrong env segment" vs "wrong key length".
    try:
        parsed = parse_api_key(cmd.api_key)
    except ApiKeyParseError as exc:
        return _card_reply(
            activity,
            card=build_error_card(
                title="That doesn't look like a Movate API key",
                message=str(exc),
                hint=(
                    "Expected shape: `mvt_<env>_<tenant>_<keyid>_<secret>`. "
                    "Generate one with `mdk auth create-key`."
                ),
                category="malformed_key",
            ),
            fallback_text="Malformed API key.",
        )

    binding = await ctx.users_store.upsert_binding(
        aad_object_id=aad_id,
        tenant_prefix=parsed.tenant_prefix,
        api_key_plaintext=cmd.api_key,
    )
    # Drop any cached MovateClient for this user — the resolver builds
    # a fresh one on the next ``run`` using the just-stored key.
    await ctx.identity_resolver.invalidate(aad_id)

    return _card_reply(
        activity,
        card=build_error_card(
            title="✓ Connected",
            message=(
                f"Bound to tenant `{binding.tenant_prefix}`. "
                f"Future `@movate run` calls will use your key "
                f"(`...{binding.key_hint}`)."
            ),
            hint=(
                "Run `/movate whoami` to verify, or paste a fresh key "
                "into `/movate connect` to rotate."
            ),
        ),
        fallback_text=(f"✓ Connected to tenant {binding.tenant_prefix} (...{binding.key_hint})."),
    )


async def _handle_whoami(
    activity: Activity,
    ctx: HandlerContext,
) -> ReplyActivity:
    """Show the current binding (no plaintext key)."""
    if not _is_dm(activity):
        return _dm_only_reject(activity, "whoami")

    pre = _require_identity_ctx(activity, ctx, command="whoami")
    if pre is not None:
        return pre
    assert ctx.users_store is not None

    aad_id = activity.from_.aad_object_id or ""
    binding = await ctx.users_store.get_binding(aad_id) if aad_id else None
    if binding is None:
        return _card_reply(
            activity,
            card=build_error_card(
                title="Not connected",
                message=(
                    "You haven't bound a Movate API key yet. DM me "
                    "`/movate connect <api-key>` to set one."
                ),
                hint=("Mint a key with `mdk auth create-key --tenant <yours>`."),
                category="no_binding",
            ),
            fallback_text="Not connected.",
        )
    return _card_reply(
        activity,
        card=build_error_card(
            title="Connected",
            message=(
                f"Tenant: `{binding.tenant_prefix}` · "
                f"Key: `...{binding.key_hint}` · "
                f"Bound: {binding.created_at:%Y-%m-%d %H:%M UTC}"
            ),
            hint=("Use `/movate connect <new-key>` to rotate, or `/movate disconnect` to remove."),
        ),
        fallback_text=(f"Connected: tenant {binding.tenant_prefix}, key ...{binding.key_hint}."),
    )


async def _handle_disconnect(
    activity: Activity,
    ctx: HandlerContext,
) -> ReplyActivity:
    """Remove the current binding."""
    if not _is_dm(activity):
        return _dm_only_reject(activity, "disconnect")

    pre = _require_identity_ctx(activity, ctx, command="disconnect")
    if pre is not None:
        return pre
    assert ctx.users_store is not None and ctx.identity_resolver is not None

    aad_id = activity.from_.aad_object_id or ""
    deleted = await ctx.users_store.delete_binding(aad_id) if aad_id else False
    await ctx.identity_resolver.invalidate(aad_id)

    if deleted:
        return _card_reply(
            activity,
            card=build_error_card(
                title="✓ Disconnected",
                message=(
                    "Your Movate API key has been removed from this bot. "
                    "Future `@movate run` calls will fall back to the "
                    "fleet key (or fail if the bot runs in strict mode)."
                ),
            ),
            fallback_text="✓ Disconnected.",
        )
    return _card_reply(
        activity,
        card=build_error_card(
            title="Not connected",
            message="You don't have a binding to remove.",
            category="no_binding",
        ),
        fallback_text="No binding to remove.",
    )


def _render_outcome(
    activity: Activity,
    outcome: RunOutcome,
    ctx: HandlerContext,
) -> ReplyActivity:
    """Pick the right card template for a :class:`RunOutcome` variant."""
    if outcome.kind == "success" and outcome.run is not None:
        return _card_reply(
            activity,
            card=build_run_result_card(
                outcome.run,
                langfuse_public_host=ctx.langfuse_public_host,
            ),
            fallback_text=_success_fallback_text(outcome),
        )

    if outcome.kind == "timeout":
        return _card_reply(
            activity,
            card=build_error_card(
                title="Job still running",
                message=outcome.message,
                hint=outcome.hint,
                category="timeout",
            ),
            fallback_text=f"Job still running: {outcome.job_id}",
        )

    # terminal_failure OR client_failure both render via the error card.
    title = "Run failed" if outcome.kind == "terminal_failure" else "Couldn't submit run"
    return _card_reply(
        activity,
        card=build_error_card(
            title=title,
            message=outcome.message,
            hint=outcome.hint or None,
            category=outcome.category or None,
        ),
        fallback_text=f"{title}: {outcome.message}",
    )


def _success_fallback_text(outcome: RunOutcome) -> str:
    """Plain-text fallback for the success path.

    Renders on channels that don't support Adaptive Cards (none today,
    but Bot Framework can deploy to e.g. Slack via the same activities
    — fallback text matters there). One-line summary of the response.
    """
    if outcome.run is None or outcome.run.output is None:
        return "✅ run succeeded"
    # Use a compact dump so the fallback fits in a single channel line.
    return f"✅ {json.dumps(outcome.run.output, ensure_ascii=False)}"
