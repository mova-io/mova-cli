# ADR 067 — Standalone voice SDK: extract `movate.voice` behind a framework-neutral `AgentTurn` seam

**Status:** Proposed
**Date:** 2026-06-01
**Deciders:** Engineering + Deva (Movate)
**Context window:** make the voice capability usable **separately from `mdk`** —
shippable as its own library so it can voice-enable agents on **other
platforms** (Lyzr ADK first; ADR 069) and embed in customer deliverables that do
not run the mdk runtime. The voice stack is already 95% decoupled; this ADR
removes the last coupling and packages the result.
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 048 (voice agents — the three speech seams `SpeechToTextProvider` /
`TextToSpeechProvider` / `RealtimeVoiceProvider`, the chunk types, the pipeline
driver, the WS transport, the optional `voice:` block; **this ADR moves that
code into its own distribution and changes none of the Protocol signatures**),
ADR 007 (the adapter/plugin pattern — `BaseLLMProvider` / `StorageProvider` /
`Tracer`; `AgentTurn` is that same seam philosophy applied to the agent stage of
the voice pipeline),
ADR 050 (voice API parity — the WS route, `?mode=realtime`, the capabilities
block; preserved unchanged, now wired to the external package),
ADR 018 (per-tenant BYOK — `api_key=` injection at the seam is unchanged; the
package never reads a global key when one is supplied).

**Defining architectural fact.** Voice is **already** a transport + adapters
that import nothing from mdk — `movate/voice/base.py` and every STT/TTS/realtime
adapter depend only on `pydantic` + a lazily-imported provider SDK. The **single
line of coupling** to mdk is in `movate/voice/pipeline.py`: it imports
`AgentBundle` / `RunRequest` from `movate.core` and calls
`executor.execute(bundle, run_request, on_token=..., tenant_id_override=...)`.
Replace that one call with an **injected `AgentTurn`** and the entire package
becomes framework-neutral and independently shippable. The extraction is a
**packaging + one-seam** change, not a rewrite.

---

## Context

The voice demo lands well, and Deva wants it to stop being an mdk-only feature:
a partner running agents on Lyzr (or any Python agent framework) should be able
to `pip install` the voice library and wrap their *own* agent's text turn with
streaming STT → agent → TTS, with **no mdk runtime in the picture**.

Today that is blocked by exactly one thing. The pipeline driver
([pipeline.py](../../src/movate/voice/pipeline.py)) hard-codes the mdk Executor
as its agent stage:

```python
from movate.core.loader import AgentBundle      # TYPE_CHECKING
from movate.core.models import RunRequest        # lazy, inside the function
...
response = await executor.execute(
    bundle, run_request, on_token=..., tenant_id_override=tenant_id,
)
```

Everything else in `movate/voice/` is already import-clean: the three Protocols,
the chunk types, the OpenAI/Deepgram/Cartesia/ElevenLabs/Azure adapters, the
realtime adapters, the test doubles, and the latency-badge helpers. A grep for
`from movate` / `import movate` across the package returns **only** those two
lines in `pipeline.py`.

So the question is not "can voice be decoupled" — it nearly is — but "what is the
right seam for the agent stage, and how do we ship the result." This ADR answers
both: a minimal `AgentTurn` Protocol, and a separate distribution
(`movate-voice`) built and versioned in its own repository.

## Decision

The voice package is **extracted into a standalone, independently-versioned
distribution**, and its pipeline's agent stage is generalized behind a new
framework-neutral seam. mdk consumes the package; the Executor becomes one
adapter behind that seam.

### D1 — A new distribution `movate-voice`, in its own repository

`movate/voice/` (the three Protocols + chunk types, all STT/TTS/realtime
adapters, the pipeline driver, the latency helpers, and the test doubles) moves
into a **separate git repository** published as the **`movate-voice`** PyPI
distribution with **independent versioning**. The provider SDKs stay **optional
extras** (`openai` / `deepgram` / `cartesia` / `elevenlabs` / `azure` /
`realtime`), each imported lazily exactly as today (ADR 048 D9), so the base
install pulls only `pydantic` + stdlib and stays permissively licensed. The
demo, CI, and ADR history for the voice stack follow into that repo (these ADRs
are authored here — the architectural home alongside 048/049/050 — and copied
into the new repo at creation).

### D2 — The `AgentTurn` seam (the agent stage, generalized)

Define a minimal, framework-neutral Protocol for the pipeline's agent stage — in
*exactly* the shape of the existing speech seams and `BaseLLMProvider` (ADR 007):
an async turn that takes the **final transcript** and produces a stream of text
deltas plus a final answer. It mirrors precisely what `run_voice_pipeline`
extracts from the Executor today — streamed tokens (the `on_token` deltas), a
final human-readable answer (`response.human_readable` or the joined tokens), and
a terminal `run_id` / `status` / optional error — but expressed without any mdk
type:

```python
@runtime_checkable
class AgentTurn(Protocol):
    """Run one text turn: transcript in → streamed text out. Framework-neutral.

    The pipeline's agent stage. An mdk Executor, a Lyzr ADK agent (ADR 069), or
    any callable that turns text into text satisfies it. No AgentBundle, no
    RunRequest, no mdk import — the seam is the contract.
    """
    def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> Awaitable[AgentTurnResult]: ...
```

with a small result envelope (`answer_text`, `run_id`, `status`, `error`) that
carries what the pipeline's `done` / `error` events need. The `on_token`
callback keeps today's streaming-token contract verbatim, so the latency story
(partial STT → streamed agent tokens → streamed TTS) is preserved.

### D3 — `run_voice_pipeline` takes an `AgentTurn`, not an Executor

The single behavioral refactor this ADR mandates: `run_voice_pipeline`
([pipeline.py](../../src/movate/voice/pipeline.py)) drops the
`from movate.core.loader import AgentBundle` / `from movate.core.models import
RunRequest` imports and the `executor.execute(...)` call, and instead accepts an
`AgentTurn`. The queue/`on_token` plumbing, the error-stage events
(`stt` / `agent` / `tts`), barge-in (`cancel`), and the `at_ms` latency stamps
are **unchanged** — only the agent-stage call site changes (it now awaits
`agent.run(transcript, on_token=...)` and reads the result envelope instead of
an mdk `RunResponse`).

### D4 — mdk depends on `movate-voice`; the Executor becomes one adapter

The only place `AgentBundle` / `RunRequest` / `executor.execute` live after this
is a thin **mdk-side** adapter, `ExecutorAgentTurn`, that satisfies `AgentTurn`
by calling the unchanged Executor. mdk's WS voice route, the `mdk voice` CLI, the
playground transport, and the capabilities endpoint import their voice types from
`movate-voice`; the `mdk[voice]` extra now resolves to `movate-voice[...]`. The
Executor stays **modality-blind** (CLAUDE.md rule 6 / ADR 048 R2) — it just sits
behind one more seam. **No change** to the WS `/api/v1/agents/{name}/voice`
transport, the message protocol, the `agent.yaml` `voice:` block, the
`MDK_VOICE_*` env vars, or the capabilities JSON shape (ADR 050).

### D5 — Backward-compatible import paths

To avoid churning every in-tree importer at once, mdk preserves the
`movate.voice.*` import surface by **re-exporting** from `movate-voice` (a thin
shim package, or a dependency alias). Existing `from movate.voice import ...`
call sites and `tests/test_voice_*` keep working; the voice tests migrate into
the new repo as the canonical suite, with mdk keeping a small integration test
that the `ExecutorAgentTurn` adapter still drives a real agent turn end-to-end.

### D6 — Own repo, CI, release, and license posture

The new repo carries its own `pyproject.toml` (build-backend `hatchling`, the
provider extras above), its own license gate (the provider SDKs stay
opt-in/out-of-tree, so the base wheel is permissively licensed), its own test
matrix, and its own version line. It does **not** inherit mdk's CalVer machinery
(ADR 066) — versioning is the new repo's decision, recorded in its own first ADR.

## Consequences

**Positive**
- **Voice runs with zero mdk.** A partner `pip install movate-voice` + a few
  lines wraps their own agent (ADR 069) — the explicit ask.
- **The extraction is small and low-risk** — one seam (`AgentTurn`) + one call
  site, because the package was already import-clean everywhere else.
- **mdk loses nothing.** Same WS API, same CLI, same capabilities, same
  zero-change-to-existing-agents promise; the Executor just sits behind a seam.
- **The seam generalizes beyond Lyzr** — any text-in/text-out agent (LangGraph,
  a bare function, a remote HTTP agent) becomes voiceable by implementing
  `AgentTurn`.

**Negative / risks**
- **Two repos to keep in step.** A `movate-voice` release that changes
  `AgentTurn` or a chunk type must be coordinated with an mdk bump.
  *Mitigation:* `AgentTurn` and the chunk types are deliberately tiny and
  stable; mdk pins a compatible `movate-voice` range; contract tests run in both.
- **A dependency-resolution change for `mdk[voice]`** — it now pulls an external
  dist. *Mitigation:* additive; a default `mdk` install (no `[voice]`) is
  unaffected, exactly as today.
- **Re-export shim is a (small) maintenance surface.** *Mitigation:* it is pure
  re-export, no logic; it can be removed in a later major once callers migrate.

**Neutral**
- All net-new surface is **additive**; no change to ADR 048's Protocol
  signatures, ADR 050's WS/API shapes, the `agent.yaml` schema, storage, or
  deploy behavior.

## New surfaces (flagged per CLAUDE.md rule 5)

All **ADDITIVE**; none changes an existing `agent.yaml`/`project.yaml` field, the
`/api/v1` runtime API, a storage schema, a `MOVATE_*`/`MDK_*` env var, an
existing `--json` shape, or deploy behavior:
- **The `movate-voice` distribution** — a new external package (the `mdk[voice]`
  extra now resolves to it).
- **The `AgentTurn` Protocol + `AgentTurnResult`** — a new adapter seam in the
  ADR-007 family, exported from `movate-voice`.
- **`ExecutorAgentTurn`** — a new mdk-side adapter (the only remaining home of
  `AgentBundle`/`RunRequest`/`executor.execute` in the voice path).

## Alternatives considered

- **(a) In-repo import boundary + the `[voice]` extra (no extraction).**
  Rejected by the deciders. It enforces "voice imports nothing from core" in
  tests but still ships *inside* `movate-cli`, so a Lyzr user must
  `pip install movate-cli` to get voice — not "separate from mdk."
- **(b) Second build target in this monorepo.** Rejected by the deciders — a
  second `hatchling` wheel from `src/movate/voice/` is lighter than a repo split
  but couples versioning/CI/release to mdk; the deciders chose a clean repo
  boundary.
- **(c) Leave the monolith; document embedding.** Rejected — does not deliver a
  standalone install at all; the pipeline still imports `movate.core`.
- **(d) Make the Executor itself the cross-framework seam.** Rejected — it drags
  `AgentBundle`/`RunRequest`/storage/tenancy into every consumer; `AgentTurn` is
  the minimal contract the pipeline actually needs.

## Boundaries (explicitly NOT in scope)

- **The resilient router / fallback** — that is ADR 068 (it ships *in*
  `movate-voice` but is its own decision).
- **The Lyzr binding** — that is ADR 069 (an `AgentTurn` impl + an extra).
- **Any change to the three speech Protocols, the WS transport, the `?mode=
  realtime` route, the `voice:` block, or the capabilities JSON** (ADR 048/050).
- **Making the Executor voice-aware** — it stays modality-blind, now behind
  `AgentTurn` (CLAUDE.md rule 6).
- **The new repo's own versioning/CI policy** — recorded in that repo's first
  ADR, not here.

## Cross-references / composition notes

- **ADR 048 (the seams).** The three speech Protocols, chunk types, and pipeline
  move verbatim; `AgentTurn` replaces the *implicit* Executor dependency the
  pipeline carried — it is the seam ADR 048 should have had for the agent stage.
- **ADR 007 (adapter pattern).** `AgentTurn` is ADR 007's seam philosophy
  applied to the pipeline's agent stage — a Protocol with swappable impls
  (`ExecutorAgentTurn`, `LyzrAgentTurn`, …).
- **ADR 050 (API parity).** The WS route, `?mode=realtime`, and capabilities
  block are unchanged; mdk simply wires them to the external package.
- **ADR 018 (BYOK).** The `api_key=` injection at the speech seams is unchanged;
  keys still resolve at the edge and never live in an adapter.
- **ADR 068 / ADR 069** build directly on the `AgentTurn` seam + the standalone
  package this ADR establishes.
