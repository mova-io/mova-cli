# Role: reply-drafter

Drafts replies to incoming messages — emails, Slack, support tickets, chat — with explicit tone + intent control.

## When to use this template

- Support workflows where a human reviews + sends each reply (the agent gives them a draft to start from)
- Internal Slack bots that draft responses for channels with high message volume
- Email auto-draft features inside a product
- Sales / customer-success workflows where every reply has a deliberate tone (apologetic for incidents, firm for boundary-setting)

## What you get out of the box

- **7 intent presets** — `acknowledge` / `answer` / `escalate` / `decline` / `propose-solution` / `gather-info` / `apologize`. Each maps to a specific body structure (e.g. `apologize` leads with the apology + ownership; `decline` says no briefly with an alternative).
- **5 tone presets** — `formal` / `friendly` (default) / `apologetic` / `firm` / `casual`. Same content, different voice.
- **No-placeholder rule** — the agent is instructed to produce ready-to-send drafts. No `[INSERT NAME]` markers; works around missing facts naturally.
- **`suggested_next_actions`** — alongside the draft, the agent suggests operational follow-ups (add to queue, set reminder, flag account). Surfaces in your UI as a checklist for the responder.
- **Length-matching** — short questions get short replies; long complaints get full responses. Built into the prompt rules.

## Typical customizations

1. **Brand voice** — add a "company voice" section to `prompt.md` describing your brand's standard phrasing patterns. Examples: "We never apologize without explaining what we're fixing." or "We always sign off with the responder's first name."
2. **Adjust the intent list** — add intents specific to your use case (e.g. `request-payment`, `share-meeting-notes`, `survey-followup`).
3. **Add tone variants** — `legal` for compliance team replies, `enthusiastic` for sales prospect outreach.
4. **Pre-fill the signature** — set a default `sign_off_as` value via a project-level default in `movate.yaml` so every reply has the right signature without per-call config.

## Pairs well with

- **`support-triage`** — triage routes the ticket, reply-drafter composes the initial response
- **`text-classifier`** — classify the incoming message's sentiment first, then pick the tone for the reply accordingly
- **`document-summarizer`** — when the incoming message has a long history, summarize first, then draft
