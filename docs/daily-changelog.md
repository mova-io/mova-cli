# Daily "What's New" changelog automation

Every morning a scheduled GitHub Action collects the **previous day's merged
PRs** and posts a dated digest as a **comment on a single pinned "What's New"
tracking Issue**. It runs durably on GitHub's infrastructure (independent of any
local or agent session) using only the built-in `GITHUB_TOKEN` — **no PAT, no
GitHub App, no PR**.

Moving parts:

| Piece | What it does |
|---|---|
| [`.github/workflows/daily-changelog.yml`](../.github/workflows/daily-changelog.yml) | Scheduled (+ manual) workflow: fetch PRs → generate digest → post a comment on the pinned tracking Issue. |
| [`scripts/gen_daily_changelog.py`](../scripts/gen_daily_changelog.py) | Pure, hermetic generator: PR JSON in (stdin/file) → dated, grouped digest. `--format issue` writes the comment body to stdout; `--format readme` (legacy) edits the README in place. No network in here. |
| The pinned "What's New" tracking Issue | One open issue (marked `<!-- whats-new-tracker -->`) that accumulates one digest comment per day. |

## Schedule

- **Cron:** `0 14 * * *` (14:00 UTC daily). GitHub cron is always UTC and does
  not observe DST.
- **US Pacific equivalent:** ~06:00 PDT (UTC-7, summer) / 07:00 PST (UTC-8,
  winter) — the digest is waiting when the US team starts the morning.

## How the digest is built

1. Resolve the digest date (yesterday UTC, or the `workflow_dispatch` `date`
   input) and the day window `[date, date+1)`.
2. `gh pr list --state merged --search "merged:<date>..<next>" --json
   number,title,author,mergedAt` → `prs.json`.
3. `python scripts/gen_daily_changelog.py --format issue --date <date> <
   prs.json > digest.md`:
   - Groups PRs by conventional-commit prefix from the title
     (`feat`/`fix`/`docs`/`chore`/other).
   - Renders a hidden per-date marker `<!-- changelog:YYYY-MM-DD -->`, a
     `### What shipped — YYYY-MM-DD` heading, and grouped bullets
     `- <title> (#<num>) @author`.
   - **Deterministic:** PRs are sorted by number within each bucket, so the same
     input renders byte-for-byte identical output.
   - **Empty day:** the workflow records `changed=false` and skips posting (a
     quiet day is not an error).
4. The workflow finds the open tracking Issue by its `<!-- whats-new-tracker -->`
   marker (creating + pinning it on the first run), checks the issue's existing
   comments for the per-date marker, and — if this date hasn't been posted —
   posts `digest.md` as a new comment.

The generator is **hermetic** — the PR list is injected (stdin or `--prs-file`),
never fetched in the script — so its unit tests
([`tests/test_gen_daily_changelog.py`](../tests/test_gen_daily_changelog.py))
run with no network and no `gh`.

## Credentials — just `GITHUB_TOKEN` (and why this design)

The workflow needs only the **built-in `GITHUB_TOKEN`** with `issues: write`
(the repo has Issues enabled). No repo secret, no PAT, no GitHub App.

### Why a tracking Issue instead of a README-on-`main` PR

The original design prepended the digest to `README.md` via an **auto-merged
PR**. In this org that path is a dead end with no zero-credential way out:

- the org **forbids fine-grained PATs** (they can't be approved), so the old
  `MOVATE_BOT_TOKEN` approach can't be provisioned;
- **"Allow GitHub Actions to create and approve pull requests" is OFF**
  (`can_approve_pull_request_reviews: false`), so the built-in `GITHUB_TOKEN`
  can't open a PR at all; and
- `main` **requires status checks**, so `GITHUB_TOKEN` can't push to it either.

Landing README on `main` would therefore require a **GitHub App** — extra infra
and credentials we don't want. Posting each day's digest as a comment on a
pinned tracking Issue needs only `GITHUB_TOKEN: issues: write` — the
org-compliant, zero-credential path.

### Idempotency

Re-dispatching the same `date` does **not** double-post. The generated comment
begins with a hidden `<!-- changelog:YYYY-MM-DD -->` marker; before posting, the
workflow scans the issue's existing comments for that marker and exits early if
the date was already recorded.

### Pinning

On first run the workflow creates the tracking Issue and **best-effort pins**
it (`gh issue pin … || true`). Pinning is nice-to-have — if the token isn't
permitted to pin, the run logs a warning and continues; the digest is still
posted.

### `MOVATE_BOT_TOKEN` is no longer used

The old fine-grained-PAT secret `MOVATE_BOT_TOKEN` is no longer referenced by
this workflow and **may be deleted** from the repo's Actions secrets.

## Testing it

### Hermetic unit tests (no network)

```bash
uv run pytest tests/test_gen_daily_changelog.py
```

### Locally, end-to-end against the real repo (read-only, no post)

```bash
gh pr list --repo mova-io/mova-cli --state merged \
  --search "merged:2026-05-26..2026-05-27" \
  --json number,title,author,mergedAt \
  | python scripts/gen_daily_changelog.py --format issue --date 2026-05-26
```

This prints the exact comment body (marker + heading + grouped bullets) to
stdout without touching any issue.

### Via `workflow_dispatch` (manual run on GitHub)

Actions → **daily-changelog** → **Run workflow**. Optionally set the `date`
input (`YYYY-MM-DD`, UTC) to backfill a specific day. The run posts (or, on the
very first run, creates + pins) the pinned tracking Issue and adds the digest
comment; re-dispatching the same date is a no-op.
