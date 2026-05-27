# Canonical agent layout

Every agent that `mdk` **scaffolds** uses ONE on-disk layout, regardless of
how it was created (`mdk init <name>`, `mdk add <role>`, or
`mdk init --llm "<description>"`):

```
<agent>/
  agent.yaml          # references schema via FILE paths: ./schema/input.yaml, ./schema/output.yaml
  prompt.md
  evals/
    dataset.jsonl
    judge.yaml.example
  schema/
    input.yaml        # YAML, not JSON
    output.yaml
```

## Why one shape

A scaffolder that emits a different shape per entry point is a source of
architectural entropy: operators learn one layout from `mdk add`, then meet a
second one from `mdk init --llm`, and tooling has to special-case both. The
decision (issue #127) is that **scaffolds standardize on `schema/*.yaml`
files plus a `judge.yaml.example`**, so a `--llm` agent is indistinguishable
from a hand-init'd one — same files, same field set in `agent.yaml`
(`model.fallback`, `timeouts`, `budget`, `tags`).

YAML (over JSON) for the schema files because:

- it matches the bundled templates (`src/movate/templates/*/schema/*.yaml`),
- it allows comments, which a JSON Schema file cannot carry, and
- it reads top-to-bottom like the rest of `agent.yaml`.

The generated schemas are full JSON Schema 2020-12 documents (they carry a
`$schema` key); the loader shape-sniffs a `$schema`-bearing YAML doc and uses
it verbatim, so a `.yaml` schema file loads identically to the old `.json`
one.

## Both schema forms still load (back-compat)

This is a convention for what **new scaffolds emit** — not a change to the
loader. All of these remain first-class and load without warning:

- **inline shorthand** in `agent.yaml` (`schema: { input: { text: string } }`)
  — the right call for tiny 2–3-field contracts,
- a **`schema/*.yaml`** file (the canonical scaffold form, above), and
- a **`schema/*.json`** file (the v0.x form) — existing agents that reference
  `./schema/input.json` keep working unchanged.

If you hand-author an agent, pick whichever form fits the contract. The
scaffolders pick `schema/*.yaml` files so every generated agent looks the
same.

## See also

- [`llm-init.md`](llm-init.md) — `mdk init --llm` design notes & runbook.
- `src/movate/scaffold/llm_scaffold.py` — `write_agent_files`, the single
  writer that materializes the canonical layout.
- `src/movate/templates/agent_init/agent.yaml` — the hand-init'd field set
  the `--llm` scaffold aligns to.
