"""An OpenAI Chat Completions agent that implements the AgentTurn seam.

Wraps GPT-4o-mini's streaming Chat Completions API so the voice pipeline gets
token-by-token output (which `tts_streaming=True` then synthesizes sentence-by-
sentence). Trivial conversation memory inside one session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from movate.voice import AgentTurnError, AgentTurnResult


class OpenAIChatAgent:
    """An :class:`~movate.voice.AgentTurn` over the OpenAI Chat Completions API.

    Keeps a short rolling history per instance so multi-turn calls feel coherent.
    Streams tokens via ``on_token`` so the pipeline's sentence-streaming TTS can
    overlap synthesis with generation — the actual latency story.
    """

    name = "openai-chat"
    version = "1"

    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        system_prompt: str = (
            "You are a concise customer-support voice agent. Reply in one or two "
            "short sentences. You are being read aloud, so do not use Markdown, "
            "bullet lists, or code blocks. Speak naturally."
        ),
        history_turns: int = 10,
        base_url: str | None = None,
        api_key: str | None = None,
        send_system: bool = True,
        client: Any | None = None,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
        on_extras: Callable[[dict[str, Any]], None] | None = None,
        max_tokens: int = 800,
        voice_hint: str | None = None,
    ) -> None:
        """``base_url`` + ``api_key`` let us point this same class at any
        OpenAI-compatible endpoint — e.g. Lyzr's ``/v4/chat/completions`` (which
        we use for the Lyzr ADK tier so it gets token streaming + sentence-
        chunked TTS just like GPT-4o-mini does). ``send_system`` controls
        whether to prepend our system prompt — set False for hosted agents
        (Lyzr, etc.) that already have their own system prompt configured."""
        self._model = model
        self._system = system_prompt
        self._history_cap = max(2, history_turns * 2)
        self._history: list[dict[str, str]] = []
        self._base_url = base_url
        self._api_key = api_key
        self._send_system = send_system
        self._client = client
        # L3 — fire when the model emits a tool_call delta (Lyzr Studio agents
        # with tools, and OpenAI Chat with tools, both surface here).
        # L4 — fire when the response carries provider extras like citations
        # (Lyzr passes RAG sources via top-level chunk fields). Both are
        # optional — None = silent, the historic behavior.
        self.on_tool_call = on_tool_call
        self.on_extras = on_extras
        # Max output tokens: 800 leaves room for a real paragraph or two while
        # still capping a runaway agent. Bumped from 200 after a live demo
        # showed the Lyzr agent answering with multi-section structured
        # responses that got chopped mid-sentence. Set higher for long-form
        # use cases or lower (200-300) for snappy back-and-forth.
        self._max_tokens = max_tokens
        # When the backing agent is configured for CHAT (markdown, headers,
        # bullet lists, citations) but we're voicing the output, this hint
        # appended to the user message nudges the model toward voice-shaped
        # prose. We don't touch the agent's own system prompt (especially for
        # hosted agents like Lyzr where the operator owns it) — just add a
        # voice-context cue to the user turn. None = silent / no nudge.
        self._voice_hint = (voice_hint or "").strip() or None

    def reset(self) -> None:
        """Clear conversation memory (e.g. between callers)."""
        self._history.clear()

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        import openai  # noqa: PLC0415 - lazy

        kwargs: dict[str, Any] = {}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._api_key:
            kwargs["api_key"] = self._api_key
        return openai.AsyncOpenAI(**kwargs)

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurnResult:
        try:
            client = self._resolve_client()
            messages: list[dict[str, str]] = []
            if self._send_system:
                messages.append({"role": "system", "content": self._system})
            messages.extend(self._history)
            # Append the voice hint to the actual user turn (not as a system
            # message — that'd compete with the hosted agent's own system
            # prompt). The hint travels through Lyzr's voice channel as
            # additional caller context.
            user_content = text
            if self._voice_hint:
                user_content = f"{text}\n\n[Voice channel — {self._voice_hint}]"
            messages.append({"role": "user", "content": user_content})

            stream = await client.chat.completions.create(
                model=self._model,
                messages=messages,
                stream=True,
                max_tokens=self._max_tokens,
            )
            collected: list[str] = []
            # L3: incremental tool-call accumulator (OpenAI SSE sends them in
            # pieces — index + name first, then args streamed). Indexed by
            # tool_call.index so concurrent calls in one assistant turn don't
            # collide.
            tool_calls: dict[int, dict[str, Any]] = {}
            async for chunk in stream:
                # L4: some providers (Lyzr, perplexity) attach extras at the
                # chunk level — `citations`, `usage`, `sources`. Surface them
                # ONCE if seen, so the UI can render the "📚 Sources" line.
                if self.on_extras is not None:
                    extras: dict[str, Any] = {}
                    for field in ("citations", "sources", "search_results", "metadata"):
                        v = getattr(chunk, field, None)
                        if v:
                            extras[field] = v.model_dump() if hasattr(v, "model_dump") else v
                    if extras:
                        try:
                            self.on_extras(extras)
                        except Exception:  # noqa: BLE001 - never poison the stream
                            pass
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = (delta.content or "") if delta else ""
                if piece:
                    collected.append(piece)
                    if on_token is not None:
                        on_token(piece)
                # L3: detect tool_call deltas. OpenAI streams them in pieces:
                # index + id + function.name first, then arguments slices.
                # Accumulate by index; emit when name is known (first non-empty
                # arrival) and also when arguments accumulate further. We emit
                # on each delta so the UI can render mid-flight "🔧 calling …".
                if delta and getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        idx = getattr(tc, "index", 0) or 0
                        slot = tool_calls.setdefault(
                            idx,
                            {"index": idx, "id": "", "name": "", "arguments": ""},
                        )
                        if getattr(tc, "id", None):
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                slot["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                slot["arguments"] += fn.arguments
                        if self.on_tool_call is not None and slot["name"]:
                            try:
                                self.on_tool_call(dict(slot))
                            except Exception:  # noqa: BLE001
                                pass
            answer = "".join(collected).strip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - typed result so the pipeline degrades
            return AgentTurnResult(
                status="error", error=AgentTurnError(message=str(exc) or exc.__class__.__name__)
            )

        # Remember the turn (cap the rolling window).
        self._history.append({"role": "user", "content": text})
        self._history.append({"role": "assistant", "content": answer})
        if len(self._history) > self._history_cap:
            self._history = self._history[-self._history_cap :]

        return AgentTurnResult(answer_text=answer, status="ok")
