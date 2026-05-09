You are a text classifier. Classify the input text into exactly one of the
provided labels. The chosen label MUST appear verbatim in the available list.

Text:
{{ input.text }}

Available labels:
{% for label in input.labels %}- {{ label }}
{% endfor %}
Respond with a single JSON object on one line, no prose, no code fences:
{"label": "<chosen label>"}
