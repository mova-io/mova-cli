You are handling a rejected fulfilment request. Nothing was fulfilled. There
are two ways a request lands here: an approver explicitly rejected it, or
NOBODY responded — both approval gates (primary, then the escalation to the
alternate approver) timed out, which fails safe to rejected.

Request: {{ input.request }}
Requester: {{ input.requester }}
{% if input.decision is defined -%}
Approver decision: {{ input.decision }}
{% else -%}
Approver decision: none — both approval gates timed out with no response.
{% endif %}
Return a JSON object with exactly one key:
- `summary`: one sentence stating the request was rejected (by the approver,
  or because no approver responded in time) and nothing was fulfilled.

Example output:
{"summary": "Your access request was rejected because no approver responded in time, and nothing was fulfilled."}
