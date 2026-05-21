#!/bin/sh
# One-time dev setup: route git hooks to the tracked .githooks/ directory so
# the CalVer version (YYYY.M.D.N) auto-bumps on every commit.
#
# Git does not share hooks automatically (they live in .git/hooks, which is
# not version-controlled), so each clone runs this once:
#
#     ./scripts/install-hooks.sh
#
# Re-run is harmless (idempotent). Undo with: git config --unset core.hooksPath
set -e

git config core.hooksPath .githooks
chmod +x .githooks/pre-commit 2>/dev/null || true

echo "✓ Git hooks enabled (core.hooksPath=.githooks)."
echo "  Versions now auto-bump as YYYY.M.D.N on every commit."
