"""Tests for ``scripts/gen_daily_changelog.py`` — the daily What's-New digest.

The generator is the pure, hermetic core behind the ``daily-changelog`` GitHub
Action: it takes a list of *already-fetched* merged-PR records (so the network
/ ``gh`` stays in the workflow, not here) and prepends a dated block to a
``## What's New`` section at the top of a README.

Coverage:

* render — grouping by conventional-commit prefix, bullet shape, bucket order.
* insert — section created when absent (under the intro), prepended (newest
  first) when present.
* idempotency — re-running for the same date does not duplicate the block and
  does not rewrite the file.
* empty day — no file write, returns ``False`` (the workflow's clean skip).
* malformed records — fail loudly rather than silently dropping shipped work.

The script is loaded as a module via ``importlib`` (the same pattern as
``test_bump_version_script.py``) since ``scripts/`` isn't an importable package.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_daily_changelog.py"

_spec = importlib.util.spec_from_file_location("gen_daily_changelog", SCRIPT)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _pr(number: int, title: str, login: str) -> dict[str, object]:
    """Build a gh-shaped merged-PR record for fixtures."""
    return {
        "number": number,
        "title": title,
        "author": {"login": login},
        "mergedAt": "2026-05-26T10:00:00Z",
    }


SAMPLE_PRS: list[dict[str, object]] = [
    _pr(501, "feat(kb): graph retrieval", "alice"),
    _pr(498, "fix(runtime): worker leak", "bob"),
    _pr(502, "docs(adr): ADR 034", "carol"),
    _pr(499, "chore: bump deps", "dave"),
    _pr(503, "Refactor pricing table", "erin"),
    _pr(500, "feat(cli)!: new dev front-door", "frank"),
]

INTRO_ONLY_README = "# MDK — title\n\nAn intro paragraph.\n\n## Status\n\nstuff\n"


# ---------------------------------------------------------------------------
# classify — conventional-commit prefix → bucket
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("feat: x", "feat"),
        ("feat(kb): x", "feat"),
        ("feat(cli)!: x", "feat"),
        ("FIX: shouting", "fix"),
        ("fix(runtime): x", "fix"),
        ("docs(adr): x", "docs"),
        ("chore: x", "chore"),
        ("refactor: x", "other"),
        ("perf(core): x", "other"),
        ("no prefix at all", "other"),
        ("", "other"),
    ],
)
def test_classify(title: str, expected: str) -> None:
    assert gen.classify(title) == expected


# ---------------------------------------------------------------------------
# render_block — grouped, dated markdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_block_groups_and_orders() -> None:
    block = gen.render_block(SAMPLE_PRS, "2026-05-26")

    assert block.startswith("### 2026-05-26")
    # Bucket headers present and in canonical order (feat, fix, docs, chore, other).
    headers = [line for line in block.splitlines() if line.startswith("**")]
    assert headers == ["**Features**", "**Fixes**", "**Docs**", "**Chores**", "**Other**"]
    # Bullet shape: "- <title> (#<num>) @author".
    assert "- feat(kb): graph retrieval (#501) @alice" in block
    assert "- fix(runtime): worker leak (#498) @bob" in block
    assert "- docs(adr): ADR 034 (#502) @carol" in block
    assert "- chore: bump deps (#499) @dave" in block
    assert "- Refactor pricing table (#503) @erin" in block
    # The breaking-change feat is still bucketed under Features.
    assert "- feat(cli)!: new dev front-door (#500) @frank" in block


@pytest.mark.unit
def test_render_block_sorts_by_pr_number_within_bucket() -> None:
    block = gen.render_block(SAMPLE_PRS, "2026-05-26")
    # Two feats: #500 and #501 — #500 should render before #501.
    idx_500 = block.index("(#500)")
    idx_501 = block.index("(#501)")
    assert idx_500 < idx_501


@pytest.mark.unit
def test_render_block_omits_empty_buckets() -> None:
    only_feat = [{"number": 1, "title": "feat: a", "author": {"login": "x"}}]
    block = gen.render_block(only_feat, "2026-05-26")
    assert "**Features**" in block
    assert "**Fixes**" not in block
    assert "**Docs**" not in block
    assert "**Chores**" not in block
    assert "**Other**" not in block


@pytest.mark.unit
def test_render_block_author_fallbacks() -> None:
    prs = [
        {"number": 1, "title": "feat: bare-string author", "author": "ghost"},
        {"number": 2, "title": "fix: no author at all"},
        {"number": 3, "title": "docs: empty login", "author": {"login": ""}},
    ]
    block = gen.render_block(prs, "2026-05-26")
    assert "(#1) @ghost" in block
    assert "(#2) @unknown" in block
    assert "(#3) @unknown" in block


# ---------------------------------------------------------------------------
# insert_block — section creation + prepend + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_insert_creates_section_when_absent() -> None:
    block = gen.render_block(SAMPLE_PRS, "2026-05-26")
    new_text, changed = gen.insert_block(INTRO_ONLY_README, block, "2026-05-26")

    assert changed is True
    assert gen.SECTION_HEADING in new_text
    # Section is inserted under the intro, BEFORE ## Status.
    assert new_text.index(gen.SECTION_HEADING) < new_text.index("## Status")
    # Intro paragraph is preserved above it.
    assert new_text.index("An intro paragraph.") < new_text.index(gen.SECTION_HEADING)
    # The auto-maintained note is present.
    assert "_Auto-maintained" in new_text
    assert "### 2026-05-26" in new_text


@pytest.mark.unit
def test_insert_prepends_newest_first() -> None:
    block_old = gen.render_block(
        [{"number": 1, "title": "feat: old day", "author": {"login": "a"}}], "2026-05-25"
    )
    base, _ = gen.insert_block(INTRO_ONLY_README, block_old, "2026-05-25")

    block_new = gen.render_block(
        [{"number": 2, "title": "feat: new day", "author": {"login": "b"}}], "2026-05-26"
    )
    new_text, changed = gen.insert_block(base, block_new, "2026-05-26")

    assert changed is True
    # Newest date appears ABOVE the older one, and both are under the heading.
    assert new_text.index("### 2026-05-26") < new_text.index("### 2026-05-25")
    assert new_text.index(gen.SECTION_HEADING) < new_text.index("### 2026-05-26")
    # The note paragraph stays directly under the heading (not pushed down).
    assert new_text.index("_Auto-maintained") < new_text.index("### 2026-05-26")


@pytest.mark.unit
def test_insert_is_idempotent_for_same_date() -> None:
    block = gen.render_block(SAMPLE_PRS, "2026-05-26")
    once, changed1 = gen.insert_block(INTRO_ONLY_README, block, "2026-05-26")
    twice, changed2 = gen.insert_block(once, block, "2026-05-26")

    assert changed1 is True
    assert changed2 is False
    assert twice == once
    # Date heading appears exactly once.
    assert once.count("### 2026-05-26") == 1


# ---------------------------------------------------------------------------
# run — file IO + empty-day skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_empty_prs_is_clean_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(INTRO_ONLY_README)
    before = readme.read_text()

    changed = gen.run(prs=[], date="2026-05-26", readme=readme, repo="mova-io/mova-cli")

    assert changed is False
    assert readme.read_text() == before  # no write
    err = capsys.readouterr().err
    assert "nothing merged" in err


@pytest.mark.unit
def test_run_writes_and_is_idempotent_on_disk(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(INTRO_ONLY_README)

    changed1 = gen.run(prs=SAMPLE_PRS, date="2026-05-26", readme=readme)
    assert changed1 is True
    first = readme.read_text()
    assert "### 2026-05-26" in first

    # Re-run same date: no change on disk.
    changed2 = gen.run(prs=SAMPLE_PRS, date="2026-05-26", readme=readme)
    assert changed2 is False
    assert readme.read_text() == first


@pytest.mark.unit
def test_run_appends_second_day_newest_first(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(INTRO_ONLY_README)

    gen.run(prs=[_pr(1, "feat: day one", "a")], date="2026-05-25", readme=readme)
    gen.run(prs=[_pr(2, "feat: day two", "b")], date="2026-05-26", readme=readme)

    text = readme.read_text()
    assert text.index("### 2026-05-26") < text.index("### 2026-05-25")


# ---------------------------------------------------------------------------
# malformed input — fail loudly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_block_rejects_record_missing_number() -> None:
    with pytest.raises(gen.ChangelogError):
        gen.render_block([{"title": "feat: no number"}], "2026-05-26")


@pytest.mark.unit
def test_render_block_rejects_non_object_record() -> None:
    with pytest.raises(gen.ChangelogError):
        gen.render_block(["just a string"], "2026-05-26")


# ---------------------------------------------------------------------------
# main() CLI — stdin/file plumbing + exit codes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_reads_prs_file(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(INTRO_ONLY_README)
    prs_file = tmp_path / "prs.json"
    prs_file.write_text(json.dumps(SAMPLE_PRS))

    rc = gen.main(["--date", "2026-05-26", "--readme", str(readme), "--prs-file", str(prs_file)])
    assert rc == 0
    assert "### 2026-05-26" in readme.read_text()


@pytest.mark.unit
def test_main_empty_array_exits_zero_no_write(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(INTRO_ONLY_README)
    prs_file = tmp_path / "prs.json"
    prs_file.write_text("[]")

    rc = gen.main(["--date", "2026-05-26", "--readme", str(readme), "--prs-file", str(prs_file)])
    assert rc == 0
    assert readme.read_text() == INTRO_ONLY_README


@pytest.mark.unit
def test_main_reads_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(INTRO_ONLY_README)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SAMPLE_PRS)))

    rc = gen.main(["--date", "2026-05-26", "--readme", str(readme)])
    assert rc == 0
    assert "### 2026-05-26" in readme.read_text()


@pytest.mark.unit
def test_main_malformed_json_exits_nonzero(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(INTRO_ONLY_README)
    prs_file = tmp_path / "prs.json"
    prs_file.write_text("{not json")

    rc = gen.main(["--date", "2026-05-26", "--readme", str(readme), "--prs-file", str(prs_file)])
    assert rc == 1
