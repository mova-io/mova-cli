You are handling a rejected draft answer in a human-escalation workflow. The
human reviewer declined the triage agent's draft, so no final answer ships.

Question: {{ input.question }}
Draft answer: {{ input.answer }}
Reviewer decision: {{ input.decision }}
Reviewer feedback: {{ input.feedback }}

Return a JSON object with exactly one key:
- `summary`: one sentence stating the draft answer was rejected by the
  reviewer, citing the reviewer's feedback as the reason.

Example output:
{"summary": "The draft answer was rejected by the reviewer because it was too speculative to ship without more context."}
