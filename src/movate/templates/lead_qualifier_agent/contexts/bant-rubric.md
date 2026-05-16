# BANT Qualification Rubric

BANT stands for **Budget · Authority · Need · Timeline**. This rubric
defines how the lead-qualifier agent scores each dimension and maps the
combined result to a recommended next action.

## Scoring each dimension

Score each dimension 0–3. Do not interpolate — use the band that best
fits the evidence in the lead record.

### Budget (B)

| Score | Evidence |
|---|---|
| 3 | Explicit budget stated and within your pricing tier |
| 2 | Budget range stated; overlap with your tier is likely |
| 1 | Budget mentioned but vague ("we have budget") or below your floor |
| 0 | No budget information; or budget clearly below minimum viable deal |

### Authority (A)

| Score | Evidence |
|---|---|
| 3 | Signer or final decision-maker is the contact or explicitly named |
| 2 | Contact is an influencer/champion with stated access to the signer |
| 1 | Contact is an end user; no visibility into decision chain |
| 0 | Contact explicitly states they are not involved in purchase decisions |

### Need (N)

| Score | Evidence |
|---|---|
| 3 | Specific pain point stated; directly maps to your product's core value prop |
| 2 | General problem articulated; fit requires some interpretation |
| 1 | Need is tangential or requires significant product extension |
| 0 | No identifiable need; exploratory/research conversation only |

### Timeline (T)

| Score | Evidence |
|---|---|
| 3 | Specific date or quarter stated; urgency is real (event-driven, contract expiry) |
| 2 | Vague but near-term: "this quarter," "by end of year" with supporting context |
| 1 | "Someday," "no rush," or no timeline stated but active evaluation underway |
| 0 | No timeline; pure research / no buying signal |

## Composite scoring → qualification tier

Sum the four dimension scores (max 12).

| Total | Tier | Meaning |
|---|---|---|
| 10–12 | **hot** | Sales-ready. Route to AE for same-day follow-up. |
| 7–9 | **warm** | Strong fit, one or two gaps. Route to AE for nurture sequence. |
| 4–6 | **nurture** | Real interest but material gaps. Add to drip; SDR check-in at 30 days. |
| 0–3 | **disqualify** | No viable path to close. Archive with reason code. |

## Tie-breaking rules

- If `authority = 0` regardless of other scores → cap tier at **nurture**
  (a deal cannot close without a path to the signer).
- If `need = 0` regardless of other scores → force **disqualify**
  (no pain = no reason to buy).
- If `budget = 0` AND `timeline = 0` → force **disqualify** unless
  `need = 3` AND `authority = 3` (rare inbound executive inquiry).

## Next-action recommendations

The `next_action` field must be one of these exact strings:

| Value | When to use |
|---|---|
| `"route_to_ae"` | Tier hot or warm |
| `"add_to_nurture"` | Tier nurture |
| `"disqualify"` | Tier disqualify |
| `"request_more_info"` | Any tier where a critical BANT field has score 0 but the lead is otherwise promising (hot/warm) — ask a specific follow-up question before routing |

## What the agent must NOT do

- **Invent scores.** If the lead record is silent on budget, score B=0,
  don't assume.
- **Conflate seniority with authority.** A VP title does not guarantee
  decision-making authority — look for explicit statements.
- **Score timeline based on the sales team's urgency.** Only the buyer's
  stated urgency counts.
- **Output free-form tier labels.** Use only the four values above; the
  downstream CRM integration depends on exact string matching.
