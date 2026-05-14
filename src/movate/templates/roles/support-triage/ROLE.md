# Role: support-triage

Reads incoming support tickets, assigns priority + team + category, decides whether to escalate, writes a 1-line summary for the team's queue.

## When to use this template

- ITSM / ticketing system integration where incoming requests need routing
- Slack / Teams workflows where customer messages land in a shared channel and need triage before anyone responds
- Email-to-ticket pipelines that need pre-classification before hitting the helpdesk
- Multi-product support orgs where the right team isn't obvious from the customer's wording

## What you get out of the box

- **Strict enum output** — priority is one of `low/medium/high/urgent`; team is one of 6 enumerated values. Your downstream code can `switch` without defensive parsing.
- **Tunable rubric** — the prompt has explicit decision criteria for each priority level + each team. Edit `prompt.md`'s rubric sections to match your org's actual escalation policy.
- **Customer-tier awareness** — if you pass `customer_tier`, the prompt bumps priority for enterprise customers on severity-1 categories only (no over-rotation on billing or feature requests).
- **Channel-aware** — pass the channel (web/email/phone/slack/teams) and the agent factors urgency cues like phone calls being higher-touch.
- **3 sample eval cases** to start measuring routing quality on day 1.

## Typical customizations

1. **Change the team list** — your org probably has different teams than the default 6 (engineering / billing / support / security / sales / escalation). Edit the enum in `agent.yaml` and the rubric in `prompt.md` to match.
2. **Add product-specific routing** — if you have product-specific support pods (e.g. `team-cards`, `team-cloud`), add them to the `assigned_team` enum.
3. **Tune the priority rubric** — your "urgent" might be tighter or looser than the default. Edit the rubric table in `prompt.md`.
4. **Add more eval cases** — drop more `{input, expected_output}` rows in `evals/dataset.jsonl` covering your real ticket distribution. Run `mdk eval` to score the agent's routing accuracy.

## Pairs well with

- **`reply-drafter`** — once triage routes the ticket, reply-drafter composes the initial response
- **`text-classifier`** — for upstream pre-filtering (e.g. spam vs real)
- **A webhook integration** — pipe ticket events to the agent, route the output back to your ticketing system
