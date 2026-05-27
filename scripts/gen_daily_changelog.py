"""Generate the daily "What's New" digest and prepend it to ``README.md``.

This is the pure, hermetic core behind the ``daily-changelog`` GitHub Action.
It takes a list of *already-fetched* merged-PR records and renders a dated
markdown block into a ``## What's New`` section at the top of the README — so
github.com/mova-io/mova-cli/tree/main shows yesterday's shipped features.

Design — **network stays out of here.** The list of merged PRs is fetched in
the workflow (``gh pr list ... --json number,title,author,mergedAt``) and piped
in via stdin (or ``--prs-file``). This module never calls ``gh`` or the network,
so the whole render+insert pipeline is unit-testable against fixtures and the
tests are hermetic (CLAUDE.md rule 9, and the "keep network OUT of the
unit-testable core" constraint).

Behavior contract:

* **Group by conventional-commit prefix.** The PR title's ``feat:`` / ``fix:`` /
  ``docs:`` / ``chore:`` prefix decides the bucket; anything else falls into
  "other". Buckets render in a fixed order (feat, fix, docs, chore, other) and
  empty buckets are omitted.
* **Newest date first.** The dated block (``### YYYY-MM-DD``) is inserted at the
  *top* of the ``## What's New`` section, just under the heading, so the most
  recent day is always first.
* **Idempotent.** Re-running for a date already present in the section is a
  no-op (the file is not rewritten) — safe to retry a failed workflow run.
* **Section auto-created.** If ``## What's New`` is absent, it's inserted just
  under the title/intro (before the first other ``##`` heading), so the very
  first run bootstraps the section.
* **Empty day skips cleanly.** An empty PR list writes nothing and exits 0 with
  a clear "nothing merged" message — a quiet day is not an error.

CLI (runnable + testable)::

    gh pr list --state merged \\
      --search "merged:>=2026-05-26..2026-05-27" \\
      --json number,title,author,mergedAt \\
      | python scripts/gen_daily_changelog.py --date 2026-05-26

    python scripts/gen_daily_changelog.py --date 2026-05-26 --prs-file prs.json

``--date`` defaults to *yesterday* (UTC); ``--readme`` defaults to ``README.md``
relative to the repo root. ``--repo`` is accepted (and recorded for context /
parity with the workflow invocation) but the PR data is always injected, never
fetched here.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TypedDict


class _PR(TypedDict):
    """A normalized merged-PR record — the fields we render."""

    number: int
    title: str
    author: str


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_README = REPO_ROOT / "README.md"

SECTION_HEADING = "## What's New"
SECTION_NOTE = (
    "_Auto-maintained daily by the "
    "[`daily-changelog`](.github/workflows/daily-changelog.yml) workflow — "
    "yesterday's merged PRs, newest first._"
)

# Conventional-commit prefixes we bucket on, in render order. The trailing
# "other" bucket is the catch-all for titles without a recognized prefix.
_KNOWN_TYPES: tuple[str, ...] = ("feat", "fix", "docs", "chore")
_BUCKET_ORDER: tuple[str, ...] = (*_KNOWN_TYPES, "other")
_BUCKET_LABELS: dict[str, str] = {
    "feat": "Features",
    "fix": "Fixes",
    "docs": "Docs",
    "chore": "Chores",
    "other": "Other",
}

# A conventional-commit prefix is the leading ``type`` (optionally with a
# ``(scope)`` and an optional ``!`` breaking-change marker) up to the first
# colon — e.g. ``feat(authoring)!: ...`` → ``feat``. Case-insensitive on the
# type token; anything not matching this shape lands in "other".
_PREFIX_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\([^)]*\))?!?:")


class ChangelogError(Exception):
    """A malformed PR record or README the caller should fix (not a quiet skip)."""


def classify(title: str) -> str:
    """Return the conventional-commit bucket for a PR ``title``.

    The leading ``type:`` (optionally ``type(scope):`` / ``type!:``) decides
    the bucket; an unrecognized or absent prefix is ``"other"``.
    """
    match = _PREFIX_RE.match(title.strip())
    if not match:
        return "other"
    type_token = match.group("type").lower()
    return type_token if type_token in _KNOWN_TYPES else "other"


def _author_login(author: object) -> str:
    """Extract a ``@login`` handle from a ``gh``-shaped author field.

    ``gh pr list --json author`` yields ``{"login": "..."}`` (sometimes also
    ``name`` / ``is_bot``). We tolerate a bare string too. Missing → "unknown".
    """
    login = ""
    if isinstance(author, dict):
        login = str(author.get("login") or author.get("name") or "").strip()
    elif isinstance(author, str):
        login = author.strip()
    return login or "unknown"


def _normalize_pr(record: object) -> _PR:
    """Validate + normalize a single PR record into the fields we render.

    Raises ``ChangelogError`` on a structurally broken record (missing number
    or title) so a malformed ``gh`` payload fails loudly rather than silently
    dropping shipped work.
    """
    if not isinstance(record, dict):
        raise ChangelogError(f"PR record is not an object: {record!r}")
    number = record.get("number")
    title = record.get("title")
    if number is None or title is None:
        raise ChangelogError(f"PR record missing number/title: {record!r}")
    return _PR(
        number=int(number),
        title=str(title).strip(),
        author=_author_login(record.get("author")),
    )


def render_block(prs: Iterable[object], date: str) -> str:
    """Render the dated digest block for ``date`` from merged-PR records.

    Returns markdown beginning ``### <date>`` followed by per-type grouped
    bullets (``- <title> (#<num>) @author``), buckets in ``_BUCKET_ORDER``,
    empty buckets omitted. PRs are sorted by number within each bucket for a
    stable, deterministic block.
    """
    normalized = [_normalize_pr(p) for p in prs]
    buckets: dict[str, list[_PR]] = {b: [] for b in _BUCKET_ORDER}
    for pr in normalized:
        buckets[classify(pr["title"])].append(pr)

    lines: list[str] = [f"### {date}", ""]
    for bucket in _BUCKET_ORDER:
        items = sorted(buckets[bucket], key=lambda p: p["number"])
        if not items:
            continue
        lines.append(f"**{_BUCKET_LABELS[bucket]}**")
        lines.append("")
        for pr in items:
            lines.append(f"- {pr['title']} (#{pr['number']}) @{pr['author']}")
        lines.append("")
    # Drop the trailing blank so insertion spacing is controlled by the caller.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _find_section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return ``(heading_idx, end_idx)`` of the ``## What's New`` section.

    ``heading_idx`` is the line index of ``## What's New``; ``end_idx`` is the
    index of the next ``## `` heading (or ``len(lines)`` if it runs to EOF).
    ``None`` if the section is absent.
    """
    heading_idx = None
    for i, line in enumerate(lines):
        if line.strip() == SECTION_HEADING:
            heading_idx = i
            break
    if heading_idx is None:
        return None
    end_idx = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end_idx = j
            break
    return heading_idx, end_idx


def _intro_end_index(lines: list[str]) -> int:
    """Index at which to insert a fresh ``## What's New`` section.

    Just before the first ``## `` heading after the title — i.e. under the
    title + intro paragraph. If there's no ``## `` heading at all, append at
    EOF.
    """
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return i
    return len(lines)


def insert_block(readme_text: str, block: str, date: str) -> tuple[str, bool]:
    """Insert ``block`` at the top of the What's-New section.

    Returns ``(new_text, changed)``. ``changed`` is ``False`` (and the text is
    returned unmodified) when ``date`` is already present in the section —
    making the operation idempotent. The section is created under the intro if
    absent.
    """
    # Idempotency: a ``### <date>`` already inside the section means we've
    # already recorded this day — do nothing.
    bounds = _find_section_bounds(readme_text.splitlines())
    date_heading = f"### {date}"
    if bounds is not None:
        heading_idx, end_idx = bounds
        section_lines = readme_text.splitlines()[heading_idx:end_idx]
        if any(line.strip() == date_heading for line in section_lines):
            return readme_text, False

    lines = readme_text.splitlines()

    if bounds is None:
        # Bootstrap the section under the intro.
        insert_at = _intro_end_index(lines)
        new_section = [SECTION_HEADING, "", SECTION_NOTE, "", block, ""]
        lines[insert_at:insert_at] = [*new_section, ""]
        return "\n".join(lines) + "\n", True

    heading_idx, _ = bounds
    # Find the line index right after the heading + optional note paragraph so
    # the newest block lands at the very top of the section's body. We insert
    # immediately after the heading, then after a contiguous note/blank run.
    body_start = heading_idx + 1
    n = len(lines)
    # Skip a leading blank line under the heading.
    while body_start < n and lines[body_start].strip() == "":
        body_start += 1
    # Skip the auto-maintained note line (and the blank after it) if present.
    if body_start < n and lines[body_start].lstrip().startswith("_Auto-maintained"):
        body_start += 1
        while body_start < n and lines[body_start].strip() == "":
            body_start += 1

    lines[body_start:body_start] = [block, ""]
    return "\n".join(lines) + "\n", True


def _yesterday_utc() -> str:
    """Yesterday's date (UTC) as ``YYYY-MM-DD``."""
    today = _dt.datetime.now(_dt.UTC).date()
    return (today - _dt.timedelta(days=1)).isoformat()


def _load_prs(prs_file: str | None) -> list[object]:
    """Load the merged-PR JSON array from ``--prs-file`` or stdin."""
    raw = Path(prs_file).read_text() if prs_file else sys.stdin.read()
    raw = raw.strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ChangelogError("expected a JSON array of PR records")
    return data


def run(
    *,
    prs: Sequence[object],
    date: str,
    readme: Path,
    repo: str | None = None,
) -> bool:
    """Apply the digest for ``date`` to ``readme``. Returns ``True`` if changed.

    Pure w.r.t. the network: ``prs`` is injected. An empty ``prs`` writes
    nothing and returns ``False`` (the caller treats that as a clean skip).
    """
    if not prs:
        print(
            f"daily-changelog: nothing merged for {date}"
            + (f" in {repo}" if repo else "")
            + " — no README change.",
            file=sys.stderr,
        )
        return False

    block = render_block(prs, date)
    original = readme.read_text()
    new_text, changed = insert_block(original, block, date)
    if not changed:
        print(
            f"daily-changelog: {date} already present in {readme.name} — no change (idempotent).",
            file=sys.stderr,
        )
        return False
    readme.write_text(new_text)
    print(f"daily-changelog: prepended {date} digest to {readme.name}.", file=sys.stderr)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--date",
        default=_yesterday_utc(),
        help="Digest date (YYYY-MM-DD). Default: yesterday (UTC).",
    )
    parser.add_argument(
        "--readme",
        default=str(DEFAULT_README),
        help="Path to the README to update. Default: repo-root README.md.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/name of the repo (recorded for context; PR data is injected, not fetched).",
    )
    parser.add_argument(
        "--prs-file",
        default=None,
        help="Path to a JSON array of merged-PR records. Default: read from stdin.",
    )
    args = parser.parse_args(argv)

    try:
        prs = _load_prs(args.prs_file)
        run(prs=prs, date=args.date, readme=Path(args.readme), repo=args.repo)
    except (ChangelogError, json.JSONDecodeError) as exc:
        print(f"daily-changelog: error — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
