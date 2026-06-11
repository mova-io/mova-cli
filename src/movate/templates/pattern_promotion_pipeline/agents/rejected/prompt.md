You are handling a stopped run in a promotion-pipeline workflow. The change
did NOT complete its stage. Either a human approver declined it (the decision
field says "reject"), or the requested stage was not one of test / staging /
production and the router failed safe (the decision field is empty).

Change: {{ input.change }}
Requested stage: {{ input.stage }}
{% if input.decision is defined %}Human decision (only set if it reached an approval gate): {{ input.decision }}{% endif %}

Return a JSON object with exactly one key:
- `summary`: one or two sentences telling the owner the stage did not
  complete and why — the human declined, or the stage value was not
  recognized (name the valid stages).

Example output:
{"summary": "checkout-service 2026.6.10 was not promoted to production: the approver declined the promotion. Address their concerns and resubmit."}
