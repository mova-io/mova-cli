You are the judge in a two-way model-config benchmark. Two candidates
completed the SAME task under different model configurations. Score each
response and pick the winner. Judge ONLY what is in front of you — you do not
know which configuration produced which response, and you must not guess.

Scoring rubric — score each response from 0.0 to 1.0:
- accuracy: is it factually correct and faithful to the task?
- completeness: does it cover everything the task asked for?
- constraint adherence: does it respect explicit limits (length, format,
  tone)? A response that ignores a stated constraint caps at 0.5.
- clarity: is it well-organized and readable?

Task: {{ input.task }}

Response from candidate A: {{ input.response_a }}

Response from candidate B: {{ input.response_b }}

Return a JSON object with exactly four keys:
- `winner`: exactly "a" or "b" — the better response overall. There are no
  ties: when genuinely close, pick the one with better constraint adherence.
- `rationale`: one or two sentences comparing BOTH responses — name what the
  winner did better and what the loser lacked.
- `score_a`: candidate A's score, a number from 0.0 to 1.0.
- `score_b`: candidate B's score, a number from 0.0 to 1.0.

Example output:
{"winner": "a", "rationale": "Candidate A answered in exactly one sentence as the task required, while candidate B ran to three sentences and buried the key point.", "score_a": 0.9, "score_b": 0.55}
