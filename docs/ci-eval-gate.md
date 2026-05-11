# CI eval-gating with movate

Block PR merges when the eval suite regresses. Two pieces compose here:

1. The `--baseline-file` / `--output-baseline` flags on `movate eval` —
   git-trackable EvalRecord snapshots, which are the unblocking primitive
   for ephemeral CI runners (sqlite is per-runner, JSON files travel with
   the repo).
2. An example GitHub Actions workflow at
   [.github/workflows/eval-gate.example.yml](../.github/workflows/eval-gate.example.yml).

This doc explains the flow and how to wire it in your own repo.

## The two-loop flow

```text
                  ┌──────────────────┐
                  │ main branch push │
                  └────────┬─────────┘
                           │ refresh-baseline job
                           ▼
        movate eval ./agent --output-baseline .movate/agent/baseline.json
                           │
                           │ git commit + push
                           ▼
              .movate/agent/baseline.json  (committed)
                           │
                           │ pulled by every PR
                           ▼
                  ┌──────────────────┐
                  │   PR opened      │
                  └────────┬─────────┘
                           │ gate-pr job
                           ▼
        movate eval ./agent --baseline-file .movate/agent/baseline.json
                           │
                           ├─ exit 0 → merge allowed
                           └─ exit 1 → PR blocked (regression)
```

## Quick start

In your *consumer* repo (the one holding agents, not movate-cli itself):

1. **Generate the first baseline locally**:

    ```bash
    movate eval ./agents/my-agent --mock --output-baseline .movate/my-agent/baseline.json
    git add .movate/my-agent/baseline.json
    git commit -m "chore(eval): seed my-agent baseline"
    ```

2. **Copy the example workflow**:

    ```bash
    cp /path/to/movate-cli/.github/workflows/eval-gate.example.yml \
       .github/workflows/eval-gate.yml
    ```

3. **Edit the matrix** to list your agents and the file paths under
   `agents/`. The example uses `faq-agent`; change it.

4. **Push**. PRs that touch `agents/**` or `.movate/**` will run the gate.

## Tuning

### `--regression-tolerance`

Default is `0.0` — strict. Any drop in mean_score or pass_rate fails the
PR. For LLM-as-judge with `runs: 3+`, bump to `0.05` to absorb sampling
noise:

```yaml
- run: |
    movate eval "./agents/${{ matrix.agent }}" \
      --baseline-file ".movate/${{ matrix.agent }}/baseline.json" \
      --regression-tolerance 0.05 \
      --output json
```

### Mock vs real-model eval

`--mock` is hermetic — no API keys, no network. Use it as the *gate*; it
catches prompt logic, schema, and template-rendering regressions.

For real-model eval (catches drift in model outputs themselves), drop
`--mock` and add provider keys as repo secrets:

```yaml
env:
  OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

A common setup runs `--mock` on every PR (fast, free) and the real-model
eval nightly on `main` (slow, costs money).

The shipped `agents.yml` workflow auto-selects between mock and real
based on whether the secrets are populated. No YAML changes needed
when you're ready to flip — just add the secrets.

### Scoring method: which judge for which agent

Three options live in `evals/judge.yaml` — pick the cheapest that
catches your regression class:

| `method:`       | When to use                                              | API keys   | Cost   |
|-----------------|----------------------------------------------------------|------------|--------|
| `exact`         | Finite-label classifier, deterministic output keys       | None       | $0     |
| `subset_match`  | Agent output is richer than the pinned dataset fields    | None       | $0     |
| `llm_judge`     | Open-ended natural-language output, quality dimensions   | Both¹      | ~$0.01/case |

¹ The judge must run on a different provider family than the agent —
cross-family enforcement at eval time. With OpenAI agent + Anthropic
judge (or vice versa) you need both keys.

`subset_match` is the sweet spot when your dataset's `expected` only
pins a subset of the agent's output (e.g.,
`expected: {"tone": "positive"}` for an agent that outputs
`{headline, body, next_steps, tone}`). It scores 1.0 iff every
expected key/value appears in the actual output; extras are tolerated.
No LLM call.

**Upgrade path** when you're ready for richer scoring:

1. Pick a judge model on a different family than your agent
   (e.g., agent on OpenAI → judge on `anthropic/claude-haiku-4-5`).
2. Add the judge's API key to repo secrets.
3. Edit `evals/judge.yaml` — set `method: llm_judge`, uncomment the
   `model:` and `rubric:` blocks.
4. Regenerate the baseline so it reflects the new scoring shape:

    ```bash
    movate eval ./agents/my-agent \
      --output-baseline .movate/my-agent/baseline.json
    git commit -am "eval: upgrade my-agent to llm_judge"
    ```

5. The next CI run uses real-LLM judge scoring against the new baseline.
   Bump `--regression-tolerance` to ~`0.05` to absorb sampling variance.

### Refreshing the baseline

The `refresh-baseline` job in the example workflow auto-commits a new
baseline on every merge to `main`. Two things to know:

- It only commits when the baseline actually changed. A no-op merge
  produces no commit.
- The bot needs `permissions: contents: write` (set in the workflow) and
  branch protections that allow it to push. If `main` is protected
  against direct pushes, change the job to open a PR instead.

If you don't want auto-refresh, delete the `refresh-baseline` job and
update baselines manually:

```bash
movate eval ./agents/my-agent --mock --output-baseline .movate/my-agent/baseline.json
git commit -am "chore(eval): refresh my-agent baseline"
```

## Mutually exclusive flags

`--baseline` and `--baseline-file` are mutually exclusive. Use one:

- `--baseline <eval-id>` — for local development, looks up an EvalRecord
  in your local sqlite. Convenient when iterating on the same machine.
- `--baseline-file <path>` — for CI, reads a git-tracked JSON file. Works
  on any fresh runner.

## Exit codes

The gate uses these exit codes (same as the local CLI):

| code | meaning                                              |
|------|------------------------------------------------------|
| 0    | all cases passed `--gate` AND no baseline regression |
| 1    | gate failed OR baseline regression detected          |
| 2    | operator error (load failure, missing baseline file) |

CI marks 1 and 2 as failures; merge is blocked on either.
