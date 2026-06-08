# CI eval-gating with movate

Block PR merges when the eval suite regresses. Two pieces compose here:

1. The `--baseline-file` / `--output-baseline` flags on `mdk eval` —
   git-trackable EvalRecord snapshots, which are the unblocking primitive
   for ephemeral CI runners (sqlite is per-runner, JSON files travel with
   the repo).
2. An example GitHub Actions workflow at
   [docs/ci/eval-gate.example.yml](../docs/ci/eval-gate.example.yml).

This doc explains the flow and how to wire it in your own repo.

## The two-loop flow

```text
                  ┌──────────────────┐
                  │ main branch push │
                  └────────┬─────────┘
                           │ refresh-baseline job
                           ▼
        mdk eval ./agent --output-baseline .movate/agent/baseline.json
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
        mdk eval ./agent --baseline-file .movate/agent/baseline.json
                           │
                           ├─ exit 0 → merge allowed
                           └─ exit 1 → PR blocked (regression)
```

## Quick start

In your *consumer* repo (the one holding agents, not movate-cli itself):

1. **Generate the first baseline locally**:

    ```bash
    mdk eval ./agents/my-agent --mock --output-baseline .movate/my-agent/baseline.json
    git add .movate/my-agent/baseline.json
    git commit -m "chore(eval): seed my-agent baseline"
    ```

2. **Copy the example workflow**:

    ```bash
    cp /path/to/movate-cli/docs/ci/eval-gate.example.yml \
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
    mdk eval "./agents/${{ matrix.agent }}" \
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
mdk eval ./agents/my-agent --mock --output-baseline .movate/my-agent/baseline.json
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
