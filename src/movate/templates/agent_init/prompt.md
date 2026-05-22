# Agent prompt

<!-- Draft your prompt here. This whole file is the agent's system prompt.
     Default I/O schema (see agent.yaml): input.text -> output.message.
     Edit the `schema:` block in agent.yaml to use different fields. -->

You are <describe what this agent does and how it should behave>.

User input:
{{ input.text }}

Respond with a single JSON object matching the output schema — no prose, no
code fences:
{"message": "<your reply>"}
