# Role: document-summarizer

Reads long-form text and extracts a structured summary + key points + action items + open questions.

## When to use this template

- Meeting transcripts → summary + decisions + action items
- Long email threads → "what's the state of this thread" briefing
- Contract reviews → key terms + risks + open questions
- Research papers / technical docs → executive summary + takeaways
- Ticket histories → "where are we with this customer" briefing
- Slack channel digests → "what happened in #engineering this week"

Basically: anywhere a reader has 5 minutes to consume what someone else wrote in an hour.

## What you get out of the box

- **3 summary styles** — `brief` (2-4 sentences), `detailed` (1-2 paragraphs, default), `action_items_only` (heavy on action_items, minimal summary).
- **Structured extraction** — `summary` + `key_points` + `action_items` + `open_questions` returned as separate fields. Your UI can render each as its own section without parsing prose.
- **Audience-aware** — pass `audience: "executive"` (or `"engineer"`, `"customer-success"`, etc.) and the agent adjusts level of detail.
- **Length cap** — `max_words` controls the `summary` field length (default 200 words). Doesn't constrain the lists.
- **Honest about gaps** — `action_items` and `open_questions` return `[]` when the source genuinely has none. The agent doesn't invent items to hit a count.
- **Faithful to source** — instructed not to assign owners that aren't named, not to add facts beyond the source. Hallucination risk is minimized by explicit rules in the prompt.

## Typical customizations

1. **Tighten the schema** for your domain — for legal contracts, replace `open_questions` with `risks_identified`; for meetings, replace it with `decisions_made`.
2. **Pre-bake the audience** — if every summary is for an executive audience, hardcode that in the prompt and drop the input field.
3. **Add structured fields** — for ticket-history summarization, add an `escalation_recommended: boolean` field. For meeting summaries, add a `next_meeting_scheduled: string?` field.
4. **Source-link tracking** — if your documents have line numbers / timestamps / page numbers, ask the agent to cite source locations in key_points (e.g. "p.5: ..."). Tune via the prompt.

## Pairs well with

- **`reply-drafter`** — summarize a long ticket thread, then have reply-drafter compose the response based on the summary
- **`text-classifier`** — classify document type first (contract / meeting / research-paper), then route to specialized summarizer instances with domain-tuned prompts
- **`sql-writer`** — summarize a verbose data analyst's question into a clean prompt for sql-writer downstream

## Performance notes

- Long inputs (4K+ tokens) cost more. The default `max_cost_usd_per_run: 0.50` accommodates ~10K-token inputs. Bump higher if you regularly process long meeting transcripts.
- Timeouts default to 60s call / 120s total — long inputs need the headroom.
- Use `summary_style: brief` aggressively when you don't need the full structured output. Saves cost + latency.
