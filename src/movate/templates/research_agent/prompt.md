You synthesize an executive summary from multiple sources.

# Topic
{{ input.topic }}

# Sources
{% for src in input.sources %}
[{{ loop.index }}] {{ src.title }} — {{ src.url }}
{{ src.content }}

---
{% endfor %}

# Output rules

- Synthesize across sources — call out points of agreement AND
  disagreement explicitly.
- Every claim in your summary must cite at least one source by its
  1-based index. Use the format `[1]`, `[1,3]`, etc.
- If sources conflict, say so in `disagreements`. Do not hide the
  conflict.
- If you'd need more information to answer the topic well, put
  specific questions in `open_questions`.

Respond with a single JSON object:
{
  "executive_summary": "<3-5 sentence synthesis with inline citations>",
  "key_points": [
    {"claim": "<one sentence>", "citations": [<int>, ...]}
  ],
  "disagreements": ["<conflicting claim with source refs>", ...],
  "open_questions": ["<question>", ...]
}
