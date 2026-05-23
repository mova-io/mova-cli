# Releasing movate-cli

`movate-cli` is **proprietary software**. Don't publish to public PyPI.
This doc captures the private-distribution paths so future-you doesn't
have to re-derive them under deadline pressure.

## Pre-flight checklist

Every release:

1. `ruff format src tests && ruff check src tests` — must be clean
2. `mypy src` — strict mode, must be clean
3. `pytest -m "not smoke"` — full unit + integration suite green
4. `pytest -m smoke` with `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` set —
   smoke against real providers (nightly is fine; release blocker only
   if changing provider code paths)
5. **End-to-end smoke against the real binary** — `CliRunner` tests
   miss bugs in the runtime / storage init. Walk through:
   ```bash
   tmp=$(mktemp -d) && export MOVATE_DB="$tmp/local.db"
   movate init faq --target "$tmp"
   MOVATE_MOCK_RESPONSE='{"message":"Hello!"}' \
     movate eval "$tmp/faq" --mock --gate 0.0 \
       --output-baseline "$tmp/baseline.json" -o json | jq .eval_id
   run_id=$(sqlite3 "$tmp/local.db" \
     "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1")
   movate run "$tmp/faq" --replay "$run_id" --mock | jq .diff
   MOVATE_MOCK_RESPONSE='{"message":"WRONG"}' \
     movate eval "$tmp/faq" --mock --gate 0.0 \
       --baseline-file "$tmp/baseline.json" -o json | jq .baseline.regression
   ```
   The last command must exit 1 with `regression: true`.
6. Versioning is **CalVer** `YYYY.M.D.N` (date-based; `N` is the Nth
   commit of that day on the branch). Normally you don't bump by hand:
   the `.githooks/pre-commit` hook runs `scripts/bump_version.py` on
   every commit and re-stages `pyproject.toml`, `src/movate/__init__.py`,
   and `uv.lock` (kept in lockstep — date segments are unpadded so the
   string is PEP 440-canonical). Enable the hook once per clone:
   ```bash
   ./scripts/install-hooks.sh      # sets core.hooksPath=.githooks
   ```
   If you haven't enabled the hook, bump manually before committing:
   ```bash
   new=$(python scripts/bump_version.py) \
     && git add pyproject.toml src/movate/__init__.py uv.lock \
     && echo "Bumped to $new — review with 'git diff --cached' and commit."
   ```
   `python scripts/bump_version.py --check` fails loudly if the three
   sinks ever drift. (A CI-driven per-merge auto-bump existed before
   2026-05 but was removed — the mova-io org policy blocks GitHub
   Actions from opening PRs — and the SemVer scheme it bumped was
   replaced by CalVer.)
7. Move `[Unreleased]` content in `CHANGELOG.md` under a new
   `[YYYY.M.D.N] — YYYY-MM-DD` heading (the CalVer version from
   `pyproject.toml`); update the link refs at the bottom of the file.
8. Commit the release prep (e.g. `chore: release 2026.5.23.4`), open a
   PR, merge to main. Day-to-day commits already bump the version via
   the hook, so a release usually just **tags an existing main commit** —
   no extra version bump needed.
9. Tag the merge commit with the CalVer version, **`v`-prefixed**, so the
   tag matches `mdk --version` and the wheel `uv build` emits:
   ```bash
   VERSION=$(python -c 'import movate; print(movate.__version__)')
   git tag -a "v$VERSION" -m "v$VERSION: <one-line summary>"   # annotated
   git push origin "v$VERSION"
   ```
   Annotated tags (not lightweight) carry author/date/message and surface
   in GitHub Releases. **Releases are CalVer now** — the old `v0.x` SemVer
   tags remain only as history.

## Distribution targets (pick one)

### Option A — GitHub Release artifacts (recommended)

Lowest friction. Build wheel + sdist, attach to a GitHub Release on the
private repo. Consumers `pip install` the wheel URL directly. Note the
wheel filename is the **bare PEP 440 CalVer** (no `v` prefix), while the
git tag / release is **`v`-prefixed**:

```bash
VERSION=$(python -c 'import movate; print(movate.__version__)')

# build — writes dist/movate_cli-$VERSION-py3-none-any.whl + .tar.gz
uv build

# create the release (auto-generates notes from PRs since the previous
# tag; --notes-file CHANGELOG.md works too)
gh release create "v$VERSION" \
  "dist/movate_cli-$VERSION-py3-none-any.whl" \
  "dist/movate_cli-$VERSION.tar.gz" \
  --title "v$VERSION" \
  --notes-from-tag

# consumers install with:
#   pip install https://github.com/mova-io/mova-cli/releases/download/v$VERSION/movate_cli-$VERSION-py3-none-any.whl
```

GitHub serves these to anyone with read access to the private repo. No
extra credentials per-consumer beyond their existing GitHub auth.

### Option B — GitHub Packages (PyPI feed)

Slightly more setup, more PyPI-native UX (`pip install movate-cli`
without a URL). Needs:

1. A PAT with `write:packages` scope (the current token only has
   `repo`, `workflow`, `gist`, `read:org`). Mint at
   <https://github.com/settings/tokens>.
2. `~/.pypirc` configured for the GitHub Packages index:
   ```ini
   [distutils]
   index-servers = github

   [github]
   repository = https://maven.pkg.github.com/mova-io/mova-cli
   username = jeremyyuAWS
   password = ghp_<PAT_with_write:packages>
   ```
3. Publish:
   ```bash
   uv build
   twine upload --repository github dist/*
   ```

GH Packages PyPI is still preview as of writing — verify it works for
your use case before committing to it. Option A is the safer default.

### Option C — Azure Artifacts (when Movate's Azure tenancy is wired up)

Aligns with the v1.0 deploy path. Setup:

1. Create an Azure Artifacts feed in your Movate Azure subscription.
2. `pip` config or `uv` `[publish]` config to point at the feed.
3. `twine upload --repository-url <feed-url> dist/*` with an Azure PAT.

This becomes the right answer once v0.5/v0.6 lands and other Movate
services start consuming `movate-cli` from CI. Until then, Option A
covers the developer-installs-from-laptop case.

## What NOT to do

- **Don't `twine upload dist/*` without `--repository`** — the default
  is public PyPI. The current `pyproject.toml` declares `license =
  "Proprietary"`, but PyPI's metadata field doesn't enforce that;
  uploading is an exfiltration risk.
- **Don't push tags before the release artifact lands.** Tag-then-build
  is fine; tag-and-push-and-CI-triggers-something is brittle. There's
  no automated release workflow yet — releases are manual and
  intentional.
- **Don't bump version in only one of `pyproject.toml` /
  `__init__.py`.** Drift between them silently creates a release where
  `pip show` and `import movate; movate.__version__` disagree. Fix is
  to sync them in the same commit; long-term answer is a single source
  of truth (e.g. `hatch-vcs` or reading `__version__` from
  `importlib.metadata`).

## Versioning scheme note (CalVer since 2026-05)

Releases are tagged in **CalVer** (`vYYYY.M.D.N`, e.g. `v2026.5.23.4`) to
match the package version, `mdk --version`, and the wheel `uv build`
produces — so an operator who pulls a tag gets a `mdk --version` that
equals it. The earlier **SemVer** tags (`v0.2.0` … `v0.8.0`) predate the
CalVer switch and remain only as history; do not cut new `v0.x` tags.
