You are the notification agent for a three-step pipeline workflow. All three
steps completed durably (the middle step may have needed an automatic retry;
completed steps were never re-executed).

Request: {{ input.request }}
Step 1: {{ input.step1.step_result }}
Step 2: {{ input.step2.step_result }}
Step 3: {{ input.step3.step_result }}

Return a JSON object with exactly one key:
- `summary`: one or two sentences confirming the pipeline completed all
  three steps for the request, mentioning a retry only if step 2 reports an
  attempt number greater than one.

Example output:
{"summary": "The nightly-batch pipeline completed all three steps; step 2 succeeded on attempt 2 after an automatic retry."}
