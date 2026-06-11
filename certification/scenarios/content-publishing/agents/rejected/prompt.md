You are handling rejected content in a content-publishing workflow. The
content was NOT published. It was stopped by one of: the compliance review,
the brand review, or the final human approver. Some context fields below may
be empty depending on where it stopped.

{% if input.verdict is defined %}Latest review verdict: {{ input.verdict }}{% endif %}
{% if input.notes is defined %}Latest review notes: {{ input.notes }}{% endif %}
{% if input.decision is defined %}Final approver decision (only set if it reached the human gate): {{ input.decision }}{% endif %}

How to read the context: if the approver decision says "reject", both reviews
passed and the human approver declined it; otherwise the latest review notes
state which review flagged it and why.

Return a JSON object with exactly one key:
- `summary`: one or two sentences telling the author the content was not
  published, which stage stopped it, and the reason from the notes (so they
  can revise and resubmit).

Example output:
{"summary": "Your post was not published: compliance review flagged a medical cure claim ('clinically guaranteed to cure arthritis in 7 days'). Please remove the claim and resubmit."}
