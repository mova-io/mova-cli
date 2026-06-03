# Welcome to the mdk Playground 🤖

You're running an **mdk** (`movate-cli`) agent in an interactive chat. This is the
fastest way to *feel* an agent before you ship it — scaffold, edit, and live-test
in one loop.

## Try this

- **Just chat** — send a message and watch the agent respond. Tool/skill calls,
  retrieval, and per-turn cost show up inline.
- **Switch targets** — use the runtime/target switcher to point this chat at a
  different deployment (local `mdk serve`, dev, or a cloud runtime).
- **Voice mode** — if the agent declares a `voice:` config, flip on voice for a
  speech-to-speech turn.

## Useful commands

- `mdk dev` — the unified scaffold → edit → live-test → deploy front door.
- `mdk serve --dev` — run the runtime locally with an auto-seeded dev key and a
  ready-to-open playground URL.
- `mdk eval` — score the agent against a dataset (accuracy, faithfulness,
  coverage, latency).

To change this welcome screen, edit `chainlit.md` at the repo root. Leave it empty
for no welcome screen.
