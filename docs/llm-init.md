# `mdk init --llm` — design notes & runbook

This is the internal companion to the [user-facing section in USER_GUIDE.md](../USER_GUIDE.md#llm-driven-scaffolding-mdk-init---llm). Aimed at:

- **Operators** debugging a failed `--llm` scaffold (the `.movate/llm-init-failed-*.json` artifact path)
- **Maintainers** changing the meta-prompt or the `GeneratedAgent` schema
- **Anyone wondering** why we picked the structure we did

## Module map

| File | Responsibility |
|---|---|
| `src/movate/scaffold/__init__.py` | Public exports (`GeneratedAgent`, `GenerationResult`, `LLMScaffoldError`, `generate_agent_from_description`, `write_agent_files`) |
| `src/movate/scaffold/llm_scaffold.py` | The meta-prompt + retry prompt + few-shot exemplars + generator function + IO helper |
| `src/movate/cli/init.py` | Wires `--llm` into the CLI: validation loop, retry, debug artifact, cost echo, spinner, summary line |

## End-to-end flow

```
mdk init <name> --llm "<description>" [--mock] [--dry-run] [--force]
    │
    ├── pre-flight: dest exists + not --force ? → exit 2
    │
    ├── build_local_runtime(mock=...)   ─── reuses the same provider plumbing as `mdk run`
    │
    ├── attempt 1: generate_agent_from_description(description, name, model, provider)
    │       │
    │       └── provider.complete(meta_prompt + few-shot)
    │           └── parse JSON (with defensive code-fence stripping)
    │           └── validate against GeneratedAgent Pydantic schema
    │           └── return GenerationResult(agent, tokens)
    │
    ├── force agent_yaml.name = <CLI arg>   ── defensive override
    │
    ├── validate: write to tempdir → load_agent(tempdir)
    │       │
    │       └── failure? → retry attempt 2 with error fed back to LLM
    │       └── second failure? → save .movate/llm-init-failed-<name>.json, exit 1
    │
    ├── --dry-run? → render preview Panel, hint, summary line, return
    │
    └── commit: write to tempdir → shutil.copytree → target
            └── success Panel + cost echo + stderr hint + mdk_init_summary line
```

## The meta-prompt

Lives in `_META_PROMPT` (in `llm_scaffold.py`). Three sections, in order:

1. **HARD CONSTRAINTS** — explicit rules the LLM must honor (api_version, JSON Schema type whitelist, additionalProperties=false, etc.). Listed FIRST because the model anchors more on early instructions than late examples.
2. **GENERATEDAGENT SCHEMA** — the JSON shape we expect back.
3. **Two few-shot examples** — FAQ + classifier, lifted verbatim from `src/movate/templates/{faq_agent,classifier_agent}/`. They're inlined as string literals (not loaded from disk at import time) so the meta-prompt is self-contained and reproducible.

**When to update the few-shot exemplars:**

- If `src/movate/templates/faq_agent/` or `classifier_agent/` change in a way that materially shifts what a "good" agent looks like.
- If pilot data shows the LLM ignoring a constraint that's better-illustrated than spelled out.

**When to NOT update them:**

- Minor template tweaks (descriptions, tags). Cosmetic drift in the inlined copies is fine — the structural shape is what teaches the model.

## The retry prompt

Lives in `_RETRY_PROMPT`. Used when attempt 1 generates valid `GeneratedAgent` JSON but `load_agent` rejects the result (bad JSON Schema, unresolvable prompt template, missing required agent.yaml field, etc.). The retry feeds:

- The full previous attempt (`previous_attempt.model_dump_json(indent=2)`)
- The `load_agent` error message verbatim
- An explicit "fix the error above and return a corrected GeneratedAgent JSON object"

We retry **once**. Two failures is the cap because:

1. If two attempts don't converge, a third probably won't either — the issue is likely in the description or a model limitation, not bad luck.
2. Cost — at 3-5 calls × $0.0004 each, three attempts is well over a cent of compute every `init --llm`.
3. Latency — operators don't want to wait 30s for a slow scaffold.

## Validation loop

`_try_validate(generated, name)` is the workhorse:

1. Write the `GeneratedAgent` to a `tempfile.TemporaryDirectory()` using `write_agent_files`.
2. Call `load_agent(tempdir)` — the same function `mdk run` / `mdk validate` use.
3. Return `None` on success, or the `AgentLoadError` message on failure.

The tempdir is auto-cleaned via the context manager. Tests can swap MockProvider in via `--mock`; the validation step still runs against the real `load_agent` so we catch shape errors even in hermetic CI.

## Debug artifact format

When **both** attempts fail validation, the raw payload + error gets stashed at `.movate/llm-init-failed-<name>.json`:

```json
{
  "error": "agent.yaml validation failed: ...",
  "name": "faq-agent",
  "payload": {
    "agent_yaml": { ... },
    "prompt_md": "...",
    "input_schema": { ... },
    "output_schema": { ... },
    "sample_evals": [...]
  }
}
```

**Operator workflow when this fires:**

1. `cat .movate/llm-init-failed-faq-agent.json | jq .error` — read the loader error.
2. `jq .payload .movate/llm-init-failed-faq-agent.json` — inspect what the LLM actually produced.
3. Decide:
   - Refine the description and re-run `mdk init --llm`.
   - Or save the JSON, hand-fix it (drop `payload` into a tempdir as separate files, edit), and run `mdk validate` to confirm.
   - Or scaffold from a template instead (`mdk init faq-agent -t faq`) and hand-edit.

The artifact is intentionally not git-ignored — operators may want to commit it temporarily while debugging.

## Cost tracking

`GenerationResult.tokens` carries the `TokenUsage` for the call that produced the agent. `_init_agent_from_llm` rolls token usage across attempt 1 + retry via `_accumulate_tokens`. Cost is computed via `_safe_cost` (pricing-table lookup) and surfaced in:

- The Rich success Panel — `Cost: $0.000401 USD` row
- The greppable summary line — `cost_usd=0.000401` field

A pricing-table miss (unknown model) returns `None` from `_safe_cost`; the Panel omits the Cost row and the summary line uses `cost_usd=unknown`. Scaffold never aborts on a pricing miss — the agent files are already on disk.

**Typical costs** at `openai/gpt-4o-mini-2024-07-18` (the default):

| Path | Tokens (in / out) | Cost |
|---|---|---|
| First-attempt success | ~1500 / ~250 | ~$0.0004 |
| With retry | ~3000 / ~500 | ~$0.0008 |
| Both attempts fail | ~3000 / ~500 | ~$0.0008 |

## Greppable summary line

```
mdk_init_summary: name=<agent> llm=<bool> model=<provider/model>
  input_tokens=N output_tokens=N cost_usd=<float|unknown>
  retried=<bool> ok=<bool>
```

Emitted on **every** successful scaffold and on failed-with-retry paths. Mirrors `mdk_audit_summary`, `mdk_eval_summary`, `mdk_doctor_summary` — one consistent prefix for CI parsers.

**Failure modes that DON'T emit a summary line today:**

- First-attempt `LLMScaffoldError` (provider error or schema mismatch before retry path engages).

If pilot data shows operators want a summary even on first-attempt provider errors, move the print BEFORE the exit in `_run_llm_scaffold`.

## Known limitations

1. **Quality drift.** LLM scaffolds may be subtly wrong (over-permissive schemas, vague prompts). The validation loop catches **structural** issues (loader rejects) but not **semantic** ones (the prompt makes sense but isn't what the operator wanted). The post-success hint `→ scaffolded by --llm · review prompt.md before first real run` sets the right expectation.
2. **JSON Schema gotchas.** LLMs frequently hallucinate types like `"datetime"` or `"uuid"` that aren't in JSON Schema 2020-12. The meta-prompt's HARD CONSTRAINTS list calls this out explicitly, and the validation loop catches survivors via `Draft202012Validator.check_schema`.
3. **Non-determinism.** Same description → potentially different output every call. We hard-set `temperature=0` in `CompletionRequest.params` but the upstream provider may still introduce variance (especially with system-level batching). Acceptable for init; not something to build downstream determinism on.
4. **Project mode + `--llm`.** Disabled — project mode is too lightweight to need LLM help. Two-step is the supported flow (`mdk init --project foo && cd foo && mdk init agent --llm "..."`).
5. **MockProvider can't see the meta-prompt.** It returns canned output verbatim, so the "name must equal `<X>`" constraint isn't honored under `--mock`. The Phase-3 defensive override (`agent_yaml.name = <CLI arg>` after generation) covers this gap for both mock and real LLM paths.

## Testing

Three test files cover the rollout:

| File | Scope |
|---|---|
| `tests/test_init_llm_phase_1.py` | CLI surface — flag presence, mutual-exclusion guards, backwards compat |
| `tests/test_init_llm_phase_2.py` | Generator module + validation loop + end-to-end with `--mock` |
| `tests/test_init_llm_phase_3.py` | UX polish — name override, spinner wiring, cost, hint, summary line |

All hermetic via `MockProvider` — no test in the suite calls a real LLM. Cost-tracking tests use `_safe_cost` directly against the packaged pricing table.

To exercise the real path manually:

```bash
export OPENAI_API_KEY=sk-...
mdk init smoke-agent --llm "an agent that echoes user messages"
mdk validate ./smoke-agent
```

## Rollout history

- **PR #54 (Phase 1)** — CLI surface. Stub generator that printed "not yet implemented".
- **PR #55 (Phase 2)** — Generator module, validation loop, retry, debug artifact.
- **PR #56 (Phase 3)** — UX polish: spinner, cost echo, hint, name override, greppable summary line.
- **PR #57 (Phase 4)** — Docs (this file + USER_GUIDE + README + module docstring).
