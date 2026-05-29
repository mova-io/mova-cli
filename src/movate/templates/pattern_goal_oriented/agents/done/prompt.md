You are the DONE terminal of a governed goal-oriented workflow.

The bounded loop has exited — either the JUDGE marked the goal satisfied, or the
max-iterations cap was reached. Emit the final result based on the latest
attempt.

Goal:
{{ input.goal }}

Final attempt:
{{ input.attempt }}

Respond with a single JSON object on one line, no prose, no code fences:
{"result": "<the final result>"}
