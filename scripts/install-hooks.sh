#!/bin/sh
# One-time dev setup: route git hooks to the tracked .githooks/ directory.
#
# NOTE (ADR 066): the version is now DERIVED FROM GIT at build time, so the
# pre-commit hook no longer bumps a version — it's a no-op today. This script is
# kept so `core.hooksPath=.githooks` is wired for any FUTURE shared hook; running
# it is harmless but currently does nothing version-related.
#
#     ./scripts/install-hooks.sh
#
# Re-run is harmless (idempotent). Undo with: git config --unset core.hooksPath
set -e

git config core.hooksPath .githooks
chmod +x .githooks/pre-commit 2>/dev/null || true

echo "✓ Git hooks enabled (core.hooksPath=.githooks)."
echo "  Version is git-derived at build time (ADR 066) — no per-commit bump."
