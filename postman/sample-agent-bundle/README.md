# Sample agent bundle (for Postman requests 1b / 1c)

Requests **1b. Create agent (multipart bundle)** and **1c. Edit agent (PUT bundle)**
upload an agent bundle as a file. Attach **`postman/sample-agent-bundle.zip`**
(in the request's Body → form-data → the `bundle` field → Select Files).

Canonical bundle layout (what the runtime accepts):
`agent.yaml`, `prompt.md`, optional `schema/input.json` + `schema/output.json`,
`evals/dataset.jsonl`, and dirs `skills/`, `contexts/`, `kb/`.
Schemas may also be inline in `agent.yaml` (as in this sample) — no schema/ dir needed.

For a no-file create, use **1a. Create agent (from wizard JSON)** instead.
