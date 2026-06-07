"""Auto-load credentials at CLI startup with the canonical precedence.

Called by :mod:`movate.cli.main` after ``load_dotenv()``. The order
matters: dotenv loads project-level ``.env`` first; then this module
fills in any provider key that ISN'T already set from the
machine-global file. That gives us the right precedence — shell env
> project .env > credentials file — without the credentials file
ever clobbering an explicit project-level value.
"""

from __future__ import annotations

import os
from typing import Literal

from movate.credentials.store import CredentialsStore

# Every provider env var movate knows about. Operators set these
# (in shell / .env / credentials file) and `_has_any_provider_key`
# downstream checks them. Kept here as the single source of truth;
# :mod:`movate.cli.doctor` + :mod:`movate.cli.init` re-export this.
PROVIDER_KEY_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LYZR_API_KEY",
)

# Notification env vars — written to the credentials file by
# ``mdk auth login telegram`` and consumed by ``mdk deploy --notify``
# and the auth-picker ``✓ configured`` marker. Kept separate from
# PROVIDER_KEY_ENV_VARS because they're not LLM-provider auth — but
# they DO need the same autoload-from-credentials-file treatment so
# operators don't have to manually export them in their shell.
NOTIFICATION_KEY_ENV_VARS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "MOVATE_DEPLOY_WEBHOOK",
)

# Observability env vars — Langfuse tracing. Set via ``mdk auth login
# langfuse`` (or by hand). The tracer auto-activates when
# ``LANGFUSE_SECRET_KEY`` is present in the environment; all three
# vars need the same autoload-from-credentials-file treatment so
# traces are emitted without requiring a manual ``export`` each shell.
# Both ``LANGFUSE_HOST`` (canonical) and ``LANGFUSE_BASE_URL`` (alias
# accepted for compatibility with the Langfuse SDK default name) are
# included — the tracer checks both.
OBSERVABILITY_KEY_ENV_VARS: tuple[str, ...] = (
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_HOST",
    "LANGFUSE_BASE_URL",
)

# Voice-provider env vars (ADR 048/049, the ``[voice]`` extra). Set via
# ``mdk auth login deepgram`` / ``cartesia`` / ``elevenlabs`` / ``azure-speech``
# (or by hand) and consumed by the speech adapters in ``movate.voice`` (Deepgram
# STT / Cartesia TTS / ElevenLabs TTS, plus the Azure Speech STT/TTS pair which
# reads key+region).
# Kept SEPARATE from PROVIDER_KEY_ENV_VARS on purpose: those are LLM-chat
# provider keys that gate ``_has_any_provider_key`` ("can this machine run a
# text agent?") and are live-verified against an LLM metadata endpoint by the
# auth-status table — a voice key can't run the text agent and has no such
# probe, so folding it in there would wrongly answer "yes". They still need
# the same autoload-from-credentials-file treatment (so operators don't
# re-export each shell), so they go through ``ALL_AUTOLOADED_ENV_VARS`` below
# — the same pattern the notification / observability groups use.
#
# Azure Speech needs a key AND a region (the region is a non-secret routing
# value, but it autoloads the same way so operators don't re-export it each
# shell). Distinct from AZURE_OPENAI_API_KEY (Azure OpenAI chat/embeddings) —
# Azure Speech is a different Azure resource with its own key.
VOICE_KEY_ENV_VARS: tuple[str, ...] = (
    "DEEPGRAM_API_KEY",
    "CARTESIA_API_KEY",
    "ELEVENLABS_API_KEY",
    "AZURE_SPEECH_KEY",
    "AZURE_SPEECH_REGION",
)

# Telephony-provider env vars (ADR 074, the ``[telephony]`` extra). Set via
# ``mdk auth login twilio`` (or by hand) and consumed by the Twilio transport
# in ``movate.voice.transports.twilio``. Kept SEPARATE from VOICE_KEY_ENV_VARS
# because telephony is a transport concern, not a speech-adapter concern --
# but they need the same autoload-from-credentials-file treatment.
TELEPHONY_KEY_ENV_VARS: tuple[str, ...] = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
)

# Enterprise connector env vars (ADR 052 Phase 1 — Action Fabric).
# Set via ``mdk auth login servicenow`` / ``msgraph`` (or by hand) and
# consumed by the HTTP skill backend when dispatching connector skills.
# ServiceNow needs an API key + instance URL; Microsoft Graph needs an
# access token + tenant ID.
CONNECTOR_KEY_ENV_VARS: tuple[str, ...] = (
    "SERVICENOW_API_KEY",
    "SERVICENOW_INSTANCE_URL",
    "MSGRAPH_ACCESS_TOKEN",
    "MSGRAPH_TENANT_ID",
)

# Temporal connection (ADR 054) — host + namespace + optional TLS cert/key for
# Temporal Cloud; reads from ~/.movate/credentials via ``mdk auth login
# temporal``. Same BYOK seam as every other provider credential (ADR 054 D8) —
# distinct from LLM keys because Temporal is a workflow BACKEND, not an LLM
# provider, so it lives in its own group (kept out of PROVIDER_KEY_ENV_VARS so
# ``_has_any_provider_key`` still answers "can this machine run a text agent?"
# correctly). The TLS pair is only required for Temporal Cloud; self-hosted
# (``temporal server start-dev`` locally, AKS, docker-compose) leaves them
# unset. All four go through the standard autoload-from-credentials-file
# pipeline so operators don't re-export each shell.
TEMPORAL_KEY_ENV_VARS: tuple[str, ...] = (
    "TEMPORAL_HOST",
    "TEMPORAL_NAMESPACE",
    "TEMPORAL_TLS_CERT",
    "TEMPORAL_TLS_KEY",
    # Set via ``mdk auth login workday`` / ``salesforce`` / ``sap`` (or by hand)
    # and consumed by the HTTP skill backend when dispatching connector skills.
    # Each connector needs a bearer token/API key + a routing URL.
    "WORKDAY_ACCESS_TOKEN",
    "WORKDAY_BASE_URL",
    "SALESFORCE_ACCESS_TOKEN",
    "SALESFORCE_INSTANCE_URL",
    "SAP_API_KEY",
    "SAP_BASE_URL",
)

# Neo4j graph adapter env vars (opt-in [neo4j] extra). Set via
# ``mdk auth login neo4j`` (or by hand) and consumed by the Neo4j
# storage adapter when ``MOVATE_GRAPH_BACKEND=neo4j`` is configured.
# ``NEO4J_URI`` is the bolt:// or neo4j:// connection string;
# ``NEO4J_USER`` + ``NEO4J_PASSWORD`` are the auth pair. Separate from
# PROVIDER_KEY_ENV_VARS (those are LLM keys, not graph-DB keys).
NEO4J_KEY_ENV_VARS: tuple[str, ...] = (
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
)

# Every env var the credentials store should autoload. Union of the
# groups above; surfaced as a constant so `autoload_credentials`
# and any future "what does mdk track?" enumeration agree on the
# canonical list.
ALL_AUTOLOADED_ENV_VARS: tuple[str, ...] = (
    *PROVIDER_KEY_ENV_VARS,
    *NOTIFICATION_KEY_ENV_VARS,
    *OBSERVABILITY_KEY_ENV_VARS,
    *VOICE_KEY_ENV_VARS,
    *TELEPHONY_KEY_ENV_VARS,
    *CONNECTOR_KEY_ENV_VARS,
    *TEMPORAL_KEY_ENV_VARS,
    *NEO4J_KEY_ENV_VARS,
)


# Where a credential ended up resolving from. Used by `mdk auth status`
# to tell operators "this key came from X, not Y".
KeySource = Literal["shell", "dotenv", "credentials_file", "unset"]

# Runtime-bearer keys (``MDK_<TARGET>_KEY``) whose saved file/keychain value
# was used to OVERRIDE a differing shell-exported value during the last
# :func:`autoload_credentials` run (ADR 022). Populated only for the
# file-authoritative runtime-key class — never for provider/notification/
# observability vars. Read via :func:`runtime_key_shadowed` so callers (the
# remote-context echo, ``mdk auth status``) can surface "the file won over a
# stale shell export" without re-deriving it. Process-global module state
# (the CLI is one process); tests reset it via :func:`_reset_shadow_state`.
_RUNTIME_KEY_SHADOWED: set[str] = set()


def runtime_key_shadowed(var: str) -> bool:
    """Did the saved value of runtime-bearer ``var`` override a differing shell value?

    ``True`` only when, during the most recent :func:`autoload_credentials`
    call, ``var`` matched :func:`_looks_like_runtime_key_env`, had a non-empty
    saved file/keychain value, AND a *differing* shell value was already
    present — so the file value won and the shell value was discarded (ADR
    022). Lets callers append a transparent " (shell value overridden)" note
    without re-implementing the resolution. ``False`` for every other case
    (no shell value, matching values, provider keys, unset).
    """
    return var in _RUNTIME_KEY_SHADOWED


def _reset_shadow_state() -> None:
    """Clear the recorded runtime-key shadows. Test-only seam.

    :func:`autoload_credentials` already clears + repopulates this on every
    call; this helper exists so tests can assert a clean baseline without
    invoking autoload."""
    _RUNTIME_KEY_SHADOWED.clear()


def autoload_credentials() -> None:
    """Fill in any unset autoloaded env vars from the credentials file.

    Called once per CLI invocation, AFTER ``dotenv.load_dotenv()``.
    The semantics:

    * If a key is already set in the environment (from shell OR .env),
      do nothing — explicit setters always win.
    * Otherwise, fill it in from ``~/.movate/credentials`` if present
      there.

    The set of autoloaded vars is :data:`ALL_AUTOLOADED_ENV_VARS` —
    LLM provider keys (e.g. ``OPENAI_API_KEY``) PLUS notification
    secrets (``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` /
    ``MOVATE_DEPLOY_WEBHOOK``). Both groups go through the same store
    + autoload pipeline because both are set via ``mdk auth login``
    and both need to be in ``os.environ`` for downstream code to
    function (LiteLLM reads provider keys, ``mdk deploy --notify``
    reads notification secrets, the auth picker reads everything to
    render the ``✓ configured`` marker).

    This implements the "narrowest beats widest" precedence model
    without having to thread credential source through every code
    path that reads the env. After this function runs, every
    downstream caller can just check ``os.environ`` like they always
    have — the lookup answers the right question because the lower-
    precedence sources were folded in here.

    **Class-split precedence (ADR 022).** Provider keys, notification
    secrets, and observability vars keep the never-clobber rule above
    (shell > .env > file) — those are user-owned secrets the world
    expects ``OPENAI_API_KEY=… cmd`` to override a config file. But
    **runtime-bearer keys** (``MDK_<TARGET>_KEY``) that ``mdk`` itself
    mints/saves/rotates into the file are **file-authoritative**: a
    non-empty saved value wins over a differing shell export and is
    written back into ``os.environ``. A no-saved-value runtime key
    still defers to the shell (CI / pure-shell unbroken). When the
    file overrides a *differing* shell value the var is recorded in
    :data:`_RUNTIME_KEY_SHADOWED` so callers can surface the override
    (never silent) — a matching value is a silent no-op.
    """
    # Always start the shadow ledger clean — autoload is the single
    # authority that repopulates it, so a re-invocation never leaves a
    # stale entry behind.
    _RUNTIME_KEY_SHADOWED.clear()
    store = CredentialsStore()
    file_entries = store.read()
    if not file_entries:
        return
    # Whitelist autoload: provider keys (OPENAI_API_KEY etc.) + notification
    # secrets. Operators set these via `mdk auth login <provider>` and
    # expect them to survive across shells without re-exporting. UNCHANGED
    # by ADR 022 — these are user-owned, so shell > .env > file (never
    # clobber an explicit shell/dotenv value).
    for key in ALL_AUTOLOADED_ENV_VARS:
        # Skip if already set (by shell OR by dotenv already running).
        if os.environ.get(key, "").strip():
            continue
        candidate = file_entries.get(key, "").strip()
        if candidate:
            os.environ[key] = candidate
    # Pattern-match autoload for MDK runtime bearer tokens (ADR 022).
    # Target configs in `~/.movate/config.yaml` declare a `key_env:`
    # field (e.g. `MDK_DEV_KEY`, `MDK_STAGING_KEY`, `MDK_PROD_KEY`) —
    # any variable matching `MDK_<X>_KEY` that's in the credentials
    # file is FILE-AUTHORITATIVE: the saved value wins even over a
    # differing shell export (mdk owns these keys; the file it writes
    # to is the source of truth). The escape hatch is to persist the
    # shell value (`mdk auth save-runtime-key`) or clear the saved one
    # — no override env var (ADR 022 D3). A runtime key with NO saved
    # value falls through untouched, so pure-shell / CI stays unbroken.
    for key, value in file_entries.items():
        if not _looks_like_runtime_key_env(key):
            continue
        saved = value.strip()
        if not saved:
            # No saved value → defer to whatever the shell has (rule 3:
            # CI / pure-shell path, unchanged).
            continue
        existing = os.environ.get(key, "").strip()
        if existing == saved:
            # File and shell agree (or shell was unset) — nothing to
            # override and nothing to warn about. Still hydrate env in
            # the unset case.
            if not existing:
                os.environ[key] = saved
            continue
        # Saved value differs from a present shell value → the FILE wins
        # (the inversion ADR 022 introduces). Record the shadow so the
        # point-of-use echo / `auth status` can surface it; a previously
        # unset var (existing == "") is a plain hydrate, not a shadow.
        os.environ[key] = saved
        if existing:
            _RUNTIME_KEY_SHADOWED.add(key)


def _looks_like_runtime_key_env(name: str) -> bool:
    """``MDK_<X>_KEY`` shape detector for runtime-bearer autoload.

    Matches the canonical pattern that `mdk config add-target
    --key-env` defaults to (e.g. ``MDK_DEV_KEY``,
    ``MDK_PROD_KEY``). Anything not matching this shape stays a
    manual export — we don't autoload arbitrary credentials file
    entries (would be a security footgun if someone tucked
    ``AWS_SECRET_ACCESS_KEY`` into the file).
    """
    return name.startswith("MDK_") and name.endswith("_KEY") and len(name) > len("MDK__KEY")


def key_source(key: str) -> KeySource:
    """Tell where the current value of ``key`` came from.

    Used by ``mdk auth status`` to render the resolution-path table.
    Distinguishes "set by shell BEFORE the CLI started" from "loaded
    from .env" from "loaded from credentials file" by checking the
    underlying sources without re-running autoload.

    Implementation note: shell vs dotenv ambiguity. ``load_dotenv()``
    sets values in ``os.environ`` AS IF they were shell-exported, so
    we can't distinguish them perfectly after the fact. We work
    around it by:

    1. Reading the project ``.env`` directly to see what dotenv would
       have set.
    2. Reading the credentials file to see what we autoloaded.
    3. Whichever has a value AND the current ``os.environ`` matches:
       attribute the source.
    4. If the env var is set and matches NEITHER source, it must be
       shell-set.

    ADR 022 interaction: for a file-authoritative runtime-bearer key,
    :func:`autoload_credentials` has already written the saved value
    into ``os.environ``, so step 3 attributes it to ``credentials_file``
    — the truthful source after the file won. The *fact that a shell
    value was overridden* is not encoded in this 4-state return (to keep
    it backward-compatible for provider-key callers); read it separately
    via :func:`runtime_key_shadowed`.
    """
    current = os.environ.get(key, "").strip()
    if not current:
        return "unset"

    # Check the credentials file — lowest precedence so we know it
    # FOUND a value here AND nothing higher overrode it.
    file_value = CredentialsStore().get(key) or ""
    file_value = file_value.strip()

    # Check the project .env (if present). This is a best-effort read
    # — we don't actually call dotenv, just check the file.
    dotenv_value = _read_dotenv_value(key)

    # Attribution heuristic: the source that matches the current
    # value AND is reachable wins. Shell wins if neither file
    # contains the matching value.
    if dotenv_value and dotenv_value == current:
        return "dotenv"
    if file_value and file_value == current:
        return "credentials_file"
    # The env var is set but no managed source has the matching value;
    # operator must have set it in their shell.
    return "shell"


def _read_dotenv_value(key: str) -> str:
    """Read one key from a project-local ``.env`` if it exists.

    Walks up from cwd looking for ``.env`` — same convention as
    dotenv's default. Returns the unquoted value, or empty string if
    not found.
    """
    from pathlib import Path  # noqa: PLC0415

    current = Path.cwd().resolve()
    while True:
        dotenv = current / ".env"
        if dotenv.is_file():
            for raw in dotenv.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
            return ""
        if current.parent == current:
            return ""
        current = current.parent
