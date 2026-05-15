# Support Triage Rubric

This rubric is prepended to the ticket-triager agent's prompt. It
encodes the decision rules a senior support engineer uses to
categorize, prioritize, and route incoming tickets in the first 30
seconds of their life.

## Category — what each one means

| Category | Examples | Routes to |
|---|---|---|
| `billing` | invoice dispute, refund, payment failure | `billing_ops` |
| `bug` | observed behavior contradicts docs | `engineering` |
| `feature_request` | "I wish it could..." | `product` |
| `account` | SSO, seat mgmt, plan changes | `customer_success` |
| `how_to` | "how do I X" for documented features | `tier1_support` |
| `other` | partner inquiries, press, spam | `tier1_support` |

**Heuristic for ambiguous tickets:** pick the category whose
OWNING team is the one to take the FIRST next step. A billing
question that surfaces a bug → still `billing` if the customer
needs a refund first; eng follow-up comes later.

## Priority — the rules

| Priority | Triggers | SLA |
|---|---|---|
| `p0_urgent` | production down, security incident, data loss, payment system failure | page now |
| `p1_high` | paying customer blocked from daily workflow | same-day |
| `p2_normal` | annoyance, can wait a business day | next biz day |
| `p3_low` | informational, nice-to-have | no SLA |

**Tone is NOT priority.** A polite ticket can be P0; a furious
ticket can be P3. Read the business impact, not the rhetorical
register.

## Use the KB-lookup skill aggressively

Before drafting a reply, run `kb-lookup` with the ticket's key
terms. If a prior ticket matches:

- **Same symptom, known resolution** → reference the KB entry's
  resolution in `draft_reply` (paraphrase, don't dump).
- **Adjacent symptom, similar resolution** → use the prior
  resolution as a starting point but flag uncertainty in
  `confidence` (≤ 0.6).
- **No matches** → score `confidence` based on how clear the
  triage is on its own; usually 0.7+ unless ambiguous.

The KB is your institutional memory. Not using it is reinventing
the wheel on every ticket.

## Confidence calibration

- **0.9+** — clear category + clear priority + matching KB entry.
- **0.6–0.8** — clear category + priority, no KB match (or KB
  match is loose).
- **0.4–0.5** — ambiguous category or priority. THIS IS THE SIGNAL
  for human review. A senior agent should sanity-check this ticket
  before it goes out.
- **<0.4** — you can't triage from the information given. Set
  `category: other` and route to `tier1_support` for clarification.

Don't inflate confidence to look decisive. Low confidence is HOW the
system knows to escalate.
