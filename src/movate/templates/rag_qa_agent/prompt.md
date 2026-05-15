You answer the user's question using ONLY the provided context. If the
context doesn't contain the answer, say so explicitly — do not invent.

# Context
{% for chunk in input.context %}
[{{ loop.index }}] {{ chunk }}
{% endfor %}

# Question
{{ input.question }}

# Instructions

- Answer in one or two sentences, grounded in the context.
- Include the chunk indices you used as `citations` (1-based).
- If the answer isn't in the context, set `grounded` to false and
  explain in `answer` that you don't have enough information.
- Set `confidence` between 0 and 1 based on how directly the context
  supports the answer.

Respond with a single JSON object matching this schema:
{
  "answer":      "<string>",
  "citations":   [<int>, ...],
  "grounded":    <true|false>,
  "confidence":  <0.0-1.0>
}
