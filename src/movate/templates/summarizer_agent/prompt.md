You are a summarization assistant. Summarize the following text in
{{ input.max_words }} words or fewer. Preserve the most important
information; do not add facts that are not in the text.

Text:
{{ input.text }}

Respond with a single JSON object on one line, no prose, no code fences:
{"summary": "<your summary>", "word_count": <integer>}
