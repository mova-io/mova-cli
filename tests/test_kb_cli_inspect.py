"""``mdk kb`` inspection commands + ``--dry-run`` for ingest (PR-A).

Covers:

* ``mdk kb list <agent>`` — prints chunks; warns on empty KB.
* ``mdk kb stats <agent>`` — per-source breakdown + totals.
* ``mdk kb clear <agent> [--source]`` — deletes chunks + confirms count.
* ``mdk kb ingest --dry-run`` — no OpenAI call, no storage writes,
  prints estimated cost.

CLI tests drive the Typer app via ``CliRunner`` against the real
sqlite backend at a tmp path so the storage roundtrip is end-to-end.
Embedding calls are stubbed via ``mdk kb ingest --dry-run`` (which
bypasses the OpenAI client entirely) so no API traffic is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own sqlite DB so kb state doesn't leak."""
    db_path = tmp_path / "kb-tests.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    # Make sure no Postgres URL is set (some CI envs export one for
    # other tests; this would dispatch to PG which our test doesn't want).
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    """Drop two .md files in a fresh dir so ingest --dry-run finds them."""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "alpha.md").write_text(
        "First paragraph of alpha doc.\n\nSecond paragraph of alpha doc.\n",
        encoding="utf-8",
    )
    (kb / "beta.md").write_text(
        "Beta doc has one paragraph here.\n",
        encoding="utf-8",
    )
    return kb


# ---------------------------------------------------------------------------
# mdk kb ingest --dry-run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dry_run_walks_files_without_calling_openai(
    kb_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run should NOT require an OpenAI key — it never calls
    the API. Pin this by explicitly clearing OPENAI_API_KEY first."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        ["kb", "ingest", "rag-qa", str(kb_dir), "--dry-run"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Dry-run" in result.stdout
    # Both files surface in the per-source table.
    assert "alpha.md" in result.stdout
    assert "beta.md" in result.stdout
    # Estimated cost line renders.
    assert "Estimated" in result.stdout
    assert "embeddings cost" in result.stdout


@pytest.mark.unit
def test_real_ingest_without_key_errors_with_hint(
    kb_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real ingest path (no --dry-run) requires an OpenAI key. The
    error should point at the --dry-run escape hatch."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        ["kb", "ingest", "rag-qa", str(kb_dir)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "no API key found" in combined
    assert "--dry-run" in combined


# ---------------------------------------------------------------------------
# mdk kb list / stats / clear — empty-KB error paths first
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_empty_kb_warns_and_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    """No chunks ingested yet → warning + hint to run ingest."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        ["kb", "list", "rag-qa"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0  # warning, not error
    combined = result.stdout + result.stderr
    assert "no chunks" in combined.lower()
    assert "mdk kb ingest" in combined


@pytest.mark.unit
def test_stats_empty_kb_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk kb stats`` on empty KB → warning, no traceback."""
    result = runner.invoke(
        app,
        ["kb", "stats", "rag-qa"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "no chunks" in combined.lower()


@pytest.mark.unit
def test_clear_with_yes_flag_on_empty_kb(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk kb clear -y`` on an empty KB succeeds with a 'nothing
    deleted' note rather than failing."""
    result = runner.invoke(
        app,
        ["kb", "clear", "rag-qa", "--yes"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "nothing deleted" in combined.lower() or "0 chunk" in combined.lower()


# ---------------------------------------------------------------------------
# End-to-end: seed via direct storage call, then list / stats / clear
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_stats_clear_after_direct_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed two chunks via the storage layer, then exercise list /
    stats / clear. Verifies the CLI commands see what storage saved.

    Sync (not async) so the CLI commands' internal ``asyncio.run()``
    can spin up its own event loop. Seeds via a fresh loop so the
    storage write is committed before the CLI commands query.
    """
    import asyncio  # noqa: PLC0415

    from movate.core.models import KbChunk  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415

    async def _seed() -> None:
        storage = build_storage()
        await storage.init()
        try:
            for i in range(2):
                await storage.save_kb_chunk(
                    KbChunk(
                        tenant_id="local",
                        agent="rag-qa",
                        source=f"/tmp/doc-{i}.md",
                        text=f"Sample chunk text number {i}",
                        embedding=[1.0, 0.0],
                        embedding_model="openai/text-embedding-3-small",
                        content_hash=f"hash-{i}",
                    )
                )
        finally:
            await storage.close()

    asyncio.new_event_loop().run_until_complete(_seed())

    # list — should show both chunks.
    r = runner.invoke(app, ["kb", "list", "rag-qa"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "doc-0.md" in r.stdout
    assert "doc-1.md" in r.stdout

    # stats — should show 2 chunks, 2 sources, the embedding model.
    r = runner.invoke(app, ["kb", "stats", "rag-qa"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert "total chunks:" in r.stdout
    assert "openai/text-embedding-3-small" in r.stdout

    # clear with --yes — should delete both.
    r = runner.invoke(app, ["kb", "clear", "rag-qa", "--yes"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert "deleted 2" in r.stdout

    # List again — empty.
    r = runner.invoke(app, ["kb", "list", "rag-qa"], env={"COLUMNS": "200"})
    combined = r.stdout + r.stderr
    assert "no chunks" in combined.lower()


# ---------------------------------------------------------------------------
# Distribution view: mdk kb stats --by-source (PR-Y)
# ---------------------------------------------------------------------------


def _seed_uneven(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a KB where one source contributes most chunks — exercises
    the 'dominance' use case for --by-source."""
    import asyncio  # noqa: PLC0415

    from movate.core.models import KbChunk  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415

    async def _do() -> None:
        storage = build_storage()
        await storage.init()
        try:
            # heavy.md: 7 chunks. light.md: 1 chunk. ten distinct chunks total.
            for i in range(7):
                await storage.save_kb_chunk(
                    KbChunk(
                        tenant_id="local",
                        agent="rag-qa",
                        source="/tmp/heavy.md",
                        text=f"Heavy chunk {i}" * 5,
                        embedding=[1.0, 0.0],
                        embedding_model="openai/text-embedding-3-small",
                        content_hash=f"heavy-{i}",
                    )
                )
            for i in range(1):
                await storage.save_kb_chunk(
                    KbChunk(
                        tenant_id="local",
                        agent="rag-qa",
                        source="/tmp/light.md",
                        text=f"Light chunk {i}",
                        embedding=[1.0, 0.0],
                        embedding_model="openai/text-embedding-3-small",
                        content_hash=f"light-{i}",
                    )
                )
        finally:
            await storage.close()

    asyncio.new_event_loop().run_until_complete(_do())


@pytest.mark.unit
def test_stats_by_source_shows_percentage_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--by-source`` adds a '% of total' column."""
    _seed_uneven(monkeypatch)
    r = runner.invoke(
        app,
        ["kb", "stats", "rag-qa", "--by-source"],
        env={"COLUMNS": "200"},
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "% of total" in r.stdout
    # heavy.md = 7/8 = 87.5%. The text rendered shows "87.5%".
    assert "87.5%" in r.stdout


@pytest.mark.unit
def test_stats_default_sort_omits_percentage_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--by-source``, the table is the legacy alphabetical
    breakdown WITHOUT the percentage column. Back-compat."""
    _seed_uneven(monkeypatch)
    r = runner.invoke(app, ["kb", "stats", "rag-qa"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert "% of total" not in r.stdout
    # Both sources still shown.
    assert "heavy.md" in r.stdout
    assert "light.md" in r.stdout


@pytest.mark.unit
def test_stats_by_source_sorts_heaviest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distribution view sorts by chunk count DESC — heavy.md (7
    chunks) must appear ABOVE light.md (1 chunk) in the table."""
    _seed_uneven(monkeypatch)
    r = runner.invoke(
        app,
        ["kb", "stats", "rag-qa", "--by-source"],
        env={"COLUMNS": "200"},
    )
    assert r.exit_code == 0
    # heavy.md's row appears before light.md's in stdout.
    heavy_idx = r.stdout.index("heavy.md")
    light_idx = r.stdout.index("light.md")
    assert heavy_idx < light_idx


@pytest.mark.unit
def test_stats_top_caps_displayed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--top N`` caps the per-source table at the top N rows."""
    _seed_uneven(monkeypatch)
    r = runner.invoke(
        app,
        ["kb", "stats", "rag-qa", "--by-source", "--top", "1"],
        env={"COLUMNS": "200"},
    )
    assert r.exit_code == 0
    # heavy.md kept (most chunks), light.md dropped.
    assert "heavy.md" in r.stdout
    assert "light.md" not in r.stdout
    # Tail-row indicator surfaces the count.
    assert "and 1 more sources" in r.stdout


# ---------------------------------------------------------------------------
# Skill template registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_kb_vector_lookup_in_skill_templates_map() -> None:
    """Wiring check: the ``kb-vector-lookup`` skill name maps to the
    ``skill_kb_vector_lookup`` template directory so ``mdk add rag-qa``
    auto-scaffolds the real impl, not the default echo stub."""
    from movate.templates import SKILL_TEMPLATES, TEMPLATES_DIR  # noqa: PLC0415

    assert SKILL_TEMPLATES.get("kb-vector-lookup") == "skill_kb_vector_lookup"
    template_dir = TEMPLATES_DIR / SKILL_TEMPLATES["kb-vector-lookup"]
    assert template_dir.is_dir()
    assert (template_dir / "skill.yaml").is_file()
    assert (template_dir / "impl.py").is_file()


# ---------------------------------------------------------------------------
# New tests: last-ingested timestamp in stats + file-type breakdown in dry-run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stats_shows_last_ingested(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk kb stats`` after ingest shows 'last ingested' in the output."""
    import asyncio  # noqa: PLC0415

    from movate.core.models import KbChunk  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415

    async def _seed() -> None:
        storage = build_storage()
        await storage.init()
        try:
            await storage.save_kb_chunk(
                KbChunk(
                    tenant_id="local",
                    agent="rag-qa",
                    source="/tmp/doc.md",
                    text="Sample chunk text for last-ingested test",
                    embedding=[1.0, 0.0],
                    embedding_model="openai/text-embedding-3-small",
                    content_hash="hash-last-ingested",
                )
            )
        finally:
            await storage.close()

    asyncio.new_event_loop().run_until_complete(_seed())

    r = runner.invoke(app, ["kb", "stats", "rag-qa"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "last ingested" in r.stdout


@pytest.mark.unit
def test_format_age_just_now() -> None:
    """_format_age with a timestamp 30 seconds ago returns 'just now'."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from movate.cli.kb_cmd import _format_age  # noqa: PLC0415

    ts = datetime.now(UTC) - timedelta(seconds=30)
    result = _format_age(ts.isoformat())
    assert result == "just now"


@pytest.mark.unit
def test_format_age_days_ago() -> None:
    """_format_age with a timestamp 3 days ago returns '3d ago'."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from movate.cli.kb_cmd import _format_age  # noqa: PLC0415

    ts = datetime.now(UTC) - timedelta(days=3)
    result = _format_age(ts.isoformat())
    assert result == "3d ago"


@pytest.mark.unit
def test_dry_run_shows_file_type_breakdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mdk kb ingest --dry-run`` shows file-type breakdown (e.g. PDF · MD)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "note.md").write_text(
        "Markdown paragraph one.\n\nMarkdown paragraph two.\n", encoding="utf-8"
    )
    (kb / "readme.txt").write_text(
        "Text file paragraph.\n\nAnother paragraph here.\n", encoding="utf-8"
    )

    result = runner.invoke(
        app,
        ["kb", "ingest", "rag-qa", str(kb), "--dry-run"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The breakdown footer should mention both extension types.
    assert "types:" in result.stdout
    assert "MD" in result.stdout
    assert "TXT" in result.stdout


@pytest.mark.unit
def test_rag_qa_agent_yaml_declares_kb_vector_lookup_skill() -> None:
    """Wiring check: the ``rag_qa_agent`` template's agent.yaml lists
    ``kb-vector-lookup`` in its ``skills:`` so a fresh ``mdk add rag-qa``
    immediately has the skill scaffolded + declared."""
    import yaml  # noqa: PLC0415

    from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

    agent_yaml_path = TEMPLATES_DIR / "rag_qa_agent" / "agent.yaml"
    spec = yaml.safe_load(agent_yaml_path.read_text())
    assert "kb-vector-lookup" in spec.get("skills", []), (
        f"rag-qa skills should include 'kb-vector-lookup', got: {spec.get('skills')}"
    )
