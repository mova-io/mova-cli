# Polish backlog — the 8-command core path

Each of the 7 MDK commands in the demo's [core path](demo-end-to-end.md#the-8-command-core-path) (`init`, `doctor`, `validate`, `run`, `eval`, `config`, `deploy`) has a few ergonomic gaps that would make first-touch demos meaningfully smoother. Below is the stacked-ranked backlog — top items first.

Scored as **(value × frequency-of-use-in-demos) / effort**.

## Top 12 — ship these before the next big customer demo

### 1. `mdk init` — auto-detect default model from available API keys
**Value: very high · Effort: 2 hours**

Currently `--llm` always defaults to `openai/gpt-4o-mini-2024-07-18`. If the operator only has `ANTHROPIC_API_KEY` set, the scaffold fails on the first real run. Detect the present key and pick a sensible default per provider (gpt-4o-mini, claude-haiku-4-5, gemini-flash, etc.). Same for `mdk run --mock` first attempts.

### 2. `mdk deploy` — pre-flight dry-run summary before the commit point
**Value: very high · Effort: 4 hours**

Before kicking off `az acr build` (2-4 min), print: "this will build image X, push to ACR Y, update apps Z, take ~3 min — continue? [y/N]". One key to abort. Saves a lot of "oh wait wrong target" pain in live demos.

### 3. `mdk doctor` — handoff to `mdk fix` interactively
**Value: high · Effort: 2 hours**

`mdk doctor` ends with the greppable summary already. Add: "doctor found N issues; M of them auto-fixable. Run `mdk fix --apply` now? [y/N]". One-keystroke remediation closes the diagnose→fix loop without making operators read `mdk fix --list` separately.

### 4. `mdk run` — `--max-cost <usd>` hard ceiling per call
**Value: high · Effort: 3 hours**

`agent.yaml` has `budget.max_cost_usd_per_run` already, but operators iterating locally want an ad-hoc override. `mdk run ... --max-cost 0.10` rejects the call before it hits the provider if estimated cost would exceed. Same flag on `mdk eval` for batch safety.

### 5. `mdk init` — show estimated cost BEFORE the LLM call
**Value: high · Effort: 1 hour**

When `--llm` is passed without `--mock`, briefly show "this will call <model>, ~2k tokens, est. cost ~$0.0004 · continue? [y/N]". Skip the prompt with `--yes`. Stops customers from accidentally calling expensive models for a scaffold.

### 6. `mdk validate` — token-count + cost-per-call estimate for the resolved prompt
**Value: high · Effort: 3 hours**

After validation succeeds, add a row: "prompt: ~1,247 tokens · est. cost per call: $0.000187 (at default input shape)". Operators catch "I scaffolded a 12kB prompt" before they're at the eval stage. The default input shape comes from the first dataset row.

### 7. `mdk deploy` — `--rollback` shortcut + post-deploy rollback hint
**Value: high · Effort: 3 hours**

Today rollback is `mdk deploy --skip-build --image-tag <previous>`. Add `mdk deploy --rollback` (auto-resolves previous tag from ACR). After every successful deploy, echo: "if this breaks, run: `mdk deploy --target prod --rollback`". Lowers the panic threshold for operators.

### 8. `mdk eval` — `--watch` for hot-reload during prompt iteration
**Value: high · Effort: 4 hours**

`mdk watch <agent>` already re-runs `mdk validate` on file changes. `mdk eval --watch` would re-run the eval suite on prompt/dataset changes, showing only the diff vs the last run. Tight prompt-engineering loop without typing `mdk eval` 50 times.

### 9. `mdk config add-target` — interactive wizard
**Value: medium-high · Effort: 4 hours**

The non-interactive flag form (`--azure-subscription <X> --azure-resource-group <Y> ...`) is fine for scripts but rough for first-touch demos — operators have to look up four Azure GUIDs/names. `mdk config add-target prod --interactive` walks through the fields with prompts and resolves the ACR name from the subscription + RG. One command, no copy-paste tax.

### 10. `mdk run` — pretty-print + Rich syntax-highlight JSON output by default
**Value: medium · Effort: 2 hours**

Today's output is raw `print(json.dumps(...))`. Rich's `console.print_json(...)` already lights it up — switch the default in TTY mode and keep the raw form for `--output json` when piping. Demos look noticeably more polished.

### 11. `mdk doctor` — show last-N agent runs as "smoke test" rows
**Value: medium · Effort: 4 hours**

If the project has agents AND there's a recent run in `~/.movate/local.db`, add a "smoke" section: "agent: faq-agent · last run: 5 min ago · OK · $0.0004". Catches "my last run died and I forgot" cases. Same channel surfaces production drift later when `mdk doctor` runs in CI.

### 12. `mdk init` — auto-create initial git commit after `--project`
**Value: medium · Effort: 1 hour**

`mdk init --project foo` already snapshots. Also run `git init && git add -A && git commit -m "initial scaffold from mdk init"` so operators start at a clean tree. Skip with `--no-git`.

## Worth doing, lower priority (next quarter)

### 13. `mdk run` — echo Langfuse trace URL when configured
**Value: medium · Effort: 2 hours**

If `LANGFUSE_HOST` is set, echo the trace URL on stderr after every run alongside the existing `→ saved as run_id …` line. One-click to the trace view.

### 14. `mdk validate` — separate Jinja-render lint pass
**Value: medium · Effort: 3 hours**

Today `mdk validate` checks the YAML + schemas. Adding a "render the prompt against the first dataset row" step would catch undefined-variable bugs at validate time, not at first run.

### 15. `mdk deploy` — `--only api` / `--only worker` (already exists, but surface better)
**Value: medium · Effort: 1 hour**

The flags ship. But `mdk deploy --help` lists them with no example. Add a one-liner: "use --only worker to deploy just the queue drainer after a memory-bound bugfix".

### 16. `mdk eval` — confidence intervals on small datasets
**Value: medium · Effort: 6 hours**

When `sample_count < 20`, surface a 95% CI on `mean_score` so operators know "0.72 ± 0.18" rather than treating 0.72 as a precise number. Defuses small-eval over-interpretation, which is a constant customer footgun.

### 17. `mdk doctor` — Azure cost-this-month row when `--target` is set
**Value: medium · Effort: 5 hours**

After the Azure preflight, query Cost Management API and show "Azure spend this month: $42 · 11 days remaining · trend: $113/mo". Lights up cost awareness early in the conversation.

### 18. `mdk init` — `--inputs '{"text":"sample"}'` example payload helper
**Value: low-medium · Effort: 2 hours**

After scaffold, if the operator passes `--inputs '<json>'`, run the agent once with that input and append the response to the success Panel. "Here's what your scaffold actually does with that input." Free-form demo bait.

### 19. `mdk run` — comparison table for `--replay <id> --diff`
**Value: low-medium · Effort: 4 hours**

The flags exist; the diff view is JSON-vs-JSON today. A side-by-side Rich table (left: original, right: current) with cell-level deltas is much more demo-friendly.

### 20. `mdk deploy` — Slack/Teams webhook for "deployed prod" announcements
**Value: low-medium · Effort: 3 hours**

`MOVATE_DEPLOY_WEBHOOK=https://hooks.slack.com/...` → fire a structured message on every successful deploy: target, image tag, git SHA, who deployed, URL. Helps ops teams stay aware.

## Cross-cutting

### 21. Consistent `--yes / -y` flag on every command that prompts
**Value: medium · Effort: 2 hours**

Items 2, 3, 5, 9, 12 above all add new interactive prompts. They each need a `--yes` flag for CI use. Standardize the flag name + behavior up front rather than letting it drift command-by-command.

### 22. Greppable summary lines for `init`, `run`, and `deploy`
**Value: medium · Effort: 3 hours**

`init` already emits `mdk_init_summary:` (Phase 3 polish). `run` and `deploy` don't. Add `mdk_run_summary:` (agent, run_id, cost, latency, ok) and `mdk_deploy_summary:` (target, image_tag, duration, ok) for full CI parity across the core path.

## What I'd ship first

If you want a single "core-path polish" PR before the next demo:

- **Items 1, 2, 3, 5, 10** → ~12 hours of work, hits every command in the path with one improvement each, all of them visible-in-the-first-30-seconds-of-the-demo.

If you want to fold in deeper iteration support (good for engineers, less visible to customers):

- **Items 4, 6, 8, 14** → tighter inner loop. Eval `--watch` is the big one — turns prompt iteration from "type, run, read, type, run" into "type, save, glance."

## Out of scope (deliberately)

These are real gaps but don't belong in a core-path polish pass:

- **`mdk run --debug` with breakpoints** — IDE integration territory; way too much scope.
- **`mdk eval` with a built-in dataset editor** — TUI complexity not worth it.
- **Cross-cloud deploy (GCP / AWS)** — Azure first, others when paying customers ask.
