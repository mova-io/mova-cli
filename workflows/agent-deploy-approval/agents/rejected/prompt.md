You are handling a rejected promotion in an agent-deploy-approval workflow.
The candidate agent version was NOT promoted. It was stopped by one of: a
below-threshold eval score (the gate requires 0.85 or higher), or a human
approver who declined the promotion AFTER a passing eval. Some context fields
below may be empty depending on where it stopped.

Candidate: {{ input.candidate }}
Version: {{ input.version }}
Eval score: {{ input.eval_score }}
{% if input.eval_report is defined %}Eval report: {{ input.eval_report }}{% endif %}
{% if input.decision is defined %}Human decision (only set if it reached the approval gate): {{ input.decision }}{% endif %}

How to read the context: if the human decision says "reject", the eval passed
and the approver declined the promotion; otherwise the eval score fell below
the 0.85 threshold — relay the eval report so the owner can see which metrics
regressed.

Return a JSON object with exactly one key:
- `summary`: one or two sentences telling the owner the version was not
  promoted, which gate stopped it, and the relevant detail from the eval
  report (or the human veto).

Example output:
{"summary": "support-triage 2026.6.10.4 was not promoted: its eval scored 0.41 against the 0.85 threshold (accuracy=0.45, safety=0.62). Review the regression before resubmitting."}
