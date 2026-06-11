You are handling an external-API call whose result could not be confirmed as
healthy: the provider check did not see a positive `provider_ok` flag, so no
downstream record was written.

Request: {{ input.request }}

Return a JSON object with exactly one key:
- `summary`: one sentence stating the external call could not be completed
  and nothing was recorded downstream.

Example output:
{"summary": "The external call could not be completed and nothing was recorded downstream."}
