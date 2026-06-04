# Twilio Telephony Setup (ADR 074 Phase 3b)

Connect inbound phone calls to mdk voice agents via Twilio Media Streams.

## Prerequisites

- An mdk runtime deployed and publicly accessible (or use ngrok for local dev)
- A Twilio account with a phone number
- The `[telephony]` extra installed: `pip install 'movate-cli[telephony]'`

## 1. Install the telephony extra

```bash
pip install 'movate-cli[telephony]'
# or with uv:
uv pip install 'movate-cli[telephony]'
```

## 2. Set Twilio credentials

```bash
# Interactive setup (saves to ~/.movate/credentials):
mdk auth login twilio

# Or set env vars directly:
export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 3. Configure the Twilio webhook

In the Twilio Console, set your phone number's **Voice webhook** to:

```
https://<your-runtime-host>/api/v1/agents/<agent-name>/call/twilio
```

- **Method:** POST
- **URL:** Replace `<your-runtime-host>` with your runtime's public URL and
  `<agent-name>` with the mdk agent you want to answer calls.

When a call arrives, Twilio hits this webhook and receives TwiML XML that
instructs it to open a bidirectional Media Stream WebSocket back to your
runtime at `/api/v1/agents/<agent-name>/call/twilio/stream`. The call audio
flows through the mdk voice pipeline (STT -> agent -> TTS) and the agent's
spoken response is streamed back to the caller.

### Example: Deva's number (217) 919-5393

```
Webhook URL: https://my-mdk-runtime.azurewebsites.net/api/v1/agents/support-agent/call/twilio
Method: POST
```

## 4. Test with `mdk voice call` (outbound)

Initiate an outbound test call from the CLI:

```bash
export TWILIO_PHONE_NUMBER=+12179195393

mdk voice call support-agent --to +15551234567
# or explicitly:
mdk voice call support-agent --to +15551234567 --from +12179195393
```

This uses the Twilio REST API to place a call to `--to`, connecting it to
the specified agent through the Media Stream.

## 5. Local development with ngrok

For local testing, expose your runtime via ngrok:

```bash
# Terminal 1: start the mdk runtime
mdk serve --port 8000

# Terminal 2: expose it publicly
ngrok http 8000
```

Use the ngrok HTTPS URL as your Twilio webhook:

```
https://abc123.ngrok.io/api/v1/agents/my-agent/call/twilio
```

## Required environment variables

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID (from console.twilio.com) |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | (Optional) Default Twilio number for `mdk voice call --from` |
| `MOVATE_DEFAULT_TENANT` | (Optional) Tenant ID for telephony calls (default: `default`) |

## How it works

1. Phone call arrives at your Twilio number
2. Twilio POSTs to `/api/v1/agents/{name}/call/twilio` (the TwiML webhook)
3. The webhook returns TwiML XML with a `<Connect><Stream>` directive
4. Twilio opens a WebSocket to `/api/v1/agents/{name}/call/twilio/stream`
5. The Twilio transport decodes mu-law audio -> PCM16 (via `telephony.py`)
6. PCM16 AudioChunks feed into `run_voice_pipeline` (STT -> agent -> TTS)
7. TTS AudioChunks are encoded back to mu-law and sent to Twilio
8. The caller hears the agent's response

The voice pipeline is **unchanged** -- the same agent that works over the
browser WebSocket works over the phone with zero modifications.
