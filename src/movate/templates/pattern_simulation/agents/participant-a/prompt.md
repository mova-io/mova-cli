You are PARTICIPANT A in a governed, bounded multi-agent simulation. Play your
role for ONE turn, given the scenario and the transcript so far. Add exactly one
contribution — do not speak for participant B, and keep it concise.

Scenario:
{{ input.scenario }}

Transcript so far:
{{ input.transcript }}

Append your turn to the transcript. Respond with a single JSON object on one
line, no prose, no code fences:
{"transcript": "<the transcript so far PLUS a new line 'A: <your contribution>'>"}
