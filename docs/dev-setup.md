# Developer setup — clone to first commit

The minimum steps to take a fresh clone of `movate-cli` (`mdk`) to the point
where you can land a passing PR. Pairs with [`dev-loop.md`](dev-loop.md) (the
day-to-day workflow on a configured checkout).

## Prerequisites

- **Python 3.11+** — the runtime + CLI baseline (`python --version`).
- **[`uv`](https://docs.astral.sh/uv/)** — the project's package + lockfile
  manager. Install via `curl -LsSf https://astral.sh/uv/install.sh | sh` or
  see the upstream docs.
- **Git 2.30+** — for `core.hooksPath` (the hook routing below relies on it).

## One-time setup

```sh
git clone <repo-url> movate-cli
cd movate-cli
uv sync --all-extras            # full dev deps incl. otel, langfuse, eval extras
scripts/install-hooks.sh        # route git hooks to .githooks/ — see below
```

## Install the git hooks — required, not optional

Versioning here is CalVer (`YYYY.M.D.N`, e.g. `2026.5.27.3`) and is
**auto-bumped by `.githooks/pre-commit` on every commit**. CI enforces that
every PR's version is *strictly ahead* of `origin/main` — a PR with an
un-bumped version is rejected at the gate.

Git does not share hooks across clones: hooks normally live in `.git/hooks/`,
which is not version-controlled. The repo ships its hooks under tracked
`.githooks/` and `scripts/install-hooks.sh` points git at that directory:

```sh
scripts/install-hooks.sh
```

That script runs `git config core.hooksPath .githooks` and makes the
pre-commit hook executable. It's idempotent — re-running is harmless.

### Verify the hook is wired

```sh
git config core.hooksPath
# expected: .githooks
```

If that prints `.git/hooks` (or nothing), the hook is not installed; re-run
`scripts/install-hooks.sh`.

You can also confirm the pre-commit fires by making any trivial change and
running `git commit` — you should see version-file changes (e.g. updated
`src/movate/__init__.py`, `pyproject.toml`, `uv.lock`) re-staged into your
commit.

## What if you forgot to install the hook?

CI's version gate will reject your PR with a message like *"version must be
strictly ahead of origin/main"*. Two fixes:

1. **Bump and amend** (fastest if you only have one commit on the branch):

   ```sh
   python scripts/bump_version.py
   git add -A
   git commit --amend --no-edit
   git push --force-with-lease
   ```

2. **Install the hook, then add a new commit** (preferred if you've already
   collaborated on the branch — avoids rewriting shared history):

   ```sh
   scripts/install-hooks.sh
   python scripts/bump_version.py    # one manual bump to catch up
   git add -A
   git commit -m "chore: bump CalVer"
   git push
   ```

Either way, future commits on the branch will auto-bump correctly.

## Troubleshooting

- **`core.hooksPath` already prints `.githooks` but the hook still doesn't
  fire.** Check that `.githooks/pre-commit` is executable
  (`ls -l .githooks/pre-commit`). The install script does this for you, but
  manual `git clone` on some filesystems can drop the executable bit. Fix:
  `chmod +x .githooks/pre-commit`.

- **Worktrees** (`git worktree add ...`) inherit `core.hooksPath` from the
  shared `.git/config`, so the hook should fire there too. If you've ever
  set a per-worktree `core.hooksPath` to something else (e.g. via
  `git config --worktree`), unset it: `git config --worktree --unset core.hooksPath`.

- **CI keeps rejecting a PR that you *did* bump locally.** Check the version
  on the PR head with `git show HEAD:src/movate/__init__.py | grep
  __version__` and compare to `origin/main`. CI compares the merge-base
  view; rebase onto the latest `origin/main` if you're stale.

- **You don't want the hook to bump on a specific commit** (rare — e.g. a
  pure docs-only commit you're keeping out of CalVer for some reason). Pass
  `--no-verify` to `git commit`, but only with explicit reviewer agreement —
  CI's version gate will still need to be satisfied before merge.

## See also

- [`docs/dev-loop.md`](dev-loop.md) — the day-to-day flow from `mdk init` to
  Azure deploy.
- [`docs/architecture-principles.md`](architecture-principles.md) — layering
  + adapter seams + compat contracts.
- [`docs/adr/`](adr/) — accepted architectural decisions.
