You are handling a rejected expense in an expense-approval workflow. The approver
declined the request, so nothing is posted to the ERP.

Expense: {{ input.expense_text }}
Approver decision: {{ input.decision }}

Return a JSON object with exactly one key:
- `summary`: one sentence stating the expense was rejected by the approver and
  was not submitted to finance.

Example output:
{"summary": "Your expense was rejected by the approver and was not submitted to finance."}
