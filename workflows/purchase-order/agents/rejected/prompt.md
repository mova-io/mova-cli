You are handling a rejected purchase order. An approver in the chain (manager
or director) declined the request, so no purchase order was created.

Item: {{ input.item }}
Requester: {{ input.requester }}
Amount: {{ input.amount }}
Approver decision: {{ input.decision }}

Return a JSON object with exactly one key:
- `summary`: one sentence stating the purchase order was rejected by the
  approver and nothing was ordered.

Example output:
{"summary": "Your purchase order for a standing desk was rejected by the approver and nothing was ordered."}
