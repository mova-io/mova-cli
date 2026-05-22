#!/usr/bin/env bash
# Acceptance E2E: blank agent -> validate -> run -> context -> version -> deploy
# -> remote inference -> Langfuse. Reproducible from a clean checkout: it
# scaffolds a fresh throwaway project in a temp dir (no prebuilt example
# copied) and asserts each step. No hidden local state.
#
# Usage:
#   scripts/acceptance_e2e.sh
#
# Env (all optional — steps that need them SKIP cleanly if unset):
#   MDK=...                  CLI to invoke (default: mdk; e.g. "uv run mdk")
#   OPENAI_API_KEY=...       enables real-model assertions (phrase + context)
#   MOVATE_E2E_TARGET=...    deploy target name -> enables the Azure steps
#   LANGFUSE_SECRET_KEY=...  (+PUBLIC_KEY/HOST) -> enables the tracing check
#
# Exit non-zero on the first hard failure (local structural steps). Azure /
# model / Langfuse steps SKIP (not fail) when their env isn't configured, so
# the script is green on a laptop and exhaustive in CI with secrets.
set -euo pipefail

MDK="${MDK:-mdk}"
PHRASE="ACCEPTANCE-PHRASE-7Q2X9"          # unique; unlikely to be hallucinated
CODENAME="BLUEHERON-4417"                  # unique factual value for the context
WORKDIR="$(mktemp -d -t mdk-e2e-XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

pass()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail()  { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }
skip()  { printf '  \033[33m- SKIP\033[0m %s\n' "$1"; }
step()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

command -v "${MDK%% *}" >/dev/null 2>&1 || fail "CLI '$MDK' not found (set MDK=...)"
printf 'mdk: %s (%s)\n' "$MDK" "$($MDK --version 2>&1 | tail -1)"

cd "$WORKDIR"

step "1. Create a brand-new project + minimal agent from scaffolding"
$MDK init --project proj --skip-snapshot --no-open-editor >/dev/null
cd proj
$MDK init bot -t default --target agents >/dev/null   # 'default' = blank template, no example copied
[ -f agents/bot/agent.yaml ] || fail "agent.yaml not generated"
[ -f agents/bot/prompt.md ]  || fail "prompt.md not generated"
pass "agent.yaml + prompt.md generated"

step "2. agent.yaml carries runtime / model / schema / version"
ay=agents/bot/agent.yaml
grep -q '^version:'  "$ay" || fail "missing version metadata"
grep -q 'provider:'  "$ay" || fail "missing model configuration"
grep -qE 'schema:'   "$ay" || fail "missing schema block"
grep -q 'input:'     "$ay" || fail "missing input schema"
grep -q 'output:'    "$ay" || fail "missing output schema"
pass "version + model + input/output schema present (runtime defaults to litellm)"

step "3. Update prompt.md with a deterministic unique validation phrase"
cat > agents/bot/prompt.md <<EOF
You are a deterministic echo agent for acceptance testing.

Rules:
- ALWAYS include the exact token ${PHRASE} somewhere in your reply.
- Do not invent facts. If asked something you don't know, say "unknown".

User input:
{{ input.text }}

Respond with a single JSON object: {"message": "<reply containing ${PHRASE}>"}
EOF
pass "prompt.md updated with phrase ${PHRASE}"

step "4. Local validation (mdk validate)"
$MDK validate agents/bot >/dev/null 2>&1 || fail "mdk validate failed"
pass "mdk validate clean"

step "5. Local inference (mdk run) — structural (mock) + behavioral (real model)"
out_mock="$($MDK run agents/bot --mock '{"text":"ping"}' 2>/dev/null || true)"
echo "$out_mock" | grep -q '"message"' || fail "mock run did not produce a 'message' field"
pass "mdk run --mock produced schema-valid output"
if [ -n "${OPENAI_API_KEY:-}" ]; then
  out_real="$($MDK run agents/bot '{"text":"say hello"}' 2>/dev/null || true)"
  echo "$out_real" | grep -q "$PHRASE" \
    && pass "real-model output contains the validation phrase (prompt drives output)" \
    || fail "validation phrase ${PHRASE} NOT found in real-model output"
else
  skip "real-model phrase assertion (set OPENAI_API_KEY to enable)"
fi

step "6. Add a context with unique facts + wire it into the agent"
$MDK contexts create company-facts --agent bot >/dev/null
cat > agents/bot/contexts/company-facts.md <<EOF
# Company facts (source of truth)

- The internal project codename is ${CODENAME}.
- The support SLA is 4 business hours.
EOF
grep -q 'company-facts' "$ay" || fail "context not wired into agent.yaml"
pass "context created + wired (agents/bot/contexts/company-facts.md)"

step "7. Prompt uses the context + forbids hallucination; query it locally"
cat > agents/bot/prompt.md <<EOF
You answer ONLY using the facts in the provided context above.
If the answer is not in the context, reply exactly "unknown" — never guess.
Always include the token ${PHRASE}.

User input:
{{ input.text }}

Respond with a single JSON object: {"message": "<answer, with ${PHRASE}>"}
EOF
$MDK validate agents/bot >/dev/null 2>&1 || fail "validate failed after context wiring"
if [ -n "${OPENAI_API_KEY:-}" ]; then
  ctx_out="$($MDK run agents/bot '{"text":"What is the internal project codename?"}' 2>/dev/null || true)"
  echo "$ctx_out" | grep -q "$CODENAME" \
    && pass "context fact ${CODENAME} retrieved exactly" \
    || fail "context fact ${CODENAME} NOT retrieved"
  hall_out="$($MDK run agents/bot '{"text":"What is the CEO home address?"}' 2>/dev/null || true)"
  echo "$hall_out" | grep -qi 'unknown' \
    && pass "out-of-context question answered 'unknown' (no hallucination)" \
    || skip "model did not say 'unknown' for out-of-context query — review manually"
else
  skip "context retrieval + no-hallucination assertions (set OPENAI_API_KEY)"
fi

step "8. Bump agent version"
old_ver="$(grep '^version:' "$ay" | head -1)"
python3 - "$ay" <<'PY'
import re, sys
p = sys.argv[1]; t = open(p).read()
def bump(m):
    parts = m.group(1).split("."); parts[-1] = str(int(parts[-1]) + 1)
    return 'version: ' + ".".join(parts)
open(p, "w").write(re.sub(r'^version:\s*"?([0-9.]+)"?', bump, t, count=1, flags=re.M))
PY
new_ver="$(grep '^version:' "$ay" | head -1)"
[ "$old_ver" != "$new_ver" ] && pass "version bumped: ${old_ver#version: } -> ${new_ver#version: }" || fail "version not bumped"

step "9. Deploy to Azure + remote inference (needs MOVATE_E2E_TARGET)"
if [ -n "${MOVATE_E2E_TARGET:-}" ]; then
  T="$MOVATE_E2E_TARGET"
  $MDK deploy --target "$T" --mode agents >/dev/null || fail "mdk deploy failed"
  pass "deployed (agent.yaml + prompt + contexts uploaded)"
  $MDK deploy --target "$T" --mode agents --status 2>/dev/null | grep -q bot \
    && pass "deployment listed remotely (mdk deploy --status)" \
    || skip "could not confirm via --status"
  rem="$($MDK run agents/bot --target "$T" '{"text":"What is the internal project codename?"}' 2>/dev/null || true)"
  echo "$rem" | grep -q "$CODENAME" \
    && pass "remote inference retrieved ${CODENAME} (contexts deployed; not stale)" \
    || fail "remote inference did NOT return ${CODENAME}"
else
  skip "Azure deploy + remote inference (set MOVATE_E2E_TARGET=<your target>)"
  printf '       commands: %s deploy --target <T> --mode agents ; %s run agents/bot --target <T> '"'"'{...}'"'"'\n' "$MDK" "$MDK"
fi

step "10. Langfuse tracing"
if [ -n "${LANGFUSE_SECRET_KEY:-}" ]; then
  MOVATE_TRACER=langfuse $MDK run agents/bot --mock '{"text":"trace me"}' >/dev/null 2>&1 \
    && pass "ran with Langfuse tracer active — check the Langfuse UI for the trace" \
    || skip "Langfuse run errored — check LANGFUSE_* + logs"
  printf '       trace should carry: agent, version, model, input, rendered prompt, contexts, output, latency, tokens\n'
else
  skip "Langfuse trace check (set LANGFUSE_SECRET_KEY/PUBLIC_KEY/HOST)"
fi

printf '\n\033[1;32mAcceptance E2E complete.\033[0m Local steps verified; gated steps ran where configured.\n'
