# Role: sql-writer

Translates natural-language questions into SQL queries with plain-English explanations and safety warnings.

## When to use this template

- "Ask your data" features inside a product (the BI / analytics chatbot pattern)
- Internal tools for non-engineers who need to query a database
- Onboarding new engineers — let them ask questions in English while learning the schema
- Building a copilot that drafts queries for an engineer to review + run

## What you get out of the box

- **Read-only-by-default** — `SELECT` queries unless the user explicitly asks for `INSERT/UPDATE/DELETE/DROP`. Destructive queries trigger a `warnings` field flagging the risk.
- **Dialect-aware** — pass `dialect: "postgres"` (or `mysql`, `sqlite`, `bigquery`, `snowflake`) and the agent uses idiomatic syntax. Default: standard SQL compatible with PostgreSQL.
- **Schema-aware** — pass `schema_hint` with your CREATE TABLE statements and the agent uses your real table/column names. Without it, the agent flags any guessed names in `warnings`.
- **Plain-English explanation** for every query — non-technical users can verify intent before running, engineers can use it as a code review comment.
- **Safety warnings** — destructive ops, ambiguous queries, large result sets without `LIMIT`, dialect-specific functions used when no dialect was specified — all surface in `warnings: []`.

## Typical customizations

1. **Add your schema as the default schema_hint** — edit `prompt.md` to bake your real `CREATE TABLE` statements into the prompt so users don't have to pass them every time.
2. **Tighten the dialect** — if you only target one database, hardcode it in the prompt and drop the `dialect` input field.
3. **Add safety guardrails** — extend the warnings rubric to flag queries that touch sensitive tables (PII, financial, etc.). The prompt's "When to populate warnings" section is the place.
4. **Add execution** — by default the agent only GENERATES SQL. To execute, wire a Python skill that takes the `sql` output and runs it against your database. See `skills/example-skill/README.md` for the skill pattern.

## Pairs well with

- **Custom skill that runs the SQL** — natural pairing; the agent writes, the skill executes, the next-step agent summarizes the rows
- **`document-summarizer`** — for showing query results in plain English to a non-technical viewer
- **A guardrail layer** — once Phase 1 guardrails ship, you can hook PII detection on the OUTPUT (the SQL query string) to catch accidentally-leaked column names or hardcoded literals
