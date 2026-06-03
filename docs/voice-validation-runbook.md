# Voice validation runbook

A short, do-this protocol to settle the two open voice questions with **real
data** from the live demo, instead of guesses. Run it before changing any
default. Owner: whoever can place real calls (Deva / Jeremy). ~20 minutes.

The demo already emits everything you need — this runbook just says where to look
and what number flips each decision.

Demo: <https://mdk-voice-demo.delightfulcoast-91af3b05.eastus.azurecontainerapps.io/>
(Open the **Detailed** view — the toggles and chips below live there.)

---

## Question 1 — Should speculative kickoff (ADR 070) default ON?

**What it does.** Starts the agent on a stable interim transcript, before
endpointing, to recover the ~1.66 s silence wait the bench measured. Commits if
the endpointed final matches; cancels (and re-runs) if you kept talking. Cancels
cost a wasted agent run, so the decision hinges on the **commit rate on real
human speech** (the bench's 83% was clean TTS — a ceiling, not a forecast).

**Protocol**
1. Detailed view → set **Speculate = on**. Use the **Mova-iO** or **OpenAI**
   agent tier (the Lyzr **SDK** tier is not cancel-safe → the chip shows
   "(no-op on this tier)").
2. Place **at least 15 real turns** spanning the natural speech you expect:
   - some clean, complete sentences;
   - some with mid-sentence pauses ("um… let me think…");
   - a few where you stop, then add more ("turn on the… the lights").
3. Read the **Speculate chip**: `N✓ / M✗ (P% commit)`.
4. Watch the **latency badge** ("responded in X ms") with Speculate on vs off on
   similar utterances — note the typical delta.

**Decision thresholds**
| Commit rate (human speech) | Action |
|---|---|
| **≥ 75%** and badge shows a clear win | Default speculation **ON** (I'll flip `VoiceConfig.speculative` default + the demo default). |
| **50–75%** | Keep **opt-in**; consider a longer `speculation_quiet_gap_s` (e.g. 0.4–0.5 s) to raise commit rate, re-measure. |
| **< 50%** | Keep **off** by default; speculation loses for this speech profile. |

Report back: the commit rate, the turn count, and the rough latency delta. That's
all I need to make (or not make) the one-line default change.

---

## Question 2 — Do keyterms (nova-3 boosting) actually help YOUR audio?

**What it does.** Boosts domain vocab (VPN, Okta, Mova-iO, MFA…) at recognition
time. The bench showed **no WER change on clean TTS** — keyterms only fixed
casing there. The real win is on **noisy / accented / fast human speech**, which
a synthetic corpus can't represent.

**Protocol**
1. Speak 8–10 utterances **dense with the enterprise vocab** — exactly the
   support phrasing real callers use ("the VIP's VPN connects but Okta rejects
   the MFA push after the SSO migration").
2. Do it **twice**: once over a clean mic, once degraded (speakerphone /
   background noise / a non-native speaker if available).
3. Read each turn's **transcript.final** in the event stream — count the
   domain-term mis-hears (VPN→BPM, Okta→October, MFA→MSA, etc.).
4. Compare against keyterms effectively off: set `DEEPGRAM_KEYTERMS=""` on the
   container (or run a build with the env cleared) and repeat the noisy set.

**Decision**
- If keyterms **measurably cut** domain-term errors on the noisy set → keep them
  on by default (they already are in the demo) and document the per-agent
  `keyterms` block (ADR 071 D4) as a recommended practice for enterprise agents.
- If **no difference even on noisy audio** → keep the capability (zero downside)
  but stop advertising it as a headline accuracy win; it's insurance only.

Report back: rough domain-term error counts, keyterms on vs off, clean vs noisy.

---

## Question 3 (lightweight) — Is the TTS phrase cache earning its keep?

Read the per-turn `done` event's `cache` field (`hit_rate`) over a session with
repeated phrases (greetings, disclaimers). A rising hit-rate = falling $/turn. If
hit-rate stays ~0 in normal use, pre-warm the cache with your common phrases
(`warm_cache`) or drop it — no need to keep a cold cache.

---

## After the data lands

Send me the three numbers (commit rate, keyterm error delta, cache hit-rate) and
I'll make the corresponding default changes (or leave them opt-in) as a small,
reviewed PR — each backed by your real numbers, not the synthetic bench.
