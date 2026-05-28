# Daily "What's New" changelog automation

Every morning a scheduled GitHub Action collects the **previous day's merged
PRs** and prepends a dated digest to the `## What's New` section at the top of
[`README.md`](../README.md) — the page rendered at
github.com/mova-io/mova-cli/tree/main. The update lands via an **auto-merged
PR**, so it runs durably on GitHub's infrastructure (independent of any local
or agent session).

Moving parts:

| Piece | What it does |
|---|---|
| [`.github/workflows/daily-changelog.yml`](../.github/workflows/daily-changelog.yml) | Scheduled (+ manual) workflow: fetch PRs → generate → bump version → open auto-merge PR. |
| [`scripts/gen_daily_changelog.py`](../scripts/gen_daily_changelog.py) | Pure, hermetic generator: PR JSON in (stdin/file) → dated, grouped block prepended to the README section. No network in here. |
| `## What's New` in `README.md` | The rendered landing-page section the digest writes into. |

## Schedule

- **Cron:** `0 14 * * *` (14:00 UTC daily). GitHub cron is always UTC and does
  not observe DST.
- **US Pacific equivalent:** ~06:00 PDT (UTC-7, summer) / 07:00 PST (UTC-8,
  winter) — the digest is waiting when the US team starts the morning.

## How the digest is built

1. Resolve the digest date (yesterday UTC, or the `workflow_dispatch` `date`
   input) and the day window `[date, date+1)`.
2. `gh pr list --state merged --search "merged:>=<date>..<next>" --json
   number,title,author,mergedAt` → `prs.json`.
3. `python scripts/gen_daily_changelog.py --date <date> < prs.json`:
   - Groups PRs by conventional-commit prefix from the title
     (`feat`/`fix`/`docs`/`chore`/other).
   - Renders `### YYYY-MM-DD` + grouped bullets `- <title> (#<num>) @author`.
   - **Prepends** the block at the top of `## What's New` (newest date first),
     creating the section under the intro if absent.
   - **Idempotent:** a date already present is a no-op (no file rewrite).
   - **Empty day:** writes nothing and exits 0 (a quiet day is not an error).
4. If the README changed, `python scripts/bump_version.py` bumps CalVer (so the
   PR clears the version gate in `ci.yml`), then a branch is committed, pushed,
   and a PR is opened + set to auto-merge (squash).

The generator is **hermetic** — the PR list is injected (stdin or `--prs-file`),
never fetched in the script — so its unit tests
([`tests/test_gen_daily_changelog.py`](../tests/test_gen_daily_changelog.py))
run with no network and no `gh`.

## Required secret — `MOVATE_BOT_TOKEN` (and why)

The auto-merge step needs a repo secret named **`MOVATE_BOT_TOKEN`**: a
**fine-grained Personal Access Token** scoped to `mova-io/mova-cli` with:

- **Contents: write** (push the branch + commit)
- **Pull requests: write** (open the PR + enable auto-merge)

### Why a PAT and not the built-in `GITHUB_TOKEN`

GitHub has a **recursion guard**: events (including `pull_request`) triggered by
the default `GITHUB_TOKEN` do **not** start other workflows. A PR opened with
`GITHUB_TOKEN` therefore never fires the repo's required `pull_request` checks
(`lint-and-test`, etc.), so `gh pr merge --auto` would wait **forever** for
checks that can never start.

Authenticating the create/merge steps with a PAT makes the PR look
"human-authored", so the required checks run and auto-merge can complete.
(Separately, the mova-io org disables "Allow GitHub Actions to create and
approve pull requests" — see the note at the bottom of
[`ci.yml`](../.github/workflows/ci.yml) — which also blocks `GITHUB_TOKEN` from
opening the PR at all. So the PAT is doubly required.)

### Behavior without the secret

The workflow still runs the generator and updates the README in the runner, but
the create/merge steps are **skipped** (guarded on the secret being present) and
it logs a warning telling the operator to add `MOVATE_BOT_TOKEN`. No PR is
opened until the secret exists.

### Creating the secret

1. GitHub → your account/org → **Settings → Developer settings → Fine-grained
   tokens → Generate new token**.
   - Resource owner: `mova-io`; repository access: `mova-io/mova-cli`.
   - Repository permissions: **Contents → Read and write**, **Pull requests →
     Read and write**.
2. Copy the token.
3. Repo → **Settings → Secrets and variables → Actions → New repository
   secret**, name **`MOVATE_BOT_TOKEN`**, paste the token.

Never commit a token; the workflow references it only as
`secrets.MOVATE_BOT_TOKEN`.

## Testing it

### Hermetic unit tests (no network)

```bash
uv run pytest tests/test_gen_daily_changelog.py
```

### Locally, end-to-end against the real repo (read-only, no PR)

```bash
gh pr list --repo mova-io/mova-cli --state merged \
  --search "merged:>=2026-05-26..2026-05-27" \
  --json number,title,author,mergedAt \
  | python scripts/gen_daily_changelog.py --date 2026-05-26 --readme /tmp/README.md
```

(Point `--readme` at a scratch copy so you can inspect the result without
touching the tracked README.)

### Via `workflow_dispatch` (manual run on GitHub)

Actions → **daily-changelog** → **Run workflow**. Optionally set the `date`
input (`YYYY-MM-DD`, UTC) to backfill a specific day. With `MOVATE_BOT_TOKEN`
set, this opens an auto-merge PR exactly as the scheduled run would; without it,
the digest is generated and the PR step is skipped with a warning.
