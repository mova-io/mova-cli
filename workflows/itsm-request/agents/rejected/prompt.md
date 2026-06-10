You are handling a rejected ITSM service request. The approver declined the
request, so nothing was provisioned.

Service: {{ input.service }}
Requester: {{ input.requester }}
Approver decision: {{ input.decision }}

Return a JSON object with exactly one key:
- `summary`: one sentence stating the service request was rejected by the
  approver and nothing was provisioned.

Example output:
{"summary": "Your new-user onboarding request was rejected by the approver and nothing was provisioned."}
