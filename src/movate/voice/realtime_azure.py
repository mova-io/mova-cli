"""Azure OpenAI Realtime adapter — full-duplex voice↔voice (ADR 048 D2b / Phase 2).

The **sovereignty-preserving** realtime backend behind the optional realtime
seam (:class:`movate.voice.base.RealtimeVoiceProvider`), run against the
**customer's own Azure OpenAI resource** so audio stays in the tenant's Azure
subscription (ADR 048 D6 / the T3 realtime tier's data-residency rationale). It
is the Azure twin of :class:`movate.voice.realtime_openai.OpenAIRealtime`: the
Azure OpenAI Realtime API speaks the **same wire events** as the public OpenAI
Realtime API, so the two adapters share the event-translation logic
(:func:`movate.voice.realtime_openai._translate_event`) and differ only in how
the connection is opened (an ``AsyncAzureOpenAI`` client + a *deployment* name
in place of a model id, plus an Azure *endpoint* + API version).

The ``openai`` SDK import is **lazy + guarded** exactly like the public-OpenAI
realtime adapter and :mod:`movate.providers.openai_native`: nothing here imports
``openai`` at module scope, so a runtime/CLI installed without ``mdk[voice]`` is
wholly unaffected (ADR 048 D9). Tests inject a fake via ``connect=``.

BYOK (ADR 048 D6 / ADR 018): the tenant key is passed in via ``api_key=`` and
wins over the constructor default. With no per-call key the SDK reads its own
``AZURE_OPENAI_API_KEY`` env — already in the credential autoload whitelist
(:data:`movate.credentials.loader.PROVIDER_KEY_ENV_VARS`), so realtime needs
**no new credential var**. The Azure *endpoint* + *deployment* + *api_version*
are non-secret routing values supplied via the constructor (defaulting to the
``AZURE_OPENAI_ENDPOINT`` / ``AZURE_OPENAI_API_VERSION`` env the SDK reads).

See :mod:`movate.voice.realtime_openai` for the session/event shape notes —
this adapter only swaps the connection factory; the streaming loop + event
mapping are identical (and shared) because the wire protocol is identical.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from movate.voice.base import AudioChunk, AudioCodec, RealtimeChunk
from movate.voice.realtime_openai import RealtimeConnect, _require_openai, _stream_session

# Azure's Realtime preview is exposed on a dated API version. Pinned the same
# way the public adapter pins its model snapshot — re-confirm at build time.
_DEFAULT_API_VERSION = "2024-10-01-preview"


class AzureOpenAIRealtime:
    """Azure OpenAI Realtime :class:`~movate.voice.base.RealtimeVoiceProvider`.

    Full-duplex voice↔voice over the customer's **own** Azure OpenAI resource.
    Constructed with the Azure ``deployment`` (the realtime deployment name in
    the customer's resource), the ``endpoint`` + ``api_version`` routing values,
    and a ``default_voice``. Production opens an ``AsyncAzureOpenAI`` connection
    on first use; tests inject ``connect=`` so no ``openai`` package / network /
    key is needed.
    """

    name = "azure_openai_realtime"
    version = "0.0.1"

    def __init__(
        self,
        *,
        deployment: str = "",
        endpoint: str | None = None,
        api_version: str = _DEFAULT_API_VERSION,
        default_voice: str = "alloy",
        connect: RealtimeConnect | None = None,
    ) -> None:
        # ``deployment`` is Azure's analogue of the model id; it can also come
        # from env (AZURE_OPENAI_REALTIME_DEPLOYMENT) for the dev path. Endpoint
        # falls back to the SDK's AZURE_OPENAI_ENDPOINT env when unset.
        self._deployment = deployment
        self._endpoint = endpoint
        self._api_version = api_version
        self._default_voice = default_voice
        self._connect = connect

    def _resolve_deployment(self) -> str:
        deployment = self._deployment or os.environ.get("AZURE_OPENAI_REALTIME_DEPLOYMENT", "")
        if not deployment:
            raise ValueError(
                "Azure OpenAI Realtime needs a deployment name: pass "
                "AzureOpenAIRealtime(deployment=...) or set "
                "AZURE_OPENAI_REALTIME_DEPLOYMENT."
            )
        return deployment

    def _resolve_connect(self) -> RealtimeConnect:
        if self._connect is not None:
            return self._connect

        endpoint = self._endpoint
        api_version = self._api_version

        def _default(api_key: str | None, deployment: str) -> Any:
            openai_mod = _require_openai()
            kwargs: dict[str, Any] = {"api_version": api_version}
            if api_key:  # tenant BYOK key wins (ADR 018); else SDK reads its env
                kwargs["api_key"] = api_key
            if endpoint:
                kwargs["azure_endpoint"] = endpoint
            client = openai_mod.AsyncAzureOpenAI(**kwargs)
            # Azure routes by *deployment*, passed where the public API takes
            # ``model`` — the SDK maps it to the deployment under the hood.
            return client.beta.realtime.connect(model=deployment)

        return _default

    async def session(
        self,
        audio_in: AsyncIterator[AudioChunk],
        *,
        voice_id: str = "",
        instructions: str = "",
        language: str | None = None,
        codec: AudioCodec = "pcm16",
        api_key: str | None = None,
    ) -> AsyncIterator[RealtimeChunk]:
        # ``language`` is accepted for the Protocol; the realtime model
        # auto-detects, so there's no per-session knob (same as the public adapter).
        _ = language
        deployment = self._resolve_deployment()
        connect = self._resolve_connect()
        # The streaming loop + event mapping are byte-for-byte identical to the
        # public OpenAI Realtime adapter (same wire protocol), so we share the
        # one driver rather than duplicating it (CLAUDE.md rule 4).
        async for out in _stream_session(
            connect=connect,
            target=deployment,
            audio_in=audio_in,
            voice_id=voice_id or self._default_voice,
            instructions=instructions,
            codec=codec,
            api_key=api_key,
        ):
            yield out
