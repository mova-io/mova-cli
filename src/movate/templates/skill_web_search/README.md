# `__SKILL_NAME__` — web search via DuckDuckGo HTML

Searches the public web and returns the top results as a structured
list. Used by the `rag-qa` demo agent to ground answers in current
information when the provided context doesn't cover the question.

## Why DuckDuckGo's HTML endpoint?

- **No API key required** — self-contained demo, runnable on any
  machine without provisioning credentials.
- **Stable URL shape** — `https://html.duckduckgo.com/html/?q=...`
  has held up across years.
- **Easy parsing** — each result is a `<a class="result__a">` +
  `<a class="result__snippet">` pair; one regex extracts both.

## Schema

**Input:**
```yaml
query: string     # natural-language search query
top_n?: integer   # results to return (1–10, default 5)
```

**Output:**
```yaml
results: array   # each item: {title, url, snippet}
warning?: string # set on failure (network error / parse miss)
```

## Cost + side effects

- `per_call_usd: 0.0` — DuckDuckGo's HTML endpoint has no per-call
  cost. Bump if you swap to a paid backend.
- `side_effects: network` — makes an outbound HTTPS request.

## Swapping to a real search API

The whole point of this skill being a template is that the integration
is replaceable. To swap in Serper / Brave / Google CSE:

1. Edit `impl.py:run` to call your provider's JSON endpoint.
2. Map the provider's response to the existing
   `[{"title", "url", "snippet"}]` shape — the agent's prompt
   assumes that schema and doesn't need to change.
3. Update `cost.per_call_usd` in `skill.yaml` to match the provider.

## Testing

After scaffolding:

```bash
mdk skills run __SKILL_NAME__ --input '{"query": "movate cli github"}'
```

A non-empty `results` list confirms the wiring; a `warning` field
indicates a failure mode the agent will see.
