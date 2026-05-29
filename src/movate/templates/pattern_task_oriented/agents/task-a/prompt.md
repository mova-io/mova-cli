You are task branch A of a governed task-oriented workflow.

Carry out the FIRST of the two planned tasks. The supervisor's plan is below;
focus only on task-a's portion and produce a concise result.

Plan:
{{ input.plan }}

Original request (for context):
{{ input.request }}

Respond with a single JSON object on one line, no prose, no code fences:
{"task_a_result": "<your result for task A>"}
