# Role: text-classifier

Classifies text into one of a caller-provided taxonomy with confidence + reasoning.

## When to use this template

- **Sentiment analysis** — positive / negative / neutral, optionally with finer-grained labels
- **Intent detection** — support-question / feature-request / complaint / sales-inquiry
- **Content moderation** — spam / safe / abusive / off-topic
- **Routing pre-filter** — coarse classification feeding into `support-triage` for the detailed routing decision
- **Document categorization** — invoice / contract / resume / newsletter / other

## Why this template instead of a hard-coded model

The taxonomy is passed **per call**. The same agent can classify into `[spam, support, sales]` on one call and `[urgent, normal, low]` on the next. You don't need a separate agent per taxonomy.

For fixed-taxonomy use cases (you always classify into the same 3 labels), you can hardcode the taxonomy in `agent.yaml` (move it from input to a constant in `prompt.md`) for cleaner caller ergonomics. The agent will still work either way.

## What you get out of the box

- **Strict label enforcement** — the prompt is explicit that `label` MUST be a string from the input `taxonomy` array. The reasoning field doubles as an audit trail when ambiguity exists.
- **Calibrated confidence** — the prompt has an explicit rubric mapping confidence ranges to evidence strength. Low-confidence outputs ALWAYS name the closest alternative label.
- **Single-label by default, multi-label opt-in** — pass `multi_label: true` to get comma-separated labels when text genuinely fits multiple categories.
- **One-sentence reasoning per classification** — useful for spot-checking + as training signal if you decide to fine-tune later.
- **Deterministic** — temperature 0.0, so the same text + same taxonomy gives the same label every time. Critical for routing decisions.

## Typical customizations

1. **Hardcode the taxonomy** if you only have one classification task. Move the list from input to a constant in `prompt.md` and drop the `taxonomy` field from the schema.
2. **Add examples per label** — the example block at the bottom of the prompt is illustrative; replace with examples specific to your actual taxonomy for better few-shot performance.
3. **Lower the confidence floor** — if your downstream pipeline shouldn't act on labels below confidence X, do that filter in your application code (the agent always returns a label even when confidence is low).
4. **Add a "none-of-the-above" label** — useful for catching out-of-distribution text. Add `"other"` to your taxonomy and the agent will use it when nothing else fits.

## Pairs well with

- **`support-triage`** — use the classifier upstream as a coarse pre-filter, then triage does the detailed routing
- **`reply-drafter`** — classify incoming sentiment first, pick the draft's tone based on the classification
- **A guardrail layer** — once the guardrails ship, content-moderation classification can be wired as a pre-LLM input filter
