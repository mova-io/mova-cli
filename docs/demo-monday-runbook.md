# MDK Monday demo runbook — the 5-beat arc

**Status:** Live demo script (the bulletproof Monday flow)
**Audience:** The demo driver
**Duration:** ~12 minutes (5 beats; beat 4 is a stretch)
**One job:** show MDK's end-to-end story — *author → eval → ingest → graph →
voice → workflow → mission-control* — with copy-paste commands that all run on
seeded, offline data.

This runbook is the companion to the broader
[`docs/demo-runbook.md`](demo-runbook.md) (the 10-segment architecture-review
walkthrough). Where that one is comprehensive, **this one is the tight Monday
arc**: five beats, one seed command up top, and a "if X breaks, do Y" fallback
under every beat so nothing is fatal live.

Everything below runs against the **one-command seed** — no provider keys, no
network, no Azure. The seed is deterministic, Movate-themed, idempotent, and
fully purgeable.

---

## 0. One-command seed (run this first, the night before AND right before)

```bash
# Pin a local store for the demo so the seed + every beat agree on the data.
export MOVATE_DB="$HOME/.movate/monday-demo.db"

# Seed EVERYTHING: tenant + 3 sample agents (1 voice-capable, 1 workflow),
# a 20-node Movate knowledge graph, 150+ runs / 40+ evals of telemetry, and
# the ADR-047 analyzer insights that feed the dashboards.
mdk demo seed --clear-first --with-voice
```

Expected tail of the output (a green "Demo seeded" panel):

```
 sample agents         │ 3 (1 voice)
 sample workflows      │ 1
 graph nodes / edges   │ 20 / 22
 insights analyzed     │ 24
 Playground + graph viewer populated — 3 sample agents (incl. voice +
 workflow) and a 20-node knowledge graph under demo-acme / agent support-triage.
```

Then **confirm the env is GO**:

```bash
mdk demo doctor --run-agents
```

Expected: a green checklist ending in `✓ Demo is GO`. The only acceptable
yellow is `playground reachable → optional` (you start the playground on
demand in beat 5). Any red `✗ MISSING` row blocks the demo — re-run the seed
and re-check. `mdk demo doctor` exits non-zero on a hard fail, so you can gate
a pre-demo script on it.

> **What got seeded, and where it shows up**
>
> | Seeded | Beat it lights up | Scope |
> |---|---|---|
> | 3 sample agents (incl. `voice-concierge`) | 1, 3 | tenant `demo-acme` |
> | `demo-triage-flow` workflow | 4 | tenant `demo-acme` |
> | 20-node / 22-edge knowledge graph | 2 | agent `support-triage` |
> | 150+ runs, 40+ evals, anomalies | 1, 5 | tenants `demo-*` |
> | 24 analyzer insights (ADR 047) | 5 | `demo-acme` / `default` |
>
> All rows are tagged `tenant=demo-*`. Purge anytime with `mdk demo clear`.

**Fallback (seed/doctor):** if `mdk demo doctor` is red, run
`mdk demo seed --clear-first` once more and re-check. If a single hard row is
red (e.g. insights), it usually means `MOVATE_DB` differs between the seed
shell and the demo shell — re-export it and re-run. If the whole seed errors,
fall back to `mdk demo seed --telemetry-only` (dashboards only) and skip
beats 2–4.

---

## Beat 1 — Author + eval-in-the-loop (`mdk dev`) (~2.5 min)

**What you'll show.** The authoring inner loop: a live agent you edit and
re-run, with an eval scorecard closing the loop. The seeded `support-triage`
agent is already in the registry, so you have something real to drive.

```bash
# The fleet the seed registered.
mdk agents list --tenant demo-acme

# Run the seeded agent offline (mock provider — no keys, deterministic).
mdk run support-triage --mock '{"question": "How do I reset my password?"}'

# Close the loop — eval the agent against its seeded dataset.
mdk eval support-triage --mock

# The live authoring loop (foreground; edit prompt.md, watch it re-run).
mdk dev support-triage
```

**What this proves.** One executor, three planes (CLI = the same core
`Executor` the runtime + worker use); the eval scorecard is the governance
pillar (ADR 016). `--mock` keeps it hermetic.

**If it breaks:** if `mdk dev` can't find the agent on disk, drive the beat
entirely from the registry: `mdk run support-triage --mock '{...}'` +
`mdk eval support-triage --mock` tell the same author→eval story without the
live-reload loop. If a real-provider run 401s, add `--mock`.

---

## Beat 2 — Ingest → live-growing graph + node drill-down (~2.5 min)

**What you'll show.** A populated knowledge graph (seeded: a 20-node
Movate-support network — plan tiers, policies, SOPs, integrations, incidents)
rendered in the graph viewer, with node drill-down showing 1-hop neighbors and
provenance.

```bash
# Boot the runtime so the graph viewer + API have a backend.
mdk serve --dev --port 8000 &     # --dev mints a known local key

# The graph viewer (sigma.js) — open in a browser.
mdk graph viewer --target local   # or: open the playground's Graph tab

# CLI view of the same graph (proves the data without a browser):
mdk graph show support-triage --target local
mdk graph show support-triage --target local --root "Pro Tier" --depth 1
```

You can also show the graph *growing* by ingesting a doc into the same agent
(`mdk kb ingest support-triage <path> --build-graph`) — the seeded graph gives
you a non-empty starting point so the new nodes land in an existing network,
not an empty canvas.

**What this proves.** ADR 010 entities/relations behind the StorageProvider
Protocol; ADR 046 read-only graphology query API; node drill-down +
provenance. The seeded graph means this beat is never "no graph — build one
first."

**If it breaks:** if the viewer won't render, fall back to the CLI:
`mdk graph show support-triage --target local` prints the node/edge table. If
the runtime isn't up, `mdk demo doctor` reads the same graph straight from
storage — the `knowledge graph` + `graph drill-down` rows prove the data
exists (`'Billing' has 4 neighbor(s)`).

---

## Beat 3 — Voice question + barge-in + latency (~2 min)

**What you'll show.** The voice-capable agent (`voice-concierge`, seeded with a
`voice:` block — Deepgram STT + Cartesia TTS) answering a spoken question, with
barge-in and a live latency readout.

```bash
# The seeded voice agent's config (proves the voice: block is present).
mdk agents show voice-concierge --tenant demo-acme

# Voice mode in the playground (mic → STT → agent → TTS).
mdk playground --voice            # then ask a question; interrupt to show barge-in

# The seeded voice-turn telemetry (STT/TTS latency, audio duration, $/turn):
#   (seeded by `mdk demo seed --with-voice`)
mdk report --since 7d --tenant demo-acme
```

**What this proves.** ADR 048 voice agents — the additive per-agent `voice:`
block makes an agent voice-capable with zero core changes; realistic STT/TTS
latency bands; barge-in. The voice-turn telemetry feeds the voice-latency
panels.

**If it breaks:** voice needs a mic + the `[voice]` extra + provider keys —
the most fragile beat. If the live mic path fails, fall back to the seeded
**voice-turn telemetry**: `mdk demo doctor` confirms the `voice-capable agent`
row is green, and `mdk report` shows the seeded STT/TTS latency figures. Show
the `voice:` block in `mdk agents show voice-concierge` and narrate the path.

---

## Beat 4 — Workflow on Temporal *(stretch / optional)* (~1.5 min)

**What you'll show.** The seeded `demo-triage-flow` workflow (classify →
draft), optionally executed on the Temporal durable backend (ADR 054).

```bash
# The seeded workflow is in the registry.
mdk workflow list --tenant demo-acme

# Inspect it.
mdk workflow show demo-triage-flow --tenant demo-acme

# (stretch) Run it — requires the worker / Temporal dev server to be up.
# mdk workflow run demo-triage-flow --mock '{"question": "Refund please"}'
```

**What this proves.** ADR 037 workflow API parity + ADR 054 (Temporal as a
deterministic, durable workflow backend). The seed registers the workflow so
the registry + inspect path always works, even when the durable backend isn't
running.

**If it breaks (expected — this is the stretch beat):** skip the live run.
`mdk workflow list` + `mdk workflow show demo-triage-flow` prove the workflow
exists and is published; narrate Temporal verbally and move to beat 5. The
`mdk demo doctor` `workflow registered` row is intentionally *optional* (soft),
so a missing Temporal server never makes the env NO-GO.

---

## Beat 5 — Mission control: single pane (~2 min)

**What you'll show.** Everything in one place — the dashboards lit up with the
seeded telemetry + analyzer insights: a cost spike, a post-deploy latency
regression, and one agent drifting below its eval gate.

```bash
# Offline rollup (no browser needed).
mdk report --since 7d

# The dashboards-as-code that ship with every deployment.
ls dashboards/grafana dashboards/grafana/insights

# The matching HTTP surface (runtime from beat 2 still running):
curl -s -H "Authorization: Bearer $MDK_LOCAL_KEY" \
  http://localhost:8000/api/v1/report | jq .

# The insight-fed view (ADR 047 analyzer — 24 daily insights seeded):
curl -s -H "Authorization: Bearer $MDK_LOCAL_KEY" \
  http://localhost:8000/api/v1/observability/insights?project_id=default | jq '.[0]'
```

The seeded **storyline** to narrate: cost spike on one agent mid-window
(model-swap → ~4x spend), a latency regression right after a deploy annotation,
and an eval pass-rate drifting below the 0.70 gate on another agent — the
classic "the deploy did it" + "this one's drifting" demo moments.

**What this proves.** ADR 031 reporting & dashboards + ADR 047 observability
analyst: one factored core, two surfaces (CLI rollup + HTTP API), dashboards
populated from seeded data, anomalies/drift annotated for a real story.

**If it breaks:** if the HTTP surface 401s, drop the `curl`s and use the
offline `mdk report --since 7d` — it reads the same seeded telemetry directly.
`mdk demo doctor` already confirmed `dashboard telemetry`, `eval scorecards`,
and `analyzer insights` are all green, so the data is there regardless of the
browser/API path.

---

## After the demo — clean up

```bash
# Purge every demo-tagged row (agents, workflow, graph, telemetry, insights).
# Real telemetry (any non-demo tenant) is never touched.
mdk demo clear --yes
```

---

## Pre-demo checklist (run the night before)

Tick each box. If anything's red, fix it before Monday — do not debug live.

- [ ] `export MOVATE_DB="$HOME/.movate/monday-demo.db"` in your demo shell.
- [ ] `mdk demo seed --clear-first --with-voice` ends with a green
      "Demo seeded" panel (3 agents / 1 voice, 1 workflow, 20/22 graph,
      ~24 insights).
- [ ] `mdk demo doctor --run-agents` prints `✓ Demo is GO` (only the
      `playground reachable` row may be yellow).
- [ ] **Beat 1:** `mdk run support-triage --mock '{"question":"hi"}'` and
      `mdk eval support-triage --mock` both succeed.
- [ ] **Beat 2:** `mdk serve --dev --port 8000` boots; `mdk graph show
      support-triage --target local` prints a 20-node table.
- [ ] **Beat 3:** `mdk agents show voice-concierge --tenant demo-acme` shows
      the `voice:` block; the playground `--voice` mode opens.
- [ ] **Beat 4 (stretch):** `mdk workflow list --tenant demo-acme` lists
      `demo-triage-flow`.
- [ ] **Beat 5:** `mdk report --since 7d` shows non-zero runs / cost / a
      drifting agent; `dashboards/grafana/` is populated.

---

## What if a step fails — triage table

Keep this open in a second tab during the demo:

| Symptom | First action | Where |
|---|---|---|
| Anything looks empty | `mdk demo doctor` — find the red row, then `mdk demo seed --clear-first` | `src/movate/cli/_demo_doctor.py` |
| Seed errored mid-way | `mdk demo seed --telemetry-only` (dashboards only), skip beats 2–4 | `src/movate/cli/demo_cmd.py` |
| Data is there but a beat is empty | confirm `MOVATE_DB` matches the seed shell | — |
| Provider/auth 401 on a run | add `--mock` | `src/movate/cli/run.py` |
| Graph viewer won't render | `mdk graph show support-triage --target local` (CLI fallback) | `src/movate/cli/graph_cmd.py` |
| Voice mic path fails | show `mdk agents show voice-concierge` + seeded voice telemetry in `mdk report` | `src/movate/cli/playground.py` |
| Workflow run hangs | skip it — `mdk workflow show demo-triage-flow` proves it's registered | `src/movate/cli/workflow_cmd.py` |
| Dashboards/API 401 | use offline `mdk report --since 7d` | `src/movate/cli/report.py` |
| Need to start over | `mdk demo clear --yes && mdk demo seed --clear-first --with-voice` | `src/movate/cli/demo_cmd.py` |

---

## Where the seed + doctor live (for the curious)

- **One-command seed:** `mdk demo seed` —
  [`src/movate/cli/demo_cmd.py`](../src/movate/cli/demo_cmd.py). Telemetry
  generation is pure in
  [`src/movate/core/demo/seeder.py`](../src/movate/core/demo/seeder.py); the
  sample agents + workflow + knowledge graph are pure in
  [`src/movate/core/demo/scenario.py`](../src/movate/core/demo/scenario.py).
  Both are deterministic + offline (no keys, no network) and write through the
  `StorageProvider` Protocol.
- **Readiness check:** `mdk demo doctor` —
  [`src/movate/cli/_demo_doctor.py`](../src/movate/cli/_demo_doctor.py). Reads
  the seeded state back through the same Protocol and prints a green/red
  checklist; exits non-zero on a hard fail.
