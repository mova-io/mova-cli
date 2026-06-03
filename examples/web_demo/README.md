# mdk-voice — browser demo (one command)

A live voice agent in your browser, end-to-end on real providers — exactly the
demo Deva can see in his browser without installing anything Movate-specific.

```
mic → FailoverSTT(Deepgram → Whisper) → OpenAI GPT-4o-mini → FailoverTTS(Cartesia → OpenAI) → speaker
```

…with PII redaction on logged transcripts, streaming TTS (sentence-by-sentence),
the cache, and a live metrics dashboard.

## Run it

```bash
# 1) keys (one-time; never goes through your shell history)
printf 'sk-...'     > ~/.mdk_openai_key   && chmod 600 ~/.mdk_openai_key
printf 'KEY...'     > ~/.mdk_deepgram_key && chmod 600 ~/.mdk_deepgram_key
printf 'sk_car_...' > ~/.mdk_cartesia_key && chmod 600 ~/.mdk_cartesia_key

# 2) install + run
pip install -e '.[openai,deepgram,cartesia]' fastapi 'uvicorn[standard]'
python examples/web_demo/server.py
# → http://localhost:8765
```

Open `http://localhost:8765`, hold the mic button (or **space**), talk, release.
You'll hear the agent answer, see the latency badge, and watch the metrics
panel update per turn.

## What you're seeing

| Panel | What it proves |
|-------|---------------|
| **This turn** | Real STT + real LLM + real TTS round-trip, end-to-end |
| **Latency** | Time-to-first-audio (the Cartesia win) measured live |
| **Router metrics** | Which engine served, failovers, cache hits, silence trimmed |
| **Event stream** | Same events the SDK exposes — what a Twilio/Genesys transport would relay |

## What this means for telephony

The *exact same* pipeline plumbed here will drive Twilio Media Streams /
Genesys AudioHook / AWS Connect — the only thing that changes is the audio
transport adapter (μ-law 8 kHz frames over WebSocket instead of PCM16 16 kHz).
The codecs are already in `mdk_voice.telephony`; see the roadmap for the
remaining work.
