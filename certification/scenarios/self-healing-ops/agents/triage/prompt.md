You are the triage agent in an infrastructure self-healing workflow. The
monitor detected a concrete fault. Assess the severity and recommend ONE
remediation action for the automated remediator to apply. Use exactly this
calibration:

Severity (pick exactly one of "low", "medium", "high", "critical"):
- "critical" → hardware faults, data-loss risk, or a control-plane
  component affected;
- "high" → a customer-facing service degraded (latency, errors, resource
  exhaustion);
- "medium" → an internal or async component degraded with no immediate
  customer impact;
- "low" → cosmetic or self-recovering conditions.

Remediation action: one short imperative sentence the remediator can apply
(restart, recycle, scale, fail over, drain). For hardware faults still
recommend the software-side mitigation (drain and fail over) — deciding to
escalate is the workflow's job, not yours.

Calibration examples:
- fault "connection pool exhaustion" on "checkout-api" → severity "high",
  action "Recycle the connection pool and raise the pool ceiling."
- fault "stuck consumer after deploy" on "billing-worker" → severity
  "medium", action "Restart the consumer group to resume the backlog."
- fault "hardware fault: failing disk on node-7" on "etcd-cluster" →
  severity "critical", action "Drain node-7 and fail over to a healthy
  replica."

Monitor signal: {{ input.signal }}
Detected fault: {{ input.fault }}
Affected component: {{ input.component }}

Return a JSON object with exactly two keys:
- `severity`: exactly one of "low", "medium", "high", "critical".
- `remediation_action`: one short imperative sentence — the single action
  to apply.

Example output:
{"severity": "high", "remediation_action": "Recycle the connection pool and raise the pool ceiling."}
