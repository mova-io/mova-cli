#!/usr/bin/env bash
# Run the live-API smoke tests. Picks up keys from .env if present.
#
# Usage:
#   bash scripts/smoke.sh           # run all enabled providers
#   bash scripts/smoke.sh -v        # verbose
#
# Each provider's tests are independently gated on the corresponding
# API key, so a partial keyring still produces a useful result.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi

export MOVATE_SMOKE=1

echo "== movate live-API smoke =="
echo "  OPENAI_API_KEY    : $([ -n "${OPENAI_API_KEY:-}" ] && echo set || echo MISSING)"
echo "  ANTHROPIC_API_KEY : $([ -n "${ANTHROPIC_API_KEY:-}" ] && echo set || echo MISSING)"
echo

uv run pytest -m smoke "$@"
