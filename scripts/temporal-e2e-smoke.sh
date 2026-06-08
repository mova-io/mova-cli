#!/usr/bin/env bash
# End-to-end smoke for the DEPLOYED durable-HITL stack (ADR 062 / 078 / 080).
# Drives a real `runtime: temporal` workflow with a HUMAN node through the full
# cycle against a live runtime — submit → pause → signal → resume → terminal —
# and asserts it reaches SUCCESS. A green run proves, end-to-end:
#
#   * the deployed Temporal SERVER + WORKER are up and connected (the workflow
#     actually started + parked at the HUMAN gate);
#   * the durable pause is listable (GET /workflow-runs?status=paused);
#   * the resume door works (POST /workflow-runs/{id}/signal → Temporal handle);
#   * the deployed WORKER IMAGE carries ADR 080 terminal-state sync — the
#     persist_workflow_result_activity flipped the run out of PAUSED to a
#     SUCCESS terminal WorkflowRunRecord (the image-drift check from the memo).
#
# This is the codified version of the validation footer in deploy-temporal.sh.
#
# Usage:
#   RUNTIME_URL=https://movate-dev-api.<domain> \
#   API_KEY=mvt_live_...  WORKFLOW=<deployed runtime:temporal workflow name> \
#     ./scripts/temporal-e2e-smoke.sh
#
# Optional env:
#   INPUT='{"text":"smoke"}'        # initial state (default: {"text":"smoke"})
#   DECISION='{"decision":"approve"}'  # the human decision merged on resume.
#                                   # MUST supply every key in the gate's
#                                   # output_contract (default: {"decision":"approve"}).
#   TIMEOUT=120                     # seconds to wait for pause, and for terminal.
#
# Prereqs: a `runtime: temporal` workflow with a HUMAN node deployed to the
# target runtime, and an API key with the `run` scope. curl + python3 on PATH.
set -uo pipefail

RUNTIME_URL="${RUNTIME_URL:-}"
API_KEY="${API_KEY:-}"
WORKFLOW="${WORKFLOW:-}"
# NOTE: do NOT default these with ${VAR:-{...}} — a brace inside the default
# value collides with the brace closing ${...}, so bash appends a stray '}' to
# ANY provided value (e.g. INPUT='{"request":"x"}' became '{"request":"x"}}' →
# invalid JSON). Assign the JSON default on a separate line instead.
INPUT="${INPUT:-}"; [ -n "$INPUT" ] || INPUT='{"text":"smoke"}'
DECISION="${DECISION:-}"; [ -n "$DECISION" ] || DECISION='{"decision":"approve"}'
TIMEOUT="${TIMEOUT:-120}"

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
info() { printf '  \033[2m%s\033[0m\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

command -v curl >/dev/null || { echo "curl not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
[ -n "$RUNTIME_URL" ] || { echo "set RUNTIME_URL (the deployed runtime base URL)" >&2; exit 1; }
[ -n "$API_KEY" ] || { echo "set API_KEY (a runtime key with the 'run' scope)" >&2; exit 1; }
[ -n "$WORKFLOW" ] || { echo "set WORKFLOW (a deployed runtime:temporal workflow name)" >&2; exit 1; }

RUNTIME_URL="${RUNTIME_URL%/}"
AUTH=(-H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json")

# Extract a JSON field from stdin via python3 (no jq dependency).
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)" 2>/dev/null; }

# List the workflow_run_ids currently PAUSED for our target workflow.
paused_ids_for_workflow() {
  curl -fsS "${AUTH[@]}" "${RUNTIME_URL}/api/v1/workflow-runs?status=paused" 2>/dev/null \
    | python3 -c "
import sys,json
d=json.load(sys.stdin)
wf='${WORKFLOW}'
print('\n'.join(r['workflow_run_id'] for r in d.get('workflow_runs',[]) if r.get('workflow')==wf))
" 2>/dev/null
}

# Status of one run id (polls the list; '' if not found).
status_of() {
  local rid="$1" st
  for s in success error paused running; do
    st=$(curl -fsS "${AUTH[@]}" "${RUNTIME_URL}/api/v1/workflow-runs?status=${s}" 2>/dev/null \
      | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(next((r['status'] for r in d.get('workflow_runs',[]) if r['workflow_run_id']=='${rid}'),''))
" 2>/dev/null)
    [ -n "$st" ] && { echo "$st"; return; }
  done
  echo ""
}

hdr "Target"
info "runtime:  ${RUNTIME_URL}"
info "workflow: ${WORKFLOW}"

# Snapshot pre-existing paused runs so we can detect the NEW one we create.
BEFORE=$(paused_ids_for_workflow | sort -u)

hdr "1. Submit the workflow"
# Build the payload with python3 (already a dep, used by jget) rather than shell
# string-interpolation — INPUT can contain spaces / commas / unicode, which made
# the old inline -d brittle. Also validates INPUT is well-formed JSON up front.
PAYLOAD=$(MDK_WF="$WORKFLOW" MDK_IN="$INPUT" python3 -c '
import os, json, sys
try:
    inp = json.loads(os.environ["MDK_IN"])
except Exception as e:
    sys.stderr.write(f"INPUT is not valid JSON: {e}\n"); sys.exit(2)
print(json.dumps({"kind": "workflow", "target": os.environ["MDK_WF"], "input": inp}))
') || fail "INPUT is not valid JSON: ${INPUT}"
SUBMIT=$(printf '%s' "$PAYLOAD" | curl -fsS -X POST "${AUTH[@]}" --data @- \
  "${RUNTIME_URL}/run" 2>/dev/null) || fail "submit failed (POST /run) — check RUNTIME_URL / API_KEY / scope"
JOB_ID=$(printf '%s' "$SUBMIT" | jget "['job_id']")
[ -n "$JOB_ID" ] || fail "no job_id in submit response: $SUBMIT"
pass "queued job ${JOB_ID}"

hdr "2. Wait for the durable pause at the HUMAN gate"
RUN_ID=""
deadline=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  NEW=$(comm -13 <(printf '%s\n' "$BEFORE") <(paused_ids_for_workflow | sort -u) | head -1)
  if [ -n "$NEW" ]; then RUN_ID="$NEW"; break; fi
  sleep 3
done
[ -n "$RUN_ID" ] || fail "no new PAUSED run for '${WORKFLOW}' within ${TIMEOUT}s — is the Temporal server+worker up? (scripts/temporal-preflight.sh)"
pass "run ${RUN_ID} is PAUSED awaiting human decision"

hdr "3. Signal the human decision"
SIG_PAYLOAD=$(MDK_DEC="$DECISION" python3 -c '
import os, json, sys
try:
    dec = json.loads(os.environ["MDK_DEC"])
except Exception as e:
    sys.stderr.write(f"DECISION is not valid JSON: {e}\n"); sys.exit(2)
print(json.dumps({"decision": dec}))
') || fail "DECISION is not valid JSON: ${DECISION}"
SIG=$(printf '%s' "$SIG_PAYLOAD" | curl -fsS -X POST "${AUTH[@]}" --data @- \
  "${RUNTIME_URL}/api/v1/workflow-runs/${RUN_ID}/signal" 2>/dev/null) \
  || fail "signal failed — decision must supply every output_contract key (set DECISION=...)"
pass "signalled (decision=${DECISION})"

hdr "4. Wait for the terminal state (resume → SUCCESS)"
FINAL=""
deadline=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  FINAL=$(status_of "$RUN_ID")
  case "$FINAL" in
    success|error) break ;;
  esac
  sleep 3
done

hdr "Result"
case "$FINAL" in
  success)
    pass "run ${RUN_ID} resumed to SUCCESS — durable HITL + ADR 080 terminal-sync verified end-to-end on the deployed stack"
    exit 0 ;;
  error)
    fail "run ${RUN_ID} resumed to ERROR — check worker logs (az containerapp logs show -n <temporal-worker> ...)" ;;
  *)
    fail "run ${RUN_ID} did not reach a terminal state within ${TIMEOUT}s (last: '${FINAL:-unknown}'). If it's stuck PAUSED, the deployed worker image may predate ADR 080 terminal-sync (image drift)." ;;
esac
