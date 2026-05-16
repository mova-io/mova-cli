# SQL Style Guide

This guide governs every query the sql-writer agent produces. The rules
exist to make generated SQL safe, readable, and deterministic — not to
enforce personal taste.

## Safety rules (non-negotiable)

1. **Read-only by default.** Never generate `INSERT`, `UPDATE`, `DELETE`,
   `DROP`, `TRUNCATE`, `CREATE`, `ALTER`, or `GRANT` statements unless
   the input schema explicitly marks the operation as write-enabled
   (`allow_writes: true`). If the user's natural-language question implies
   a write, explain the restriction and generate the equivalent `SELECT`
   that shows what would be affected.

2. **No wildcards in production column lists.** Avoid `SELECT *` unless
   the user explicitly asks for all columns. Name columns explicitly —
   it protects callers from schema drift and signals intentionality.

3. **Always qualify table names.** Use `schema.table` notation when a
   schema is provided in the input. Bare table names are only acceptable
   when no schema prefix is given.

4. **Parameterize user-supplied values.** Never interpolate raw user
   strings into the query body. Represent them as `%(param)s` (psycopg2
   style) or `?` (SQLite style) based on the `dialect` input field, and
   return the parameter map in `params`.

## Formatting

- Keywords (`SELECT`, `FROM`, `WHERE`, `JOIN`, `GROUP BY`, etc.) in
  **uppercase**.
- Each major clause on its own line.
- Indent continuation lines 4 spaces.
- Column aliases in snake_case.
- Table aliases: single-character only for simple joins; meaningful short
  words (e.g. `ord`, `cust`) for queries with 3+ tables.

Example of correct formatting:

```sql
SELECT
    c.customer_id,
    c.name AS customer_name,
    COUNT(o.order_id) AS order_count
FROM customers AS c
LEFT JOIN orders AS o
    ON c.customer_id = o.customer_id
WHERE c.created_at >= %(since)s
GROUP BY c.customer_id, c.name
ORDER BY order_count DESC
LIMIT 100;
```

## Performance hints (emit in `explanation`, not in the SQL itself)

- If the query filters on a column not in any index, note: "Add an index
  on `<table>.<column>` if this query runs frequently."
- If the result set is unbounded and the user did not ask for a LIMIT,
  add `LIMIT 1000` and explain why.
- Prefer `EXISTS` over `COUNT(*) > 0` for existence checks.
- Prefer `NOT EXISTS` over `LEFT JOIN … WHERE … IS NULL` for exclusions.

## Dialect notes

Honor the `dialect` field in the input:

| Dialect | Date truncation | String concat | Regex |
|---|---|---|---|
| `postgresql` | `DATE_TRUNC('month', col)` | `\|\|` or `CONCAT()` | `~` operator |
| `mysql` | `DATE_FORMAT(col, '%Y-%m')` | `CONCAT()` | `REGEXP` |
| `sqlite` | `strftime('%Y-%m', col)` | `\|\|` | `GLOB` (limited) |
| `bigquery` | `DATE_TRUNC(col, MONTH)` | `CONCAT()` | `REGEXP_CONTAINS()` |
| `snowflake` | `DATE_TRUNC('month', col)` | `\|\|` | `REGEXP_LIKE()` |

When `dialect` is absent, default to `postgresql`.

## What to put in `explanation`

The `explanation` field is plain English for a non-SQL reader. Include:
- What the query returns (one sentence).
- Any non-obvious join logic or filter conditions.
- Performance notes (see above).
- What parameters must be supplied at runtime.

Do NOT repeat the SQL verbatim in the explanation.
