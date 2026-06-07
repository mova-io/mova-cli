# ADR 085 — OpenAI-compatible API surface (`/v1/models`, `/v1/chat/completions`)

- Status: Accepted
- Date: 2026-06-07
- Deciders: platform-team

## Context

Operators want to reach mdk agents from **off-the-shelf OpenAI-API clients** —
above all **OpenWebUI** (a popular self-host chat UI, incl. mic/voice), but also
LibreChat, the `openai` SDK, LangChain's `ChatOpenAI`, `llm`/`aichat`, etc. Every
one of these speaks the OpenAI HTTP contract: `GET /v1/models` +
`POST /v1/chat/completions` (with `stream: true` SSE). mdk's runtime exposes a
rich native API (`/api/v1/agents/{name}/runs`, the voice WS), but nothing an
OpenAI client understands — so there is no zero-glue way to point a generic chat
UI at a deployed agent.

## Decision

Add a thin **OpenAI-compatibility router** to the runtime that maps the OpenAI
contract onto the existing `Executor` — a *protocol adapter at the edge*, not a
new execution path (CLAUDE.md rule 6/7; the Executor stays the one place agents
run, meter, and trace).

- **`GET /v1/models`** → lists the runtime's deployed agents, one OpenAI "model"
  per agent (`id = agent name`).
- **`POST /v1/chat/completions`** → resolves the agent named by `model`, maps the
  last user message into the agent's primary input field (derived from its input
  schema — same idea as the voice path's `input_key`), runs `Executor.execute()`,
  and shapes the `RunResponse` back into an OpenAI `chat.completion` (usage =
  mdk token metrics). `stream: true` returns the OpenAI SSE chunk format.
- **Auth + tenancy reuse the existing seam**: same `Bearer mvt_*` dependency and
  tenant resolution as every other route; gated on the `run` scope. (OpenWebUI
  puts the key in its "OpenAI API key" field.)

### Scope / compat (rule 5)
Purely additive — a new `/v1/*` route group. No change to the native `/api/v1`
surface, the agent/workflow schema, the Executor, or env vars. Off no toggle:
the routes are always present but do nothing until agents exist.

### Non-goals (follow-ons)
- **True token-by-token streaming**: v1 emits the completed response as a single
  SSE content chunk (OpenWebUI renders it fine). Real per-token streaming via the
  Executor `on_token` callback is a fast-follow.
- **Multi-turn history**: v1 maps the last user message to the agent input; full
  OpenAI `messages[]` → mdk `history` mapping is a follow-on.
- **`/v1/audio/transcriptions`** (OpenWebUI mic → STT) and tool-calling
  passthrough are separate follow-ons.

## Consequences
- Any OpenAI client (OpenWebUI, SDKs, LangChain) can drive a deployed mdk agent
  with zero glue — the headline being OpenWebUI incl. its built-in voice/mic.
- One more public surface to keep stable; kept deliberately small + adapter-only
  so the blast radius is a single router function.
