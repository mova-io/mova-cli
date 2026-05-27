# ADR 027 — `mdk dev` live-reload test loop: re-run on save, see the diff

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x authoring DX — make the inner edit→test loop *live*, so
editing a prompt/context/schema instantly shows the agent's new output instead
of forcing a manual `mdk run`.
**Builds on / related:** ADR 025 (`mdk dev` copilot + `cli/dev_cmd.py`), ADR 026
(name resolution), ADR 023 (auto-retrieval — context edits matter live), the
saved `mdk dev` unified-workflow plan, and `cli/watch.py` / `core/loader.py` /
`cli/run.py`.

## Context

`mdk watch` already re-runs **`validate`** on file change (`cli/watch.py`), and
ADR 025 made `mdk dev` a conversational authoring copilot. But neither gives the
single thing that makes authoring feel alive: **"I edited the prompt — what does
the output look like now?"** Today you must switch to `mdk run` by hand; the
Chainlit playground talks to a remote runtime and has no reload. Adding a context
also gives no live feedback.

**Key enabling fact (verified):** local execution loads everything fresh from
disk on every call — `_dispatch_agent` → `load_agent(path)` → `_run_local_agent`
(`cli/run.py`); `load_agent` re-reads `agent.yaml`, prompt, schemas, skills, and
contexts each time (`core/loader.py`). There is **no cache or daemon**. So
"hot-reload" reduces to **re-run on file change** — no invalidation machinery
needed. The feature is mostly orchestration, not new engine work.

## Decision

Add a **hot-reload test loop** to `mdk dev` — also exposed as `mdk watch --run`
so the loop logic has one home. On every save to the agent's
`prompt.md` / `agent.yaml` / `schema/*` / `contexts/*`, **re-execute** the agent
against a test input and print the **new output plus a diff vs. the previous
run**, so "did my edit change anything?" is answerable at a glance.

### D1 — Extend `watch.py`, don't fork it
Reuse the existing mtime-poll architecture (`_compute_watched_paths`,
`_snapshot_mtimes`, the debounced loop). Two changes:
1. **Watch `contexts/`.** `_compute_watched_paths` currently watches
   yaml/prompt/schema/dataset/judge but **not** contexts. Add the project-level
   (`<root>/contexts/*.md`) and agent-local (`<agent_dir>/contexts/*.md`)
   markdown to the watched set — so a context edit *or a newly attached context*
   triggers a re-run.
2. **Add `dispatch_run_once(agent_dir, test_input, *, mock)`** beside the
   existing validate-only `dispatch_once`. It calls `load_agent` then the
   existing `_run_local_agent`, guarded by `contextlib.suppress(AgentLoadError)`
   so a half-saved file never crashes the loop (mirror watch.py's existing
   suppression).

### D2 — Output diff
Render the new run output and a short diff against the previous output (the
"did my edit change the answer?" signal). Keep the full output available; the
diff is the at-a-glance summary.

### D3 — Test-input precedence
Explicit `--input` > first row of `evals/dataset.jsonl` (reuse the existing
`_suggest_dataset_example` loader, `cli/run.py`) > prompt the user once and
remember it for the session.

### D4 — Concurrency model (the one real risk)
A **single foreground loop** that polls mtimes *between* `Prompt.ask` calls,
running `asyncio.run` per dispatch. **No background thread** — that avoids
event-loop reentrancy and terminal-input races (interleaving an interactive
prompt, mtime polling, and per-dispatch `asyncio.run`). Validate this shape with
a prototype before building the rest.

### D5 — Inline eval-delta (optional; folds in D7b / F4′)
After a re-run, optionally surface the pass-rate delta from a quick `--mock`
eval (the autopilot's `EvalRunner` seam already exists). Off by default; opt-in
so the loop stays fast.

## Consequences

**Positive**
- The inner loop is live: edit → see new output + diff, no manual `mdk run`.
- Context edits (and newly attached contexts) finally trigger feedback.
- One loop implementation shared by `mdk watch --run` and `mdk dev`.
- Reuses load-fresh-from-disk, `_run_local_agent`, `_suggest_dataset_example` —
  small net-new surface.

**Negative / risks**
- D4 concurrency is the crux: a synchronous watch loop driving async
  `_run_local_agent` while also taking interactive input. Mitigated by the
  single-foreground-loop, no-thread design + `AgentLoadError` suppression;
  prototype first.
- "Added a context, output didn't change" confusion when a new `contexts/foo.md`
  isn't referenced in `agent.yaml`. Mitigated: the copilot's attach action wires
  it; for raw file adds, print an explicit hint.

## Alternatives considered
- **A daemon/long-lived runtime with cache invalidation.** Rejected: load is
  already fresh-from-disk, so a daemon adds invalidation complexity for no gain.
- **A background watcher thread.** Rejected: event-loop reentrancy + terminal
  input races (D4).

## Scope / rollout
The loop lives in `watch.py` (shared by `mdk watch --run` and `mdk dev`);
`dev_cmd.py` drives it as one phase. Degrades to a documentation-only print on a
non-TTY (mirror the existing `sys.stdin.isatty()` gate). One PR; sequence after
the in-flight `dev_cmd.py` work (D7c) to avoid churn.
